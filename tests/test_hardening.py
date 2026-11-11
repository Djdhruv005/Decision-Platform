"""Edge cases found by an adversarial review: each test reproduces one way the
system could be broken, and proves it no longer can."""
import json
import sqlite3

import pytest

from app.dependencies import MockCreditBureau, call_with_retry
from app.errors import IdempotencyConflict, InvalidStateError, ValidationFailed
from app.rule_engine import evaluate_condition
from app.service import DecisionService


# ---- retries are bounded for every retryable state ------------------------

def always_explode(stage, context):
    raise RuntimeError("persistent engine bug")


def test_failed_requests_cannot_be_retried_forever(service, loan, monkeypatch):
    monkeypatch.setattr("app.workflow_engine.evaluate_stage", always_explode)
    request_id = service.submit("loan_approval", loan).request["request_id"]
    assert service.get(request_id)["status"] == "FAILED"

    statuses = [service.retry(request_id)["status"] for _ in range(3)]  # max_resumes = 3

    assert statuses == ["FAILED", "FAILED", "MANUAL_REVIEW"]
    assert service.get(request_id)["resume_count"] == 3
    assert "Retry budget" in service.get(request_id)["reason"]
    with pytest.raises(InvalidStateError):
        service.retry(request_id)


def test_permanent_error_routed_to_failed_is_also_bounded(platform, service, loan):
    def fail_instead_of_retry(config):
        config["stages"][1]["dependency"]["on_failure"] = "FAILED"
    platform.edit_config("loan_approval.json", fail_instead_of_retry)
    service.reload_workflows()
    platform.bureau.faults.fail_next = 999
    platform.bureau.faults.mode = "permanent"

    request_id = service.submit("loan_approval", loan).request["request_id"]
    statuses = [service.retry(request_id)["status"] for _ in range(3)]

    assert statuses == ["FAILED", "FAILED", "MANUAL_REVIEW"]


def test_retry_itself_refuses_when_the_budget_is_already_spent(platform, service, loan, monkeypatch):
    """Safety net: even if a request is somehow parked with no budget left."""
    monkeypatch.setattr("app.workflow_engine.evaluate_stage", always_explode)
    request_id = service.submit("loan_approval", loan).request["request_id"]
    request = service.store.get_request(request_id)
    with service.store.transaction() as tx:
        tx.update_request(request_id, request["row_version"], resume_count=3)

    result = service.retry(request_id)

    assert result["status"] == "MANUAL_REVIEW"
    assert len(platform.events(request_id, "RETRY_BUDGET_SPENT")) == 1


def test_backoff_wait_has_a_hard_ceiling():
    bureau = MockCreditBureau()
    bureau.faults.fail_next = 99
    sleeps = []
    call_with_retry(bureau, "r1", {}, {"max_attempts": 4, "backoff_ms": 60000, "backoff_multiplier": 10},
                    sleep=sleeps.append)
    assert sleeps == [10.0, 10.0, 10.0]


# ---- non-finite numbers ---------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_refused_at_intake(service, loan, bad):
    """Python's JSON parser accepts NaN/Infinity. Infinity income used to be
    approved, and NaN made the audit endpoint unreadable."""
    with pytest.raises(ValidationFailed) as exc:
        service.submit("loan_approval", {**loan, "annual_income": bad})

    assert exc.value.errors[0]["field"] == "annual_income"
    assert exc.value.errors[0]["constraint"] == "finite"
    assert service.store.count_requests() == 0


def test_non_finite_values_are_found_when_nested(service, loan):
    with pytest.raises(ValidationFailed) as exc:
        service.submit("loan_approval", {**loan, "extra": {"scores": [1, float("nan")]}})
    assert exc.value.errors[0]["field"] == "extra.scores.1"


# ---- two workers on one request -------------------------------------------

