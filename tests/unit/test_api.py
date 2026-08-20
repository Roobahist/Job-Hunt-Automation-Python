from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from job_hunt.api.app import create_app
from job_hunt.config import Settings
from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.run_store import RunStore


class Pipeline:
    def __init__(self, redis: FakeRedis) -> None:
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

    def ping(self) -> bool:
        return True

    def pipeline(self) -> Pipeline:
        return Pipeline(self)


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def submission(
        self,
        tenant: str,
        payload: dict[str, Any],
        run_id: UUID,
        force: bool,
        snapshot_id: str | None = None,
        checkpoint_namespace: str | None = None,
    ) -> str:
        self.calls.append(
            (
                "submission",
                tenant,
                payload,
                run_id,
                force,
                snapshot_id,
                checkpoint_namespace,
            )
        )
        return "task-1"

    def discovery(self, tenant: str, run_id: UUID) -> str:
        self.calls.append(("discovery", tenant, run_id))
        return "task-2"


class FakeBootstrap:
    enabled = True

    def secret(self, name: str, *, required: bool = True) -> str:
        values = {"fillout": "webhook-secret"}
        value = values.get(name, "")
        if required and not value:
            raise KeyError(name)
        return value


class FakeNotifier:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str]] = []
        self.fail_answers = False

    def answer_callback(self, callback_id: str, text: str) -> None:
        if self.fail_answers:
            raise ProviderError(
                "callback acknowledgement failed",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="telegram",
            )
        self.answers.append((callback_id, text))


class FakeRepository:
    def __init__(self) -> None:
        self.statuses: list[tuple[int, str]] = []

    def set_status(self, row_id: int, status: str) -> None:
        self.statuses.append((row_id, status))


class FakeContainer:
    notifier = FakeNotifier()
    repositories = {"mahsa": FakeRepository(), "mojtaba": FakeRepository()}
    bootstraps = {"mahsa": FakeBootstrap(), "mojtaba": FakeBootstrap()}

    def __init__(self) -> None:
        self.registry = SimpleNamespace(
            get=lambda _: object(),
            bootstraps=self.bootstraps,
        )

    def telegram_route(self, chat_id: str) -> object | None:
        tenants = {"100": "mahsa", "200": "mojtaba"}
        tenant = tenants.get(chat_id)
        if tenant is None:
            return None
        return SimpleNamespace(
            tenant=tenant,
            repository=self.repositories[tenant],
            notifier=self.notifier,
        )

    def tenant(self, tenant: str) -> object:
        chat_ids = {"mahsa": "100", "mojtaba": "200"}
        return SimpleNamespace(
            context=SimpleNamespace(bootstrap=self.bootstraps[tenant]),
            config=SimpleNamespace(
                fillout_form_id="form-1",
                fillout_field_ids={},
                telegram_chat_id=chat_ids[tenant],
            ),
            notifier=self.notifier,
            repository=self.repositories[tenant],
        )


def client() -> tuple[TestClient, FakeQueue]:
    queue = FakeQueue()
    store = RunStore(FakeRedis())  # type: ignore[arg-type]
    settings = Settings(
        operator_token="operator",
        redis_url="redis://unused",
        telegram_bot_token="bot-token",
        telegram_webhook_secret="telegram-secret",
    )
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
    assert queue.calls[0][4] is True


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
        "/webhooks/fillout/mahsa",
        json=payload,
        headers={"Authorization": "Bearer webhook-secret"},
    )
    assert accepted.status_code == 202
    assert queue.calls[-1][4] is False


def test_fillout_invalid_values_return_structured_validation_error() -> None:
    api, _ = client()
    response = api.post(
        "/webhooks/fillout/mojtaba",
        json={"formId": "form-1", "entryType": "url", "jobUrl": ""},
        headers={"Authorization": "Bearer webhook-secret"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert response.json()["message"] == "Submitted values are incomplete or invalid"


def test_shared_telegram_callback_routes_by_chat_id() -> None:
    FakeContainer.repositories["mahsa"].statuses.clear()
    FakeContainer.repositories["mojtaba"].statuses.clear()
    FakeContainer.notifier.answers.clear()
    FakeContainer.notifier.fail_answers = False
    api, _ = client()
    response = api.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
        json={
            "callback_query": {
                "id": "cb",
                "data": "status:applied:42",
                "message": {"chat": {"id": 100}},
            }
        },
    )
    assert response.status_code == 200
    assert FakeContainer.repositories["mahsa"].statuses == [(42, "applied")]
    assert FakeContainer.repositories["mojtaba"].statuses == []
    assert FakeContainer.notifier.answers == [("cb", "Status updated")]


def test_shared_telegram_callback_ignores_unknown_chat() -> None:
    FakeContainer.repositories["mahsa"].statuses.clear()
    api, _ = client()
    response = api.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
        json={
            "callback_query": {
                "id": "cb",
                "data": "status:applied:42",
                "message": {"chat": {"id": 999}},
            }
        },
    )
    assert response.status_code == 200
    assert FakeContainer.repositories["mahsa"].statuses == []


def test_telegram_acknowledgement_failure_does_not_retry_action() -> None:
    FakeContainer.repositories["mojtaba"].statuses.clear()
    FakeContainer.notifier.fail_answers = True
    api, _ = client()
    response = api.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
        json={
            "callback_query": {
                "id": "cb-fail",
                "data": "status:applied:43",
                "message": {"chat": {"id": 200}},
            }
        },
    )
    FakeContainer.notifier.fail_answers = False
    assert response.status_code == 200
    assert FakeContainer.repositories["mojtaba"].statuses == [(43, "applied")]


def test_validation_error_is_structured() -> None:
    api, _ = client()
    response = api.post(
        "/v1/tenants/mahsa/jobs",
        headers=auth(),
        json={"entry_type": "linkedin", "linkedin_job_id": -1},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
