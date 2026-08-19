from __future__ import annotations

from uuid import uuid4

from job_hunt.domain.models import RunState, RunStatus
from job_hunt.run_store import RunStore


class Pipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.pending: tuple[str, str] | None = None

    def watch(self, _: str) -> None:
        return None

    def get(self, key: str) -> str | None:
        return self.redis.get(key)

    def multi(self) -> None:
        return None

    def setex(self, key: str, _: int, value: str) -> None:
        self.pending = (key, value)

    def execute(self) -> list[bool]:
        if self.pending:
            self.redis.data[self.pending[0]] = self.pending[1]
        return [True]

    def reset(self) -> None:
        self.pending = None


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def setex(self, key: str, _: int, value: str) -> None:
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def pipeline(self) -> Pipeline:
        return Pipeline(self)


def test_run_store_status_and_replay() -> None:
    store = RunStore(FakeRedis())  # type: ignore[arg-type]
    status = RunStatus(tenant="mahsa", kind="manual")
    store.save(status)
    assert store.get(status.run_id).state is RunState.QUEUED  # type: ignore[union-attr]
    updated = store.update(
        status.run_id,
        state=RunState.RUNNING,
        stage="qualification",
        notification={"state": "queued"},
    )
    assert updated.stage == "qualification"
    assert updated.notification == {"state": "queued"}
    store.save_request(status.run_id, {"force": True})
    assert store.get_request(status.run_id) == {"force": True}
    assert store.get(uuid4()) is None
