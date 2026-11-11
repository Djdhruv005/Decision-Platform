"""Scenario 1: happy path, and each decision branch of the loan workflow."""


def test_valid_loan_is_approved_through_all_stages(platform, service, loan):
    result = service.submit("loan_approval", loan, "key-1")

    assert result.created is True
    assert result.request["status"] == "APPROVED"
    assert result.request["is_final"] is True
    assert result.request["current_stage"] == "risk_assessment"

    stages = [e["stage"] for e in platform.events(result.request["request_id"], "STAGE_EVALUATED")]
    assert stages == ["eligibility", "credit_check", "risk_assessment"]


def test_state_history_records_every_transition(service, loan):
    request_id = service.submit("loan_approval", loan).request["request_id"]
    history = service.audit(request_id)["state_history"]

    assert [(h["from_status"], h["to_status"], h["to_stage"]) for h in history] == [
        (None, "RECEIVED", None),
        ("RECEIVED", "IN_PROGRESS", "credit_check"),
        ("IN_PROGRESS", "IN_PROGRESS", "risk_assessment"),
        ("IN_PROGRESS", "APPROVED", "risk_assessment"),
    ]
    assert all(h["reason"] and h["actor"] for h in history)


def test_threshold_failure_rejects_and_names_the_rule(service, loan):
    result = service.submit("loan_approval", {**loan, "annual_income": 250000})

    assert result.request["status"] == "REJECTED"
    assert "ELIG-INCOME" in result.request["reason"]
    assert result.request["current_stage"] == "eligibility"


def test_missing_mandatory_field_rejects_with_audit_not_a_400(service, loan):
    """PAN is optional in the schema but mandatory by business rule, so its
    absence is an audited REJECTED decision, not a malformed-request error."""
    payload = {k: v for k, v in loan.items() if k != "pan"}
    result = service.submit("loan_approval", payload)

    assert result.request["status"] == "REJECTED"
    assert "ELIG-PAN" in result.request["reason"]


def test_low_credit_score_rejects_using_dependency_data(service, loan):
    result = service.submit("loan_approval", {**loan, "applicant_id": "A-LOW"})

    assert result.request["status"] == "REJECTED"
    assert "CREDIT-MIN" in result.request["reason"]
    assert "610" in result.request["reason"]


def test_conditional_rule_routes_borderline_score_to_manual_review(service, loan):
    result = service.submit("loan_approval", {**loan, "applicant_id": "A-BORDER"})

    assert result.request["status"] == "MANUAL_REVIEW"
    assert result.request["is_final"] is False
    assert "CREDIT-BORDERLINE" in result.request["reason"]


def test_conditional_rule_in_later_stage_routes_large_loan_to_manual_review(service, loan):
    result = service.submit("loan_approval", {**loan, "amount": 2000000})

    assert result.request["status"] == "MANUAL_REVIEW"
    assert "RISK-HIGH-VALUE" in result.request["reason"]


def test_earlier_stage_failure_stops_later_stages_and_skips_the_dependency(platform, service, loan):
    """Multi-step evaluation: a request rejected at stage 1 never reaches the
    credit bureau, so no external call is paid for."""
    service.submit("loan_approval", {**loan, "age": 19})

    assert platform.bureau.calls == 0
