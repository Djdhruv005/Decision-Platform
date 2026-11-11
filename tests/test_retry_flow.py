"""Scenario 5: retry flows, bounded retries, and recovery after a crash."""
import pytest

from app.errors import InvalidStateError


def test_transient_failure_recovers_within_one_run(platform, service, loan):
    platform.bureau.faults.fail_next = 2

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "APPROVED"
    attempts = platform.events(result.request["request_id"], "DEPENDENCY_CALL")
    assert [a["detail"]["ok"] for a in attempts] == [False, False, True]


def test_parked_request_resumes_from_failed_stage_on_retry(platform, service, loan):
    platform.bureau.faults.fail_next = 3
    request_id = service.submit("loan_approval", loan).request["request_id"]
    assert service.get(request_id)["status"] == "RETRY_PENDING"

    result = service.retry(request_id)

    assert result["status"] == "APPROVED"
    assert result["resume_count"] == 1
    # eligibility ran once only: the retry resumed at credit_check, it did not start over
    stages = [e["stage"] for e in platform.events(request_id, "STAGE_EVALUATED")]
    assert stages == ["eligibility", "credit_check", "risk_assessment"]
    history = [(h["from_status"], h["to_status"]) for h in service.audit(request_id)["state_history"]]
    assert ("IN_PROGRESS", "RETRY_PENDING") in history
    assert ("RETRY_PENDING", "IN_PROGRESS") in history


def test_retry_budget_is_bounded_then_escalates_to_a_human(platform, service, loan):
    platform.bureau.faults.fail_next = 999
    request_id = service.submit("loan_approval", loan).request["request_id"]

    statuses = [service.retry(request_id)["status"] for _ in range(3)]  # max_resumes = 3

    assert statuses == ["RETRY_PENDING", "RETRY_PENDING", "MANUAL_REVIEW"]
    assert "Retry budget" in service.get(request_id)["reason"]
    with pytest.raises(InvalidStateError):
        service.retry(request_id)


@pytest.mark.parametrize("applicant, status", [("A-GOOD", "APPROVED"), ("A-LOW", "REJECTED")])
def test_final_decisions_cannot_be_retried(service, loan, applicant, status):
    request_id = service.submit("loan_approval", {**loan, "applicant_id": applicant}).request["request_id"]
    assert service.get(request_id)["status"] == status

    with pytest.raises(InvalidStateError):
        service.retry(request_id)


def test_dependency_data_already_fetched_is_not_fetched_again(platform, service, loan, monkeypatch):
    """Fail at risk_assessment after the bureau answered; the retry must reuse the score."""
    import app.workflow_engine as engine_module
    real = engine_module.evaluate_stage

    def flaky(stage, context):
        if stage["id"] == "risk_assessment" and not flaky.failed:
            flaky.failed = True
            raise RuntimeError("temporary glitch")
        return real(stage, context)
    flaky.failed = False
    monkeypatch.setattr(engine_module, "evaluate_stage", flaky)

    request_id = service.submit("loan_approval", loan).request["request_id"]
    assert service.get(request_id)["status"] == "FAILED"

    assert service.retry(request_id)["status"] == "APPROVED"
    assert platform.bureau.calls == 1


# ---- partial save failure and crash recovery -----------------------------

def test_partial_save_failure_rolls_back_the_whole_stage(platform, service, loan):
    """The database fails while writing the audit rows of the 2nd stage.
    State, history and audit must all stay at the last consistent point."""
    writes = {"n": 0}

    def fail_during_second_stage(operation):
        if operation == "update_request":
            writes["n"] += 1
        if operation == "add_audit" and writes["n"] == 2:
            raise OSError("disk full")
    # Arm the fault only after intake, so the request itself is stored.
    original_run = service.engine.run

    def run_with_fault(request_id):
        service.store.fault_hook = fail_during_second_stage
        return original_run(request_id)
    service.engine.run = run_with_fault

    with pytest.raises(OSError):
        service.submit("loan_approval", loan, "key-1")
    service.store.fault_hook = None

    request = service.store.find_by_idempotency_key("loan_approval", "key-1")
    # Stage 1 committed, stage 2 did not: no half-written stage anywhere.
    assert request["status"] == "IN_PROGRESS"
    assert request["current_stage"] == "credit_check"
    assert request["dependency_data"] == {}
    assert [e["stage"] for e in platform.events(request["id"], "STAGE_EVALUATED")] == ["eligibility"]
    assert len(service.store.get_history(request["id"])) == 2
    # The bureau call itself did happen, and that fact is kept even though the
    # stage it belonged to was rolled back.
    assert len(platform.events(request["id"], "DEPENDENCY_CALL")) == platform.bureau.calls == 1


