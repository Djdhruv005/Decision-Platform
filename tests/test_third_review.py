"""Regression tests for the third review's findings."""
import datetime
import time

import pytest

from app.dependencies import DependencyRegistry, FaultPlan, MockCreditBureau
from app.errors import InvalidStateError
from app.service import DecisionService


def test_required_check_cannot_be_approved_after_outage(platform, service, loan):
    """Loan routes bureau outages to RETRY_PENDING: once retries are spent and the
    case reaches review, a human may reject it but not approve it with no score."""
    platform.bureau.faults.fail_next = 10**6
    request = service.submit("loan_approval", loan, "outage").request
    while request["status"] != "MANUAL_REVIEW":
        request = service.retry(request["request_id"])
    with pytest.raises(InvalidStateError, match="cannot be approved"):
        service.review(request["request_id"], "APPROVED", "bob")
    assert service.get(request["request_id"])["status"] == "MANUAL_REVIEW"
    assert service.review(request["request_id"], "REJECTED", "bob")["status"] == "REJECTED"


def test_permanent_refusal_on_required_check_cannot_be_approved(platform, service, loan):
    platform.bureau.faults = FaultPlan(fail_next=1, mode="permanent")
    request = service.submit("loan_approval", loan).request
    assert request["status"] == "MANUAL_REVIEW"
    with pytest.raises(InvalidStateError):
        service.review(request["request_id"], "APPROVED", "bob")


def test_optional_check_outage_can_still_be_approved(platform, service, vendor):
    """vendor_approval routes outages to MANUAL_REVIEW on purpose: approval stays allowed."""
    platform.vendors.faults.fail_next = 10
    request = service.submit("vendor_approval", vendor).request
    assert request["status"] == "MANUAL_REVIEW"
    assert service.review(request["request_id"], "APPROVED", "bob")["status"] == "APPROVED"


class _DatetimeBureau(MockCreditBureau):
    def respond(self, request_id, payload):
        return {"score": 780, "as_of": datetime.datetime.now()}


def test_unstorable_dependency_response_never_leaves_request_stuck(platform, loan):
    service = DecisionService(platform.config_dir, platform.db_path,
                              dependencies=DependencyRegistry([_DatetimeBureau(), platform.vendors]),
                              sleep=platform.sleeps.append)
    request = service.submit("loan_approval", loan).request
    assert request["status"] == "MANUAL_REVIEW"   # permanent failure, not a stuck IN_PROGRESS
    assert "not JSON" in request["reason"]


def test_save_error_parks_request_as_failed_with_retry_budget(platform, service, loan, monkeypatch):
    """Any encoding error at save time parks the request as FAILED with an ERROR
    event, so retries are counted and bounded."""
    real = service.engine._commit
    calls = {"n": 0}

    def flaky_commit(request, status, *args, **kwargs):
        calls["n"] += 1
        if status != "FAILED" and status != "MANUAL_REVIEW":
            raise TypeError("cannot encode")
        return real(request, status, *args, **kwargs)

    monkeypatch.setattr(service.engine, "_commit", flaky_commit)
    request = service.submit("loan_approval", loan).request
    assert request["status"] == "FAILED"
    assert platform.events(request["request_id"], "ERROR")
    for _ in range(10):
        request = service.retry(request["request_id"])
        if request["status"] == "MANUAL_REVIEW":
            break
    assert request["status"] == "MANUAL_REVIEW"     # budget spent, a human decides


def test_client_replays_do_not_keep_a_stuck_request_alive(platform, loan):
    service = platform.new_service(stale_after_seconds=0.5)

    def db_down(operation):
        if operation == "update_request":
            raise OSError("db down")

    service.store.fault_hook = db_down
    with pytest.raises(OSError):
        service.submit("loan_approval", loan, "stuck")
    service.store.fault_hook = None
    service.submit("loan_approval", loan, "stuck")       # early replay: still RECEIVED
    time.sleep(0.6)
    # Only 0.6 s since the first replay, but replays are not worker activity.
    assert service.submit("loan_approval", loan, "stuck").request["status"] == "APPROVED"


@pytest.mark.parametrize("blank", ["   ", "\t"])
def test_mandatory_rejects_blank_values(service, vendor, blank):
    request = service.submit("vendor_approval", {**vendor, "gst_number": blank}).request
    assert request["status"] == "REJECTED"


@pytest.mark.parametrize("empty", [[], {}, "  "])
def test_empty_values_are_not_present(empty):
    from app.rule_engine import evaluate_condition
    ok, _ = evaluate_condition({"field": "input.x", "op": "exists"}, {"input": {"x": empty}})
    assert ok is False


def test_unparseable_body_gets_documented_400_and_is_logged(platform):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.api import create_app

    client = TestClient(create_app(platform.service))
    response = client.post("/workflows/loan_approval/requests", content=b"{bad",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_input"
    assert platform.service.list_intake_rejections("loan_approval")
