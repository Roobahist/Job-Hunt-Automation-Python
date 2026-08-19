from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis import Redis

from job_hunt.application.normalization import fillout_submission
from job_hunt.config import Settings
from job_hunt.container import Container
from job_hunt.domain.models import EnqueueResponse, JobSubmission, RetryResponse, RunStatus
from job_hunt.errors import ConfigurationError, WorkflowError
from job_hunt.logging import configure_logging
from job_hunt.run_store import RunStore
from job_hunt.security import verify_bearer
from job_hunt.worker import discover_tenant, process_submission


class Queue(Protocol):
    def submission(
        self, tenant: str, payload: dict[str, Any], run_id: UUID, force: bool
    ) -> str: ...
    def discovery(self, tenant: str, run_id: UUID) -> str: ...


class CeleryQueue:
    def submission(self, tenant: str, payload: dict[str, Any], run_id: UUID, force: bool) -> str:
        task = process_submission.delay(tenant, payload, str(run_id), force)
        return str(task.id)

    def discovery(self, tenant: str, run_id: UUID) -> str:
        task = discover_tenant.delay(tenant, str(run_id))
        return str(task.id)


def create_app(
    *,
    settings: Settings | None = None,
    queue: Queue | None = None,
    store: RunStore | None = None,
    container_factory: Callable[[], Container] | None = None,
) -> FastAPI:
    configured = settings or Settings()
    configure_logging(json_logs=configured.environment != "development")
    run_store = store or RunStore(
        Redis.from_url(configured.redis_url, decode_responses=True), configured.run_ttl_seconds
    )
    task_queue = queue or CeleryQueue()
    container = container_factory or (lambda: Container(configured))
    app = FastAPI(title="Job Hunt Automation", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Request validation failed",
                "details": exc.errors(),
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        labels = {
            400: "bad_request",
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limit_exceeded",
            503: "service_unavailable",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": labels.get(exc.status_code, "request_error"),
                "message": str(exc.detail),
            },
            headers=exc.headers,
        )

    @app.exception_handler(ConfigurationError)
    async def configuration_error(_: Request, exc: ConfigurationError) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"error": "configuration_error", "message": str(exc)}
        )

    @app.exception_handler(WorkflowError)
    async def workflow_error(_: Request, exc: WorkflowError) -> JSONResponse:
        code = 400 if exc.kind.value == "validation" else 502
        return JSONResponse(
            status_code=code, content={"error": exc.kind.value, "message": str(exc)}
        )

    def operator(authorization: Annotated[str | None, Header()] = None) -> None:
        if not verify_bearer(authorization, configured.operator_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid operator bearer token")

    def enqueue(tenant: str, payload: dict[str, Any], kind: str, *, force: bool) -> EnqueueResponse:
        container().registry.get(tenant)
        run = RunStatus(tenant=tenant, kind=kind)
        run_store.save(run)
        run_store.save_request(
            run.run_id, {"tenant": tenant, "payload": payload, "kind": kind, "force": force}
        )
        task_id = task_queue.submission(tenant, payload, run.run_id, force)
        run_store.update(run.run_id, task_id=task_id)
        return EnqueueResponse(run_id=run.run_id)

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> dict[str, str]:
        try:
            run_store.redis.ping()
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Redis is unavailable"
            ) from exc
        return {"status": "ready"}

    @app.post("/webhooks/fillout/{tenant}", response_model=EnqueueResponse, status_code=202)
    def fillout_webhook(
        tenant: str,
        payload: dict[str, Any],
        authorization: Annotated[str | None, Header()] = None,
    ) -> EnqueueResponse:
        services = container().tenant(tenant)
        if not verify_bearer(authorization, services.context.bootstrap.secret("fillout")):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Fillout bearer token")
        submission_root = payload.get("submission", {})
        nested_form_id = (
            submission_root.get("formId") if isinstance(submission_root, dict) else None
        )
        form_id = payload.get("formId") or payload.get("form_id") or nested_form_id
        if form_id != services.config.fillout_form_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Fillout form ID does not match tenant"
            )
        submission = fillout_submission(payload, services.config.fillout_field_ids)
        return enqueue(
            tenant,
            submission.model_dump(mode="json"),
            "fillout",
            force=False,
        )

    @app.post(
        "/v1/tenants/{tenant}/jobs",
        response_model=EnqueueResponse,
        status_code=202,
        dependencies=[Depends(operator)],
    )
    def submit_job(tenant: str, submission: JobSubmission, force: bool = False) -> EnqueueResponse:
        payload = submission.model_dump(mode="json")
        return enqueue(tenant, payload, "manual", force=force)

    @app.post(
        "/v1/tenants/{tenant}/discoveries",
        response_model=EnqueueResponse,
        status_code=202,
        dependencies=[Depends(operator)],
    )
    def submit_discovery(tenant: str) -> EnqueueResponse:
        container().registry.get(tenant)
        run = RunStatus(tenant=tenant, kind="discovery")
        run_store.save(run)
        run_store.save_request(run.run_id, {"tenant": tenant, "kind": "discovery"})
        task_id = task_queue.discovery(tenant, run.run_id)
        run_store.update(run.run_id, task_id=task_id)
        return EnqueueResponse(run_id=run.run_id)

    @app.get("/v1/runs/{run_id}", response_model=RunStatus, dependencies=[Depends(operator)])
    def get_run(run_id: UUID) -> RunStatus:
        found = run_store.get(run_id)
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        return found

    @app.post(
        "/v1/runs/{run_id}/retry",
        response_model=RetryResponse,
        status_code=202,
        dependencies=[Depends(operator)],
    )
    def retry_run(run_id: UUID) -> RetryResponse:
        original = run_store.get(run_id)
        request_data = run_store.get_request(run_id)
        if not original or not request_data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run or replay data not found")
        retry = RunStatus(tenant=original.tenant, kind=original.kind, original_run_id=run_id)
        run_store.save(retry)
        run_store.save_request(retry.run_id, request_data)
        if request_data["kind"] == "discovery":
            task_id = task_queue.discovery(original.tenant, retry.run_id)
        else:
            task_id = task_queue.submission(
                original.tenant,
                request_data["payload"],  # type: ignore[arg-type]
                retry.run_id,
                bool(request_data.get("force", False)),
            )
        run_store.update(retry.run_id, task_id=task_id)
        return RetryResponse(original_run_id=run_id, run_id=retry.run_id)

    return app
