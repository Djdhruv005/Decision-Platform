"""REST API (FastAPI).

Deliberately thin: parse the HTTP request, call one ``DecisionService``
method, map the result or the domain error to an HTTP response. No business
logic lives here.

Status codes:
    201  new request processed          200  duplicate submission (same answer)
    202  duplicate of a request that is still being processed
    400  input failed schema validation 404  unknown workflow / request
    409  idempotency key reused with different payload, or action not
         allowed in the current state
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.errors import (
    ConcurrentUpdateError, IdempotencyConflict, InvalidStateError,
    RequestNotFound, ValidationFailed, WorkflowNotFound,
)
from app.service import DecisionService


class ReviewBody(BaseModel):
    decision: str
    reviewer: str
    comment: str = ""


class FaultBody(BaseModel):
    fail_next: int = Field(default=0, ge=0, le=1000)
    failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    mode: str = "error"


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra})


def create_app(service: DecisionService, enable_simulation: bool = True,
               recover_on_startup: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Crash recovery runs when the server starts, not when the module is
        # imported, and a failure in it cannot stop the server from starting.
        if recover_on_startup:
            service.recover_interrupted()
        yield

    app = FastAPI(
        title="Configurable Workflow Decision Platform",
        description="Config-driven decision workflows with audit trails, retries and idempotency.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.service = service

    # ---- domain error -> HTTP mapping -----------------------------------

    @app.exception_handler(WorkflowNotFound)
    def _workflow_not_found(_, exc: WorkflowNotFound):
        return _error(404, "workflow_not_found", str(exc))

    @app.exception_handler(RequestNotFound)
    def _request_not_found(_, exc: RequestNotFound):
        return _error(404, "request_not_found", str(exc))

    @app.exception_handler(ValidationFailed)
    def _validation_failed(_, exc: ValidationFailed):
        return _error(400, "invalid_input", str(exc), details=exc.errors)

    @app.exception_handler(IdempotencyConflict)
    def _idempotency_conflict(_, exc: IdempotencyConflict):
        return _error(409, "idempotency_conflict", str(exc),
                      existing_request_id=exc.existing_request_id)

    @app.exception_handler(InvalidStateError)
    def _invalid_state(_, exc: InvalidStateError):
        return _error(409, "invalid_state", str(exc))

    @app.exception_handler(ConcurrentUpdateError)
    def _concurrent_update(_, exc: ConcurrentUpdateError):
        return _error(409, "concurrent_update", str(exc))

    @app.exception_handler(RequestValidationError)
    def _unparseable(request: Request, exc: RequestValidationError):
        # A submission whose body is not even JSON gets the same 400 shape as
        # one that fails the schema, and is kept in the intake log too.
        workflow = request.path_params.get("workflow")
        if request.method == "POST" and workflow and request.url.path.endswith("/requests"):
            errors = [{"field": "(body)", "message": err.get("msg", "invalid body"),
                       "constraint": err.get("type", "json")} for err in exc.errors()]
            service.store.log_intake_rejection(
                workflow, request.headers.get("Idempotency-Key"), str(exc.body)[:10_000], errors)
            return _error(400, "invalid_input", f"Request body could not be parsed ({len(errors)} error(s))",
                          details=errors)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    # ---- routes ----------------------------------------------------------

    @app.get("/health")
    def health():
        # config_errors lists workflow files that failed to load (the rest keep serving).
        errors = service.registry.errors
        return {"status": "degraded" if errors else "ok",
                "workflows": [c.name for c in service.registry.list()],
                "config_errors": errors}

    @app.post("/workflows/{workflow}/requests")
    def submit_request(
        workflow: str,
        payload: Any = Body(...),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        result = service.submit(workflow, payload, idempotency_key)
        if result.created:
            status_code = 201
        elif result.request["status"] in ("RECEIVED", "IN_PROGRESS"):
            status_code = 202   # the original submission is still being processed
        else:
            status_code = 200
        return JSONResponse(
            status_code=status_code,
            content={**result.request, "duplicate": not result.created},
        )

    @app.get("/requests/{request_id}")
    def get_request(request_id: str):
        return service.get(request_id)

    @app.get("/requests/{request_id}/audit")
    def get_audit(request_id: str):
        return service.audit(request_id)

    @app.get("/requests/{request_id}/explanation")
    def get_explanation(request_id: str):
        return service.explanation(request_id)

    @app.post("/requests/{request_id}/retry")
    def retry_request(request_id: str):
        return service.retry(request_id)

    @app.post("/requests/{request_id}/review")
    def review_request(request_id: str, body: ReviewBody):
        return service.review(request_id, body.decision, body.reviewer, body.comment)

    @app.get("/workflows")
    def list_workflows():
        return service.list_workflows()

    @app.get("/workflows/{workflow}")
    def get_workflow(workflow: str):
        return service.get_workflow(workflow)

    @app.post("/workflows/reload")
    def reload_workflows():
        return service.reload_workflows()

    @app.get("/workflows/{workflow}/intake-rejections")
    def list_intake_rejections(workflow: str):
        """Requests that were refused at intake (invalid input), newest first."""
        return service.list_intake_rejections(workflow)

    if not enable_simulation:
        # Production mode: the fault-injection route does not exist at all.
        return app

    @app.post("/simulate/dependencies/{name}/faults")
    def set_dependency_faults(name: str, body: FaultBody):
        """Demo/test control: make a mock dependency fail on purpose.
        Switched off with DECISION_ENABLE_SIMULATION=0."""
        try:
            dependency = service.dependencies.get(name)
        except KeyError as exc:
            return _error(404, "dependency_not_found", str(exc.args[0]))
        if body.mode not in ("error", "timeout", "permanent"):
            return _error(400, "invalid_input", "mode must be error, timeout or permanent")
        dependency.faults.fail_next = body.fail_next
        dependency.faults.failure_rate = body.failure_rate
        dependency.faults.mode = body.mode
        return {"dependency": name, "faults": dependency.faults.as_dict()}

    return app
