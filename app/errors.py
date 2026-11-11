"""Domain errors.

The service layer raises these; the API layer is the only place that maps
them to HTTP status codes. Nothing in the core knows about HTTP.
"""
from __future__ import annotations


class PlatformError(Exception):
    """Base class for every error the platform raises on purpose."""


class WorkflowNotFound(PlatformError):
    pass


class RequestNotFound(PlatformError):
    pass


class ConfigError(PlatformError):
    """A workflow config file is malformed. Carries every problem found."""

    def __init__(self, source: str, problems: list[str]):
        self.source = source
        self.problems = problems
        super().__init__(f"Invalid workflow config {source}: " + "; ".join(problems))


class ValidationFailed(PlatformError):
    """The request payload does not match the workflow's input schema."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"Input validation failed with {len(errors)} error(s)")


class IdempotencyConflict(PlatformError):
    """Same idempotency key was reused with a different payload."""

    def __init__(self, existing_request_id: str):
        self.existing_request_id = existing_request_id
        super().__init__(
            "Idempotency key already used with a different payload "
            f"(existing request {existing_request_id})"
        )


class InvalidStateError(PlatformError):
    """The requested action is not allowed in the request's current state."""


class ConcurrentUpdateError(PlatformError):
    """Another worker changed the request since it was read (optimistic lock)."""


class DuplicateKeyError(PlatformError):
    """Insert hit the UNIQUE(workflow, idempotency_key) constraint."""
