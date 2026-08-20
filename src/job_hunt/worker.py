from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from pydantic import TypeAdapter
from redis import Redis

from job_hunt.application.discovery import build_search_url, normalize_discovery
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


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _store() -> RunStore:
    return RunStore(_redis(), settings.run_ttl_seconds)


def _state() -> RedisState:
    return RedisState(_redis())


def _notification_caption(job: Job, score: int | None) -> str:
    published = job.published_at.strftime("%B %d, %Y") if job.published_at else "Not specified"
    job_id = job.external_id or str(job.internal_id)
    match_score = f"{score}/100" if score is not None else "Not available"
    return "\n".join(
        [
            job.title,
            f"🏢 {job.company_name}",
            "",
            f"📍 Location: {job.location or 'Not specified'}",
            f"💼 Contract: {job.contract_type or 'Not specified'}",
            f"🎯 Match score: {match_score}",
            f"🆔 Job ID: {job_id}",
            f"🗓 Published: {published}",
        ]
    )


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
) -> dict[str, str]:
    store = _store()
    identifier = UUID(run_id)
    try:
        notifier = TelegramNotifier(
            settings.shared_telegram_token(),
            timeout_seconds=settings.telegram_request_timeout_seconds,
        )
        artifacts = [Path(path) for path in paths]
        if not artifacts:
            raise ValueError("No notification artifacts were provided")

        action_id = retry_transient(
            notifier.send_application_bundle,
            chat_id,
            artifacts,
            caption=caption,
            job_url=job_url,
            row_id=row_id,
            run_id=run_id,
        )
        store.update(
            identifier,
            notification={
                "state": "sent",
                "action_message_id": action_id,
                "artifact_count": len(artifacts),
            },
        )
        return {"state": "sent", "message_id": action_id}
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
            store.update(
                identifier,
                notification={
                    "state": "deferred",
                    "error": str(exc),
                    "retry_in_seconds": countdown,
                },
            )
            _defer_task(
                self,
                exc,
                tenant=tenant,
                run_id=run_id,
                stage="notification",
                retries=retries,
                countdown=countdown,
            )
        store.update(identifier, notification={"state": "failed", "error": str(exc)})
        _log_terminal_failure(
            "notification_failed",
            exc,
            tenant=tenant,
            run_id=run_id,
            stage="notification",
        )
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
        job = Job.model_validate(job_data)
        result = services.workflow.generate_documents(
            job,
            run_id=identifier,
            row_id=row_id,
            score=score,
            master_cv=services.context.master_cv,
            prompts=services.prompts,
            applicant_filename=services.config.applicant_filename,
        )
        store.update(
            identifier,
            state=RunState.SUCCEEDED,
            stage="complete",
            notification={"state": "queued"} if result.notification_paths else None,
            error=None,
        )
        if result.notification_paths:
            notify_documents.delay(
                tenant,
                run_id,
                list(result.notification_paths),
                _notification_caption(job, result.score),
                services.config.telegram_chat_id,
                result.row_id,
                job.url,
            )
        return {
            "state": RunState.SUCCEEDED,
            "row_id": result.row_id,
            "published": result.artifacts_published,
        }
    except Exception as exc:
        plan = _retry_plan(self, exc)
        if plan is not None and isinstance(exc, WorkflowError):
            retries, countdown = plan
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
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _log_terminal_failure(
            "document_generation_failed",
            exc,
            tenant=tenant,
            run_id=run_id,
            stage="documents",
        )
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
        parsed: Any = TypeAdapter(JobSubmission).validate_python(submission)
        job = services.normalizer.normalize(
            parsed,
            country=services.config.apify_proxy_country,
            max_concurrency=services.config.apify_max_concurrency,
        )

        lock = redis.lock(
            f"job-hunt:lock:{tenant}:{job.identity}",
            timeout=settings.job_lock_timeout_seconds,
            blocking_timeout=settings.job_lock_timeout_seconds,
        )
        if not lock.acquire(blocking=True):
            raise TimeoutError("Timed out waiting for an in-progress run of the same job")
        try:
            store.update(identifier, stage="qualification")
            qualification = services.workflow.persist_and_qualify(
                job,
                run_id=identifier,
                master_cv=services.context.master_cv,
                prompts=services.prompts,
                threshold=services.config.qualification_threshold,
                force=force,
            )
        finally:
            if lock.owned():
                lock.release()

        if not qualification.passed:
            store.update(identifier, state=RunState.SKIPPED, stage="complete", error=None)
            return {
                "state": RunState.SKIPPED,
                "row_id": qualification.row_id,
                "published": False,
            }

        store.update(identifier, state=RunState.RUNNING, stage="documents_queued", error=None)
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
        criteria = list(services.baserow.iter_rows(services.config.baserow_table_ids["searchCriteria"]))
        urls = [build_search_url(services.config.linkedin_base_search_url, row) for row in criteria]
        rows = services.discovery.discover(urls, max_items=services.config.linkedin_max_items)
        jobs = normalize_discovery(
            rows,
            services.config.company_exclusions,
            services.config.title_exclusions,
        )
        for job in jobs:
            child = RunStatus(tenant=tenant, kind="scheduled-job")
            checkpoint_namespace = str(child.run_id)
            store.save(child)
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
        store.update(
            identifier,
            state=RunState.SUCCEEDED,
            stage="dispatched",
            counts={"queued": len(jobs)},
            error=None,
        )
        return {"queued": len(jobs)}
    except Exception as exc:
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        _log_terminal_failure(
            "discovery_failed",
            exc,
            tenant=tenant,
            run_id=run_id,
            stage="discovery",
        )
        raise


@celery_app.task(name="job_hunt.dispatch_due_tenants", throws=(WorkflowError,))  # type: ignore[untyped-decorator]
def dispatch_due_tenants() -> dict[str, int]:
    redis = _redis()
    queued = 0
    failed = 0
    local_now = datetime.now(ZoneInfo(settings.scheduler_timezone)).replace(minute=0, second=0, microsecond=0)
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
