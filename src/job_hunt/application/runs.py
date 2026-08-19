from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from job_hunt.domain.models import EnqueueResponse, RetryResponse, RunStatus
from job_hunt.run_store import RunStore


class Queue(Protocol):
    def submission(
        self,
        tenant: str,
        payload: dict[str, Any],
        run_id: UUID,
        force: bool,
        snapshot_id: str | None = None,
    ) -> str: ...

    def discovery(self, tenant: str, run_id: UUID) -> str: ...


class RunCoordinator:
    def __init__(
        self,
        store: RunStore,
        queue: Queue,
        tenant_exists: Callable[[str], object],
    ) -> None:
        self.store = store
        self.queue = queue
        self.tenant_exists = tenant_exists

    def enqueue_submission(
        self,
        tenant: str,
        payload: dict[str, Any],
        kind: str,
        *,
        force: bool,
        snapshot_id: str | None = None,
    ) -> EnqueueResponse:
        self.tenant_exists(tenant)
        run = RunStatus(tenant=tenant, kind=kind)
        self.store.save(run)
        request: dict[str, object] = {
            "tenant": tenant,
            "payload": payload,
            "kind": kind,
            "force": force,
        }
        if snapshot_id:
            request["snapshot_id"] = snapshot_id
        self.store.save_request(run.run_id, request)
        task_id = self.queue.submission(
            tenant, payload, run.run_id, force, snapshot_id=snapshot_id
        )
        self.store.update(run.run_id, task_id=task_id)
        return EnqueueResponse(run_id=run.run_id)

    def enqueue_discovery(self, tenant: str) -> EnqueueResponse:
        self.tenant_exists(tenant)
        run = RunStatus(tenant=tenant, kind="discovery")
        self.store.save(run)
        self.store.save_request(run.run_id, {"tenant": tenant, "kind": "discovery"})
        task_id = self.queue.discovery(tenant, run.run_id)
        self.store.update(run.run_id, task_id=task_id)
        return EnqueueResponse(run_id=run.run_id)

    def retry(self, run_id: UUID) -> RetryResponse:
        original = self.store.get(run_id)
        request = self.store.get_request(run_id)
        if not original or not request:
            raise KeyError(str(run_id))
        retry = RunStatus(tenant=original.tenant, kind=original.kind, original_run_id=run_id)
        self.store.save(retry)
        self.store.save_request(retry.run_id, request)
        if request["kind"] == "discovery":
            task_id = self.queue.discovery(original.tenant, retry.run_id)
        else:
            task_id = self.queue.submission(
                original.tenant,
                dict(request["payload"]),  # type: ignore[arg-type]
                retry.run_id,
                bool(request.get("force", False)),
                snapshot_id=(str(request["snapshot_id"]) if request.get("snapshot_id") else None),
            )
        self.store.update(retry.run_id, task_id=task_id)
        return RetryResponse(original_run_id=run_id, run_id=retry.run_id)
