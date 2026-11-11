"""Persistence layer (SQLite).

Owns five tables:

* ``requests``          current state of every request (one row each)
* ``state_history``     append-only log of every state / stage change
* ``audit_log``         append-only log of everything the engine did and why
* ``workflow_versions`` immutable snapshot of every config a request ran under
* ``intake_rejections`` requests refused at intake (invalid input)

Design rules:

* All writes go through ``Store.transaction()``. One stage execution = one
  transaction, so state, history and audit can never disagree. If anything
  fails mid-way the whole unit rolls back (no partial saves).
* ``requests.row_version`` is an optimistic lock. Two workers cannot both
  advance the same request; the loser gets ``ConcurrentUpdateError``.
* ``UNIQUE(workflow, idempotency_key)`` is the final guard against duplicates,
  even under a race between two identical submissions.
* History, audit and config snapshots are append-only, and database triggers
  refuse any UPDATE or DELETE on them.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from app.errors import ConcurrentUpdateError, DuplicateKeyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_versions (
    config_hash   TEXT PRIMARY KEY,
    workflow      TEXT NOT NULL,
    version       INTEGER NOT NULL,
    body          TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id               TEXT PRIMARY KEY,
    workflow         TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    config_hash      TEXT NOT NULL REFERENCES workflow_versions(config_hash),
    idempotency_key  TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    payload          TEXT NOT NULL,
    dependency_data  TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL,
    current_stage    TEXT,
    decision_reason  TEXT,
    resume_count     INTEGER NOT NULL DEFAULT 0,
    row_version      INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (workflow, idempotency_key)
);

CREATE TABLE IF NOT EXISTS state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL REFERENCES requests(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    from_stage  TEXT,
    to_stage    TEXT,
    reason      TEXT,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests(id),
    event_type TEXT NOT NULL,
    stage      TEXT,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Requests refused at intake (never became a request), kept for audit.
CREATE TABLE IF NOT EXISTS intake_rejections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow        TEXT NOT NULL,
    idempotency_key TEXT,
    payload         TEXT NOT NULL,
    errors          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- Append-only is enforced by the database, not left to convention.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_history_no_update BEFORE UPDATE ON state_history
BEGIN SELECT RAISE(ABORT, 'state_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_history_no_delete BEFORE DELETE ON state_history
BEGIN SELECT RAISE(ABORT, 'state_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS workflow_versions_no_update BEFORE UPDATE ON workflow_versions
BEGIN SELECT RAISE(ABORT, 'workflow_versions is immutable'); END;
CREATE TRIGGER IF NOT EXISTS workflow_versions_no_delete BEFORE DELETE ON workflow_versions
BEGIN SELECT RAISE(ABORT, 'workflow_versions is immutable'); END;

CREATE INDEX IF NOT EXISTS idx_history_request ON state_history(request_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_request   ON audit_log(request_id, id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
"""

