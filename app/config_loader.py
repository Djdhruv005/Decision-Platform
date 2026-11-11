"""Workflow configuration: load, validate, version.

A workflow is one JSON file. This module turns it into a ``WorkflowConfig``
and refuses anything malformed *at load time*, with every problem listed, so
a bad config can never reach a live request.

Validation is strict on purpose: an unknown or misspelled key is an error,
not something to ignore. A typo such as ``onfail`` would otherwise silently
change where requests are routed.

Versioning: every config gets a content hash. Each request stores the hash of
the config it started under and the full config body is snapshotted in the
database, so an old decision can always be explained against the exact rules
that produced it, even after the file has been edited.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.errors import ConfigError, WorkflowNotFound
from app.rule_engine import IGNORE, OPERATORS, PRESENCE_OPERATORS, RULE_TYPES

logger = logging.getLogger(__name__)

# States a rule or a stage is allowed to route a request to.
DECISION_STATES = ("APPROVED", "REJECTED", "MANUAL_REVIEW")
# States a stage may fall into when its external dependency stays down.
DEPENDENCY_FAILURE_STATES = ("RETRY_PENDING", "MANUAL_REVIEW", "REJECTED", "FAILED")
RESERVED_NAMES = {"RECEIVED", "IN_PROGRESS", "FAILED", "RETRY_PENDING", *DECISION_STATES}

DEFAULT_RETRY = {"max_attempts": 3, "backoff_ms": 100, "backoff_multiplier": 2.0}
DEFAULT_MAX_RESUMES = 3

# Upper bounds, so a config cannot block a request thread for hours.
MAX_ATTEMPTS_LIMIT = 10
MAX_BACKOFF_MS_LIMIT = 60_000
MAX_BACKOFF_MULTIPLIER_LIMIT = 10
MAX_RESUMES_LIMIT = 20

# Every key the loader understands, per level. Anything else is an error.
TOP_KEYS = {"workflow", "version", "description", "max_resumes", "input_schema", "stages"}
STAGE_KEYS = {"id", "description", "dependency", "rules", "on_pass", "on_fail"}
DEPENDENCY_KEYS = {"name", "input", "retry", "on_failure"}
RETRY_KEYS = {"max_attempts", "backoff_ms", "backoff_multiplier"}
LEAF_CONDITION_KEYS = {"field", "op", "value", "value_ref", "value_factor"}
RULE_COMMON_KEYS = {"id", "type", "description"}


def canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def content_hash(data: object) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


@dataclass(frozen=True)
class WorkflowConfig:
    name: str
    version: int
    config_hash: str
    description: str
    input_schema: dict
    stages: dict          # stage id -> normalised stage dict, in file order
    first_stage: str
    max_resumes: int
    raw: dict             # the config exactly as written, for snapshots

    @classmethod
    def from_dict(cls, raw: dict, source: str = "<dict>",
                  known_dependencies: Optional[Iterable[str]] = None) -> "WorkflowConfig":
        problems = _find_problems(raw, known_dependencies)
        if problems:
            raise ConfigError(source, problems)
        stages = {stage["id"]: _normalise_stage(stage) for stage in raw["stages"]}
        return cls(
            name=raw["workflow"],
            version=raw["version"],
            config_hash=content_hash(raw),
            description=raw.get("description", ""),
            input_schema=raw["input_schema"],
            stages=stages,
            first_stage=raw["stages"][0]["id"],
            max_resumes=raw.get("max_resumes", DEFAULT_MAX_RESUMES),
            raw=raw,
        )

    def summary(self) -> dict:
        return {
            "workflow": self.name,
            "version": self.version,
            "config_hash": self.config_hash,
            "description": self.description,
            "stages": [
                {
                    "id": stage["id"],
                    "dependency": stage["dependency"]["name"] if stage["dependency"] else None,
                    "rules": [rule["id"] for rule in stage["rules"]],
                    "on_pass": stage["on_pass"],
                    "on_fail": stage["on_fail"],
                }
                for stage in self.stages.values()
            ],
        }


def _normalise_stage(stage: dict) -> dict:
    """Fill in defaults so the engine never has to guess."""
    dependency = None
    if stage.get("dependency"):
        dep = stage["dependency"]
        dependency = {
            "name": dep["name"],
            "input": dep.get("input"),   # parameter name -> data path; None = pass the whole payload
            "retry": {**DEFAULT_RETRY, **dep.get("retry", {})},
            "on_failure": dep.get("on_failure", "RETRY_PENDING"),
        }
    return {
        "id": stage["id"],
        "description": stage.get("description", ""),
        "dependency": dependency,
        "rules": stage.get("rules", []),
        "on_pass": stage["on_pass"],
        "on_fail": stage.get("on_fail", "REJECTED"),
    }


# ---- validation helpers ----------------------------------------------------

def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unknown_keys(obj: dict, allowed: set, where: str) -> list[str]:
    """Report keys the loader does not understand, with a spelling suggestion."""
    problems = []
    for key in sorted(set(obj) - allowed):
        close = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        problems.append(f"{where}: unknown key '{key}'{hint}")
    return problems


def _field_problems(field: object, label: str, key: str = "field") -> list[str]:
    if not isinstance(field, str) or not field.startswith(("input.", "deps.")):
        return [f"{label}: '{key}' must be a path starting with 'input.' or 'deps.' (got {field!r})"]
    return []


def _condition_problems(cond: object, label: str) -> list[str]:
    if not isinstance(cond, dict):
        return [f"{label}: condition must be an object"]
    for combinator in ("all", "any"):
        if combinator in cond:
            problems = _unknown_keys(cond, {combinator}, label)
            children = cond[combinator]
            if not isinstance(children, list) or not children:
                return problems + [f"{label}: '{combinator}' must be a non-empty list of conditions"]
            return problems + [p for child in children for p in _condition_problems(child, label)]
    if "not" in cond:
        return _unknown_keys(cond, {"not"}, label) + _condition_problems(cond["not"], label)

    problems = _unknown_keys(cond, LEAF_CONDITION_KEYS, label)
    problems += _field_problems(cond.get("field"), label)
    op = cond.get("op")
    if op not in OPERATORS and op not in PRESENCE_OPERATORS:
        known_ops = sorted([*OPERATORS, *PRESENCE_OPERATORS])
        return problems + [f"{label}: unknown operator '{op}' (known: {known_ops})"]
    if op in PRESENCE_OPERATORS:
        return problems
    if "value" in cond and "value_ref" in cond:
        problems.append(f"{label}: use either 'value' or 'value_ref', not both")
    elif "value_ref" in cond:
        problems += _field_problems(cond["value_ref"], label, "value_ref")
        if "value_factor" in cond and not _is_number(cond["value_factor"]):
            problems.append(f"{label}: 'value_factor' must be a number")
    elif "value" not in cond:
        problems.append(f"{label}: operator '{op}' needs a 'value' or a 'value_ref'")
    elif op in ("in", "not_in") and not isinstance(cond["value"], list):
        problems.append(f"{label}: operator '{op}' needs a list 'value'")
    if "value_factor" in cond and "value_ref" not in cond:
        problems.append(f"{label}: 'value_factor' only applies together with 'value_ref'")
    return problems


# ---- rule validators (one per rule type) -----------------------------------
# A rule type is two things: an evaluator in rule_engine.RULE_TYPES and a
# validator here. A type registered without a validator is refused at load
# time, so a new rule type can never run unvalidated.

Validator = Callable[[dict, str, Callable[[object, str], None]], list[str]]


def _validate_mandatory(rule: dict, label: str, check_target) -> list[str]:
    problems = _unknown_keys(rule, RULE_COMMON_KEYS | {"field", "on_fail"}, label)
    return problems + _field_problems(rule.get("field"), label)


def _validate_threshold(rule: dict, label: str, check_target) -> list[str]:
    condition_keys = LEAF_CONDITION_KEYS | {"all", "any", "not"}
    problems = _unknown_keys(rule, RULE_COMMON_KEYS | {"on_fail"} | condition_keys, label)
    condition = {k: v for k, v in rule.items() if k in condition_keys}
    return problems + _condition_problems(condition, label)


def _validate_conditional(rule: dict, label: str, check_target) -> list[str]:
    problems = _unknown_keys(rule, RULE_COMMON_KEYS | {"if", "then", "on_missing"}, label)
    if "on_missing" in rule and rule["on_missing"] != IGNORE:
        check_target(rule["on_missing"], f"{label} on_missing")
    if "if" not in rule:
        problems.append(f"{label}: conditional rule needs an 'if' condition")
    else:
        problems += _condition_problems(rule["if"], label)
    if "then" not in rule:
        problems.append(f"{label}: conditional rule needs a 'then' target")
    else:
        check_target(rule["then"], f"{label} then")
    return problems


RULE_VALIDATORS: dict[str, Validator] = {
    "mandatory": _validate_mandatory,
    "threshold": _validate_threshold,
    "conditional": _validate_conditional,
}


def _rule_problems(rule: object, where: str, seen_ids: set, check_target) -> list[str]:
    if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule.get("id"):
        return [f"{where}: every rule must be an object with a non-empty string 'id'"]
    label = f"{where} rule '{rule['id']}'"
    problems: list[str] = []
    if rule["id"] in seen_ids:
        problems.append(f"{label}: duplicate rule id")
    seen_ids.add(rule["id"])

    rule_type = rule.get("type")
    if rule_type not in RULE_TYPES:
        return problems + [f"{label}: unknown type '{rule_type}' (known: {sorted(RULE_TYPES)})"]
    if rule_type not in RULE_VALIDATORS:
        return problems + [f"{label}: rule type '{rule_type}' has no validator registered in RULE_VALIDATORS"]

    problems += RULE_VALIDATORS[rule_type](rule, label, check_target)
    if "on_fail" in rule:
        check_target(rule["on_fail"], f"{label} on_fail")
    return problems


def _dependency_problems(dep: object, where: str, known: Optional[set]) -> list[str]:
    if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
        return [f"{where}: 'dependency' must be an object with a string 'name'"]
    problems = _unknown_keys(dep, DEPENDENCY_KEYS, f"{where} dependency")
    if known is not None and dep["name"] not in known:
        problems.append(f"{where}: unknown dependency '{dep['name']}' (known: {sorted(known)})")
    if dep.get("on_failure", "RETRY_PENDING") not in DEPENDENCY_FAILURE_STATES:
        problems.append(
            f"{where}: dependency on_failure must be one of {list(DEPENDENCY_FAILURE_STATES)}"
        )
    mapping = dep.get("input")
    if mapping is not None:
        if not isinstance(mapping, dict) or not mapping:
            problems.append(f"{where}: dependency 'input' must map parameter names to data paths")
        else:
            for parameter, path in mapping.items():
                problems += _field_problems(path, f"{where} dependency input '{parameter}'", "path")
    retry = dep.get("retry", {})
    if not isinstance(retry, dict):
        return problems + [f"{where}: dependency 'retry' must be an object"]
    problems += _unknown_keys(retry, RETRY_KEYS, f"{where} dependency retry")
    attempts = retry.get("max_attempts", 1)
    if not _is_int(attempts) or not 1 <= attempts <= MAX_ATTEMPTS_LIMIT:
        problems.append(f"{where}: retry.max_attempts must be an integer from 1 to {MAX_ATTEMPTS_LIMIT}")
    backoff = retry.get("backoff_ms", 0)
    if not _is_number(backoff) or not 0 <= backoff <= MAX_BACKOFF_MS_LIMIT:
        problems.append(f"{where}: retry.backoff_ms must be a number from 0 to {MAX_BACKOFF_MS_LIMIT}")
    multiplier = retry.get("backoff_multiplier", 1)
    if not _is_number(multiplier) or not 1 <= multiplier <= MAX_BACKOFF_MULTIPLIER_LIMIT:
        problems.append(
            f"{where}: retry.backoff_multiplier must be a number from 1 to {MAX_BACKOFF_MULTIPLIER_LIMIT}"
        )
    return problems


def _find_problems(raw: object, known_dependencies: Optional[Iterable[str]]) -> list[str]:
    if not isinstance(raw, dict):
        return ["config must be a JSON object"]
    problems = _unknown_keys(raw, TOP_KEYS, "workflow")

    if not isinstance(raw.get("workflow"), str) or not raw.get("workflow"):
        problems.append("'workflow' must be a non-empty string")
    if not _is_int(raw.get("version")):
        problems.append("'version' must be an integer")
    max_resumes = raw.get("max_resumes", DEFAULT_MAX_RESUMES)
    if not _is_int(max_resumes) or not 0 <= max_resumes <= MAX_RESUMES_LIMIT:
        problems.append(f"'max_resumes' must be an integer from 0 to {MAX_RESUMES_LIMIT}")

    schema = raw.get("input_schema")
    if not isinstance(schema, dict):
        problems.append("'input_schema' must be a JSON Schema object")
    else:
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            problems.append(f"'input_schema' is not a valid JSON Schema: {exc.message}")

    stages = raw.get("stages")
    if not isinstance(stages, list) or not stages:
        problems.append("'stages' must be a non-empty list")
        return problems
    if not all(isinstance(s, dict) and isinstance(s.get("id"), str) and s.get("id") for s in stages):
        problems.append("every stage must be an object with a non-empty string 'id'")
        return problems

    stage_ids = [s["id"] for s in stages]
    for stage_id in sorted({i for i in stage_ids if stage_ids.count(i) > 1}):
        problems.append(f"duplicate stage id '{stage_id}'")
    for stage_id in stage_ids:
        if stage_id in RESERVED_NAMES:
            problems.append(f"stage id '{stage_id}' collides with a reserved state name")

    valid_targets = set(stage_ids) | set(DECISION_STATES)
    known = set(known_dependencies) if known_dependencies is not None else None
    seen_rule_ids: set[str] = set()

    def check_target(value: object, where: str) -> None:
        if not isinstance(value, str) or value not in valid_targets:
            problems.append(
                f"{where}: '{value}' is not a stage id or one of {list(DECISION_STATES)}"
            )

    for stage in stages:
        where = f"stage '{stage['id']}'"
        problems += _unknown_keys(stage, STAGE_KEYS, where)
        if "on_pass" not in stage:
            problems.append(f"{where}: 'on_pass' is required")
        else:
            check_target(stage["on_pass"], f"{where} on_pass")
        if "on_fail" in stage:
            check_target(stage["on_fail"], f"{where} on_fail")

        dep = stage.get("dependency")
        if dep is not None:
            problems.extend(_dependency_problems(dep, where, known))

        rules = stage.get("rules", [])
        if not isinstance(rules, list):
            problems.append(f"{where}: 'rules' must be a list")
            continue
        for rule in rules:
            problems.extend(_rule_problems(rule, where, seen_rule_ids, check_target))

    if not problems:
        edges = _stage_edges(stages)
        cycle = _find_cycle(stages, edges)
        if cycle:
            problems.append("stages form a cycle: " + " -> ".join(cycle))
        unreachable = _unreachable(stages, edges)
        for stage_id in unreachable:
            problems.append(f"stage '{stage_id}' can never be reached from the first stage")
        if not cycle and not unreachable:
            problems += _dependency_data_problems(stages, edges)
    return problems


def _stage_edges(stages: list[dict]) -> dict[str, set[str]]:
    """Which stages each stage can route to."""
    stage_ids = {s["id"] for s in stages}
    edges: dict[str, set[str]] = {}
    for stage in stages:
        targets = {stage.get("on_pass"), stage.get("on_fail", "REJECTED")}
        for rule in stage.get("rules", []):
            targets.update({rule.get("on_fail"), rule.get("then"), rule.get("on_missing")})
        edges[stage["id"]] = {t for t in targets if t in stage_ids}
    return edges


def _find_cycle(stages: list[dict], edges: dict[str, set[str]]) -> Optional[list[str]]:
    """Depth-first search for a loop in the stage graph (would never terminate)."""
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> Optional[list[str]]:
        if node in visiting:
            return visiting[visiting.index(node):] + [node]
        if node in done:
            return None
        visiting.append(node)
        for nxt in sorted(edges[node]):
            found = visit(nxt)
            if found:
                return found
        visiting.pop()
        done.add(node)
        return None

    for stage in stages:
        found = visit(stage["id"])
        if found:
            return found
    return None


def _unreachable(stages: list[dict], edges: dict[str, set[str]]) -> list[str]:
    seen: set[str] = set()
    frontier = [stages[0]["id"]]
    while frontier:
        node = frontier.pop()
        if node not in seen:
            seen.add(node)
            frontier.extend(edges[node])
    return [s["id"] for s in stages if s["id"] not in seen]


def _condition_paths(cond: object) -> list[str]:
    if not isinstance(cond, dict):
        return []
    for combinator in ("all", "any"):
        if combinator in cond:
            return [p for child in cond[combinator] for p in _condition_paths(child)]
    if "not" in cond:
        return _condition_paths(cond["not"])
    return [p for p in (cond.get("field"), cond.get("value_ref")) if isinstance(p, str)]


def _dependency_data_problems(stages: list[dict], edges: dict[str, set[str]]) -> list[str]:
    """Every ``deps.<name>`` path must refer to a dependency that has been
    fetched on *every* route to the rule that reads it. Otherwise the rule
    would be looking at data that is not there."""
    by_id = {s["id"]: s for s in stages}
    predecessors: dict[str, set[str]] = {s["id"]: set() for s in stages}
    for source, targets in edges.items():
        for target in targets:
            predecessors[target].add(source)

    fetched_after: dict[str, set[str]] = {}

    def fetched_before(stage_id: str) -> set[str]:
        sets = [after(p) for p in predecessors[stage_id]]
        return set.intersection(*sets) if sets else set()

    def after(stage_id: str) -> set[str]:      # the stage graph is acyclic at this point
        if stage_id not in fetched_after:
            own = by_id[stage_id].get("dependency")
            fetched_after[stage_id] = fetched_before(stage_id) | ({own["name"]} if own else set())
        return fetched_after[stage_id]

    problems = []
    for stage in stages:
        where = f"stage '{stage['id']}'"
        checks = [(f"{where} dependency input", path, fetched_before(stage["id"]))
                  for path in ((stage.get("dependency") or {}).get("input") or {}).values()]
        for rule in stage.get("rules", []):
            paths = [rule.get("field")] if rule.get("type") == "mandatory" else []
            paths += _condition_paths(rule) + _condition_paths(rule.get("if"))
            checks += [(f"{where} rule '{rule['id']}'", p, after(stage["id"])) for p in paths]
        for label, path, available in checks:
            if isinstance(path, str) and path.startswith("deps."):
                name = path.split(".")[1]
                if name not in available:
                    problems.append(
                        f"{label}: '{path}' reads dependency '{name}', which is not fetched "
                        f"on every route to this stage (available here: {sorted(available) or 'none'})"
                    )
    return problems


# ---- registry ------------------------------------------------------------

class WorkflowRegistry:
    """Loads every ``*.json`` in a directory and serves the current version.

    ``reload()`` picks up edits without a restart. If an edited file is
    broken, the last good version keeps serving and the error is reported:
    a bad config push must not take down a running system.

    Startup is lenient in the same way: one broken file does not stop the
    other workflows from loading. The problems are logged and kept in
    ``errors`` (shown by ``GET /health``).
    """

    def __init__(self, config_dir: str | Path, known_dependencies: Optional[Iterable[str]] = None):
        self.config_dir = Path(config_dir)
        self._known_dependencies = list(known_dependencies) if known_dependencies is not None else None
        self._by_file: dict[str, WorkflowConfig] = {}
        self.errors: dict[str, list[str]] = {}
        self.reload()
        for filename, problems in self.errors.items():
            logger.error("Workflow config %s was not loaded: %s", filename, "; ".join(problems))

    def reload(self) -> dict:
        errors: dict[str, list[str]] = {}
        loaded: dict[str, WorkflowConfig] = {}
        owners: dict[str, str] = {}

        for path in sorted(self.config_dir.glob("*.json")):
            try:
                # Always UTF-8: the platform default differs between Windows
                # and Linux and would change both the text and the config hash.
                raw = json.loads(path.read_text(encoding="utf-8"))
                config = WorkflowConfig.from_dict(raw, path.name, self._known_dependencies)
                if config.name in owners:
                    raise ConfigError(path.name, [
                        f"workflow '{config.name}' is already defined in {owners[config.name]}"
                    ])
            except json.JSONDecodeError as exc:
                errors[path.name] = [f"not valid JSON: {exc}"]
            except (UnicodeDecodeError, OSError) as exc:
                errors[path.name] = [f"could not be read as UTF-8 text: {exc}"]
            except ConfigError as exc:
                errors[path.name] = exc.problems
            else:
                loaded[path.name] = config
                owners[config.name] = path.name
                continue
            # Broken file: keep serving the previous good version if there is one.
            previous = self._by_file.get(path.name)
            if previous and previous.name not in owners:
                loaded[path.name] = previous
                owners[previous.name] = path.name

        self._by_file = loaded
        self.errors = errors
        return {
            "workflows": [
                {"workflow": c.name, "version": c.version, "config_hash": c.config_hash}
                for c in self.list()
            ],
            "errors": errors,
        }

    def get(self, name: str) -> WorkflowConfig:
        for config in self._by_file.values():
            if config.name == name:
                return config
        raise WorkflowNotFound(f"No workflow named '{name}'")

    def list(self) -> list[WorkflowConfig]:
        return sorted(self._by_file.values(), key=lambda c: c.name)
