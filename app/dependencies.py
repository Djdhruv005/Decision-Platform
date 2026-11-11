"""External dependencies: simulated services plus the retry wrapper.

Real systems call credit bureaus, KYC providers, registries. Those calls time
out and fail. Here they are simulated in-process with controllable faults so
failure handling can be demonstrated and tested deterministically.

A dependency is anything with ``name`` and ``call(request_id, params) -> dict``.
Swapping a mock for a real HTTP client means implementing that one method.

``params`` is built by the engine from the workflow config's ``input`` mapping
(``{"applicant_id": "input.customer_id"}``), so an adapter names the
parameters it needs and each workflow says where they come from. Two
workflows with different field names can share one adapter without new code.

Every call carries the request id so a real provider could de-duplicate it:
after a crash the engine may call the dependency again for the same request.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from app.validator import non_finite_fields


class DependencyError(Exception):
    """Transient failure: worth retrying."""
    transient = True


class DependencyTimeout(DependencyError):
    pass


class PermanentDependencyError(DependencyError):
    """The dependency answered and the answer is 'no'. Retrying will not help."""
    transient = False


class Dependency(Protocol):
    name: str

    def call(self, request_id: str, payload: dict) -> dict: ...


@dataclass
class FaultPlan:
    """How a mock should misbehave."""
    fail_next: int = 0           # fail exactly this many upcoming calls
    failure_rate: float = 0.0    # then fail randomly at this rate (0..1)
    mode: str = "error"          # "error" | "timeout" | "permanent"
    rng: random.Random = field(default_factory=random.Random)

    def maybe_fail(self, name: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
        elif not (self.failure_rate > 0 and self.rng.random() < self.failure_rate):
            return
        if self.mode == "timeout":
            raise DependencyTimeout(f"{name} timed out")
        if self.mode == "permanent":
            raise PermanentDependencyError(f"{name} rejected the request")
        raise DependencyError(f"{name} is unavailable (503)")

    def as_dict(self) -> dict:
        return {"fail_next": self.fail_next, "failure_rate": self.failure_rate, "mode": self.mode}


class MockDependency:
    name = "mock"

    def __init__(self, faults: Optional[FaultPlan] = None):
        self.faults = faults or FaultPlan()
        self.calls = 0

    def call(self, request_id: str, payload: dict) -> dict:
        self.calls += 1
        self.faults.maybe_fail(self.name)
        return self.respond(request_id, payload)

    def respond(self, request_id: str, payload: dict) -> dict:
        raise NotImplementedError


def _stable_number(text: str, low: int, high: int) -> int:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return low + int(digest[:8], 16) % (high - low + 1)


class MockCreditBureau(MockDependency):
    """Returns a credit score. Known applicants have fixed scores so demos are
    repeatable; unknown ones get a stable score derived from their id."""
    name = "credit_bureau"

    def __init__(self, scores: Optional[dict[str, int]] = None, faults: Optional[FaultPlan] = None):
        super().__init__(faults)
        self.scores = scores if scores is not None else {"A-1001": 780, "A-1002": 610, "A-1003": 675}

    def respond(self, request_id: str, payload: dict) -> dict:
        applicant = str(payload.get("applicant_id", ""))
        score = self.scores.get(applicant, _stable_number(applicant, 550, 850))
        return {"score": score, "bureau_reference": f"CB-{_stable_number(request_id, 100000, 999999)}"}


class MockVendorRegistry(MockDependency):
    """Compliance lookup: is this vendor registered, is it blacklisted."""
    name = "vendor_registry"

    def __init__(self, blacklist: Optional[set[str]] = None, faults: Optional[FaultPlan] = None):
        super().__init__(faults)
        self.blacklist = blacklist if blacklist is not None else {"V-666"}

    def respond(self, request_id: str, payload: dict) -> dict:
        vendor = str(payload.get("vendor_id", ""))
        return {"registered": True, "blacklisted": vendor in self.blacklist}


class DependencyRegistry:
    def __init__(self, dependencies: Optional[list[Dependency]] = None):
        self._items: dict[str, Dependency] = {}
        for dependency in dependencies or []:
            self.register(dependency)

    def register(self, dependency: Dependency) -> None:
        self._items[dependency.name] = dependency

    def get(self, name: str) -> Dependency:
        if name not in self._items:
            raise KeyError(f"No dependency registered under '{name}'")
        return self._items[name]

    def names(self) -> list[str]:
        return sorted(self._items)


def default_dependencies() -> DependencyRegistry:
    return DependencyRegistry([MockCreditBureau(), MockVendorRegistry()])


# Hard ceiling on a single wait, whatever the config says. Kept well under the
# 30 s "abandoned request" window so a request that is merely backing off is
# never mistaken for one whose worker died (each attempt is also a heartbeat).
MAX_BACKOFF_MS = 10_000


@dataclass
class DependencyOutcome:
    ok: bool
    result: Optional[dict]
    attempts: list[dict]         # one record per attempt, for the audit trail
    last_error: Optional[str] = None
    transient: bool = True       # False when the last error was permanent


def _check_response(name: str, result: object) -> None:
    """A dependency's answer is untrusted input, the same as a request."""
    if not isinstance(result, dict):
        raise PermanentDependencyError(f"{name} returned {type(result).__name__}, expected an object")
    bad = non_finite_fields(result)
    if bad:
        raise PermanentDependencyError(
            f"{name} returned an unusable value: {bad[0]['field']} {bad[0]['message']}"
        )
    try:
        # It must be storable as JSON, or saving the stage would fail later
        # and leave the request stuck mid-workflow.
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PermanentDependencyError(f"{name} returned a value that is not JSON: {exc}") from exc


def call_with_retry(dependency: Dependency, request_id: str, payload: dict, retry: dict,
                    sleep: Callable[[float], None] = time.sleep,
                    on_attempt: Optional[Callable[[dict], None]] = None) -> DependencyOutcome:
    """Call a dependency with bounded retries and exponential backoff.

    Transient errors are retried up to ``max_attempts``. Permanent errors stop
    immediately. Unexpected exceptions from a dependency are treated as
    transient failures of that dependency, never as a crash of the engine.

    ``on_attempt`` is called with each attempt's record as soon as the attempt
    ends, so the caller can write it to the audit log before anything else
    can go wrong.
    """
    notify = on_attempt or (lambda record: None)
    attempts: list[dict] = []
    max_attempts = retry["max_attempts"]
    last_error: Optional[str] = None
    transient = True

    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        record = {"dependency": dependency.name, "attempt": attempt, "max_attempts": max_attempts}
        try:
            result = dependency.call(request_id, payload)
            _check_response(dependency.name, result)
        except Exception as exc:  # noqa: BLE001 - a dependency must never crash the engine
            transient = getattr(exc, "transient", True)
            last_error = f"{type(exc).__name__}: {exc}"
            record.update(ok=False, error=last_error, transient=transient)
            will_retry = transient and attempt < max_attempts
            if will_retry:
                delay_ms = min(MAX_BACKOFF_MS,
                               retry["backoff_ms"] * (retry["backoff_multiplier"] ** (attempt - 1)))
                record["retry_in_ms"] = delay_ms
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            attempts.append(record)
            notify(record)
            if not will_retry:
                break
            sleep(delay_ms / 1000)
        else:
            record.update(ok=True, response=result,
                          duration_ms=round((time.perf_counter() - started) * 1000, 2))
            attempts.append(record)
            notify(record)
            return DependencyOutcome(True, result, attempts)

    return DependencyOutcome(False, None, attempts, last_error, transient)
