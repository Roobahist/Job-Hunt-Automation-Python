from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]
from pydantic import TypeAdapter
from redis import Redis

from job_hunt.application.discovery import build_search_url, normalize_discovery
from job_hunt.config import Settings, load_registry
from job_hunt.container import Container
from job_hunt.domain.models import JobSubmission, RunState, RunStatus
from job_hunt.integrations.telegram import TelegramNotifier
from job_hunt.logging import configure_logging, logger
from job_hunt.retry import retry_transient
from job_hunt.run_store import RunStore
from job_hunt.state import RedisState

settings = Settings()
configure_logging(json_logs=settings.environment != "development")
celery_app = Celery("job_hunt", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=settings.run_ttl_seconds,
    worker_send_task_events=True,
    task_send_sent_event=True,
    beat_schedule={"dispatch-due-tenants": {"task": "job_hunt.dispatch_due_tenants", "schedule": 3600.0}},
)


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _store() -> RunStore:
    return RunStore(_redis(), settings.run_ttl_seconds)


def _state() -> RedisState:
    return RedisState(_redis())


@celery_app.task(name="job_hunt.notify_documents", bind=True, max_retries=3)  # type: ignore[untyped-decorator]
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
        notifier = TelegramNotifier(settings.shared_telegram_token())
        artifacts = [Path(path) for path in paths]
        if not artifacts:
            raise ValueError("No notification artifacts were provided")

        media_id: str | None = None
        pdfs = artifacts[1:]
        if len(pdfs) >= 2:
            media_id = retry_transient(notifier.send_documents, chat_id, pdfs, "")

        action_id = retry_transient(
            notifier.send_document_with_actions,
            chat_id,
            artifacts[0],
            caption=caption,
            job_url=job_url,
            row_id=row_id,
            run_id=run_id,
        )
        store.update(
            identifier,
            notification={
                "state": "sent",
                "media_message_id": media_id,
                "action_message_id": action_id,
            },
        )
        return {"state": "sent", "message_id": action_id}
    except Exception as exc:
        store.update(
            identifier,
            notification={"state": "failed", "error": str(exc)},
        )
        logger().exception(
            "notification_failed",
            tenant=tenant,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        raise self.retry(exc=exc, countdown=min(60, 2 ** int(self.request.retries))) from exc


@celery_app.task(name="job_hunt.process_submission", bind=True)  # type: ignore[untyped-decorator]
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
    store.update(identifier, state=RunState.RUNNING, stage="normalization", task_id=self.request.id)
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
            timeout=1800,
            blocking_timeout=1800,
        )
        if not lock.acquire(blocking=True):
            raise TimeoutError("Timed out waiting for an in-progress run of the same job")
        try:
            store.update(identifier, stage="workflow")
            result = services.workflow.process(
                job,
                run_id=identifier,
                master_cv=services.context.master_cv,
                prompts=services.prompts,
                threshold=services.config.qualification_threshold,
                force=force,
                applicant_filename=services.config.applicant_filename,
            )
        finally:
            if lock.owned():
                lock.release()

        final_state = RunState.SUCCEEDED if result.passed else RunState.SKIPPED
        store.update(
            identifier,
            state=final_state,
            stage="complete",
            notification={"state": "queued"} if result.notification_paths else None,
        )
        if result.notification_paths:
            notify_documents.delay(
                tenant,
                run_id,
                list(result.notification_paths),
                f"{job.title} at {job.company_name}",
                services.config.telegram_chat_id,
                result.row_id,
                job.url,
            )
        return {
            "state": final_state,
            "row_id": result.row_id,
            "published": result.artifacts_published,
        }
    except Exception as exc:
        logger().exception(
            "job_failed",
            tenant=tenant,
            run_id=run_id,
            stage="worker",
            error_type=type(exc).__name__,
        )
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        raise


@celery_app.task(name="job_hunt.discover_tenant")  # type: ignore[untyped-decorator]
def discover_tenant(tenant: str, run_id: str) -> dict[str, int]:
    store = _store()
    identifier = UUID(run_id)
    try:
        store.update(identifier, state=RunState.RUNNING, stage="discovery")
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
        jobs = normalize_discovery(rows, services.config.company_exclusions, services.config.title_exclusions)
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
                "published_at": (job.published_at.isoformat() if job.published_at else None),
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
        )
        return {"queued": len(jobs)}
    except Exception as exc:
        logger().exception(
            "discovery_failed",
            tenant=tenant,
            run_id=run_id,
            error_type=type(exc).__name__,
        )
        store.update(
            identifier,
            state=RunState.FAILED,
            stage="failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        raise


@celery_app.task(name="job_hunt.dispatch_due_tenants")  # type: ignore[untyped-decorator]
def dispatch_due_tenants() -> dict[str, int]:
    redis = _redis()
    queued = 0
    failed = 0
    for key, bootstrap in load_registry(settings.registry_path).items():
        if not bootstrap.enabled:
            continue
        try:
            services = Container(settings).tenant(key)
            due_key = f"job-hunt:last-discovery:{key}"
            last = redis.get(due_key)
            interval = services.config.linkedin_schedule_interval_hours * 3600
            now = int(datetime.now(UTC).timestamp())
            if last and now - int(str(last)) < interval:
                continue
            redis.set(due_key, now)
            run = RunStatus(tenant=key, kind="discovery")
            _store().save(run)
            discover_tenant.delay(key, str(run.run_id))
            queued += 1
        except Exception as exc:
            failed += 1
            logger().exception("tenant_dispatch_failed", tenant=key, error_type=type(exc).__name__)
    return {"queued": queued, "failed": failed}
