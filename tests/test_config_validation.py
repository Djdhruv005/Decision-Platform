"""Broken configs are refused at load time with a specific message."""
import copy
import json

import pytest

from app.config_loader import WorkflowConfig, WorkflowRegistry
from app.errors import ConfigError

VALID = {
    "workflow": "demo", "version": 1,
    "input_schema": {"type": "object"},
    "stages": [
        {"id": "first", "on_pass": "second", "rules": [
            {"id": "R1", "type": "mandatory", "field": "input.name"},
        ]},
        {"id": "second", "on_pass": "APPROVED", "rules": [
            {"id": "R2", "type": "threshold", "field": "input.amount", "op": ">", "value": 0},
            {"id": "R3", "type": "conditional", "then": "MANUAL_REVIEW",
             "if": {"field": "input.amount", "op": ">", "value": 100}},
        ]},
    ],
}


def broken(mutate) -> dict:
    config = copy.deepcopy(VALID)
    mutate(config)
    return config


def test_valid_config_loads_with_defaults_filled_in():
    config = WorkflowConfig.from_dict(VALID)
    assert config.first_stage == "first"
    assert config.stages["first"]["on_fail"] == "REJECTED"
    assert config.stages["first"]["dependency"] is None
    assert len(config.config_hash) == 64


def test_hash_changes_with_content_but_not_with_key_order():
    reordered = json.loads(json.dumps(VALID, sort_keys=True))
    changed = broken(lambda c: c["stages"][1]["rules"][0].update(value=5))
    assert WorkflowConfig.from_dict(reordered).config_hash == WorkflowConfig.from_dict(VALID).config_hash
    assert WorkflowConfig.from_dict(changed).config_hash != WorkflowConfig.from_dict(VALID).config_hash


@pytest.mark.parametrize("mutate, expected", [
    (lambda c: c.pop("workflow"), "'workflow' must be"),
    (lambda c: c.update(version="one"), "'version' must be an integer"),
    (lambda c: c.update(stages=[]), "'stages' must be a non-empty list"),
    (lambda c: c.update(input_schema={"type": "not-a-type"}), "not a valid JSON Schema"),
    (lambda c: c["stages"][0].update(on_pass="nowhere"), "'nowhere' is not a stage id"),
    (lambda c: c["stages"][0].pop("on_pass"), "'on_pass' is required"),
    (lambda c: c["stages"][1].update(id="first"), "duplicate stage id 'first'"),
    (lambda c: c["stages"][0].update(id="APPROVED"), "collides with a reserved state name"),
    (lambda c: c["stages"][1]["rules"][0].update(id="R1"), "duplicate rule id"),
    (lambda c: c["stages"][0]["rules"][0].update(type="magic"), "unknown type 'magic'"),
    (lambda c: c["stages"][1]["rules"][0].update(op="~="), "unknown operator '~='"),
    (lambda c: c["stages"][1]["rules"][0].pop("value"), "needs a 'value'"),
    (lambda c: c["stages"][1]["rules"][0].update(op="in", value=5), "needs a list 'value'"),
    (lambda c: c["stages"][0]["rules"][0].update(field="amount"), "must be a path starting with"),
    (lambda c: c["stages"][1]["rules"][1].pop("then"), "needs a 'then' target"),
    (lambda c: c["stages"][1]["rules"][1].update(then="RETRY_PENDING"), "'RETRY_PENDING' is not a stage id"),
    (lambda c: c["stages"][1].update(on_pass="first"), "stages form a cycle"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "retry": {"max_attempts": 0}}), "max_attempts must be"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "on_failure": "APPROVED"}), "on_failure must be one of"),
    # misspelled or unknown keys, at every level
    (lambda c: c.update(stage=[]), "workflow: unknown key 'stage' (did you mean 'stages'?)"),
    (lambda c: c["stages"][0].update(on_fial="REJECTED"), "unknown key 'on_fial' (did you mean 'on_fail'?)"),
    (lambda c: c["stages"][0]["rules"][0].update(onfail="REJECTED"), "unknown key 'onfail' (did you mean 'on_fail'?)"),
    (lambda c: c["stages"][1]["rules"][0].update(valeu=1), "unknown key 'valeu' (did you mean 'value'?)"),
    (lambda c: c["stages"][1]["rules"][1].update(on_fail="REJECTED"), "unknown key 'on_fail'"),
    (lambda c: c["stages"][1]["rules"][1]["if"].update(operator=">"), "unknown key 'operator'"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "on_failur": "FAILED"}), "did you mean 'on_failure'?"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "retry": {"max_attempt": 2}}), "did you mean 'max_attempts'?"),
    # limits and types
    (lambda c: c["stages"][0].update(dependency={"name": "x", "retry": {"max_attempts": 11}}), "max_attempts must be an integer from 1 to 10"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "retry": {"backoff_ms": 10**7}}), "backoff_ms must be a number from 0 to 60000"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "retry": {"backoff_multiplier": 50}}), "backoff_multiplier must be"),
    (lambda c: c.update(max_resumes=True), "'max_resumes' must be an integer"),
    (lambda c: c.update(max_resumes=500), "'max_resumes' must be an integer from 0 to 20"),
    (lambda c: c.update(version=True), "'version' must be an integer"),
    # structure
    (lambda c: c["stages"][0].update(on_pass="APPROVED"), "stage 'second' can never be reached"),
    (lambda c: c["stages"][1]["rules"][0].update(value_ref="input.limit"), "either 'value' or 'value_ref'"),
    (lambda c: c["stages"][1]["rules"][0].update(value_ref="limit") or c["stages"][1]["rules"][0].pop("value"),
     "'value_ref' must be a path starting with"),
    (lambda c: c["stages"][1]["rules"][0].update(value_factor=2), "'value_factor' only applies together with 'value_ref'"),
    (lambda c: c["stages"][1]["rules"][1].update(on_missing="nowhere"), "'nowhere' is not a stage id"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "input": {"id": "applicant_id"}}),
     "dependency input 'id': 'path' must be a path starting with"),
    (lambda c: c["stages"][0].update(dependency={"name": "x", "input": []}), "must map parameter names to data paths"),
])
def test_broken_config_is_refused_with_a_specific_message(mutate, expected):
    with pytest.raises(ConfigError) as exc:
        WorkflowConfig.from_dict(broken(mutate))
    assert expected in " | ".join(exc.value.problems)


