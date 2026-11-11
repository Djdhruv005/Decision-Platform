"""Scenario 2: invalid input is refused before anything is stored or processed."""
import pytest

from app.errors import ValidationFailed, WorkflowNotFound


def test_missing_required_fields_are_all_reported(service):
    with pytest.raises(ValidationFailed) as exc:
        service.submit("loan_approval", {"applicant_id": "A-GOOD"})

    messages = " ".join(e["message"] for e in exc.value.errors)
    assert "'age' is a required property" in messages
    assert "'annual_income' is a required property" in messages
    assert "'amount' is a required property" in messages


def test_wrong_type_names_the_field(service, loan):
    with pytest.raises(ValidationFailed) as exc:
        service.submit("loan_approval", {**loan, "amount": "five lakh"})

    assert exc.value.errors[0]["field"] == "amount"
    assert exc.value.errors[0]["constraint"] == "type"


@pytest.mark.parametrize("change", [
    {"amount": 0},                 # must be > 0
    {"age": -1},                   # must be >= 0
    {"pan": "not-a-pan"},          # wrong format
    {"employment_type": "pirate"}, # not in the allowed list
])
def test_out_of_range_and_malformed_values_are_refused(service, loan, change):
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", {**loan, **change})


@pytest.mark.parametrize("payload", [None, [], "text", 42])
def test_non_object_payload_is_refused(service, payload):
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", payload)


def test_invalid_input_stores_nothing_and_calls_no_dependency(platform, service, loan):
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", {**loan, "amount": "bad"}, "key-1")

    assert service.store.count_requests() == 0
    assert platform.bureau.calls == 0


def test_invalid_attempt_does_not_burn_the_idempotency_key(service, loan):
    """A client can fix its payload and resend with the same key."""
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", {**loan, "amount": "bad"}, "key-1")

    result = service.submit("loan_approval", loan, "key-1")
    assert result.created is True
    assert result.request["status"] == "APPROVED"


def test_unknown_workflow(service, loan):
    with pytest.raises(WorkflowNotFound):
        service.submit("no_such_workflow", loan)
