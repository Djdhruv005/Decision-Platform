"""Rule engine: pure functions, no I/O, no knowledge of workflows or storage.

Given one stage's rules and a data context, it returns where the request
should go next *and* a full trace of every rule: what data it looked at,
what it expected, and whether it passed. That trace is the audit record.

Data context:
    {"input": <request payload>, "deps": {<dependency name>: <its response>}}
Rules reference data by dotted path, e.g. ``input.amount`` or
``deps.credit_bureau.score``, so every decision names its data source.

Rule types (add one by writing an evaluator here and a validator in
config_loader.RULE_VALIDATORS):

* mandatory    the field must be present and non-empty
* threshold    ``field <op> value`` must hold. ``value_ref`` compares with another
               field, and ``value_factor`` scales it (amount <= 10 x income)
* conditional  if the ``if`` condition holds, route to ``then``; otherwise no effect

Missing data (three-valued logic). A comparison whose data is missing, or
whose operands cannot be compared, is neither true nor false: it is
*indeterminate* (``None``). Indeterminate survives ``not``, and combines with
``all`` / ``any`` the way SQL NULL does. An indeterminate rule never lets a
request through quietly:

* a threshold rule that is indeterminate FAILS
* a conditional rule that is indeterminate routes to its ``on_missing`` target
  (default MANUAL_REVIEW). A config that really wants "skip when absent" says
  so with ``"on_missing": "IGNORE"`` or an explicit ``exists`` check.

Routing: every rule in the stage is evaluated and recorded (no short-circuit,
so the trace is complete). The first rule in config order that produces a
route decides the stage outcome; if none does, the stage's ``on_pass`` applies.
Config order is therefore rule priority: put hard rejections first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

MISSING = object()
IGNORE = "IGNORE"


class Incomparable(Exception):
    """The two values cannot be meaningfully compared with this operator."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _equal(actual: Any, expected: Any) -> bool:
    # In Python True == 1. In a decision rule a boolean must never satisfy a
    # numeric expectation (or the reverse), so the types have to agree too.
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def _ordered(compare: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    """Ordering is defined for number vs number and string vs string only."""
    def check(actual: Any, expected: Any) -> bool:
        if (_is_number(actual) and _is_number(expected)) or \
                (isinstance(actual, str) and isinstance(expected, str)):
            return compare(actual, expected)
        raise Incomparable(f"cannot compare {type(actual).__name__} with {type(expected).__name__}")
    return check


def _member(actual: Any, expected: Any) -> bool:
    # Membership means "one of a list". A string on the right would turn this
    # into a substring test, which is never what a rule author intends.
    if not isinstance(expected, list):
        raise Incomparable(f"'in' needs a list on the right, got {type(expected).__name__}")
    return any(_equal(actual, item) for item in expected)


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": _equal,
    "!=": lambda actual, expected: not _equal(actual, expected),
    ">": _ordered(lambda a, b: a > b),
    ">=": _ordered(lambda a, b: a >= b),
    "<": _ordered(lambda a, b: a < b),
    "<=": _ordered(lambda a, b: a <= b),
    "in": _member,
    "not_in": lambda actual, expected: not _member(actual, expected),
}
PRESENCE_OPERATORS = ("exists", "not_exists")


def resolve(context: dict, path: str) -> Any:
    """Look up a dotted path; returns MISSING rather than raising."""
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _is_present(value: Any) -> bool:
    """Present means a real value: not missing, null, blank text, or an empty list/object."""
    if value is MISSING or value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def evaluate_condition(condition: dict, context: dict) -> tuple[Optional[bool], list[dict]]:
    """Evaluate a (possibly nested) condition.

    Returns True, False or None (indeterminate), plus one record per leaf
    comparison so the audit shows the actual value each comparison saw.
    """
    if "all" in condition:
        results = [evaluate_condition(child, context) for child in condition["all"]]
        outcomes = [ok for ok, _ in results]
        verdict = False if False in outcomes else (None if None in outcomes else True)
        return verdict, [c for _, checks in results for c in checks]
    if "any" in condition:
        results = [evaluate_condition(child, context) for child in condition["any"]]
        outcomes = [ok for ok, _ in results]
        verdict = True if True in outcomes else (None if None in outcomes else False)
        return verdict, [c for _, checks in results for c in checks]
    if "not" in condition:
        ok, checks = evaluate_condition(condition["not"], context)
        return (None if ok is None else not ok), checks

    field, op = condition["field"], condition["op"]
    expected = condition.get("value")
    expected_from = condition.get("value_ref")   # compare against another field...
    factor = condition.get("value_factor")       # ...optionally scaled (10 x income)
    scale_error = None
    if expected_from:
        expected = resolve(context, expected_from)
        if factor is not None and _is_present(expected):
            if _is_number(expected):
                expected = expected * factor
            else:
                scale_error = f"cannot multiply {type(expected).__name__} {expected_from} by {factor}"
    actual = resolve(context, field)
    check = {
        "field": field,
        "operator": op,
        "expected": None if expected is MISSING else expected,
        "expected_from": expected_from,
        "expected_factor": factor,
        "actual": None if actual is MISSING else actual,
        "present": _is_present(actual),
        "note": None,
    }
    ok: Optional[bool]
    if op == "exists":
        ok = _is_present(actual)
    elif op == "not_exists":
        ok = not _is_present(actual)
    elif not _is_present(actual):
        # Recorded as indeterminate instead of raising or guessing, so the
        # decision stays explainable and never passes by accident.
        ok, check["note"] = None, "field is missing"
    elif expected_from and not _is_present(expected):
        ok, check["note"] = None, f"reference field {expected_from} is missing"
    elif scale_error:
        ok, check["note"] = None, scale_error
    else:
        try:
            ok = bool(OPERATORS[op](actual, expected))
        except Incomparable as exc:
            ok, check["note"] = None, str(exc)
    check["result"] = ok
    return ok, [check]


