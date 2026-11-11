"""HTTP-level tests: status codes and response shapes of the REST API.

Skipped automatically if FastAPI / httpx are not installed. All business
behaviour is covered by the service-level tests; these check the HTTP mapping.
"""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402


@pytest.fixture
def client(platform):
    return TestClient(create_app(platform.service))


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "loan_approval" in body["workflows"]
    assert body["config_errors"] == {}


def test_health_reports_a_workflow_file_that_failed_to_load(platform):
    (platform.config_dir / "vendor_approval.json").write_text("{ broken")
    degraded = TestClient(create_app(platform.new_service()))

    body = degraded.get("/health").json()

    assert body["status"] == "degraded"
    assert body["workflows"] == ["loan_approval"]
    assert "vendor_approval.json" in body["config_errors"]


def test_interrupted_requests_are_finished_when_the_server_starts(platform, loan):
    service = platform.service

    def crash(operation):
        if operation == "update_request":
            raise KeyboardInterrupt("process killed")
    original_run = service.engine.run

    def run_with_fault(request_id):
        service.store.fault_hook = crash
        return original_run(request_id)
    service.engine.run = run_with_fault
    with pytest.raises(KeyboardInterrupt):
        service.submit("loan_approval", loan, "key-1")
    stuck = service.store.find_by_idempotency_key("loan_approval", "key-1")
    assert stuck["status"] == "RECEIVED"

    restarted = create_app(platform.new_service(), recover_on_startup=True)
    with TestClient(restarted) as running:   # entering the block runs the startup hook
        assert running.get(f"/requests/{stuck['id']}").json()["status"] == "APPROVED"


def test_blank_reviewer_name_is_refused(client, loan):
    created = client.post("/workflows/loan_approval/requests", json={**loan, "applicant_id": "A-BORDER"}).json()
    response = client.post(f"/requests/{created['request_id']}/review",
                           json={"decision": "APPROVED", "reviewer": "   "})
    assert response.status_code == 409


def test_submit_returns_201_then_200_for_a_duplicate(client, loan):
    headers = {"Idempotency-Key": "key-1"}
    first = client.post("/workflows/loan_approval/requests", json=loan, headers=headers)
    second = client.post("/workflows/loan_approval/requests", json=loan, headers=headers)

    assert first.status_code == 201 and first.json()["duplicate"] is False
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert first.json()["request_id"] == second.json()["request_id"]
    assert first.json()["status"] == "APPROVED"


def test_invalid_input_returns_400_with_field_details(client, loan):
    response = client.post("/workflows/loan_approval/requests", json={**loan, "amount": "lots"})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_input"
    assert response.json()["details"][0]["field"] == "amount"


def test_key_reuse_with_different_payload_returns_409(client, loan):
    headers = {"Idempotency-Key": "key-1"}
    first = client.post("/workflows/loan_approval/requests", json=loan, headers=headers)
    clash = client.post("/workflows/loan_approval/requests", json={**loan, "amount": 1}, headers=headers)

    assert clash.status_code == 409
    assert clash.json()["error"] == "idempotency_conflict"
    assert clash.json()["existing_request_id"] == first.json()["request_id"]


def test_unknown_workflow_and_unknown_request_return_404(client, loan):
    assert client.post("/workflows/nope/requests", json=loan).status_code == 404
    assert client.get("/requests/nope").status_code == 404
    assert client.get("/requests/nope/audit").status_code == 404


def test_fault_injection_then_retry_over_http(client, loan):
    faults = client.post("/simulate/dependencies/credit_bureau/faults", json={"fail_next": 3})
    assert faults.status_code == 200

    parked = client.post("/workflows/loan_approval/requests", json=loan).json()
    assert parked["status"] == "RETRY_PENDING"

    retried = client.post(f"/requests/{parked['request_id']}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "APPROVED"
    # a finished request cannot be retried again
    assert client.post(f"/requests/{parked['request_id']}/retry").status_code == 409


def test_manual_review_over_http(client, loan):
    created = client.post("/workflows/loan_approval/requests", json={**loan, "applicant_id": "A-BORDER"}).json()
    assert created["status"] == "MANUAL_REVIEW"

    url = f"/requests/{created['request_id']}/review"
    reviewed = client.post(url, json={"decision": "APPROVED", "reviewer": "asha.k", "comment": "ok"})
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "APPROVED"
    assert client.post(url, json={"decision": "REJECTED", "reviewer": "asha.k"}).status_code == 409


def test_audit_and_explanation_endpoints(client, loan):
    request_id = client.post("/workflows/loan_approval/requests", json=loan).json()["request_id"]

    audit = client.get(f"/requests/{request_id}/audit").json()
    explanation = client.get(f"/requests/{request_id}/explanation").json()

    assert audit["input"] == loan
    assert audit["state_history"] and audit["audit_events"]
    assert explanation["decision"] == "APPROVED" and explanation["narrative"]


def test_nan_in_the_request_body_returns_400_not_500(client):
    body = '{"applicant_id": "A-GOOD", "pan": "ABCDE1234F", "age": 30, "annual_income": NaN, "amount": 500000}'
    response = client.post("/workflows/loan_approval/requests", content=body,
                           headers={"Content-Type": "application/json"})

    # Refused either by the service (400) or by the framework's own JSON
    # parsing (422), depending on the FastAPI version. Never accepted, never a 500.
    assert response.status_code in (400, 422)
    if response.status_code == 400:
        assert response.json()["details"][0]["constraint"] == "finite"


def test_intake_rejections_endpoint(client, loan):
    client.post("/workflows/loan_approval/requests", json={**loan, "amount": "lots"})

    rejections = client.get("/workflows/loan_approval/intake-rejections").json()

    assert len(rejections) == 1
    assert rejections[0]["errors"][0]["field"] == "amount"


def test_fault_injection_rejects_nonsense_values(client):
    url = "/simulate/dependencies/credit_bureau/faults"
    assert client.post(url, json={"fail_next": -5}).status_code == 422
    assert client.post(url, json={"failure_rate": 7}).status_code == 422


def test_fault_injection_can_be_switched_off(platform):
    production = TestClient(create_app(platform.service, enable_simulation=False))

    response = production.post("/simulate/dependencies/credit_bureau/faults", json={"fail_next": 1})

    assert response.status_code == 404
    assert production.get("/health").status_code == 200


def test_workflow_listing_and_reload(client):
    names = [w["workflow"] for w in client.get("/workflows").json()]
    assert names == ["loan_approval", "vendor_approval"]
    assert client.get("/workflows/loan_approval").json()["config"]["version"] == 1
    assert client.post("/workflows/reload").json()["errors"] == {}
