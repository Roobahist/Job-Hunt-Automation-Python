from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from pydantic import TypeAdapter
from redis import Redis

from job_hunt.application.discovery import build_search_url, normalize_discovery, search_criteria_active
from job_hunt.config import Settings, load_registry
from job_hunt.container import Container
from job_hunt.domain.models import Job, JobSubmission, RunState, RunStatus
from job_hunt.errors import ErrorKind, WorkflowError
from job_hunt.integrations.telegram import TelegramNotifier
from job_hunt.logging import configure_logging, logger
from job_hunt.retry import retry_transient
from job_hunt.run_store import RunStore
from job_hunt.state import RedisState

settings = Settings()
configure_logging(json_logs=settings.environment != "development")
celery_app = Celery("job_hunt", broker=settings.redis_url, backend=settings.redis_url)
celery_config: dict[str, object] = {
    "task_acks_late": True,
    "task_reject_on_worker_lost": True,
    "worker_prefetch_multiplier": 1,
    "result_expires": settings.run_ttl_seconds,
    "worker_send_task_events": True,
    "task_send_sent_event": True,
    "timezone": settings.scheduler_timezone,
    "enable_utc": True,
    "beat_schedule": {
        "dispatch-due-tenants": {
            "task": "job_hunt.dispatch_due_tenants",
            "schedule": crontab(minute=0),
        }
    },
    "task_routes": {
        "job_hunt.discover_tenant": {"queue": "fast"},
        "job_hunt.dispatch_due_tenants": {"queue": "fast"},
        "job_hunt.process_submission": {"queue": "fast"},
        "job_hunt.generate_documents": {"queue": "documents"},
        "job_hunt.init_job_notification": {"queue": "notifications"},
        "job_hunt.update_job_notification": {"queue": "notifications"},
        "job_hunt.notify_documents": {"queue": "notifications"},
    },
    "task_default_queue": "fast",
}
if settings.task_soft_time_limit_seconds > 0:
    celery_config["task_soft_time_limit"] = settings.task_soft_time_limit_seconds
if settings.task_time_limit_seconds > 0:
    celery_config["task_time_limit"] = settings.task_time_limit_seconds
celery_app.conf.update(**celery_config)

_TASK_MAX_RETRIES = settings.task_max_retries
_RATE_LIMIT_FALLBACK_SECONDS = settings.rate_limit_fallback_seconds
_TRANSIENT_BASE_DELAY_SECONDS = settings.transient_base_delay_seconds
_TRANSIENT_MAX_DELAY_SECONDS = settings.transient_max_delay_seconds

_STAGE_ORDER = (
    "discovery",
    "normalization",
    "persistence",
    "qualification",
    "documents_queue",
    "document_check",
    "tailoring",
    "rendering",
    "artifact_upload",
    "notification_queue",
    "telegram_finalization",
)
_STAGE_LABELS = {
    "discovery": "Discovered",
    "normalization": "Normalize job",
    "persistence": "Persist to Baserow",
    "qualification": "Qualification",
    "documents_queue": "Waiting for document worker",
    "document_check": "Check current Baserow status",
    "tailoring": "Tailor CV and cover letter",
    "rendering": "Render application files",
    "artifact_upload": "Upload artifacts",
    "notification_queue": "Waiting for Telegram worker",
    "telegram_finalization": "Finalize Telegram message",
}


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _store() -> RunStore:
    return RunStore(_redis(), settings.run_ttl_seconds)


def _state() -> RedisState:
    return RedisState(_redis())


def _notifier() -> TelegramNotifier:
    return TelegramNotifier(
        settings.shared_telegram_token(),
        timeout_seconds=settings.telegram_request_timeout_seconds,
    )