def _describe(check: dict) -> str:
    if check["operator"] in PRESENCE_OPERATORS:
        state = "present" if check["present"] else "missing"
        return f"{check['field']} is {state}"
    actual = "<missing>" if not check["present"] else repr(check["actual"])
    expected = repr(check["expected"])
    if check.get("expected_from"):
        source = check["expected_from"]
        if check.get("expected_factor") is not None:
            source = f"{check['expected_factor']:g} x {source}"
        expected = f"{source} = {expected}"
    text = f"{check['field']} = {actual} (condition: {check['operator']} {expected})"
    return f"{text} ({check['note']})" if check["note"] and check["present"] else text


def _result(rule: dict, status: str, checks: list[dict], route: Optional[str]) -> dict:
    return {
        "rule_id": rule["id"],
        "type": rule["type"],
        "description": rule.get("description", ""),
        "status": status,
        "checks": checks,
        "route": route,
        "message": "; ".join(_describe(c) for c in checks) + f" -> {status}",
    }


def _eval_mandatory(rule: dict, stage: dict, context: dict) -> dict:
    ok, checks = evaluate_condition({"field": rule["field"], "op": "exists"}, context)
    route = None if ok else rule.get("on_fail", stage["on_fail"])
    return _result(rule, "PASS" if ok else "FAIL", checks, route)


def _eval_threshold(rule: dict, stage: dict, context: dict) -> dict:
    ok, checks = evaluate_condition(rule, context)
    # Only a definite True passes. Indeterminate (missing data) fails.
    route = None if ok is True else rule.get("on_fail", stage["on_fail"])
    return _result(rule, "PASS" if ok is True else "FAIL", checks, route)


def _eval_conditional(rule: dict, stage: dict, context: dict) -> dict:
    triggered, checks = evaluate_condition(rule["if"], context)
    if triggered is True:
        return _result(rule, "TRIGGERED", checks, rule["then"])
    if triggered is False:
        return _result(rule, "NOT_TRIGGERED", checks, None)
    # The guard could not be evaluated. It must not silently switch itself off.
    on_missing = rule.get("on_missing", "MANUAL_REVIEW")
    return _result(rule, "INDETERMINATE", checks, None if on_missing == IGNORE else on_missing)


RULE_TYPES: dict[str, Callable[[dict, dict, dict], dict]] = {
    "mandatory": _eval_mandatory,
    "threshold": _eval_threshold,
    "conditional": _eval_conditional,
}

_VERBS = {"FAIL": "failed", "TRIGGERED": "triggered", "INDETERMINATE": "could not be evaluated"}


@dataclass
class StageResult:
    stage: str
    route: str                       # next stage id, or a decision state
    decisive_rule: Optional[dict]    # the rule that set the route (None = all passed)
    rules: list[dict]                # trace of every rule, in order
    reason: str

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "route": self.route,
            "decisive_rule": self.decisive_rule["rule_id"] if self.decisive_rule else None,
            "reason": self.reason,
            "rules": self.rules,
        }


def evaluate_stage(stage: dict, context: dict) -> StageResult:
    results = [RULE_TYPES[rule["type"]](rule, stage, context) for rule in stage["rules"]]
    decisive = next((r for r in results if r["route"]), None)
    if decisive is None:
        count = len(results)
        reason = f"All {count} rule(s) passed at stage '{stage['id']}'"
        return StageResult(stage["id"], stage["on_pass"], None, results, reason)

    label = decisive["rule_id"]
    if decisive["description"]:
        label += f" ({decisive['description']})"
    reason = f"Rule {label} {_VERBS[decisive['status']]} at stage '{stage['id']}': {decisive['message']}"
    return StageResult(stage["id"], decisive["route"], decisive, results, reason)
