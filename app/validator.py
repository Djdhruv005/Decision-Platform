"""Input intake: validate a request payload against the workflow's JSON Schema.

The schema lives in the workflow config (``input_schema``), so each workflow
defines its own request shape without any code change.

Structural problems (missing required field, wrong type) are caught here and
the request is refused before anything is stored. Business problems (income
too low, PAN not supplied) are the rule engine's job and produce an audited
REJECTED decision instead.
"""
from __future__ import annotations

import math

from jsonschema import Draft202012Validator


def non_finite_fields(value: object, path: str = "") -> list[dict]:
    """NaN and Infinity are accepted by Python's JSON parser but are not valid
    JSON and make every comparison meaningless. They are refused wherever
    data enters the system: request payloads and dependency responses."""
    if isinstance(value, float) and not math.isfinite(value):
        return [{"field": path or "(root)", "message": f"{value} is not a finite number",
                 "constraint": "finite"}]
    if isinstance(value, dict):
        return [e for key, item in value.items()
                for e in non_finite_fields(item, f"{path}.{key}" if path else str(key))]
    if isinstance(value, list):
        return [e for index, item in enumerate(value)
                for e in non_finite_fields(item, f"{path}.{index}" if path else str(index))]
    return []


def validate_payload(schema: dict, payload: object) -> list[dict]:
    """Return every validation error (empty list means the payload is valid)."""
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: [str(p) for p in e.absolute_path])
    return [
        {
            "field": ".".join(str(part) for part in err.absolute_path) or "(root)",
            "message": err.message,
            "constraint": err.validator,
        }
        for err in errors
    ]