def _notification_caption(
    job: Job,
    score: int | None,
    notification: dict[str, object] | None = None,
) -> str:
    published = job.published_at.strftime("%B %d, %Y") if job.published_at else "Not specified"
    job_id = job.external_id or str(job.internal_id)
    match_score = f"{score}/100" if score is not None else "Pending"
    meta = notification or {}
    current_stage = str(meta.get("current_stage") or "discovery")
    processing_state = str(meta.get("processing_state") or "processing")
    state_label = {
        "complete": "✅ Complete",
        "dropped": "⛔ Dropped",
        "failed": "❌ Failed",
        "deferred": "⏳ Waiting to retry",
    }.get(processing_state, f"⚙️ {_STAGE_LABELS.get(current_stage, current_stage)}")
    lines = [
        job.title,
        f"🏢 {job.company_name}",
        "",
        f"📍 Location: {job.location or 'Not specified'}",
        f"💼 Contract: {job.contract_type or 'Not specified'}",
        f"🎯 Match score: {match_score}",
        f"🆔 Job ID: {job_id}",
        f"🗓 Published: {published}",
        "",
        f"Status: {state_label}",
    ]
    timeline = meta.get("timeline")
    if isinstance(timeline, dict):
        for stage in _STAGE_ORDER:
            raw = timeline.get(stage)
            if not isinstance(raw, dict):
                continue
            label = _STAGE_LABELS.get(stage, stage)
            duration = raw.get("duration_seconds")
            if isinstance(duration, (int, float)):
                lines.append(f"✓ {label}: {float(duration):.1f}s")
                continue
            started = raw.get("started_at")
            if stage == current_stage and isinstance(started, str):
                try:
                    local = datetime.fromisoformat(started).astimezone(ZoneInfo(settings.scheduler_timezone))
                    lines.append(f"▶ {label}: started {local.strftime('%I:%M:%S %p').lstrip('0')}")
                except ValueError:
                    lines.append(f"▶ {label}: running")
    retry_in = meta.get("retry_in_seconds")
    if processing_state == "deferred" and isinstance(retry_in, (int, float)):
        lines.append(f"Retry in: {int(retry_in)}s")
    error_message = meta.get("error_message")
    if processing_state == "failed" and isinstance(error_message, str):
        lines.append(f"Error: {error_message[:160]}")
    return "\n".join(lines)[:1024]


def _retry_countdown(exc: WorkflowError, retries: int) -> int:
    if exc.retry_after is not None and exc.retry_after > 0:
        return max(1, min(_TRANSIENT_MAX_DELAY_SECONDS, int(exc.retry_after)))
    if exc.kind == ErrorKind.RATE_LIMIT:
        return _RATE_LIMIT_FALLBACK_SECONDS
    exponential = _TRANSIENT_BASE_DELAY_SECONDS * (2**retries)
    return max(1, min(_TRANSIENT_MAX_DELAY_SECONDS, exponential))


def _retry_plan(task: Any, exc: Exception) -> tuple[int, int] | None:
    if not isinstance(exc, WorkflowError) or not exc.retryable:
        return None
    retries = int(getattr(task.request, "retries", 0))
    if retries >= _TASK_MAX_RETRIES:
        return None
    return retries, _retry_countdown(exc, retries)


def _defer_task(
    task: Any,
    exc: WorkflowError,
    *,
    tenant: str,
    run_id: str,
    stage: str,
    retries: int,
    countdown: int,
) -> NoReturn:
    logger().warning(
        "task_deferred",
        tenant=tenant,
        run_id=run_id,
        stage=stage,
        error_kind=exc.kind,
        reason=str(exc),
        retry_in_seconds=countdown,
        retry_number=retries + 1,
        max_retries=_TASK_MAX_RETRIES,
    )
    raise task.retry(exc=exc, countdown=countdown, max_retries=_TASK_MAX_RETRIES)


def _log_terminal_failure(event: str, exc: Exception, **context: object) -> None:
    if isinstance(exc, WorkflowError):
        logger().error(
            event,
            **context,
            error_type=type(exc).__name__,
            error_kind=exc.kind,
            error_message=str(exc),
            retryable=exc.retryable,
        )
        return
    logger().exception(event, **context, error_type=type(exc).__name__)


def _automatic_discovery_slot(now: datetime, interval_hours: int) -> str | None:
    local = now.astimezone(ZoneInfo(settings.scheduler_timezone))
    if local.minute != 0 or local.hour % interval_hours != 0:
        return None
    return local.strftime("%Y-%m-%dT%H%z")


