from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from job_hunt.api.app import create_app
from job_hunt.config import Settings
from job_hunt.run_store import RunStore


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def setex(self, key: str, _: int, value: str) -> None:
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def ping(self) -> bool:
        return True


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def submission(self, tenant: str, payload: dict[str, Any], run_id: UUID, force: bool) -> str:
        self.calls.append(("submission", tenant, payload, run_id, force))
        return "task-1"

    def discovery(self, tenant: str, run_id: UUID) -> str:
        self.calls.append(("discovery", tenant, run_id))
        return "task-2"


class FakeBootstrap:
    def secret(self, name: str) -> str:
        assert name == "fillout"
        return "webhook-secret"


class FakeContainer:
    def __init__(self) -> None:
        self.registry = SimpleNamespace(get=lambda _: object())

    def tenant(self, _: str) -> object:
        return SimpleNamespace(
            context=SimpleNamespace(bootstrap=FakeBootstrap()),
            config=SimpleNamespace(fillout_form_id="form-1", fillout_field_ids={}),
        )


def client() -> tuple[TestClient, FakeQueue]:
    queue = FakeQueue()
    store = RunStore(FakeRedis())  # type: ignore[arg-type]
    settings = Settings(operator_token="operator", redis_url="redis://unused")
    app = create_app(settings=settings, queue=queue, store=store, container_factory=FakeContainer)
    return TestClient(app), queue


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer operator"}


def test_health_and_operator_auth() -> None:
    api, _ = client()
    assert api.get("/health/live").json() == {"status": "ok"}
    assert api.get("/health/ready").status_code == 200
    assert api.post("/v1/tenants/mahsa/jobs", json={}).status_code == 401


def test_submit_status_and_retry() -> None:
    api, queue = client()
    response = api.post(
        "/v1/tenants/mahsa/jobs?force=true",
        headers=auth(),
        json={
            "entry_type": "external",
            "company_name": "C",
            "job_title": "T",
            "job_description": "D",
            "job_url": "https://example.com/j",
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    status_response = api.get(f"/v1/runs/{run_id}", headers=auth())
    assert status_response.json()["task_id"] == "task-1"
    retry = api.post(f"/v1/runs/{run_id}/retry", headers=auth())
    assert retry.status_code == 202
    assert queue.calls[0][-1] is True


def test_discovery_and_fillout_auth_form_validation() -> None:
    api, queue = client()
    assert api.post("/v1/tenants/mojtaba/discoveries", headers=auth()).status_code == 202
    assert queue.calls[-1][0] == "discovery"
    payload = {"formId": "wrong", "entryType": "linkedin", "linkedinJobId": 1}
    assert (
        api.post(
            "/webhooks/fillout/mahsa",
            json=payload,
            headers={"Authorization": "Bearer webhook-secret"},
        ).status_code
        == 400
    )
    payload["formId"] = "form-1"
    accepted = api.post(
        "/webhooks/fillout/mahsa", json=payload, headers={"Authorization": "Bearer webhook-secret"}
    )
    assert accepted.status_code == 202
    assert queue.calls[-1][-1] is False


def test_validation_error_is_structured() -> None:
    api, _ = client()
    response = api.post(
        "/v1/tenants/mahsa/jobs",
        headers=auth(),
        json={"entry_type": "linkedin", "linkedin_job_id": -1},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