def test_submit_that_loses_a_race_returns_the_decision_instead_of_an_error(platform, loan):
    """While the submit waits in backoff, another worker finishes the request.
    The submit must return the real outcome, not a concurrency error."""
    holder = {}

    def another_worker_finishes_it(_seconds):
        if "done" not in holder:
            holder["done"] = True
            request = holder["service"].store.find_by_idempotency_key("loan_approval", "key-1")
            holder["other"] = holder["service"].retry(request["id"], actor="other-worker")

    service = DecisionService(platform.config_dir, platform.db_path,
                              dependencies=platform.service.dependencies,
                              sleep=another_worker_finishes_it, stale_after_seconds=0)
    holder["service"] = service
    platform.bureau.faults.fail_next = 1

    result = service.submit("loan_approval", loan, "key-1")

    assert holder["other"]["status"] == "APPROVED"
    assert result.request["status"] == "APPROVED"
    request_id = result.request["request_id"]
    stages = [e["stage"] for e in service.store.get_audit(request_id) if e["event_type"] == "STAGE_EVALUATED"]
    assert stages == ["eligibility", "credit_check", "risk_assessment"]  # nothing recorded twice
    assert [e["detail"]["actor"] for e in service.store.get_audit(request_id)
            if e["event_type"] == "RECOVERED"] == ["other-worker"]


def crash_on_second_stage(service):
    def hook(operation):
        if operation == "update_request":
            hook.n += 1
            if hook.n == 2:
                raise KeyboardInterrupt("process killed")
    hook.n = 0
    original_run = service.engine.run

    def run(request_id):
        service.store.fault_hook = hook
        return original_run(request_id)
    service.engine.run = run


def test_retry_is_refused_while_a_request_is_being_processed(platform, service, loan):
    crash_on_second_stage(service)
    with pytest.raises(KeyboardInterrupt):
        service.submit("loan_approval", loan, "key-1")
    cautious = platform.new_service(stale_after_seconds=30)
    request = cautious.store.find_by_idempotency_key("loan_approval", "key-1")

    with pytest.raises(InvalidStateError) as exc:
        cautious.retry(request["id"])

    assert "still being processed" in str(exc.value)
    assert cautious.get(request["id"])["status"] == "IN_PROGRESS"


def test_taking_over_an_abandoned_request_is_audited(platform, service, loan):
    crash_on_second_stage(service)
    with pytest.raises(KeyboardInterrupt):
        service.submit("loan_approval", loan, "key-1")
    restarted = platform.new_service()
    request = restarted.store.find_by_idempotency_key("loan_approval", "key-1")

    assert restarted.retry(request["id"], actor="ops")["status"] == "APPROVED"

    recovered = [e for e in restarted.store.get_audit(request["id"]) if e["event_type"] == "RECOVERED"]
    assert recovered[0]["detail"]["actor"] == "ops"
    assert recovered[0]["detail"]["status"] == "IN_PROGRESS"


def test_startup_recovery_while_the_dependency_is_down_parks_the_request(platform, service, loan):
    crash_on_second_stage(service)
    with pytest.raises(KeyboardInterrupt):
        service.submit("loan_approval", loan, "key-1")
    platform.bureau.faults.fail_next = 99

    restarted = platform.new_service()
    recovered = restarted.recover_interrupted()

    assert len(recovered) == 1
    assert restarted.get(recovered[0])["status"] == "RETRY_PENDING"


def test_one_unrecoverable_request_does_not_block_startup(platform, service, loan, monkeypatch):
    crash_on_second_stage(service)
    with pytest.raises(KeyboardInterrupt):
        service.submit("loan_approval", loan, "key-1")
    restarted = platform.new_service()

    def broken(request_id):
        raise OSError("database unavailable")
    monkeypatch.setattr(restarted.engine, "run", broken)

    assert restarted.recover_interrupted() == []  # logged and skipped, no exception


# ---- idempotency edge cases -----------------------------------------------

def test_same_amount_as_int_or_float_is_the_same_request(service, loan):
    first = service.submit("loan_approval", {**loan, "amount": 500000}, "key-1")
    second = service.submit("loan_approval", {**loan, "amount": 500000.0}, "key-1")

    assert second.created is False
    assert second.request["request_id"] == first.request["request_id"]


def test_idempotency_conflict_is_recorded_on_the_original_request(platform, service, loan):
    request_id = service.submit("loan_approval", loan, "key-1").request["request_id"]

    with pytest.raises(IdempotencyConflict):
        service.submit("loan_approval", {**loan, "amount": 999999}, "key-1")

    conflicts = platform.events(request_id, "IDEMPOTENCY_CONFLICT")
    assert len(conflicts) == 1
    assert conflicts[0]["detail"]["original_payload_hash"] != conflicts[0]["detail"]["conflicting_payload_hash"]


