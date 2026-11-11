"""Scenario 6: rules and workflows change through config alone, with no code change."""
import json

import pytest

from app.errors import ValidationFailed


def lower_income_bar(config):
    config["version"] = 2
    config["stages"][0]["rules"][2]["value"] = 200000


def test_changing_a_threshold_changes_the_decision(platform, service, loan):
    applicant = {**loan, "annual_income": 250000}
    before = service.submit("loan_approval", applicant, "before")
    assert before.request["status"] == "REJECTED"

    platform.edit_config("loan_approval.json", lower_income_bar)
    report = service.reload_workflows()
    after = service.submit("loan_approval", applicant, "after")

    assert report["errors"] == {}
    assert after.request["status"] == "APPROVED"
    assert (before.request["workflow_version"], after.request["workflow_version"]) == (1, 2)
    assert before.request["config_hash"] != after.request["config_hash"]


def test_old_decision_stays_explainable_against_the_old_rules(platform, service, loan):
    request_id = service.submit("loan_approval", {**loan, "annual_income": 250000}).request["request_id"]
    platform.edit_config("loan_approval.json", lower_income_bar)
    service.reload_workflows()

    explanation = service.explanation(request_id)

    assert explanation["workflow_version"] == 1
    assert explanation["decisive_rule"]["rule_id"] == "ELIG-INCOME"
    assert explanation["decisive_rule"]["data"][0]["expected"] == 300000  # the threshold at the time
    snapshot = service.store.get_workflow_version(explanation["config_hash"])
    assert snapshot["body"]["stages"][0]["rules"][2]["value"] == 300000


def test_adding_a_rule_through_config(platform, service, loan):
    def add_rule(config):
        config["version"] = 2
        config["stages"][0]["rules"].append({
            "id": "ELIG-MAX-AGE", "type": "threshold", "field": "input.age", "op": "<=", "value": 60,
            "description": "Applicant must be 60 or younger",
        })
    platform.edit_config("loan_approval.json", add_rule)
    service.reload_workflows()

    result = service.submit("loan_approval", {**loan, "age": 64})

    assert result.request["status"] == "REJECTED"
    assert "ELIG-MAX-AGE" in result.request["reason"]


def test_adding_a_stage_through_config(platform, service, loan):
    def add_stage(config):
        config["version"] = 2
        config["stages"][2]["on_pass"] = "final_checks"
        config["stages"].append({
            "id": "final_checks",
            "rules": [{
                "id": "FINAL-SELF-EMPLOYED", "type": "conditional",
                "if": {"field": "input.employment_type", "op": "==", "value": "self_employed"},
                "then": "MANUAL_REVIEW",
            }],
            "on_pass": "APPROVED",
        })
    platform.edit_config("loan_approval.json", add_stage)
    service.reload_workflows()

    salaried = service.submit("loan_approval", loan)
    self_employed = service.submit("loan_approval", {**loan, "employment_type": "self_employed"})

    assert salaried.request["status"] == "APPROVED"
    assert salaried.request["current_stage"] == "final_checks"
    assert self_employed.request["status"] == "MANUAL_REVIEW"


def test_changing_the_input_schema_through_config(platform, service, loan):
    def require_employment_type(config):
        config["version"] = 2
        config["input_schema"]["required"].append("employment_type")
    platform.edit_config("loan_approval.json", require_employment_type)
    service.reload_workflows()

    payload = {k: v for k, v in loan.items() if k != "employment_type"}
    with pytest.raises(ValidationFailed):
        service.submit("loan_approval", payload)


def test_request_in_flight_finishes_under_the_rules_it_started_with(platform, service, loan):
    """A request parked under v1 must not be judged half by v1 and half by v2."""
    platform.bureau.faults.fail_next = 3
    request_id = service.submit("loan_approval", {**loan, "applicant_id": "A-BORDER"}).request["request_id"]
    assert service.get(request_id)["status"] == "RETRY_PENDING"

    def drop_borderline_review(config):
        config["version"] = 2
        del config["stages"][1]["rules"][1]
    platform.edit_config("loan_approval.json", drop_borderline_review)
    service.reload_workflows()

    resumed = service.retry(request_id)
    fresh = service.submit("loan_approval", {**loan, "applicant_id": "A-BORDER"}, "fresh")

    assert resumed["status"] == "MANUAL_REVIEW" and resumed["workflow_version"] == 1
    assert fresh.request["status"] == "APPROVED" and fresh.request["workflow_version"] == 2


def test_broken_config_edit_is_reported_and_old_version_keeps_serving(platform, service, loan):
    def break_it(config):
        config["stages"][0]["on_pass"] = "stage_that_does_not_exist"
    platform.edit_config("loan_approval.json", break_it)

    report = service.reload_workflows()

    assert "stage_that_does_not_exist" in report["errors"]["loan_approval.json"][0]
    assert service.submit("loan_approval", loan).request["status"] == "APPROVED"


def test_new_workflow_is_added_by_dropping_in_a_file(platform, service):
    (platform.config_dir / "document_verification.json").write_text(json.dumps({
        "workflow": "document_verification", "version": 1,
        "input_schema": {"type": "object", "required": ["doc_type"]},
        "stages": [{
            "id": "check",
            "rules": [{"id": "DOC-TYPE", "type": "threshold", "field": "input.doc_type",
                       "op": "in", "value": ["passport", "aadhaar"]}],
            "on_pass": "APPROVED",
        }],
    }))
    service.reload_workflows()

    assert service.submit("document_verification", {"doc_type": "passport"}).request["status"] == "APPROVED"
    assert service.submit("document_verification", {"doc_type": "selfie"}).request["status"] == "REJECTED"