def _progress_start(
    tenant: str,
    run_id: UUID,
    job: Job,
    chat_id: str,
    stage: str,
    *,
    row_id: int | None = None,
    score: int | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    fields: dict[str, object] = {
        "processing_state": "processing",
        "current_stage": stage,
        "retry_in_seconds": None,
        "timeline": {stage: {"started_at": now}},
    }
    if row_id is not None:
        fields["row_id"] = row_id
    if score is not None:
        fields["score"] = score
    _store().merge_notification(run_id, **fields)
    update_job_notification.delay(tenant, str(run_id), job.model_dump(mode="json"), chat_id)


def _progress_finish(run_id: UUID, stage: str) -> None:
    store = _store()
    status = store.get(run_id)
    started_at: str | None = None
    if status and isinstance(status.notification, dict):
        timeline = status.notification.get("timeline")
        if isinstance(timeline, dict):
            entry = timeline.get(stage)
            if isinstance(entry, dict) and isinstance(entry.get("started_at"), str):
                started_at = str(entry["started_at"])
    now = datetime.now(UTC)
    stage_update: dict[str, object] = {"completed_at": now.isoformat()}
    if started_at is not None:
        try:
            started = datetime.fromisoformat(started_at)
            stage_update["duration_seconds"] = max(0.0, (now - started).total_seconds())
        except ValueError:
            pass
    store.merge_notification(run_id, timeline={stage: stage_update})


def _progress_deferred(
    tenant: str,
    run_id: UUID,
    job: Job | None,
    chat_id: str | None,
    stage: str,
    countdown: int,
) -> None:
    _store().merge_notification(
        run_id,
        processing_state="deferred",
        current_stage=stage,
        retry_in_seconds=countdown,
    )
    if job is not None and chat_id is not None:
        update_job_notification.delay(tenant, str(run_id), job.model_dump(mode="json"), chat_id)


def _progress_terminal(
    tenant: str,
    run_id: UUID,
    job: Job | None,
    chat_id: str | None,
    state: str,
    *,
    row_id: int | None = None,
    score: int | None = None,
    error_message: str | None = None,
) -> None:
    fields: dict[str, object] = {"processing_state": state, "current_stage": state}
    if row_id is not None:
        fields["row_id"] = row_id
    if score is not None:
        fields["score"] = score
    if error_message is not None:
        fields["error_message"] = error_message
    _store().merge_notification(run_id, **fields)
    if job is not None and chat_id is not None:
        update_job_notification.delay(tenant, str(run_id), job.model_dump(mode="json"), chat_id)


def _ensure_notification(tenant: str, run_id: UUID, job: Job, chat_id: str) -> None:
    store = _store()
    status = store.get(run_id)
    notification = status.notification if status and isinstance(status.notification, dict) else {}
    if notification.get("init_queued"):
        return
    store.merge_notification(run_id, init_queued=True)
    init_job_notification.delay(tenant, str(run_id), job.model_dump(mode="json"), chat_id)


@celery_app.task(
    name="job_hunt.init_job_notification",
    bind=True,
    max_retries=_TASK_MAX_RETRIES,
    throws=(WorkflowError,),
)  # type: ignore[untyped-decorator]
def init_job_notification(
    self: Any,
    tenant: str,
    run_id: str,
    job_data: dict[str, Any],
    chat_id: str,
) -> dict[str, str]:
    identifier = UUID(run_id)
    store = _store()
    try:
        status = store.get(identifier)
        notification = status.notification if status and isinstance(status.notification, dict) else {}
        existing = notification.get("message_id")
        if existing:
            return {"state": "exists", "message_id": str(existing)}
        job = Job.model_validate(job_data)
        score = notification.get("score") if isinstance(notification.get("score"), int) else None
        row_id = notification.get("row_id") if isinstance(notification.get("row_id"), int) else None
        caption = _notification_caption(job, score, notification)
        message_id = _notifier().send_processing_message(
            chat_id,
            caption=caption,
            job_url=job.url,
            row_id=row_id,
        )
        store.merge_notification(
            identifier,
            message_id=message_id,
            state="processing",
            last_caption=caption,
        )
        return {"state": "sent", "message_id": message_id}
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
            _defer_task(
                self,
                exc,
                tenant=tenant,
                run_id=run_id,
                stage="telegram_init",
                retries=retries,
                countdown=countdown,
            )
        logger().warning("telegram_progress_init_failed", tenant=tenant, run_id=run_id, error=str(exc))
        return {"state": "failed", "message_id": ""}


@celery_app.task(
    name="job_hunt.update_job_notification",
    bind=True,
    max_retries=_TASK_MAX_RETRIES,
    throws=(WorkflowError,),
)  # type: ignore[untyped-decorator]
def update_job_notification(
    self: Any,
    tenant: str,
    run_id: str,
    job_data: dict[str, Any],
    chat_id: str,
) -> dict[str, str]:
    identifier = UUID(run_id)
    store = _store()
    try:
        status = store.get(identifier)
        notification = status.notification if status and isinstance(status.notification, dict) else {}
        job = Job.model_validate(job_data)
        score = notification.get("score") if isinstance(notification.get("score"), int) else None
        row_id = notification.get("row_id") if isinstance(notification.get("row_id"), int) else None
        message_id = notification.get("message_id")
        if not message_id:
            return {"state": "pending_init", "message_id": ""}
        caption = _notification_caption(job, score, notification)
        if notification.get("last_caption") == caption:
            return {"state": "unchanged", "message_id": str(message_id)}
        _notifier().edit_processing_message(
            chat_id,
            str(message_id),
            caption=caption,
            job_url=job.url,
            row_id=row_id,
        )
        store.merge_notification(identifier, last_caption=caption)
        return {"state": "updated", "message_id": str(message_id)}
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
            _defer_task(
                self,
                exc,
                tenant=tenant,
                run_id=run_id,
                stage="telegram_progress",
                retries=retries,
                countdown=countdown,
            )
        logger().warning("telegram_progress_update_failed", tenant=tenant, run_id=run_id, error=str(exc))
        return {"state": "failed", "message_id": ""}


@celery_app.task(
    name="job_hunt.notify_documents",
    bind=True,
    max_retries=_TASK_MAX_RETRIES,
    throws=(WorkflowError,),
)  # type: ignore[untyped-decorator]
def notify_documents(
    self: Any,
    tenant: str,
    run_id: str,
    paths: list[str],
    caption: str,
    chat_id: str,
    row_id: int,
    job_url: str,
    job_data: dict[str, Any] | None = None,
) -> dict[str, str]:
    store = _store()
    identifier = UUID(run_id)
    job = Job.model_validate(job_data) if job_data is not None else None
    try:
        _progress_finish(identifier, "notification_queue")
        if job is not None:
            _progress_start(tenant, identifier, job, chat_id, "telegram_finalization", row_id=row_id)
        artifacts = [Path(path) for path in paths]
        archives = [path for path in artifacts if path.suffix.casefold() == ".zip"]
        if len(archives) != 1:
            raise ValueError("Final Telegram notification requires exactly one ZIP archive")
        status = store.get(identifier)
        notification = status.notification if status and isinstance(status.notification, dict) else {}
        message_id = notification.get("message_id")
        score = notification.get("score") if isinstance(notification.get("score"), int) else None
        notifier = _notifier()
        if message_id and job is not None:
            action_id = notifier.finalize_processing_message(
                chat_id,
                str(message_id),
                archives[0],
                caption=_notification_caption(job, score, notification),
                job_url=job_url,
                row_id=row_id,
                run_id=run_id,
            )
        else:
            action_id = retry_transient(
                notifier.send_application_bundle,
                chat_id,
                artifacts,
                caption=caption,
                job_url=job_url,
                row_id=row_id,
                run_id=run_id,
            )
        _progress_finish(identifier, "telegram_finalization")
        store.merge_notification(
            identifier,
            processing_state="complete",
            current_stage="complete",
            state="sent",
            message_id=action_id,
            action_message_id=action_id,
            artifact_count=1,
            row_id=row_id,
        )
        final_caption = caption
        if job is not None:
            final_status = store.get(identifier)
            final_notification = final_status.notification if final_status and isinstance(final_status.notification, dict) else {}
            final_score = final_notification.get("score") if isinstance(final_notification.get("score"), int) else None
            final_caption = _notification_caption(job, final_score, final_notification)
            notifier.edit_final_caption(
                chat_id,
                action_id,
                caption=final_caption,
                job_url=job_url,
                row_id=row_id,
                run_id=run_id,
            )
        store.merge_notification(identifier, last_caption=final_caption)
        store.update(identifier, state=RunState.SUCCEEDED, stage="complete", error=None)
        return {"state": "sent", "message_id": action_id}
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
            _progress_deferred(tenant, identifier, job, chat_id, "telegram_finalization", countdown)
            _defer_task(
                self,
                exc,
                tenant=tenant,
                run_id=run_id,
                stage="notification",
                retries=retries,
                countdown=countdown,
            )
        store.merge_notification(
            identifier,
            state="failed",
            processing_state="failed",
            error_message=str(exc),
        )
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _log_terminal_failure("notification_failed", exc, tenant=tenant, run_id=run_id, stage="notification")
        raise


@celery_app.task(
    name="job_hunt.generate_documents",
    bind=True,
    max_retries=_TASK_MAX_RETRIES,
    throws=(WorkflowError,),
)  # type: ignore[untyped-decorator]
def generate_documents(
    self: Any,
    tenant: str,
    job_data: dict[str, Any],
    run_id: str,
    row_id: int,
    score: int,
    snapshot_id: str | None = None,
    checkpoint_namespace: str | None = None,
) -> dict[str, Any]:
    store = _store()
    identifier = UUID(run_id)
    job = Job.model_validate(job_data)
    chat_id: str | None = None
    store.update(
        identifier,
        state=RunState.RUNNING,
        stage="documents",
        task_id=self.request.id,
        error=None,
    )
    try:
        snapshot = _state().get_snapshot(snapshot_id) if snapshot_id else None
        services = Container(settings).tenant(
            tenant,
            snapshot=snapshot,
            checkpoint_namespace=checkpoint_namespace or run_id,
        )
        chat_id = services.config.telegram_chat_id
        store.merge_notification(identifier, row_id=row_id, score=score)
        _progress_finish(identifier, "documents_queue")
        _progress_start(
            tenant,
            identifier,
            job,
            chat_id,
            "document_check",
            row_id=row_id,
            score=score,
        )

        def progress(stage: str, event: str) -> None:
            if event == "start":
                if stage == "tailoring":
                    _progress_finish(identifier, "document_check")
                _progress_start(
                    tenant,
                    identifier,
                    job,
                    chat_id or "",
                    stage,
                    row_id=row_id,
                    score=score,
                )
            else:
                _progress_finish(identifier, stage)

        result = services.workflow.generate_documents(
            job,
            run_id=identifier,
            row_id=row_id,
            score=score,
            master_cv=services.context.master_cv,
            prompts=services.prompts,
            applicant_filename=services.config.applicant_filename,
            progress=progress,
        )
        if not result.passed:
            _progress_finish(identifier, "document_check")
            _progress_terminal(
                tenant,
                identifier,
                job,
                chat_id,
                "dropped",
                row_id=row_id,
                score=score,
            )
            store.update(identifier, state=RunState.SKIPPED, stage="complete", error=None)
            return {"state": RunState.SKIPPED, "row_id": row_id, "published": False}

        _progress_start(
            tenant,
            identifier,
            job,
            chat_id,
            "notification_queue",
            row_id=row_id,
            score=score,
        )
        store.update(identifier, state=RunState.RUNNING, stage="notification_queued", error=None)
        if result.notification_paths:
            notify_documents.delay(
                tenant,
                run_id,
                list(result.notification_paths),
                _notification_caption(job, result.score),
                chat_id,
                result.row_id,
                job.url,
                job.model_dump(mode="json"),
            )
        return {
            "state": "notification_queued",
            "row_id": result.row_id,
            "published": result.artifacts_published,
        }
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
            _progress_deferred(tenant, identifier, job, chat_id, "documents", countdown)
            store.update(
                identifier,
                state=RunState.RUNNING,
                stage="documents_deferred",
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "retry_in_seconds": countdown,
                },
            )
            _defer_task(
                self,
                exc,
                tenant=tenant,
                run_id=run_id,
                stage="documents",
                retries=retries,
                countdown=countdown,
            )
        _progress_terminal(
            tenant,
            identifier,
            job,
            chat_id,
            "failed",
            row_id=row_id,
            score=score,
            error_message=str(exc),
        )
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _log_terminal_failure("document_generation_failed", exc, tenant=tenant, run_id=run_id, stage="documents")
        raise


