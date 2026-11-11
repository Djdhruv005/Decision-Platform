"""Scenario 4: an external dependency fails."""
from app.dependencies import MockDependency


def test_dependency_down_parks_request_as_retry_pending(platform, service, loan):
    platform.bureau.faults.fail_next = 99

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "RETRY_PENDING"
    assert result.request["is_final"] is False
    assert result.request["current_stage"] == "credit_check"
    assert "credit_bureau" in result.request["reason"]
    assert platform.bureau.calls == 3  # max_attempts from config, no more


def test_every_failed_attempt_is_in_the_audit_trail(platform, service, loan):
    platform.bureau.faults.fail_next = 99
    request_id = service.submit("loan_approval", loan).request["request_id"]

    attempts = [e["detail"] for e in platform.events(request_id, "DEPENDENCY_CALL")]
    assert [a["attempt"] for a in attempts] == [1, 2, 3]
    assert all(a["ok"] is False and "503" in a["error"] for a in attempts)
    assert platform.events(request_id, "DEPENDENCY_FAILED")[0]["detail"]["routed_to"] == "RETRY_PENDING"


def test_backoff_is_exponential_and_comes_from_config(platform, service, loan):
    platform.bureau.faults.fail_next = 99
    service.submit("loan_approval", loan)

    # backoff_ms=100, multiplier=2, 3 attempts -> wait 0.1s then 0.2s (no wait after the last)
    assert platform.sleeps == [0.1, 0.2]


def test_timeouts_are_retried_like_other_transient_errors(platform, service, loan):
    platform.bureau.faults.fail_next = 1
    platform.bureau.faults.mode = "timeout"

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "APPROVED"
    assert platform.bureau.calls == 2


def test_permanent_error_is_not_retried_and_goes_to_a_human(platform, service, loan):
    platform.bureau.faults.fail_next = 99
    platform.bureau.faults.mode = "permanent"

    result = service.submit("loan_approval", loan)

    # Not retried, and not parked for a retry that could never succeed.
    assert result.request["status"] == "MANUAL_REVIEW"
    assert platform.bureau.calls == 1
    assert platform.sleeps == []


def test_failure_route_is_configurable_per_workflow(platform, service, vendor):
    """The vendor workflow sends a registry outage straight to a human."""
    platform.vendors.faults.fail_next = 99

    result = service.submit("vendor_approval", vendor)

    assert result.request["status"] == "MANUAL_REVIEW"
    assert platform.vendors.calls == 2  # this workflow's own retry policy


def test_dependency_crashing_with_an_unexpected_exception_does_not_crash_the_engine(platform, loan):
    class BrokenBureau(MockDependency):
        name = "credit_bureau"

        def respond(self, request_id, payload):
            raise ZeroDivisionError("bug inside the client library")

    platform.bureau = BrokenBureau()
    service = platform.new_service()

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "RETRY_PENDING"
    assert "ZeroDivisionError" in result.request["reason"]


def test_engine_bug_parks_request_as_failed_with_audit(platform, service, loan, monkeypatch):
    def explode(stage, context):
        raise RuntimeError("rule engine bug")
    monkeypatch.setattr("app.workflow_engine.evaluate_stage", explode)

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "FAILED"
    assert "rule engine bug" in result.request["reason"]
    assert len(platform.events(result.request["request_id"], "ERROR")) == 1
