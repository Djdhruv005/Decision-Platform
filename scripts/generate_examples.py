"""Generate the decision explanation examples in ./examples from real runs.

    python scripts/generate_examples.py

Runs each scenario through the real service (in-memory database, mock
dependencies with fixed data) and writes what the system actually produced.
Nothing in the output is hand-written.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.errors import IdempotencyConflict, ValidationFailed  # noqa: E402
from app.service import DecisionService  # noqa: E402

OUT = ROOT / "examples"
LOAN = {"applicant_id": "A-1001", "pan": "ABCDE1234F", "age": 30,
        "annual_income": 900000, "amount": 500000, "employment_type": "salaried"}
VENDOR = {"vendor_id": "V-204", "legal_name": "Acme Supplies Pvt Ltd", "category": "it_services",
          "annual_contract_value": 400000, "gst_number": "29ABCDE1234F1Z5", "years_in_business": 6}


def block(data: object) -> str:
    return "```json\n" + json.dumps(data, indent=2) + "\n```"


def rules_table(explanation: dict) -> str:
    rows = ["| Stage | Rule | Result | Data the rule looked at | Decisive |", "|---|---|---|---|---|"]
    for step in explanation["steps"]:
        for rule in step["rules"]:
            data = "; ".join(
                f"`{d['field']}` = `{json.dumps(d['actual'])}`"
                + ("" if d["operator"] in ("exists", "not_exists") else f" (test: `{d['operator']} {json.dumps(d['expected'])}`)")
                for d in rule["data"]
            )
            rows.append(f"| {step['stage']} | {rule['rule_id']} | {rule['status']} | {data} | "
                        f"{'yes' if rule['decisive'] else ''} |")
    return "\n".join(rows)


def section(title: str, intro: str, payload: object, service: DecisionService,
            request_id: str, extra: str = "") -> str:
    explanation = service.explanation(request_id)
    audit = service.audit(request_id)
    history = " -> ".join(
        f"{h['to_status']}" + (f" ({h['to_stage']})" if h["to_status"] == "IN_PROGRESS" else "")
        for h in audit["state_history"]
    )
    slug = title.split(".")[0].strip().zfill(2) + "_" + "_".join(title.split(".")[1].lower().split()[:4])
    (OUT / f"{slug}.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    parts = [
        f"## {title}", intro,
        "**Input**", block(payload),
        f"**Output:** `{explanation['decision']}`  \n**Why:** {audit['request']['reason']}  \n"
        f"**Decided by:** {explanation['decided_by']}  \n"
        f"**Rules version:** {explanation['workflow']} v{explanation['workflow_version']} "
        f"(config hash `{explanation['config_hash'][:12]}`)",
        "**Rules triggered**", rules_table(explanation),
        "**State history:** " + history,
        "**Audit reasoning** (built from the audit log)",
        "\n".join(f"{i}. {line}" for i, line in enumerate(explanation["narrative"], 1)),
    ]
    if extra:
        parts.append(extra)
    parts.append(f"Full audit record: [`{slug}.json`]({slug}.json)")
    return "\n\n".join(parts)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    workdir = Path(tempfile.mkdtemp())
    config_dir = workdir / "configs"
    shutil.copytree(ROOT / "configs", config_dir)
    service = DecisionService(config_dir, sleep=lambda _: None)
    bureau = service.dependencies.get("credit_bureau")
    sections = []

    # 1. Approved
    r = service.submit("loan_approval", LOAN, "example-1").request
    sections.append(section("1. Loan approved", "Every rule in all three stages passes.",
                            LOAN, service, r["request_id"]))

    # 2. Rejected on dependency data
    payload = {**LOAN, "applicant_id": "A-1002"}
    r = service.submit("loan_approval", payload, "example-2").request
    sections.append(section("2. Loan rejected on credit score",
                            "The bureau returns 610; the minimum is 650. The risk stage never runs.",
                            payload, service, r["request_id"]))

    # 3. Manual review, resolved by a person
    payload = {**LOAN, "applicant_id": "A-1003"}
    r = service.submit("loan_approval", payload, "example-3").request
    service.review(r["request_id"], "APPROVED", "asha.k", "Stable employer, low exposure")
    sections.append(section("3. Borderline score sent to manual review",
                            "Score 675 passes the minimum but triggers the conditional rule. "
                            "A reviewer clears the credit stage, and the workflow then continues: "
                            "the risk stage still runs before the loan is approved.",
                            payload, service, r["request_id"]))

    # 4. Dependency failure and retry
    bureau.faults.fail_next = 4
    payload = {**LOAN, "amount": 650000}
    r = service.submit("loan_approval", payload, "example-4").request
    parked = r["status"]
    service.retry(r["request_id"], actor="ops.team")
    sections.append(section("4. Dependency failure then retry",
                            f"The credit bureau fails 4 calls in a row. The first run uses its 3 attempts and "
                            f"parks the request as `{parked}`. On retry the bureau fails once more, then answers.",
                            payload, service, r["request_id"]))

    # 5. Vendor workflow (second workflow, same engine)
    payload = {**VENDOR, "category": "defense"}
    r = service.submit("vendor_approval", payload, "example-5").request
    sections.append(section("5. Vendor in a sensitive category",
                            "A different workflow on the same engine. A compound `any` condition routes it to review.",
                            payload, service, r["request_id"]))

    # 6. Rule change
    payload = {**LOAN, "applicant_id": "A-1001", "annual_income": 250000}
    before = service.submit("loan_approval", payload, "example-6-before").request
    path = config_dir / "loan_approval.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["version"] = 2
    config["stages"][0]["rules"][2]["value"] = 200000
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    service.reload_workflows()
    after = service.submit("loan_approval", payload, "example-6-after").request
    sections.append(section(
        "6. Rule change without a code change",
        "The same applicant (income 2,50,000) is submitted before and after the income threshold in "
        "`loan_approval.json` is edited from 3,00,000 to 2,00,000 and `POST /workflows/reload` is called. "
        f"Before: `{before['status']}` under v{before['workflow_version']}. "
        f"After: `{after['status']}` under v{after['workflow_version']}. The record below is the *before* "
        "request, read after the change: it still explains itself against the old threshold.",
        payload, service, before["request_id"]))

    # 7 and 8: refusals that produce no decision
    tail = ["## 7. Invalid input", "Structurally invalid requests are refused before anything is stored.",
            "**Input**"]
    bad = {"applicant_id": "A-1001", "age": "thirty", "amount": -5}
    try:
        service.submit("loan_approval", bad)
    except ValidationFailed as exc:
        tail += [block(bad), "**Output:** HTTP 400", block({"error": "invalid_input", "details": exc.errors})]

    dup = service.submit("loan_approval", LOAN, "example-1")
    tail += ["## 8. Duplicate request",
             "Example 1 is sent again with the same `Idempotency-Key`. No new request is created and "
             "nothing is re-evaluated; the original answer comes back (HTTP 200 instead of 201).",
             block({**dup.request, "duplicate": not dup.created})]
    try:
        service.submit("loan_approval", {**LOAN, "amount": 999999}, "example-1")
    except IdempotencyConflict as exc:
        tail += ["The same key with a *different* payload is refused (HTTP 409):",
                 block({"error": "idempotency_conflict", "message": str(exc)})]

    header = (
        "# Decision explanation examples\n\n"
        "Generated by `python scripts/generate_examples.py` from real runs of the system. "
        "Request ids and timestamps change on each run; everything else is deterministic.\n"
    )
    (OUT / "README.md").write_text(encoding="utf-8", data=header + "\n" + "\n\n---\n\n".join(sections) + "\n\n---\n\n" + "\n\n".join(tail) + "\n")
    shutil.rmtree(workdir)
    print(f"Wrote {len(list(OUT.glob('*')))} files to {OUT}")


if __name__ == "__main__":
    main()