@celery_app.task(
    name="job_hunt.process_submission",
    bind=True,
    max_retries=_TASK_MAX_RETRIES,
    throws=(WorkflowError,),
)  # type: ignore[untyped-decorator]
def process_submission(
    self: Any,
    tenant: str,
    submission: dict[str, Any],
    run_id: str,
    force: bool = False,
    snapshot_id: str | None = None,
    checkpoint_namespace: str | None = None,
) -> dict[str, Any]:
    store = _store()
    identifier = UUID(run_id)
    redis = store.redis
    job: Job | None = None
    chat_id: str | None = None
    store.update(
        identifier,
        state=RunState.RUNNING,
        stage="normalization",
        task_id=self.request.id,
        error=None,
    )
    try:
        snapshot = _state().get_snapshot(snapshot_id) if snapshot_id else None
        services = Container(settings).tenant(
            tenant,
            snapshot=snapshot,
            checkpoint_namespace=checkpoint_namespace or run_id,
        )
        chat_id = services.config.telegram_chat_id
        parsed: Any = TypeAdapter(JobSubmission).validate_python(submission)
        now = datetime.now(UTC).isoformat()
        store.merge_notification(
            identifier,
            processing_state="processing",
            current_stage="normalization",
            timeline={"normalization": {"started_at": now}},
        )
        job = services.normalizer.normalize(
            parsed,
            country=services.config.apify_proxy_country,
            max_concurrency=services.config.apify_max_concurrency,
        )
        _progress_finish(identifier, "normalization")

        lock = redis.lock(
            f"job-hunt:lock:{tenant}:{job.identity}",
            timeout=settings.job_lock_timeout_seconds,
            blocking_timeout=settings.job_lock_timeout_seconds,
        )
        if not lock.acquire(blocking=True):
            raise TimeoutError("Timed out waiting for an in-progress run of the same job")
        try:
            store.update(identifier, stage="qualification")

            def progress(stage: str, event: str) -> None:
                if event == "start":
                    _progress_start(tenant, identifier, job, chat_id or "", stage)
                else:
                    _progress_finish(identifier, stage)

            def persisted(row_id: int) -> None:
                # The row must exist before Telegram is allowed to expose the job.
                store.merge_notification(identifier, row_id=row_id)
                _ensure_notification(tenant, identifier, job, chat_id or "")

            qualification = services.workflow.persist_and_qualify(
                job,
                run_id=identifier,
                master_cv=services.context.master_cv,
                prompts=services.prompts,
                threshold=services.config.qualification_threshold,
                force=force,
                progress=progress,
                persisted=persisted,
            )
        finally:
            if lock.owned():
                lock.release()

        store.merge_notification(identifier, row_id=qualification.row_id, score=qualification.score)
        update_job_notification.delay(tenant, run_id, job.model_dump(mode="json"), chat_id)
        if not qualification.passed:
            _progress_terminal(
                tenant,
                identifier,
                job,
                chat_id,
                "dropped",
                row_id=qualification.row_id,
                score=qualification.score,
            )
            store.update(identifier, state=RunState.SKIPPED, stage="complete", error=None)
            return {
                "state": RunState.SKIPPED,
                "row_id": qualification.row_id,
                "published": False,
            }

        if qualification.score is None:
            raise RuntimeError("Passing qualification result must include a score")

        store.update(identifier, state=RunState.RUNNING, stage="documents_queued", error=None)
        _progress_start(
            tenant,
            identifier,
            job,
            chat_id,
            "documents_queue",
            row_id=qualification.row_id,
            score=qualification.score,
        )
        generate_documents.delay(
            tenant,
            job.model_dump(mode="json"),
            run_id,
            qualification.row_id,
            qualification.score,
            snapshot_id,
            checkpoint_namespace or run_id,
        )
        return {
            "state": "documents_queued",
            "row_id": qualification.row_id,
            "published": False,
        }
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
            _progress_deferred(tenant, identifier, job, chat_id, "qualification", countdown)
            store.update(
                identifier,
                state=RunState.RUNNING,
                stage="qualification_deferred",
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "retry_in_seconds": countdown,
                },
            )
            _defer_task(
                self,
                exc,
                tenant=tenant,
                run_id=run_id,
                stage="qualification",
                retries=retries,
                countdown=countdown,
            )
        _progress_terminal(
            tenant,
            identifier,
            job,
            chat_id,
            "failed",
            error_message=str(exc),
        )
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _log_terminal_failure("job_failed", exc, tenant=tenant, run_id=run_id, stage="worker")
        raise