def test_replay_with_a_client_key_works_after_the_workflow_is_withdrawn(platform, service, loan):
    first = service.submit("loan_approval", loan, "key-1")
    (platform.config_dir / "loan_approval.json").unlink()
    service.reload_workflows()

    replay = service.submit("loan_approval", loan, "key-1")

    assert replay.created is False
    assert replay.request["request_id"] == first.request["request_id"]


def test_keyless_dedupe_does_not_outlive_a_rule_change(platform, service, loan):
    """Someone rejected under the old income rule re-applies after the rule is
    relaxed. Without a client key they must be judged again, not handed the
    old rejection."""
    applicant = {**loan, "annual_income": 250000}
    before = service.submit("loan_approval", applicant)
    assert before.request["status"] == "REJECTED"
    assert service.submit("loan_approval", applicant).created is False  # same rules: deduplicated

    def relax(config):
        config["version"] = 2
        config["stages"][0]["rules"][2]["value"] = 200000
    platform.edit_config("loan_approval.json", relax)
    service.reload_workflows()
    after = service.submit("loan_approval", applicant)

    assert after.created is True
    assert after.request["status"] == "APPROVED"
    assert after.request["request_id"] != before.request["request_id"]


# ---- config mistakes cannot silently change decisions ----------------------

def test_misspelled_config_key_is_refused_instead_of_changing_a_decision(platform, service, vendor):
    """Renaming a rule's on_fail to onfail used to load cleanly and turn a
    MANUAL_REVIEW into a REJECTED."""
    def typo(config):
        rule = config["stages"][0]["rules"][1]
        rule["onfail"] = rule.pop("on_fail")
    platform.edit_config("vendor_approval.json", typo)

    report = service.reload_workflows()
    result = service.submit("vendor_approval", {**vendor, "years_in_business": 1})

    assert "unknown key 'onfail' (did you mean 'on_fail'?)" in report["errors"]["vendor_approval.json"][0]
    assert result.request["status"] == "MANUAL_REVIEW"  # last good config still in force


# ---- audit trail is complete and cannot be rewritten ------------------------

@pytest.mark.parametrize("statement", [
    "UPDATE audit_log SET detail = '{}'",
    "DELETE FROM audit_log",
    "UPDATE state_history SET reason = 'edited'",
    "DELETE FROM state_history",
    "UPDATE workflow_versions SET body = '{}'",
    "DELETE FROM workflow_versions",
])
def test_audit_tables_reject_updates_and_deletes(platform, service, loan, statement):
    service.submit("loan_approval", loan)

    connection = sqlite3.connect(platform.db_path)
    with pytest.raises(sqlite3.DatabaseError) as exc:
        connection.execute(statement)
    connection.close()

    assert "append-only" in str(exc.value) or "immutable" in str(exc.value)


def test_refused_input_is_kept_in_the_intake_log(service, loan):
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", {**loan, "amount": "lots"}, "key-1")
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", {**loan, "annual_income": float("nan")})

    rejections = service.list_intake_rejections("loan_approval")

    assert len(rejections) == 2
    assert rejections[1]["idempotency_key"] == "key-1"
    assert rejections[1]["errors"][0]["field"] == "amount"
    assert rejections[0]["errors"][0]["constraint"] == "finite"
    json.dumps(rejections)  # readable even though one payload held NaN


def test_review_without_the_stage_rules_is_flagged(platform, service, vendor):
    """A registry outage sends the vendor to a human. If the human approves,
    the trail must say the blacklist check never ran."""
    platform.vendors.faults.fail_next = 99
    request_id = service.submit("vendor_approval", {**vendor, "vendor_id": "V-BAD"}).request["request_id"]

    result = service.review(request_id, "APPROVED", "asha.k")

    event = platform.events(request_id, "MANUAL_REVIEW_RESOLVED")[0]["detail"]
    assert event["rules_not_evaluated"] == {"stage": "compliance", "dependency": "vendor_registry",
                                            "cause": "vendor_registry was unavailable"}
    assert "never evaluated" in result["reason"]
    assert "vendor_registry was unavailable" in service.explanation(request_id)["warnings"][0]


def test_ordinary_review_carries_no_such_warning(service, loan):
    request_id = service.submit("loan_approval", {**loan, "applicant_id": "A-BORDER"}).request["request_id"]
    service.review(request_id, "APPROVED", "asha.k")
    assert service.explanation(request_id)["warnings"] == []


# ---- rule engine ----------------------------------------------------------