def test_value_ref_is_accepted_in_place_of_value():
    config = broken(lambda c: (c["stages"][1]["rules"][0].pop("value"),
                               c["stages"][1]["rules"][0].update(value_ref="input.limit")))
    assert WorkflowConfig.from_dict(config)


def test_rule_type_without_a_validator_is_refused(monkeypatch):
    """Registering an evaluator alone is not enough: an unvalidated rule type
    must never load."""
    monkeypatch.setitem(__import__("app.rule_engine").rule_engine.RULE_TYPES, "between", lambda *a: {})
    config = broken(lambda c: c["stages"][0]["rules"][0].update(type="between"))
    with pytest.raises(ConfigError) as exc:
        WorkflowConfig.from_dict(config)
    assert "rule type 'between' has no validator registered" in exc.value.problems[0]


def test_all_problems_are_reported_together():
    config = broken(lambda c: (c["stages"][0].update(on_pass="nowhere"),
                               c["stages"][1]["rules"][0].update(op="~=")))
    with pytest.raises(ConfigError) as exc:
        WorkflowConfig.from_dict(config)
    assert len(exc.value.problems) == 2


def test_unknown_dependency_is_refused_when_the_registry_knows_the_list():
    config = broken(lambda c: c["stages"][0].update(dependency={"name": "ghost_service"}))
    with pytest.raises(ConfigError) as exc:
        WorkflowConfig.from_dict(config, known_dependencies=["credit_bureau"])
    assert "unknown dependency 'ghost_service'" in exc.value.problems[0]


def test_one_broken_file_does_not_stop_the_other_workflows_from_loading(tmp_path):
    (tmp_path / "bad.json").write_text("{ this is not json")
    (tmp_path / "good.json").write_text(json.dumps(VALID))

    registry = WorkflowRegistry(tmp_path)

    assert [c.name for c in registry.list()] == ["demo"]
    assert "not valid JSON" in registry.errors["bad.json"][0]


def test_second_file_defining_the_same_workflow_is_reported_and_ignored(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(VALID))
    (tmp_path / "b.json").write_text(json.dumps(VALID))

    registry = WorkflowRegistry(tmp_path)

    assert len(registry.list()) == 1
    assert "already defined in a.json" in registry.errors["b.json"][0]


def test_config_is_read_as_utf8_on_every_platform(tmp_path):
    config = broken(lambda c: c.update(description="Loans up to ₹50,00,000 (Łódź branch)"))
    (tmp_path / "demo.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    registry = WorkflowRegistry(tmp_path)

    assert registry.errors == {}
    assert registry.get("demo").description == "Loans up to ₹50,00,000 (Łódź branch)"
    assert registry.get("demo").config_hash == WorkflowConfig.from_dict(config).config_hash


def test_file_that_is_not_utf8_is_reported_instead_of_crashing(tmp_path):
    (tmp_path / "demo.json").write_bytes(b'{"workflow": "caf\xe9"}')

    registry = WorkflowRegistry(tmp_path)

    assert registry.list() == []
    assert "could not be read as UTF-8" in registry.errors["demo.json"][0]


DEP_STAGE = {"id": "lookup", "dependency": {"name": "bureau"}, "on_pass": "APPROVED", "rules": []}


def test_rule_reading_a_dependency_that_is_never_fetched_is_refused():
    config = broken(lambda c: c["stages"][1]["rules"][0].update(field="deps.bureau.score"))
    with pytest.raises(ConfigError) as exc:
        WorkflowConfig.from_dict(config)
    assert "reads dependency 'bureau', which is not fetched on every route" in exc.value.problems[0]


def test_dependency_fetched_on_only_one_route_is_refused():
    """first -> lookup -> second, but first can also jump straight to second."""
    def build(c):
        c["stages"][0]["on_pass"] = "lookup"
        c["stages"][0]["rules"].append({"id": "SKIP", "type": "conditional", "then": "second",
                                        "if": {"field": "input.fast_track", "op": "==", "value": True}})
        c["stages"].insert(1, {**DEP_STAGE, "on_pass": "second"})
        c["stages"][2]["rules"][0].update(field="deps.bureau.score")
    with pytest.raises(ConfigError) as exc:
        WorkflowConfig.from_dict(broken(build))
    assert "stage 'second' rule 'R2'" in exc.value.problems[0]


def test_dependency_fetched_on_every_route_is_accepted():
    def build(c):
        c["stages"][0]["on_pass"] = "lookup"
        c["stages"].insert(1, {**DEP_STAGE, "on_pass": "second"})
        c["stages"][2]["rules"][0].update(field="deps.bureau.score")
    assert WorkflowConfig.from_dict(broken(build))
