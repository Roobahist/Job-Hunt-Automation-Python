from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis import Redis

from job_hunt.application.normalization import fillout_submission
from job_hunt.application.runs import Queue, RunCoordinator
from job_hunt.config import Settings
from job_hunt.container import Container, TelegramRoute
from job_hunt.domain.models import EnqueueResponse, JobSubmission, RetryResponse, RunStatus
from job_hunt.errors import ConfigurationError, ProviderError, WorkflowError
from job_hunt.logging import configure_logging
from job_hunt.queueing import CeleryQueue
from job_hunt.run_store import RunStore
from job_hunt.security import verify_bearer

logger = logging.getLogger(__name__)


def _telegram_message_job_url(message: dict[str, object]) -> str:
    markup = message.get("reply_markup")
    keyboard = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if not isinstance(keyboard, list):
        return ""
    for row in keyboard:
        if not isinstance(row, list):
            continue
        for button in row:
            if isinstance(button, dict) and isinstance(button.get("url"), str):
                return str(button["url"])
    return ""


def _dropped_processing_caption(caption: str) -> str:
    lines: list[str] = []
    found_status = False
    for line in caption.splitlines():
        if line.startswith("Status:"):
            lines.append("Status: ⛔ Dropped manually")
            found_status = True
        elif line.startswith("Retry in:"):
            continue
        elif line.startswith("▶ "):
            stage = line[2:].split(":", 1)[0].strip()
            lines.append(f"■ {stage}: stopped manually")
        else:
            lines.append(line)
    if not found_status:
        lines.extend(["", "Status: ⛔ Dropped manually"])
    return "\n".join(lines)[:1024]


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
    coordinator = RunCoordinator(run_store, task_queue, lambda tenant: container().registry.get(tenant))
    app = FastAPI(title="Job Hunt Automation", version="0.2.0")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Request validation failed",
                "details": exc.errors(),
            },
        )

    @app.exception_handler(ValidationError)
    async def model_validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "Submitted values are incomplete or invalid",
                "details": exc.errors(include_url=False),
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
        return JSONResponse(status_code=503, content={"error": "configuration_error", "message": str(exc)})

    @app.exception_handler(WorkflowError)
    async def workflow_error(_: Request, exc: WorkflowError) -> JSONResponse:
        code = 400 if exc.kind.value == "validation" else 502
        return JSONResponse(status_code=code, content={"error": exc.kind.value, "message": str(exc)})

    def operator(authorization: Annotated[str | None, Header()] = None) -> None:
        if not verify_bearer(authorization, configured.operator_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid operator bearer token")

    @app.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness() -> dict[str, str]:
        try:
            run_store.redis.ping()
        except Exception as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis is unavailable") from exc
        return {"status": "ready"}

    @app.post("/webhooks/fillout/{tenant}", response_model=EnqueueResponse, status_code=202)
    def fillout_webhook(
        tenant: str,
        payload: dict[str, object],
        authorization: Annotated[str | None, Header()] = None,
    ) -> EnqueueResponse:
        services = container().tenant(tenant)
        if not verify_bearer(authorization, services.context.bootstrap.secret("fillout")):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Fillout bearer token")
        submission_root = payload.get("submission", {})
        nested_form_id = submission_root.get("formId") if isinstance(submission_root, dict) else None
        form_id = payload.get("formId") or payload.get("form_id") or nested_form_id
        if form_id != services.config.fillout_form_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fillout form ID does not match tenant")
        submission = fillout_submission(payload, services.config.fillout_field_ids)
        return coordinator.enqueue_submission(
            tenant,
            submission.model_dump(mode="json"),
            "fillout",
            force=True,
        )

    def telegram_route(chat_id: str) -> TelegramRoute | None:
        # Telegram callback volume is low. Resolve against current Baserow configuration
        # so changing a tenant chat ID does not require an API restart to invalidate a cache.
        return container().telegram_route(chat_id)

    @app.post("/webhooks/telegram")
    def telegram_webhook(
        payload: dict[str, object],
        x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        expected = configured.shared_telegram_webhook_secret()
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(expected, x_telegram_bot_api_secret_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Telegram webhook secret")
        callback = payload.get("callback_query")
        if not isinstance(callback, dict):
            return {"ok": True}
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else ""
        routed = telegram_route(chat_id) if chat_id else None
        if routed is None:
            return {"ok": True}
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        response_text = "Action ignored"
        try:
            if data.startswith("status:"):
                _, status_key, raw_row_id = data.split(":", 2)
                row_id = int(raw_row_id)
                routed.repository.set_status(row_id, status_key)
                response_text = "Status updated"
                if status_key == "dropped" and isinstance(message, dict):
                    message_id = message.get("message_id")
                    caption = message.get("caption")
                    job_url = _telegram_message_job_url(message)
                    if message_id is not None and isinstance(caption, str) and job_url:
                        try:
                            routed.notifier.edit_processing_message(
                                chat_id,
                                str(message_id),
                                caption=_dropped_processing_caption(caption),
                                job_url=job_url,
                                row_id=None,
                            )
                        except ProviderError as exc:
                            logger.warning(
                                "Telegram dropped-state message update failed",
                                extra={
                                    "tenant": routed.tenant,
                                    "chat_id": chat_id,
                                    "row_id": row_id,
                                    "error": str(exc),
                                },
                            )
            elif data.startswith("retry:"):
                run_id = UUID(data.split(":", 1)[1])
                run = run_store.get(run_id)
                if run is None or run.tenant != routed.tenant:
                    raise ValueError("Run does not belong to Telegram chat tenant")
                coordinator.retry(run_id, fresh=True)
                response_text = "Regeneration queued"
        except (KeyError, ValueError):
            response_text = "Action could not be applied"
        if callback_id:
            try:
                routed.notifier.answer_callback(callback_id, response_text)
            except ProviderError as exc:
                logger.warning(
                    "Telegram callback acknowledgement failed",
                    extra={
                        "tenant": routed.tenant,
                        "chat_id": chat_id,
                        "error": str(exc),
                    },
                )
        return {"ok": True}

    @app.post(
        "/v1/tenants/{tenant}/jobs",
        response_model=EnqueueResponse,
        status_code=202,
        dependencies=[Depends(operator)],
    )
    def submit_job(tenant: str, submission: JobSubmission, force: bool = False) -> EnqueueResponse:
        return coordinator.enqueue_submission(
            tenant,
            submission.model_dump(mode="json"),
            "manual",
            force=force,
        )

    @app.post(
        "/v1/tenants/{tenant}/discoveries",
        response_model=EnqueueResponse,
        status_code=202,
        dependencies=[Depends(operator)],
    )
    def submit_discovery(tenant: str) -> EnqueueResponse:
        return coordinator.enqueue_discovery(tenant)

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
        try:
            return coordinator.retry(run_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run or replay data not found") from exc

    return app