@pytest.mark.parametrize("actual, expected, result", [
    (True, 1, False), (1, True, False), (False, 0, False), (True, True, True), (1, 1.0, True),
])
def test_booleans_never_equal_numbers(actual, expected, result):
    context = {"input": {"flag": actual}}
    assert evaluate_condition({"field": "input.flag", "op": "==", "value": expected}, context)[0] is result
    assert evaluate_condition({"field": "input.flag", "op": "!=", "value": expected}, context)[0] is not result


def test_field_can_be_compared_with_another_field():
    context = {"input": {"claim": 900, "cover": 1000}}
    condition = {"field": "input.claim", "op": "<=", "value_ref": "input.cover"}

    ok, checks = evaluate_condition(condition, context)
    assert ok is True
    assert checks[0]["expected"] == 1000 and checks[0]["expected_from"] == "input.cover"

    ok, checks = evaluate_condition({**condition, "value_ref": "input.absent"}, context)
    assert ok is None and "reference field input.absent is missing" in checks[0]["note"]


# ===========================================================================
# Second review round
# ===========================================================================

# ---- dependency responses are untrusted input ------------------------------

@pytest.mark.parametrize("bad_score", [float("inf"), float("nan")])
def test_non_finite_value_from_a_dependency_is_never_approved(platform, service, loan, bad_score):
    """An infinite credit score used to pass 'score >= 650' and get APPROVED."""
    platform.bureau.scores["A-GOOD"] = bad_score

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "MANUAL_REVIEW"
    assert "unusable value" in result.request["reason"]
    assert platform.bureau.calls == 1                      # a bad answer is not retried
    json.dumps(service.audit(result.request["request_id"]), allow_nan=False)  # audit stays readable


def test_dependency_returning_the_wrong_shape_is_a_dependency_failure(platform, service, loan):
    platform.bureau.respond = lambda request_id, params: "OK"

    result = service.submit("loan_approval", loan)

    assert result.request["status"] == "MANUAL_REVIEW"
    assert "returned str, expected an object" in result.request["reason"]


# ---- missing data fails closed --------------------------------------------

MISSING_CONTEXT = {"input": {"amount": 500}, "deps": {"registry": {}}}


def test_not_over_missing_data_is_indeterminate_not_true():
    blacklisted = {"field": "deps.registry.blacklistd", "op": "==", "value": True}  # typo in path
    assert evaluate_condition({"not": blacklisted}, MISSING_CONTEXT)[0] is None
    assert evaluate_condition({"all": [{"not": blacklisted}]}, MISSING_CONTEXT)[0] is None
    present = {"field": "input.amount", "op": ">", "value": 1}
    assert evaluate_condition({"any": [blacklisted, present]}, MISSING_CONTEXT)[0] is True
    assert evaluate_condition({"all": [blacklisted, {"not": present}]}, MISSING_CONTEXT)[0] is False


def conditional_stage(**rule_extra):
    from app.rule_engine import evaluate_stage
    rule = {"id": "GUARD", "type": "conditional", "then": "REJECTED",
            "if": {"field": "deps.registry.blacklistd", "op": "==", "value": True}, **rule_extra}
    stage = {"id": "s", "rules": [rule], "on_pass": "APPROVED", "on_fail": "REJECTED"}
    return evaluate_stage(stage, MISSING_CONTEXT)


def test_conditional_guard_over_missing_data_goes_to_a_human_by_default():
    result = conditional_stage()
    assert result.route == "MANUAL_REVIEW"
    assert result.rules[0]["status"] == "INDETERMINATE"
    assert "could not be evaluated" in result.reason


def test_what_a_conditional_does_on_missing_data_is_set_in_config():
    assert conditional_stage(on_missing="REJECTED").route == "REJECTED"
    assert conditional_stage(on_missing="IGNORE").route == "APPROVED"


def test_typo_in_a_guard_path_cannot_approve_a_blacklisted_vendor(platform, service, vendor):
    def typo(config):
        config["stages"][1]["rules"] = [{
            "id": "COMP-BLACKLIST", "type": "conditional", "then": "REJECTED",
            "if": {"field": "deps.vendor_registry.blacklistd", "op": "==", "value": True},
        }]
    platform.edit_config("vendor_approval.json", typo)
    assert service.reload_workflows()["errors"] == {}  # the typo is inside the response, invisible at load

    result = service.submit("vendor_approval", {**vendor, "vendor_id": "V-BAD"})

    assert result.request["status"] == "MANUAL_REVIEW"
    assert "could not be evaluated" in result.request["reason"]


