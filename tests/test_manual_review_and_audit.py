"""Manual review stage, the second workflow, and the audit / explanation output."""
import pytest

from app.errors import InvalidStateError, RequestNotFound


@pytest.fixture
def in_review(service, loan):
    return service.submit("loan_approval", {**loan, "applicant_id": "A-BORDER"}).request["request_id"]


def test_reviewer_approval_is_attributed_and_the_workflow_continues(service, in_review):
    """Approval clears the stage under review; the remaining stage still runs."""
    result = service.review(in_review, "APPROVED", "asha.k", "Stable employer, low exposure")

    assert result["status"] == "APPROVED" and result["is_final"] is True
    assert result["current_stage"] == "risk_assessment"
    history = service.audit(in_review)["state_history"]
    step = history[-2]
    assert (step["from_status"], step["to_status"], step["actor"]) == ("MANUAL_REVIEW", "IN_PROGRESS", "asha.k")
    assert (step["from_stage"], step["to_stage"]) == ("credit_check", "risk_assessment")
    assert "Stable employer" in step["reason"]
    assert (history[-1]["to_status"], history[-1]["actor"]) == ("APPROVED", "engine")


def test_reviewer_rejection_is_final(service, in_review):
    result = service.review(in_review, "REJECTED", "asha.k", "Unverifiable employer")

    assert result["status"] == "REJECTED" and result["is_final"] is True
    last = service.audit(in_review)["state_history"][-1]
    assert (last["from_status"], last["to_status"], last["actor"]) == ("MANUAL_REVIEW", "REJECTED", "asha.k")
    assert service.explanation(in_review)["decided_by"] == "asha.k"


def test_review_audit_keeps_why_the_review_was_needed(platform, service, in_review):
    service.review(in_review, "REJECTED", "asha.k")

    event = platform.events(in_review, "MANUAL_REVIEW_RESOLVED")[0]["detail"]
    assert "CREDIT-BORDERLINE" in event["review_was_triggered_by"]


def test_request_cannot_be_reviewed_twice(service, in_review):
    service.review(in_review, "APPROVED", "asha.k")
    with pytest.raises(InvalidStateError):
        service.review(in_review, "REJECTED", "someone.else")


def test_only_requests_in_manual_review_can_be_reviewed(service, loan):
    approved = service.submit("loan_approval", loan).request["request_id"]
    with pytest.raises(InvalidStateError):
        service.review(approved, "REJECTED", "asha.k")


@pytest.mark.parametrize("decision, reviewer", [("MAYBE", "asha.k"), ("APPROVED", ""), ("APPROVED", "   ")])
def test_review_needs_a_valid_decision_and_a_named_reviewer(service, in_review, decision, reviewer):
    with pytest.raises(InvalidStateError):
        service.review(in_review, decision, reviewer)


def test_unknown_request_id(service):
    with pytest.raises(RequestNotFound):
        service.get("does-not-exist")


# ---- second workflow on the same engine ----------------------------------

def test_vendor_workflow_runs_on_the_same_engine(service, vendor):
    assert service.submit("vendor_approval", vendor).request["status"] == "APPROVED"


def test_vendor_blacklist_rejects(service, vendor):
    result = service.submit("vendor_approval", {**vendor, "vendor_id": "V-BAD"})
    assert result.request["status"] == "REJECTED"
    assert "COMP-BLACKLIST" in result.request["reason"]


def test_rule_level_route_overrides_stage_default(service, vendor):
    """DOC-TRACK-RECORD sets on_fail=MANUAL_REVIEW; the stage default is REJECTED."""
    result = service.submit("vendor_approval", {**vendor, "years_in_business": 1})
    assert result.request["status"] == "MANUAL_REVIEW"
    assert "DOC-TRACK-RECORD" in result.request["reason"]


@pytest.mark.parametrize("change", [{"category": "defense"}, {"annual_contract_value": 9000000}])
def test_compound_any_condition(service, vendor, change):
    result = service.submit("vendor_approval", {**vendor, **change})
    assert result.request["status"] == "MANUAL_REVIEW"
    assert "COMP-SENSITIVE" in result.request["reason"]


def test_first_routing_rule_in_config_order_wins(service, vendor):
    """Missing GST (reject) is listed before the track-record rule (review)."""
    payload = {k: v for k, v in vendor.items() if k != "gst_number"}
    result = service.submit("vendor_approval", {**payload, "years_in_business": 1})
    assert result.request["status"] == "REJECTED"
    assert "DOC-GST" in result.request["reason"]


# ---- auditability --------------------------------------------------------

def test_every_rule_is_traced_with_the_data_it_saw(service, loan):
    request_id = service.submit("loan_approval", {**loan, "applicant_id": "A-LOW"}).request["request_id"]
    explanation = service.explanation(request_id)

    assert explanation["decision"] == "REJECTED"
    rule = explanation["decisive_rule"]
    assert (rule["rule_id"], rule["stage"], rule["status"]) == ("CREDIT-MIN", "credit_check", "FAIL")
    assert rule["data"] == [{
        "field": "deps.credit_bureau.score", "actual": 610, "operator": ">=", "expected": 650,
        "expected_from": None,
    }]
    # Rules that passed are in the trace too, not just the one that failed.
    traced = [r["rule_id"] for step in explanation["steps"] for r in step["rules"]]
    assert traced == ["ELIG-PAN", "ELIG-AGE", "ELIG-INCOME", "CREDIT-MIN", "CREDIT-BORDERLINE"]


def test_all_rules_in_a_stage_are_evaluated_even_after_one_fails(service, loan):
    request_id = service.submit("loan_approval", {**loan, "age": 18, "annual_income": 1}).request["request_id"]
    rules = service.explanation(request_id)["steps"][0]["rules"]

    assert [(r["rule_id"], r["status"]) for r in rules] == [
        ("ELIG-PAN", "PASS"), ("ELIG-AGE", "FAIL"), ("ELIG-INCOME", "FAIL"),
    ]
    assert [r["decisive"] for r in rules] == [False, True, False]


def test_audit_bundle_contains_input_dependency_data_history_and_events(service, loan):
    request_id = service.submit("loan_approval", loan, "key-1").request["request_id"]
    audit = service.audit(request_id)

    assert audit["input"] == loan
    assert audit["dependency_data"]["credit_bureau"]["score"] == 780
    assert audit["audit_events"][0]["event_type"] == "REQUEST_RECEIVED"
    assert audit["audit_events"][0]["detail"]["idempotency_key_source"] == "client"
    assert audit["audit_events"][-1]["event_type"] == "DECISION"
    assert audit["audit_events"][-1]["detail"]["workflow_version"] == 1
    assert audit["explanation"]["narrative"]


def test_audit_and_history_tables_are_append_only_across_a_retry(platform, service, loan):
    platform.bureau.faults.fail_next = 3
    request_id = service.submit("loan_approval", loan).request["request_id"]
    before = service.audit(request_id)

    service.retry(request_id)
    after = service.audit(request_id)

    assert after["audit_events"][:len(before["audit_events"])] == before["audit_events"]
    assert after["state_history"][:len(before["state_history"])] == before["state_history"]
