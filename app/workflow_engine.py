"""Workflow engine: the state machine.

Drives one request through the stages of its workflow. It knows *how* to run
a workflow, never *which* workflow: stages, rules, retry policy and routing
all come from config.

Request states:

    RECEIVED ──> IN_PROGRESS ──> APPROVED            (final)
                     │      ├──> REJECTED            (final)
                     │      ├──> MANUAL_REVIEW ──> REJECTED, or on approval the next
                     │      │                      stage (APPROVED if it was the last)
                     │      ├──> RETRY_PENDING ──> IN_PROGRESS (on retry)
                     │      └──> FAILED        ──> IN_PROGRESS (on retry)
                     └── stage to stage while IN_PROGRESS

Guarantees:

* One stage execution is one database transaction: the new state, the history
  row and the stage's audit events are saved together or not at all.
* Calls to external dependencies are audited as they happen, outside that
  transaction: a call that was made stays on record even if the stage is not saved.
* A request is pinned to the config version it started under. Editing a
  config changes future requests, never one that is half-way through.
* A crash between stages loses nothing: the request stays at its last
  committed stage and ``run`` picks it up from there.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from app.config_loader import WorkflowConfig
from app.dependencies import DependencyRegistry, call_with_retry
from app.errors import ConcurrentUpdateError, PlatformError
from app.rule_engine import MISSING, evaluate_stage, resolve
from app.store import Store

ACTIVE_STATES = ("RECEIVED", "IN_PROGRESS")          # the engine should be working on these
RESUMABLE_STATES = ("RETRY_PENDING", "FAILED")       # parked, waiting for a retry
FINAL_STATES = ("APPROVED", "REJECTED")

Event = tuple[str, Optional[str], dict]              # (event_type, stage, detail)


class WorkflowEngine:
    MAX_STEPS = 100  # backstop; config validation already rejects cyclic workflows

    def __init__(self, store: Store, dependencies: DependencyRegistry,
                 sleep: Callable[[float], None] = time.sleep):
        self.store = store
        self.dependencies = dependencies
        self.sleep = sleep
        self._config_cache: dict[str, WorkflowConfig] = {}

    def run(self, request_id: str) -> dict:
        """Advance a request until it reaches a state that needs no more work."""
        for _ in range(self.MAX_STEPS):
            request = self.store.get_request(request_id)
            if request["status"] not in ACTIVE_STATES:
                return request
            try:
                self._execute_stage(request)
            except ConcurrentUpdateError:
                # Another worker saved this stage first. Its result stands;
                # drop ours, re-read, and carry on from wherever the request is now.
                continue
            except (TypeError, ValueError) as exc:
                # The stage result could not be saved (e.g. it cannot be
                # encoded). Its transaction rolled back. Park the request as
                # FAILED with the error audited, so the retry budget applies
                # instead of the request staying active and retried forever.
                self._park_after_save_error(request, exc)
        raise PlatformError(f"Request {request_id} exceeded {self.MAX_STEPS} stage executions")

    def _park_after_save_error(self, request: dict, exc: Exception) -> None:
        config = self.config_for(request)
        stage_id = request["current_stage"] or config.first_stage
        reason = f"Could not save stage '{stage_id}': {type(exc).__name__}: {exc}"
        target, reason = self._apply_retry_budget("FAILED", reason, request, config)
        try:
            self._commit(request, target, stage_id, dict(request["dependency_data"]), reason,
                         [("ERROR", stage_id, {"error": reason})])
        except ConcurrentUpdateError:
            pass  # someone else moved the request on; the loop re-reads it

    def config_for(self, request: dict) -> WorkflowConfig:
        """The exact config this request started under, from the DB snapshot."""
        config_hash = request["config_hash"]
        if config_hash not in self._config_cache:
            snapshot = self.store.get_workflow_version(config_hash)
            self._config_cache[config_hash] = WorkflowConfig.from_dict(
                snapshot["body"], f"snapshot:{config_hash[:12]}"
            )
        return self._config_cache[config_hash]

    def _execute_stage(self, request: dict) -> None:
        config = self.config_for(request)
        stage_id = request["current_stage"] or config.first_stage
        stage = config.stages[stage_id]
        dependency_data = dict(request["dependency_data"])
        events: list[Event] = []

        # Phase 1: do the work (call the dependency, evaluate rules). Nothing is
        # saved yet. Phase 2 below saves the outcome in a single transaction.
        failed_outcome = None
        result = None
        try:
            dep = stage["dependency"]
            # Skip the call if an earlier attempt already fetched this data.
            if dep and dep["name"] not in dependency_data:
                outcome = call_with_retry(
                    self.dependencies.get(dep["name"]), request["id"],
                    self._dependency_params(dep, request, dependency_data),
                    dep["retry"], self.sleep,
                    on_attempt=lambda record: self._audit_dependency_attempt(request["id"], stage_id, record),
                )
                if outcome.ok:
                    dependency_data[dep["name"]] = outcome.result
                else:
                    failed_outcome = outcome
            if failed_outcome is None:
                result = evaluate_stage(stage, {"input": request["payload"], "deps": dependency_data})
        except Exception as exc:  # noqa: BLE001 - an engine bug must park the request, not lose it
            reason = f"Unexpected error at stage '{stage_id}': {type(exc).__name__}: {exc}"
            events.append(("ERROR", stage_id, {"error": reason}))
            target, reason = self._apply_retry_budget("FAILED", reason, request, config)
            self._commit(request, target, stage_id, dict(request["dependency_data"]), reason, events)
            return

        if failed_outcome is not None:
            self._park_on_dependency_failure(request, config, stage_id, dep, failed_outcome, events)
            return

        events.append(("STAGE_EVALUATED", stage_id, result.to_dict()))
        if result.route in config.stages:
            self._commit(request, "IN_PROGRESS", result.route, dependency_data, result.reason, events)
            return

        events.append(("DECISION", stage_id, {
            "decision": result.route,
            "reason": result.reason,
            "decisive_rule": result.decisive_rule["rule_id"] if result.decisive_rule else None,
            "workflow_version": config.version,
            "config_hash": config.config_hash,
        }))
        self._commit(request, result.route, stage_id, dependency_data, result.reason, events)

    @staticmethod
    def _dependency_params(dep: dict, request: dict, dependency_data: dict) -> dict:
        """Build the call parameters from the config's ``input`` mapping.
        With no mapping the whole payload is passed."""
        if not dep["input"]:
            return request["payload"]
        context = {"input": request["payload"], "deps": dependency_data}
        values = {name: resolve(context, path) for name, path in dep["input"].items()}
        return {name: (None if value is MISSING else value) for name, value in values.items()}

    def _audit_dependency_attempt(self, request_id: str, stage_id: str, record: dict) -> None:
        """An external call is a fact the moment it happens, whatever becomes
        of the stage afterwards. It is written at once, in its own
        transaction, so it survives a lost write race or a crash before the
        stage is saved. (A paid or side-effecting call must never go missing
        from the audit trail.)"""
        with self.store.transaction() as tx:
            tx.add_audit(request_id, "DEPENDENCY_CALL", stage_id, record)

    def _park_on_dependency_failure(self, request: dict, config: WorkflowConfig, stage_id: str,
                                    dep: dict, outcome, events: list[Event]) -> None:
        target = dep["on_failure"]
        reason = (
            f"Dependency '{dep['name']}' failed at stage '{stage_id}' after "
            f"{len(outcome.attempts)} attempt(s): {outcome.last_error}"
        )
        if target == "RETRY_PENDING" and not outcome.transient:
            # The dependency answered with a definite refusal. Retrying cannot
            # change that, so a person looks at it instead.
            target = "MANUAL_REVIEW"
            reason += ". Error is permanent, so it was escalated to manual review instead of retried"
        target, reason = self._apply_retry_budget(target, reason, request, config)
        events.append(("DEPENDENCY_FAILED", stage_id, {
            "dependency": dep["name"],
            "attempts": len(outcome.attempts),
            "last_error": outcome.last_error,
            "routed_to": target,
        }))
        self._commit(request, target, stage_id, dict(request["dependency_data"]), reason, events)

    @staticmethod
    def _apply_retry_budget(target: str, reason: str, request: dict,
                            config: WorkflowConfig) -> tuple[str, str]:
        """Retries are bounded at two levels: attempts per run (retry policy)
        and runs per request (max_resumes). When a request would be parked for
        yet another retry with its budget spent, a human decides instead.
        Applies to every retryable state, not only dependency failures."""
        if target in RESUMABLE_STATES and request["resume_count"] >= config.max_resumes:
            reason += f". Retry budget of {config.max_resumes} resume(s) is spent, escalated to manual review"
            return "MANUAL_REVIEW", reason
        return target, reason

    def _commit(self, request: dict, status: str, stage: str, dependency_data: dict,
                reason: str, events: list[Event], actor: str = "engine") -> None:
        """Save everything one stage execution produced, atomically."""
        with self.store.transaction() as tx:
            tx.update_request(
                request["id"], request["row_version"],
                status=status, current_stage=stage,
                dependency_data=dependency_data, decision_reason=reason,
            )
            tx.add_history(request["id"], request["status"], status,
                           request["current_stage"], stage, reason, actor)
            for event_type, event_stage, detail in events:
                tx.add_audit(request["id"], event_type, event_stage, detail)
