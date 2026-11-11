"""Unit tests for the rule engine (pure functions, no database)."""
import pytest

from app.rule_engine import evaluate_condition, evaluate_stage, resolve, MISSING

CONTEXT = {
    "input": {"amount": 500, "name": "Asha", "tags": ["a", "b"], "nested": {"deep": 7}, "empty": "", "none": None},
    "deps": {"bureau": {"score": 710}},
}


@pytest.mark.parametrize("op, value, expected", [
    ("==", 500, True), ("!=", 500, False), (">", 499, True), (">=", 500, True),
    ("<", 500, False), ("<=", 500, True), ("in", [100, 500], True), ("not_in", [100, 500], False),
])
def test_operators(op, value, expected):
    ok, checks = evaluate_condition({"field": "input.amount", "op": op, "value": value}, CONTEXT)
    assert ok is expected
    assert checks[0]["actual"] == 500 and checks[0]["expected"] == value


def test_dotted_paths_reach_nested_and_dependency_data():
    assert resolve(CONTEXT, "input.nested.deep") == 7
    assert resolve(CONTEXT, "deps.bureau.score") == 710
    assert resolve(CONTEXT, "deps.other.score") is MISSING
    assert resolve(CONTEXT, "input.amount.too.deep") is MISSING


@pytest.mark.parametrize("field", ["input.absent", "input.empty", "input.none"])
def test_missing_empty_and_null_all_count_as_not_present(field):
    assert evaluate_condition({"field": field, "op": "exists"}, CONTEXT)[0] is False
    assert evaluate_condition({"field": field, "op": "not_exists"}, CONTEXT)[0] is True


def test_comparison_against_missing_field_is_indeterminate_not_an_error():
    ok, checks = evaluate_condition({"field": "input.absent", "op": ">=", "value": 1}, CONTEXT)
    assert ok is None and checks[0]["note"] == "field is missing"


def test_type_mismatch_is_indeterminate_not_an_error():
    ok, checks = evaluate_condition({"field": "input.name", "op": ">=", "value": 10}, CONTEXT)
    assert ok is None and "cannot compare str with int" in checks[0]["note"]


def test_threshold_over_missing_or_mistyped_data_fails_closed():
    for field in ("input.absent", "input.name"):
        result = evaluate_stage(stage([
            {"id": "R1", "type": "threshold", "field": field, "op": ">=", "value": 1},
        ]), CONTEXT)
        assert result.route == "REJECTED" and result.rules[0]["status"] == "FAIL"


def test_all_any_not_combinators():
    big = {"field": "input.amount", "op": ">", "value": 100}
    huge = {"field": "input.amount", "op": ">", "value": 10000}
    assert evaluate_condition({"all": [big, huge]}, CONTEXT)[0] is False
    assert evaluate_condition({"any": [big, huge]}, CONTEXT)[0] is True
    assert evaluate_condition({"not": huge}, CONTEXT)[0] is True
    assert evaluate_condition({"all": [big, {"any": [huge, {"not": huge}]}]}, CONTEXT)[0] is True
    # every leaf comparison is recorded for the audit trail
    assert len(evaluate_condition({"all": [big, huge]}, CONTEXT)[1]) == 2


def stage(rules, **overrides):
    return {"id": "s1", "rules": rules, "on_pass": "next_stage", "on_fail": "REJECTED", **overrides}


def test_stage_with_no_rules_passes():
    result = evaluate_stage(stage([]), CONTEXT)
    assert result.route == "next_stage" and result.decisive_rule is None


def test_stage_routes_to_on_fail_and_reports_the_decisive_rule():
    result = evaluate_stage(stage([
        {"id": "R1", "type": "threshold", "field": "input.amount", "op": ">=", "value": 1000},
    ]), CONTEXT)
    assert result.route == "REJECTED"
    assert result.decisive_rule["rule_id"] == "R1"
    assert "input.amount = 500" in result.reason


def test_conditional_rule_only_routes_when_triggered():
    rule = {"id": "R1", "type": "conditional", "then": "MANUAL_REVIEW",
            "if": {"field": "input.amount", "op": ">", "value": 100}}
    quiet = {**rule, "if": {"field": "input.amount", "op": ">", "value": 9999}}

    assert evaluate_stage(stage([rule]), CONTEXT).route == "MANUAL_REVIEW"
    assert evaluate_stage(stage([quiet]), CONTEXT).route == "next_stage"
    assert evaluate_stage(stage([quiet]), CONTEXT).rules[0]["status"] == "NOT_TRIGGERED"


def test_rule_can_route_to_another_stage():
    rule = {"id": "R1", "type": "conditional", "then": "extra_checks",
            "if": {"field": "deps.bureau.score", "op": "<", "value": 750}}
    assert evaluate_stage(stage([rule]), CONTEXT).route == "extra_checks"