def test_interrupted_request_is_finished_after_restart(platform, service, loan):
    def crash_at_second_stage(operation):
        if operation == "update_request":
            crash_at_second_stage.n += 1
            if crash_at_second_stage.n == 2:
                raise KeyboardInterrupt("process killed")
    crash_at_second_stage.n = 0
    original_run = service.engine.run

    def run_with_fault(request_id):
        service.store.fault_hook = crash_at_second_stage
        return original_run(request_id)
    service.engine.run = run_with_fault

    with pytest.raises(KeyboardInterrupt):
        service.submit("loan_approval", loan, "key-1")

    restarted = platform.new_service()           # new process, same database
    recovered = restarted.recover_interrupted()

    request = restarted.store.find_by_idempotency_key("loan_approval", "key-1")
    assert recovered == [request["id"]]
    assert request["status"] == "APPROVED"
    stages = [e["stage"] for e in restarted.store.get_audit(request["id"]) if e["event_type"] == "STAGE_EVALUATED"]
    assert stages == ["eligibility", "credit_check", "risk_assessment"]  # no stage ran twice


def test_client_retry_after_a_crash_does_not_create_a_second_request(platform, service, loan):
    def fail_first_engine_write(operation):
        if operation == "update_request":
            raise OSError("db unavailable")
    original_run = service.engine.run

    def run_with_fault(request_id):
        service.store.fault_hook = fail_first_engine_write
        return original_run(request_id)
    service.engine.run = run_with_fault
    with pytest.raises(OSError):
        service.submit("loan_approval", loan, "key-1")
    service.store.fault_hook = None
    service.engine.run = original_run

    # The client replays its key. The stored request is found abandoned, so
    # the replay finishes it: no second request, no manual retry needed.
    replay = service.submit("loan_approval", loan, "key-1")
    assert replay.created is False
    assert replay.request["status"] == "APPROVED"
    assert service.store.count_requests() == 1
    recovered = platform.events(replay.request["request_id"], "RECOVERED")
    assert recovered[0]["detail"]["actor"] == "client-replay"


def test_replay_does_not_take_over_a_request_that_is_still_being_processed(platform, service, loan):
    def fail_first_engine_write(operation):
        if operation == "update_request":
            raise OSError("db unavailable")
    original_run = service.engine.run

    def run_with_fault(request_id):
        service.store.fault_hook = fail_first_engine_write
        return original_run(request_id)
    service.engine.run = run_with_fault
    with pytest.raises(OSError):
        service.submit("loan_approval", loan, "key-1")

    cautious = platform.new_service(stale_after_seconds=30)
    replay = cautious.submit("loan_approval", loan, "key-1")

    assert replay.created is False
    assert replay.request["status"] == "RECEIVED"   # touched a moment ago: left alone


def test_stale_worker_cannot_overwrite_a_newer_state(service, loan):
    """Optimistic lock: a worker holding an old copy of the request is refused."""
    from app.errors import ConcurrentUpdateError

    request_id = service.submit("loan_approval", {**loan, "applicant_id": "A-BORDER"}).request["request_id"]
    stale = service.store.get_request(request_id)
    service.review(request_id, "APPROVED", "asha")

    with pytest.raises(ConcurrentUpdateError):
        with service.store.transaction() as tx:
            tx.update_request(request_id, stale["row_version"], status="REJECTED")
    assert service.get(request_id)["status"] == "APPROVED"
