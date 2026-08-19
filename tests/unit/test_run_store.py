from __future__ import annotations

from uuid import uuid4

from job_hunt.domain.models import RunState, RunStatus
from job_hunt.run_store import RunStore


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def setex(self, key: str, _: int, value: str) -> None:
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)


def test_run_store_status_and_replay() -> None:
    store = RunStore(FakeRedis())  # type: ignore[arg-type]
    status = RunStatus(tenant="mahsa", kind="manual")
    store.save(status)
    assert store.get(status.run_id).state is RunState.QUEUED  # type: ignore[union-attr]
    updated = store.update(status.run_id, state=RunState.RUNNING, stage="qualification")
    assert updated.stage == "qualification"
    store.save_request(status.run_id, {"force": True})
    assert store.get_request(status.run_id) == {"force": True}
    assert store.get(uuid4()) is None