@celery_app.task(name="job_hunt.discover_tenant", throws=(WorkflowError,))  # type: ignore[untyped-decorator]
def discover_tenant(tenant: str, run_id: str) -> dict[str, int]:
    store = _store()
    identifier = UUID(run_id)
    try:
        store.update(identifier, state=RunState.RUNNING, stage="discovery", error=None)
        services = Container(settings).tenant(tenant)
        snapshot_id = run_id
        _state().set_snapshot(
            snapshot_id,
            services.snapshot(),
            ttl_seconds=settings.discovery_snapshot_ttl_seconds,
        )
        all_criteria = list(services.baserow.iter_rows(services.config.baserow_table_ids["searchCriteria"]))
        criteria = [row for row in all_criteria if search_criteria_active(row)]
        urls = [build_search_url(services.config.linkedin_base_search_url, row) for row in criteria]
        rows = services.discovery.discover(urls, max_items=services.config.linkedin_max_items)
        jobs = normalize_discovery(
            rows,
            services.config.company_exclusions,
            services.config.title_exclusions,
        )

        new_jobs: list[Job] = []
        duplicates = 0
        for job in jobs:
            existing = retry_transient(services.repository.find, job)
            if existing is not None:
                duplicates += 1
                logger().info(
                    "discovery_duplicate_skipped",
                    tenant=tenant,
                    run_id=run_id,
                    job_identity=job.identity,
                    row_id=existing.get("id"),
                )
                continue
            new_jobs.append(job)

        for job in new_jobs:
            child = RunStatus(tenant=tenant, kind="scheduled-job")
            checkpoint_namespace = str(child.run_id)
            store.save(child)
            now = datetime.now(UTC).isoformat()
            store.merge_notification(
                child.run_id,
                processing_state="processing",
                current_stage="discovery",
                timeline={
                    "discovery": {
                        "started_at": now,
                        "completed_at": now,
                        "duration_seconds": 0.0,
                    }
                },
            )
            payload = {
                "entry_type": "external",
                "source": job.source,
                "external_job_id": job.external_id,
                "company_name": job.company_name,
                "job_title": job.title,
                "job_description": job.description,
                "job_url": job.url,
                "location": job.location,
                "contract_type": job.contract_type,
                "published_at": job.published_at.isoformat() if job.published_at else None,
            }
            store.save_request(
                child.run_id,
                {
                    "tenant": tenant,
                    "payload": payload,
                    "kind": "scheduled-job",
                    "force": False,
                    "snapshot_id": snapshot_id,
                    "checkpoint_namespace": checkpoint_namespace,
                },
            )
            process_submission.delay(
                tenant,
                payload,
                str(child.run_id),
                False,
                snapshot_id,
                checkpoint_namespace,
            )
        counts = {
            "criteria_total": len(all_criteria),
            "criteria_active": len(criteria),
            "discovered": len(jobs),
            "duplicates": duplicates,
            "queued": len(new_jobs),
        }
        store.update(
            identifier,
            state=RunState.SUCCEEDED,
            stage="dispatched",
            counts=counts,
            error=None,
        )
        logger().info("discovery_dispatched", tenant=tenant, run_id=run_id, **counts)
        return counts
    except Exception as exc:
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _log_terminal_failure("discovery_failed", exc, tenant=tenant, run_id=run_id, stage="discovery")
        raise


