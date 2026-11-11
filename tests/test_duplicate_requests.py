"""Scenario 3: duplicate requests and idempotency."""
import threading

import pytest

from app.errors import IdempotencyConflict


def test_same_key_same_payload_returns_the_original_result(platform, service, loan):
    first = service.submit("loan_approval", loan, "key-1")
    second = service.submit("loan_approval", loan, "key-1")

    assert first.created is True and second.created is False
    assert second.request["request_id"] == first.request["request_id"]
    assert second.request["status"] == first.request["status"] == "APPROVED"
    assert service.store.count_requests() == 1


def test_duplicate_does_not_reprocess_or_call_the_dependency_again(platform, service, loan):
    request_id = service.submit("loan_approval", loan, "key-1").request["request_id"]
    calls, evaluations = platform.bureau.calls, len(platform.events(request_id, "STAGE_EVALUATED"))

    service.submit("loan_approval", loan, "key-1")

    assert platform.bureau.calls == calls
    assert len(platform.events(request_id, "STAGE_EVALUATED")) == evaluations
    assert len(platform.events(request_id, "DUPLICATE_REQUEST")) == 1  # but it is audited


def test_same_key_different_payload_is_a_conflict(service, loan):
    first = service.submit("loan_approval", loan, "key-1")

    with pytest.raises(IdempotencyConflict) as exc:
        service.submit("loan_approval", {**loan, "amount": 999999}, "key-1")

    assert exc.value.existing_request_id == first.request["request_id"]
    assert service.store.count_requests() == 1


def test_no_key_falls_back_to_payload_fingerprint(service, loan):
    first = service.submit("loan_approval", loan)
    again = service.submit("loan_approval", dict(reversed(list(loan.items()))))  # same data, new key order
    other = service.submit("loan_approval", {**loan, "amount": 400000})

    assert again.created is False
    assert again.request["request_id"] == first.request["request_id"]
    assert other.created is True


def test_keys_are_scoped_per_workflow(service, loan, vendor):
    a = service.submit("loan_approval", loan, "shared-key")
    b = service.submit("vendor_approval", vendor, "shared-key")

    assert a.created and b.created
    assert a.request["request_id"] != b.request["request_id"]


def test_replay_returns_original_answer_even_after_rules_change(platform, service, loan):
    first = service.submit("loan_approval", loan, "key-1")

    def raise_income_bar(config):
        config["version"] = 2
        config["stages"][0]["rules"][2]["value"] = 5000000
    platform.edit_config("loan_approval.json", raise_income_bar)
    service.reload_workflows()

    replay = service.submit("loan_approval", loan, "key-1")
    assert replay.created is False
    assert replay.request["status"] == "APPROVED"
    assert replay.request["workflow_version"] == first.request["workflow_version"] == 1


def test_concurrent_identical_submissions_create_exactly_one_request(platform, loan):
    # Real staleness window: a duplicate must not take over a request that
    # another thread started a moment ago.
    service = platform.new_service(stale_after_seconds=30)
    results, errors = [], []
    start = threading.Barrier(8)

    def submit():
        try:
            start.wait()
            results.append(service.submit("loan_approval", loan, "race-key"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert service.store.count_requests() == 1
    assert sum(1 for r in results if r.created) == 1
    assert len({r.request["request_id"] for r in results}) == 1
    assert platform.bureau.calls == 1
