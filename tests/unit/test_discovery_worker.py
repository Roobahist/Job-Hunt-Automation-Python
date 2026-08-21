from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import job_hunt.worker as worker


class FakeStore:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []

    def update(self, _run_id: object, **values: object) -> None:
        self.updates.append(dict(values))

    def save(self, _status: object) -> None:
        return None

    def merge_notification(self, _run_id: object, **_values: object) -> None:
        return None

    def save_request(self, _run_id: object, request: dict[str, object]) -> None:
        self.requests.append(request)


class FakeState:
    def set_snapshot(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeRepository:
    def find(self, job: object) -> dict[str, int] | None:
        external_id = getattr(job, "external_id", None)
        return {"id": 99} if external_id == "1" else None


class FakeBaserow:
    def iter_rows(self, _table_id: int) -> list[dict[str, object]]:
        return [
            {"Active": False, "Generated URL": "https://linkedin.example/inactive"},
            {"Active": True, "Generated URL": "https://linkedin.example/active"},
        ]


class FakeDiscovery:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def discover(self, urls: list[str], *, max_items: int) -> list[dict[str, object]]:
        self.urls = urls
        assert max_items == 25
        return [
            {
                "id": 1,
                "url": "https://linkedin.com/jobs/view/1",
                "companyName": "Existing",
                "title": "Existing Job",
                "description": "Already tracked",
            },
            {
                "id": 2,
                "url": "https://linkedin.com/jobs/view/2",
                "companyName": "New",
                "title": "New Job",
                "description": "New posting",
            },
        ]


class FakeServices:
    def __init__(self) -> None:
        self.baserow = FakeBaserow()
        self.repository = FakeRepository()
        self.discovery = FakeDiscovery()
        self.config = SimpleNamespace(
            baserow_table_ids={"searchCriteria": 1},
            linkedin_base_search_url="https://linkedin.example/search",
            company_exclusions=[],
            title_exclusions=[],
            linkedin_max_items=25,
            telegram_chat_id="42",
        )

    def snapshot(self) -> dict[str, object]:
        return {}


class FakeContainer:
    def __init__(self, services: FakeServices) -> None:
        self.services = services

    def tenant(self, _tenant: str) -> FakeServices:
        return self.services


def test_discovery_uses_only_active_criteria_and_skips_existing_jobs(monkeypatch: object) -> None:
    store = FakeStore()
    state = FakeState()
    services = FakeServices()
    container = FakeContainer(services)
    notification_jobs: list[str] = []
    submission_jobs: list[str] = []

    monkeypatch.setattr(worker, "_store", lambda: store)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "_state", lambda: state)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "Container", lambda _settings: container)  # type: ignore[attr-defined]
    monkeypatch.setattr(
        worker.init_job_notification,
        "delay",
        lambda _tenant, _run_id, job, _chat: notification_jobs.append(str(job["external_id"])),
    )  # type: ignore[attr-defined]
    monkeypatch.setattr(
        worker.process_submission,
        "delay",
        lambda _tenant, payload, *_args: submission_jobs.append(str(payload["external_job_id"])),
    )  # type: ignore[attr-defined]

    result = worker.discover_tenant.run("mojtaba", str(uuid4()))

    assert services.discovery.urls == ["https://linkedin.example/active"]
    assert result["criteria_total"] == 2
    assert result["criteria_active"] == 1
    assert result["discovered"] == 2
    assert result["duplicates"] == 1
    assert result["queued"] == 1
    # Discovery must not expose a Telegram message before process_submission
    # persists the corresponding Baserow row.
    assert notification_jobs == []
    assert submission_jobs == ["2"]
