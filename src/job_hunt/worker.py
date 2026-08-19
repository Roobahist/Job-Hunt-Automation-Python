from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Celery  # type: ignore[import-untyped]
from pydantic import TypeAdapter
from redis import Redis

from job_hunt.application.discovery import build_search_url, normalize_discovery
from job_hunt.config import Settings, load_registry
from job_hunt.container import Container
from job_hunt.domain.models import JobSubmission, RunState, RunStatus
from job_hunt.logging import configure_logging, logger
from job_hunt.run_store import RunStore

settings = Settings()
configure_logging(json_logs=settings.environment != "development")
celery_app = Celery("job_hunt", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=settings.run_ttl_seconds,
    beat_schedule={
        "dispatch-due-tenants": {"task": "job_hunt.dispatch_due_tenants", "schedule": 3600.0}
    },
)


def _store() -> RunStore:
    return RunStore(
        Redis.from_url(settings.redis_url, decode_responses=True), settings.run_ttl_seconds
    )


@celery_app.task(name="job_hunt.process_submission", bind=True)  # type: ignore[untyped-decorator]
def process_submission(
    self: Any, tenant: str, submission: dict[str, Any], run_id: str, force: bool = False
) -> dict[str, Any]:
    store = _store()
    identifier = UUID(run_id)
    store.update(identifier, state=RunState.RUNNING, stage="normalization", task_id=self.request.id)
    try:
        services = Container(settings).tenant(tenant)
        parsed: Any = TypeAdapter(JobSubmission).validate_python(submission)
        job = services.normalizer.normalize(
            parsed,
            country=services.config.apify_proxy_country,
            max_concurrency=services.config.apify_max_concurrency,
        )
        redis = store.redis
        lock = redis.lock(
            f"job-hunt:lock:{tenant}:{job.identity}", timeout=1800, blocking_timeout=1
        )
        if not lock.acquire(blocking=True):
            store.update(identifier, state=RunState.SKIPPED, stage="duplicate")
            return {"state": "skipped", "reason": "duplicate in progress"}
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
                cloudinary_folder=services.config.cloudinary_folder_prefix,
                cloudinary_tags=services.config.cloudinary_tags,
                telegram_chat_id=services.config.telegram_chat_id,
            )
        finally:
            if lock.owned():
                lock.release()
        final_state = RunState.SUCCEEDED if result.passed else RunState.SKIPPED
        store.update(identifier, state=final_state, stage="complete")
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
        criteria = list(
            services.baserow.iter_rows(services.config.baserow_table_ids["searchCriteria"])
        )
        urls = [build_search_url(services.config.linkedin_base_search_url, row) for row in criteria]
        rows = services.discovery.discover(urls, max_items=services.config.linkedin_max_items)
        jobs = normalize_discovery(
            rows, services.config.company_exclusions, services.config.title_exclusions
        )
        for job in jobs:
            child = RunStatus(tenant=tenant, kind="scheduled-job")
            store.save(child)
            process_submission.delay(
                tenant,
                {
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
                },
                str(child.run_id),
                False,
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
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
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