_JSON_COLUMNS = ("payload", "dependency_data")
_UPDATABLE = {"status", "current_stage", "decision_reason", "dependency_data", "resume_count"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _request_from_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    for col in _JSON_COLUMNS:
        data[col] = json.loads(data[col])
    return data


class Transaction:
    """Write operations available inside ``Store.transaction()``."""

    def __init__(self, conn: sqlite3.Connection, fault_hook: Optional[Callable[[str], None]]):
        self._conn = conn
        self._fault_hook = fault_hook

    def _checkpoint(self, operation: str) -> None:
        # Test-only seam: lets a test simulate the database failing halfway
        # through a unit of work, to prove the whole unit rolls back.
        if self._fault_hook:
            self._fault_hook(operation)

    def save_workflow_version(self, workflow: str, version: int, config_hash: str, body: dict) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO workflow_versions VALUES (?, ?, ?, ?, ?)",
            (config_hash, workflow, version, json.dumps(body, sort_keys=True), utc_now()),
        )

    def insert_request(self, request: dict) -> None:
        self._checkpoint("insert_request")
        now = utc_now()
        try:
            self._conn.execute(
                """INSERT INTO requests
                   (id, workflow, workflow_version, config_hash, idempotency_key, payload_hash,
                    payload, dependency_data, status, current_stage, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)""",
                (
                    request["id"], request["workflow"], request["workflow_version"],
                    request["config_hash"], request["idempotency_key"], request["payload_hash"],
                    json.dumps(request["payload"], sort_keys=True), request["status"],
                    request.get("current_stage"), now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "idempotency_key" in str(exc):
                raise DuplicateKeyError(str(exc)) from exc
            raise

    def update_request(self, request_id: str, expected_row_version: int, **fields: Any) -> None:
        """Update a request only if nobody else changed it since it was read."""
        self._checkpoint("update_request")
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Columns not updatable: {sorted(unknown)}")
        if "dependency_data" in fields:
            fields["dependency_data"] = json.dumps(fields["dependency_data"], sort_keys=True)
        assignments = ", ".join(f"{col} = ?" for col in fields)
        cursor = self._conn.execute(
            f"UPDATE requests SET {assignments}, row_version = row_version + 1, updated_at = ? "
            "WHERE id = ? AND row_version = ?",
            (*fields.values(), utc_now(), request_id, expected_row_version),
        )
        if cursor.rowcount != 1:
            raise ConcurrentUpdateError(
                f"Request {request_id} was modified concurrently (expected row_version {expected_row_version})"
            )

    def add_history(self, request_id: str, from_status: Optional[str], to_status: str,
                    from_stage: Optional[str], to_stage: Optional[str],
                    reason: Optional[str], actor: str) -> None:
        self._checkpoint("add_history")
        self._conn.execute(
            "INSERT INTO state_history (request_id, from_status, to_status, from_stage, to_stage, "
            "reason, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, from_status, to_status, from_stage, to_stage, reason, actor, utc_now()),
        )

    def add_audit(self, request_id: str, event_type: str, stage: Optional[str], detail: dict) -> None:
        self._checkpoint("add_audit")
        self._conn.execute(
            "INSERT INTO audit_log (request_id, event_type, stage, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (request_id, event_type, stage, json.dumps(detail, sort_keys=True, default=str), utc_now()),
        )


class Store:
    def __init__(self, path: str = ":memory:"):
        # One shared connection guarded by a lock keeps the demo simple and
        # correct under FastAPI's thread pool. See ARCHITECTURE.md for the
        # Postgres / connection-pool path.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.fault_hook: Optional[Callable[[str], None]] = None
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """All-or-nothing unit of work."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield Transaction(self._conn, self.fault_hook)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ---- reads -----------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def get_request(self, request_id: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM requests WHERE id = ?", (request_id,))
        return _request_from_row(rows[0]) if rows else None

    def find_by_idempotency_key(self, workflow: str, key: str) -> Optional[dict]:
        rows = self._query(
            "SELECT * FROM requests WHERE workflow = ? AND idempotency_key = ?", (workflow, key)
        )
        return _request_from_row(rows[0]) if rows else None

    def list_requests_by_status(self, statuses: tuple[str, ...]) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._query(
            f"SELECT * FROM requests WHERE status IN ({marks}) ORDER BY created_at", statuses
        )
        return [_request_from_row(r) for r in rows]

    def last_activity_at(self, request_id: str) -> Optional[str]:
        """When a worker last made progress on a request: a state change or a
        dependency attempt (written as it happens, so it acts as a heartbeat
        while a request waits on a slow service). Events caused by clients
        (duplicate replays, conflicts, takeovers) do not count, or a client
        re-sending its request would keep a stuck request looking busy."""
        rows = self._query(
            "SELECT MAX(at) AS at FROM ("
            " SELECT updated_at AS at FROM requests WHERE id = ?"
            " UNION ALL SELECT MAX(created_at) FROM audit_log"
            "  WHERE request_id = ? AND event_type = 'DEPENDENCY_CALL')",
            (request_id, request_id),
        )
        return rows[0]["at"]

    def count_requests(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM requests")[0]["n"]

    def get_history(self, request_id: str) -> list[dict]:
        rows = self._query("SELECT * FROM state_history WHERE request_id = ? ORDER BY id", (request_id,))
        return [dict(r) for r in rows]

    def get_audit(self, request_id: str) -> list[dict]:
        rows = self._query("SELECT * FROM audit_log WHERE request_id = ? ORDER BY id", (request_id,))
        events = []
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event["detail"])
            events.append(event)
        return events

    def log_intake_rejection(self, workflow: str, idempotency_key: Optional[str],
                             payload: object, errors: list[dict]) -> None:
        """Record a request that was refused before it became a request."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO intake_rejections (workflow, idempotency_key, payload, errors, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                # The payload is kept as text: it may hold values (NaN) that are not valid JSON.
                (workflow, idempotency_key, json.dumps(payload, default=str), json.dumps(errors), utc_now()),
            )

    def list_intake_rejections(self, workflow: str, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT * FROM intake_rejections WHERE workflow = ? ORDER BY id DESC LIMIT ?", (workflow, limit)
        )
        return [{**dict(r), "errors": json.loads(r["errors"])} for r in rows]

    def get_workflow_version(self, config_hash: str) -> Optional[dict]:
        rows = self._query("SELECT * FROM workflow_versions WHERE config_hash = ?", (config_hash,))
        if not rows:
            return None
        data = dict(rows[0])
        data["body"] = json.loads(data["body"])
        return data