@celery_app.task(name="job_hunt.dispatch_due_tenants", throws=(WorkflowError,))  # type: ignore[untyped-decorator]
def dispatch_due_tenants() -> dict[str, int]:
    redis = _redis()
    queued = 0
    failed = 0
    local_now = datetime.now(ZoneInfo(settings.scheduler_timezone)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    for key, bootstrap in load_registry(settings.registry_path).items():
        if not bootstrap.enabled:
            continue
        try:
            services = Container(settings).tenant(key)
            interval_hours = services.config.linkedin_schedule_interval_hours
            slot = _automatic_discovery_slot(local_now, interval_hours)
            if slot is None:
                continue
            due_key = f"job-hunt:last-auto-discovery-slot:{key}"
            if redis.get(due_key) == slot:
                continue
            redis.set(due_key, slot)
            run = RunStatus(tenant=key, kind="discovery")
            _store().save(run)
            discover_tenant.delay(key, str(run.run_id))
            queued += 1
            logger().info(
                "automatic_discovery_queued",
                tenant=key,
                run_id=str(run.run_id),
                schedule_timezone=settings.scheduler_timezone,
                schedule_slot=slot,
                interval_hours=interval_hours,
            )
        except Exception as exc:
            failed += 1
            _log_terminal_failure("tenant_dispatch_failed", exc, tenant=key, stage="dispatch")
    return {"queued": queued, "failed": failed}
