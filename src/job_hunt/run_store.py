from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from redis import Redis

from job_hunt.domain.models import RunState, RunStatus


class RunStore:
    def __init__(self, redis: Redis, ttl_seconds: int = 604800) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    def save(self, status: RunStatus) -> None:
        status.updated_at = datetime.now(UTC)
        self.redis.setex(self._key(status.run_id), self.ttl_seconds, status.model_dump_json())

    def get(self, run_id: UUID) -> RunStatus | None:
        raw = cast(str | bytes | None, self.redis.get(self._key(run_id)))
        return RunStatus.model_validate_json(raw) if raw else None

    def save_request(self, run_id: UUID, request: dict[str, object]) -> None:
        self.redis.setex(
            f"{self._key(run_id)}:request", self.ttl_seconds, json.dumps(request, default=str)
        )

    def get_request(self, run_id: UUID) -> dict[str, object] | None:
        raw = cast(str | bytes | None, self.redis.get(f"{self._key(run_id)}:request"))
        return json.loads(raw) if raw else None

    def update(
        self,
        run_id: UUID,
        *,
        state: RunState | None = None,
        stage: str | None = None,
        **fields: object,
    ) -> RunStatus:
        status = self.get(run_id)
        if status is None:
            raise KeyError(str(run_id))
        values = status.model_dump()
        if state is not None:
            values["state"] = state
        if stage is not None:
            values["stage"] = stage
        values.update(fields)
        updated = RunStatus.model_validate(values)
        self.save(updated)
        return updated

    @staticmethod
    def _key(run_id: UUID) -> str:
        return f"job-hunt:run:{run_id}"
