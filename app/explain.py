"""Turn the raw audit log into an explanation a person can read.

The audit log is the source of truth; this module only reshapes it. Nothing
here re-evaluates rules, so the explanation cannot drift from what happened.
"""
from __future__ import annotations


def build_explanation(request: dict, audit_events: list[dict]) -> dict:
    steps: list[dict] = []
    narrative: list[str] = []
    warnings: list[str] = []
    decided_by = "engine"
    decisive_rule = None
    current: dict | None = None

    def step_for(stage: str) -> dict:
        nonlocal current
        # A new step starts whenever the stage changes or the previous run of
        # this stage already finished (e.g. a retry of the same stage).
        if current is None or current["stage"] != stage or current["closed"]:
            current = {"stage": stage, "dependency_calls": [], "rules": [],
                       "outcome": None, "reason": None, "closed": False}
            steps.append(current)
        return current

    for event in audit_events:
        kind, stage, detail = event["event_type"], event["stage"], event["detail"]

        if kind == "REQUEST_RECEIVED":
            narrative.append(
                f"Request received for workflow '{request['workflow']}' "
                f"v{request['workflow_version']}; input passed schema validation."
            )
        elif kind == "DUPLICATE_REQUEST":
            narrative.append("A duplicate submission arrived and was answered with this same request.")
        elif kind == "DEPENDENCY_CALL":
            step_for(stage)["dependency_calls"].append(detail)
            if detail["ok"]:
                narrative.append(
                    f"[{stage}] {detail['dependency']} answered on attempt "
                    f"{detail['attempt']}: {detail['response']}."
                )
            else:
                narrative.append(
                    f"[{stage}] {detail['dependency']} attempt {detail['attempt']}/"
                    f"{detail['max_attempts']} failed: {detail['error']}."
                )
        elif kind == "DEPENDENCY_FAILED":
            step = step_for(stage)
            step.update(outcome=detail["routed_to"], closed=True,
                        reason=f"{detail['dependency']} unavailable: {detail['last_error']}")
            narrative.append(
                f"[{stage}] {detail['dependency']} still failing after {detail['attempts']} "
                f"attempt(s); request moved to {detail['routed_to']}."
            )
        elif kind == "STAGE_EVALUATED":
            step = step_for(stage)
            step.update(outcome=detail["route"], reason=detail["reason"], closed=True)
            decisive_rule = None  # only the latest stage's decisive rule explains the outcome
            decided_by = "engine"
            for rule in detail["rules"]:
                step["rules"].append({
                    "rule_id": rule["rule_id"], "type": rule["type"],
                    "description": rule["description"], "status": rule["status"],
                    "data": [
                        {"field": c["field"], "actual": c["actual"], "operator": c["operator"],
                         "expected": c["expected"], "expected_from": c.get("expected_from")}
                        for c in rule["checks"]
                    ],
                    "message": rule["message"],
                    "decisive": rule["rule_id"] == detail["decisive_rule"],
                })
                narrative.append(f"[{stage}] {rule['rule_id']}: {rule['message']}.")
                if rule["rule_id"] == detail["decisive_rule"]:
                    decisive_rule = {"stage": stage, **step["rules"][-1]}
            narrative.append(f"[{stage}] Outcome: {detail['route']}. {detail['reason']}.")
        elif kind == "RESUMED":
            narrative.append(
                f"Retry #{detail['resume_count']} requested by {detail['actor']}; "
                f"resuming at stage '{stage}'."
            )
        elif kind == "RECOVERED":
            narrative.append(
                f"Request found {detail['status']} and idle for {detail['idle_seconds']}s; "
                f"processing was taken over by {detail['actor']}."
            )
        elif kind == "RETRY_BUDGET_SPENT":
            narrative.append(
                f"Retry by {detail['actor']} refused: {detail['resume_count']} of "
                f"{detail['max_resumes']} resume(s) already used. Escalated to manual review."
            )
        elif kind == "IDEMPOTENCY_CONFLICT":
            narrative.append(
                "The idempotency key of this request was reused with a different payload; "
                "that submission was refused."
            )
        elif kind == "MANUAL_REVIEW_RESOLVED":
            outcome = (f"stage '{stage}' approved, workflow continues at '{detail['continues_at']}'"
                       if detail.get("continues_at") else detail["decision"])
            narrative.append(
                f"Manual review by {detail['reviewer']}: {outcome}"
                + (f" ({detail['comment']})" if detail.get("comment") else "") + "."
            )
            decided_by = detail["reviewer"]
            decisive_rule = None  # a person decided this step, not a rule
            unverified = detail.get("rules_not_evaluated")
            if unverified:
                warnings.append(
                    f"Reviewer {detail['reviewer']} approved stage '{unverified['stage']}' without its "
                    f"rules: {unverified['cause']}, so they were never evaluated."
                )
            for rule in detail.get("overridden_rules") or []:
                warnings.append(
                    f"Reviewer {detail['reviewer']} approval at stage '{stage}' also overrides rule "
                    f"{rule['rule_id']} ({rule['status']}, would route to {rule['route']})."
                )
        elif kind == "ERROR":
            narrative.append(f"[{stage}] {detail['error']}.")

    for step in steps:
        step.pop("closed")

    return {
        "request_id": request["id"],
        "workflow": request["workflow"],
        "workflow_version": request["workflow_version"],
        "config_hash": request["config_hash"],
        "decision": request["status"],
        "summary": f"{request['status']}: {request['decision_reason'] or 'not processed yet'}",
        "decided_by": decided_by,
        "decisive_rule": decisive_rule,
        "warnings": warnings,
        "steps": steps,
        "narrative": narrative,
    }