@pytest.mark.parametrize("condition, expected", [
    ({"field": "input.flag", "op": "in", "value": [1]}, False),          # True is not 1
    ({"field": "input.flag", "op": "not_in", "value": [1]}, True),
    ({"field": "input.flag", "op": ">=", "value": 1}, None),             # booleans have no order
    ({"field": "input.word", "op": "in", "value_ref": "input.text"}, None),  # no substring matching
])
def test_operators_do_not_coerce_types(condition, expected):
    context = {"input": {"flag": True, "word": "ab", "text": "xabx"}}
    assert evaluate_condition(condition, context)[0] is expected


# ---- manual review is a stage, not the end of the workflow -------------------

def test_reviewer_approval_does_not_skip_later_hard_limits(service, loan):
    """Borderline score AND a loan above the 50,00,000 cap. The reviewer clears
    the credit stage; the cap in the next stage must still reject."""
    borderline = {**loan, "applicant_id": "A-BORDER", "amount": 6000000, "annual_income": 5000000}
    request_id = service.submit("loan_approval", borderline).request["request_id"]
    assert service.get(request_id)["current_stage"] == "credit_check"

    result = service.review(request_id, "APPROVED", "asha.k")

    assert result["status"] == "REJECTED"
    assert "RISK-CAP" in result["reason"]
    explanation = service.explanation(request_id)
    assert explanation["decided_by"] == "engine"
    assert explanation["decisive_rule"]["rule_id"] == "RISK-CAP"


def test_request_can_need_review_at_more_than_one_stage(platform, service, loan):
    twice = {**loan, "applicant_id": "A-BORDER", "amount": 2000000}
    request_id = service.submit("loan_approval", twice).request["request_id"]

    first = service.review(request_id, "APPROVED", "asha.k")
    assert (first["status"], first["current_stage"]) == ("MANUAL_REVIEW", "risk_assessment")
    assert "RISK-HIGH-VALUE" in first["reason"]

    second = service.review(request_id, "APPROVED", "ravi.m")
    assert second["status"] == "APPROVED" and second["is_final"] is True
    reviewers = [e["detail"]["reviewer"] for e in platform.events(request_id, "MANUAL_REVIEW_RESOLVED")]
    assert reviewers == ["asha.k", "ravi.m"]
    assert service.explanation(request_id)["decided_by"] == "ravi.m"


def test_human_step_in_the_middle_of_a_workflow(platform, service):
    """Onboarding: automated checks, then a manager sign-off, then IT setup rules."""
    (platform.config_dir / "onboarding.json").write_text(json.dumps({
        "workflow": "onboarding", "version": 1,
        "input_schema": {"type": "object", "required": ["employee_id", "laptop"]},
        "stages": [
            {"id": "manager_signoff", "on_pass": "it_setup", "rules": [
                {"id": "SIGNOFF", "type": "conditional", "then": "MANUAL_REVIEW",
                 "description": "Every hire needs a manager sign-off",
                 "if": {"field": "input.employee_id", "op": "exists"}},
            ]},
            {"id": "it_setup", "on_pass": "APPROVED", "rules": [
                {"id": "LAPTOP", "type": "threshold", "field": "input.laptop",
                 "op": "in", "value": ["mac", "linux"]},
            ]},
        ],
    }))
    service.reload_workflows()

    ok = service.submit("onboarding", {"employee_id": "E1", "laptop": "mac"}).request
    bad = service.submit("onboarding", {"employee_id": "E2", "laptop": "abacus"}).request
    assert ok["status"] == bad["status"] == "MANUAL_REVIEW"

    assert service.review(ok["request_id"], "APPROVED", "manager")["status"] == "APPROVED"
    assert service.review(bad["request_id"], "APPROVED", "manager")["status"] == "REJECTED"


