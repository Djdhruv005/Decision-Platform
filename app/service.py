"""Application service: the use cases of the platform.

This is the single entry point for anything a caller can do: submit a
request, read its state, read its audit trail, retry it, resolve a manual
review, reload configs. The REST API is a thin wrapper around this class, and
a CLI or a queue worker could wrap it the same way.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from app.config_loader import WorkflowRegistry, content_hash
from app.dependencies import DependencyRegistry, default_dependencies
from app.errors import (
    DuplicateKeyError, IdempotencyConflict, InvalidStateError, RequestNotFound, ValidationFailed,
)
from app.explain import build_explanation
from app.store import Store
from app.validator import non_finite_fields, validate_payload
from app.workflow_engine import ACTIVE_STATES, FINAL_STATES, RESUMABLE_STATES, WorkflowEngine


logger = logging.getLogger(__name__)


def _fingerprint_form(value: object) -> object:
    """Payload as used for the idempotency fingerprint: 500000 and 500000.0
    are the same amount, so they must produce the same hash."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _fingerprint_form(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fingerprint_form(item) for item in value]
    return value


@dataclass
class SubmitResult:
    request: dict     # public view of the request
    created: bool     # False when this was a duplicate of an earlier submission


def request_view(request: dict) -> dict:
    """What callers see. Internal columns (row_version, hashes of payload) stay inside."""
    return {
        "request_id": request["id"],
        "workflow": request["workflow"],
        "workflow_version": request["workflow_version"],
        "config_hash": request["config_hash"],
        "status": request["status"],
        "is_final": request["status"] in FINAL_STATES,
        "current_stage": request["current_stage"],
        "reason": request["decision_reason"],
        "resume_count": request["resume_count"],
        "created_at": request["created_at"],
        "updated_at": request["updated_at"],
    }


class DecisionService:
    def __init__(self, config_dir: str | Path, db_path: str = ":memory:",
                 dependencies: Optional[DependencyRegistry] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 stale_after_seconds: float = 30.0):
        # A request sitting in an active state for longer than this is taken
        # to be abandoned (its worker died) and may be resumed by a retry.
        self.stale_after_seconds = stale_after_seconds
        self.dependencies = dependencies or default_dependencies()
        self.store = Store(db_path)
        self.registry = WorkflowRegistry(config_dir, known_dependencies=self.dependencies.names())
        self.engine = WorkflowEngine(self.store, self.dependencies, sleep)

    # ---- submit ----------------------------------------------------------

    def submit(self, workflow: str, payload: object, idempotency_key: Optional[str] = None) -> SubmitResult:
        payload_hash = content_hash(_fingerprint_form(payload))

        # A client key is a contract: the same key always gets the original
        # answer. That lookup runs first, before validation and before the
        # workflow is even resolved, so a replay still works after the rules
        # or schema changed or the workflow was withdrawn.
        if idempotency_key:
            existing = self.store.find_by_idempotency_key(workflow, idempotency_key)
            if existing:
                return self._answer_duplicate(existing, payload_hash)

        config = self.registry.get(workflow)

        # No client key: derive one from the payload so an accidental
        # double-submit is still caught. The config version is part of it, so
        # this protection lasts only while the rules are unchanged: someone
        # rejected under old rules can re-apply after a policy change.
        key = idempotency_key or f"auto:{config.config_hash[:16]}:{payload_hash}"
        if not idempotency_key:
            existing = self.store.find_by_idempotency_key(workflow, key)
            if existing:
                return self._answer_duplicate(existing, payload_hash)

        errors = non_finite_fields(payload) or validate_payload(config.input_schema, payload)
        if errors:
            # Refused requests never become a request, but the attempt is kept.
            self.store.log_intake_rejection(workflow, idempotency_key, payload, errors)
            raise ValidationFailed(errors)

        request_id = str(uuid.uuid4())
        try:
            with self.store.transaction() as tx:
                tx.save_workflow_version(config.name, config.version, config.config_hash, config.raw)
                tx.insert_request({
                    "id": request_id, "workflow": config.name,
                    "workflow_version": config.version, "config_hash": config.config_hash,
                    "idempotency_key": key, "payload_hash": payload_hash,
                    "payload": payload, "status": "RECEIVED", "current_stage": None,
                })
                tx.add_history(request_id, None, "RECEIVED", None, None, "Request accepted", "api")
                tx.add_audit(request_id, "REQUEST_RECEIVED", None, {
                    "input": payload,
                    "idempotency_key": key,
                    "idempotency_key_source": "client" if idempotency_key else "derived_from_payload",
                    "workflow_version": config.version,
                    "config_hash": config.config_hash,
                })
        except DuplicateKeyError:
            # Lost a race with an identical submission: the UNIQUE constraint
            # caught it. Answer with the winner's record.
            existing = self.store.find_by_idempotency_key(workflow, key)
            return self._answer_duplicate(existing, payload_hash)

        return SubmitResult(request_view(self.engine.run(request_id)), created=True)

    def _answer_duplicate(self, existing: dict, payload_hash: str) -> SubmitResult:
        if existing["payload_hash"] != payload_hash:
            # Reusing a key for different data may be a client bug or tampering.
            # Either way it is recorded against the original request.
            with self.store.transaction() as tx:
                tx.add_audit(existing["id"], "IDEMPOTENCY_CONFLICT", existing["current_stage"], {
                    "original_payload_hash": existing["payload_hash"],
                    "conflicting_payload_hash": payload_hash,
                })
            raise IdempotencyConflict(existing["id"])
        idle = self._idle_seconds(existing)
        with self.store.transaction() as tx:
            tx.add_audit(existing["id"], "DUPLICATE_REQUEST", existing["current_stage"], {
                "status_found": existing["status"],
            })
        # The original attempt may have died mid-way (a crash or a database
        # error after the request was stored). A client that replays its key
        # is the natural recovery trigger: if nothing has touched the request
        # for the staleness window, finish it now instead of answering "RECEIVED".
        if existing["status"] in ACTIVE_STATES and idle >= self.stale_after_seconds:
            with self.store.transaction() as tx:
                tx.add_audit(existing["id"], "RECOVERED", existing["current_stage"], {
                    "actor": "client-replay", "status": existing["status"], "idle_seconds": round(idle, 1),
                })
            return SubmitResult(request_view(self.engine.run(existing["id"])), created=False)
        return SubmitResult(request_view(existing), created=False)

    # ---- read ------------------------------------------------------------

    def _load(self, request_id: str) -> dict:
        request = self.store.get_request(request_id)
        if request is None:
            raise RequestNotFound(f"No request with id '{request_id}'")
        return request

    def get(self, request_id: str) -> dict:
        return request_view(self._load(request_id))

    def explanation(self, request_id: str) -> dict:
        request = self._load(request_id)
        return build_explanation(request, self.store.get_audit(request_id))

    def audit(self, request_id: str) -> dict:
        request = self._load(request_id)
        events = self.store.get_audit(request_id)
        return {
            "request": request_view(request),
            "input": request["payload"],
            "dependency_data": request["dependency_data"],
            "state_history": self.store.get_history(request_id),
            "audit_events": events,
            "explanation": build_explanation(request, events),
        }

    # ---- retry / recovery ------------------------------------------------

    def retry(self, request_id: str, actor: str = "api") -> dict:
        """Re-run a request from the stage where it stopped.

        * Parked (RETRY_PENDING, FAILED): resumed, within the retry budget.
        * Active but idle for ``stale_after_seconds``: taken to be abandoned
          by a crashed worker and continued from its last committed stage.
        * Active and recently updated: refused, because it is being processed.
        """
        request = self._load(request_id)
        status, stage = request["status"], request["current_stage"]

        if status in RESUMABLE_STATES:
            config = self.engine.config_for(request)
            if request["resume_count"] >= config.max_resumes:
                return self._escalate_spent_budget(request, config.max_resumes, actor)
            resume_count = request["resume_count"] + 1
            reason = f"Retry #{resume_count} requested by {actor}"
            with self.store.transaction() as tx:
                tx.update_request(request_id, request["row_version"], status="IN_PROGRESS",
                                  resume_count=resume_count, decision_reason=reason)
                tx.add_history(request_id, status, "IN_PROGRESS", stage, stage, reason, actor)
                tx.add_audit(request_id, "RESUMED", stage, {
                    "actor": actor, "resume_count": resume_count, "previous_status": status,
                })
        elif status in ACTIVE_STATES:
            idle = self._idle_seconds(request)
            if idle < self.stale_after_seconds:
                raise InvalidStateError(
                    f"Request is {status} and was updated {idle:.0f}s ago, so it is still being "
                    f"processed. It can be resumed once idle for {self.stale_after_seconds:.0f}s"
                )
            # The state does not change, so there is no history row; the
            # takeover itself is recorded.
            with self.store.transaction() as tx:
                tx.add_audit(request_id, "RECOVERED", stage, {
                    "actor": actor, "status": status, "idle_seconds": round(idle, 1),
                })
        else:
            raise InvalidStateError(
                f"Request is {status}; only {list(RESUMABLE_STATES + ACTIVE_STATES)} can be retried"
            )
        return request_view(self.engine.run(request_id))

    def _escalate_spent_budget(self, request: dict, max_resumes: int, actor: str) -> dict:
        reason = (f"Retry refused: budget of {max_resumes} resume(s) is spent. "
                  f"Escalated to manual review. Last state: {request['decision_reason']}")
        with self.store.transaction() as tx:
            tx.update_request(request["id"], request["row_version"],
                              status="MANUAL_REVIEW", decision_reason=reason)
            tx.add_history(request["id"], request["status"], "MANUAL_REVIEW",
                           request["current_stage"], request["current_stage"], reason, actor)
            tx.add_audit(request["id"], "RETRY_BUDGET_SPENT", request["current_stage"], {
                "actor": actor, "resume_count": request["resume_count"], "max_resumes": max_resumes,
            })
        return self.get(request["id"])

    def _idle_seconds(self, request: dict) -> float:
        """Seconds since anything happened on the request. Dependency attempts
        are audited as they happen, so a request waiting on a slow service
        keeps showing activity and is not mistaken for an abandoned one."""
        last = self.store.last_activity_at(request["id"]) or request["updated_at"]
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()

    def recover_interrupted(self) -> list[str]:
        """Finish requests a crash left in an active state. Called at startup.

        One request failing to recover must not stop the others or block
        startup, so each is handled on its own. Assumes this is the only
        instance starting; see ARCHITECTURE.md section 13 for the multi-instance gap.
        """
        recovered = []
        for request in self.store.list_requests_by_status(ACTIVE_STATES):
            try:
                with self.store.transaction() as tx:
                    tx.add_audit(request["id"], "RECOVERED", request["current_stage"], {
                        "actor": "startup", "status": request["status"],
                        "idle_seconds": round(self._idle_seconds(request), 1),
                    })
                self.engine.run(request["id"])
                recovered.append(request["id"])
            except Exception:  # noqa: BLE001
                logger.exception("Could not recover request %s; it stays resumable", request["id"])
        return recovered

    # ---- manual review ---------------------------------------------------

    def review(self, request_id: str, decision: str, reviewer: str, comment: str = "") -> dict:
        """Resolve a manual review.

        Manual review is a step in the workflow, not the end of it. REJECTED
        is final. APPROVED means "this stage is cleared": the request moves to
        the stage's ``on_pass`` target, so later stages (and their hard
        limits) still run, and may ask for another review. Only when
        ``on_pass`` is APPROVED does the reviewer's approval end the workflow.
        """
        reviewer, comment = (reviewer or "").strip(), (comment or "").strip()
        if decision not in FINAL_STATES:
            raise InvalidStateError(f"Review decision must be one of {list(FINAL_STATES)}")
        if not reviewer:
            raise InvalidStateError("A reviewer name is required for the audit trail")
        request = self._load(request_id)
        if request["status"] != "MANUAL_REVIEW":
            raise InvalidStateError(
                f"Request is {request['status']}; only MANUAL_REVIEW requests can be reviewed"
            )
        config = self.engine.config_for(request)
        stage_id = request["current_stage"]
        on_pass = config.stages[stage_id]["on_pass"]
        continues = decision == "APPROVED" and on_pass in config.stages
        gaps = self._review_gaps(request_id, stage_id)

        # A stage whose dependency is configured to wait for retry
        # (on_failure RETRY_PENDING) is a check no human may skip: it can
        # reach review only because its retries ran out or the service
        # refused permanently. Approving it would wave the request through
        # with no data, so only a rejection is allowed.
        dependency = config.stages[stage_id]["dependency"]
        if (decision == "APPROVED" and gaps["rules_not_evaluated"] and dependency
                and dependency["on_failure"] == "RETRY_PENDING"):
            raise InvalidStateError(
                f"Stage '{stage_id}' cannot be approved: its rules were never evaluated because "
                f"{gaps['rules_not_evaluated']['cause']}, and its dependency is configured as "
                "required (on_failure RETRY_PENDING). Reject it, or resubmit once the service is back"
            )

        if continues:
            status, next_stage = "IN_PROGRESS", on_pass
            reason = f"Manual review by {reviewer}: stage '{stage_id}' approved, continuing at '{on_pass}'"
        else:
            status, next_stage = decision, stage_id
            reason = f"Manual review by {reviewer}: {decision}"
        if comment:
            reason += f" ({comment})"
        # What the reviewer's approval set aside has to be on the record.
        if decision == "APPROVED" and gaps["rules_not_evaluated"]:
            reason += (f". Note: the rules of stage '{stage_id}' were never evaluated "
                       f"because {gaps['rules_not_evaluated']['cause']}")
        if decision == "APPROVED" and gaps["overridden_rules"]:
            reason += ". Note: this approval also overrides " + ", ".join(
                f"{r['rule_id']} ({r['status']})" for r in gaps["overridden_rules"])

        with self.store.transaction() as tx:
            tx.update_request(request_id, request["row_version"], status=status,
                              current_stage=next_stage, decision_reason=reason)
            tx.add_history(request_id, "MANUAL_REVIEW", status, stage_id, next_stage, reason, reviewer)
            tx.add_audit(request_id, "MANUAL_REVIEW_RESOLVED", stage_id, {
                "decision": decision, "reviewer": reviewer, "comment": comment,
                "continues_at": on_pass if continues else None,
                "review_was_triggered_by": request["decision_reason"],
                "rules_not_evaluated": gaps["rules_not_evaluated"] if decision == "APPROVED" else None,
                "overridden_rules": gaps["overridden_rules"] if decision == "APPROVED" else [],
            })
        if continues:
            return request_view(self.engine.run(request_id))
        return self.get(request_id)

    def _review_gaps(self, request_id: str, stage_id: str) -> dict:
        """What an approval at this stage would wave through.

        * ``rules_not_evaluated``: the request reached review without the
          stage's rules running (dependency outage, internal error).
        * ``overridden_rules``: other rules of the stage that also objected
          but were not the deciding one (first routing rule wins).
        """
        outcomes = [e for e in self.store.get_audit(request_id)
                    if e["event_type"] in ("STAGE_EVALUATED", "DEPENDENCY_FAILED", "ERROR")]
        last = outcomes[-1] if outcomes else None
        if last and last["event_type"] == "STAGE_EVALUATED" and last["stage"] == stage_id:
            detail = last["detail"]
            others = [{"rule_id": r["rule_id"], "status": r["status"], "route": r["route"]}
                      for r in detail["rules"] if r["route"] and r["rule_id"] != detail["decisive_rule"]]
            return {"rules_not_evaluated": None, "overridden_rules": others}
        if last and last["event_type"] == "DEPENDENCY_FAILED":
            dependency = last["detail"]["dependency"]
            cause = f"{dependency} was unavailable"
        else:
            dependency, cause = None, "the stage did not complete (internal error)"
        return {"rules_not_evaluated": {"stage": stage_id, "dependency": dependency, "cause": cause},
                "overridden_rules": []}

    def list_intake_rejections(self, workflow: str) -> list[dict]:
        self.registry.get(workflow)
        return self.store.list_intake_rejections(workflow)

    # ---- workflows -------------------------------------------------------

    def list_workflows(self) -> list[dict]:
        return [config.summary() for config in self.registry.list()]

    def get_workflow(self, name: str) -> dict:
        config = self.registry.get(name)
        return {**config.summary(), "config": config.raw}

    def reload_workflows(self) -> dict:
        return self.registry.reload()