def test_approval_that_overrides_another_objecting_rule_says_so(platform, service, vendor):
    """A review rule listed before a reject rule wins the routing. If the
    reviewer approves, the masked rejection must be visible."""
    def review_rule_first(config):
        config["stages"][0]["rules"].reverse()   # DOC-TRACK-RECORD (review) now before DOC-GST (reject)
    platform.edit_config("vendor_approval.json", review_rule_first)
    service.reload_workflows()
    payload = {k: v for k, v in vendor.items() if k != "gst_number"}
    request_id = service.submit("vendor_approval", {**payload, "years_in_business": 1}).request["request_id"]

    service.review(request_id, "APPROVED", "asha.k")

    event = platform.events(request_id, "MANUAL_REVIEW_RESOLVED")[0]["detail"]
    assert event["overridden_rules"] == [{"rule_id": "DOC-GST", "status": "FAIL", "route": "REJECTED"}]
    assert "also overrides rule DOC-GST" in service.explanation(request_id)["warnings"][0]


# ---- every external call is on the audit trail ------------------------------

def test_calls_made_by_a_worker_that_lost_the_race_are_still_audited(platform, loan):
    holder = {}

    def another_worker_finishes_it(_seconds):
        if "done" not in holder:
            holder["done"] = True
            request = holder["service"].store.find_by_idempotency_key("loan_approval", "key-1")
            holder["service"].retry(request["id"], actor="other-worker")

    service = DecisionService(platform.config_dir, platform.db_path,
                              dependencies=platform.service.dependencies,
                              sleep=another_worker_finishes_it, stale_after_seconds=0)
    holder["service"] = service
    platform.bureau.faults.fail_next = 1

    request_id = service.submit("loan_approval", loan, "key-1").request["request_id"]

    calls = [e for e in service.store.get_audit(request_id) if e["event_type"] == "DEPENDENCY_CALL"]
    assert platform.bureau.calls == 3
    assert len(calls) == 3      # one row per real call, including the discarded worker's


def test_dependency_attempts_count_as_activity_on_the_request(platform, service, loan):
    """A request waiting on a slow dependency must not look abandoned."""
    seen = {}

    def during_backoff(_seconds):
        request = service.store.find_by_idempotency_key("loan_approval", "key-1")
        seen["last_activity"] = service.store.last_activity_at(request["id"])
        seen["updated_at"] = request["updated_at"]
    service.engine.sleep = during_backoff
    platform.bureau.faults.fail_next = 1

    service.submit("loan_approval", loan, "key-1")

    assert seen["last_activity"] >= seen["updated_at"]
    request = service.store.find_by_idempotency_key("loan_approval", "key-1")
    first_attempt = platform.events(request["id"], "DEPENDENCY_CALL")[0]
    assert seen["last_activity"] == first_attempt["created_at"]


# ---- dependency adapters are reusable across workflows -----------------------

def test_two_workflows_with_different_field_names_share_one_dependency(platform, service):
    """The bureau adapter asks for 'applicant_id'. A second workflow calls its
    field 'customer_id' and maps it in config, with no new code."""
    (platform.config_dir / "card_limit.json").write_text(json.dumps({
        "workflow": "card_limit", "version": 1,
        "input_schema": {"type": "object", "required": ["customer_id"]},
        "stages": [{
            "id": "score",
            "dependency": {"name": "credit_bureau", "input": {"applicant_id": "input.customer_id"}},
            "rules": [{"id": "MIN", "type": "threshold", "field": "deps.credit_bureau.score",
                       "op": ">=", "value": 700}],
            "on_pass": "APPROVED",
        }],
    }))
    service.reload_workflows()

    assert service.submit("card_limit", {"customer_id": "A-GOOD"}).request["status"] == "APPROVED"
    assert service.submit("card_limit", {"customer_id": "A-LOW"}).request["status"] == "REJECTED"


# ---- derived values --------------------------------------------------------

def test_rule_can_compare_a_field_with_a_multiple_of_another_field(service, loan):
    """RISK-INCOME-MULTIPLE: amount must not exceed 10 x annual income."""
    result = service.submit("loan_approval", {**loan, "annual_income": 350000, "amount": 4000000})

    assert result.request["status"] == "REJECTED"
    assert "RISK-INCOME-MULTIPLE" in result.request["reason"]
    assert "10 x input.annual_income = 3500000" in result.request["reason"]


# ---- startup ---------------------------------------------------------------

def test_broken_workflow_file_does_not_take_the_other_workflows_down(platform, loan):
    (platform.config_dir / "vendor_approval.json").write_text("{ broken")

    service = platform.new_service()   # a fresh start, as after a deploy

    assert service.submit("loan_approval", loan).request["status"] == "APPROVED"
    assert "vendor_approval.json" in service.registry.errors
