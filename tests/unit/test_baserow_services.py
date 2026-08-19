from __future__ import annotations

from typing import Any

import pytest

from job_hunt.domain.identity import assign_identity
from job_hunt.domain.models import Job, Qualification
from job_hunt.errors import ConfigurationError
from job_hunt.integrations.baserow import BaserowJobRepository
from job_hunt.integrations.configuration import BaserowConfigurationRepository


class Client:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.fields = [
            {"name": name}
            for name in {
                "Job ID",
                "Company Name",
                "Title",
                "Job Description",
                "Link",
                "Status",
                "Score",
                "Apply",
                "CV",
                "Cover Letter",
                "Date",
                "Location",
                "Contract Type",
            }
        ]

    def find_equal(self, _: int, field: str, value: object) -> dict[str, Any] | None:
        return next((row for row in self.rows if row.get(field) == value), None)

    def create_row(self, _: int, values: dict[str, Any]) -> dict[str, Any]:
        return {"id": 3, **values}

    def update_row(self, _: int, row_id: int, values: dict[str, Any]) -> dict[str, Any]:
        self.updates.append(values)
        return {"id": row_id, **values}

    def iter_rows(self, _: int) -> object:
        return iter(self.rows)

    def list_fields(self, _: int) -> list[dict[str, Any]]:
        return self.fields


def sample_job() -> Job:
    return assign_identity(
        Job(
            source="x",
            external_id="1",
            url="https://x/1",
            company_name="C",
            title="T",
            description="D",
        )
    )


def test_repository_create_find_reset_and_updates() -> None:
    client = Client()
    repo = BaserowJobRepository(
        client,
        1,
        {"new": 10, "dropped": 11},
        {"fullTime": 12},
    )  # type: ignore[arg-type]
    job = sample_job()
    created = repo.create(job)
    assert created["Status"] == 10
    client.rows = [{"id": 3, "Job ID": job.internal_id, "Link": job.url}]
    assert repo.find(job)["id"] == 3  # type: ignore[index]
    reset = repo.reset(3, job)
    assert reset["CV"] == [] and reset["Status"] == 10
    repo.save_qualification(
        3, Qualification(score=20, should_apply=False, reasoning="low"), passed=False
    )
    assert client.updates[-1]["Status"] == 11
    repo.save_artifacts(3, {"CV": [{"name": "x"}]})
    assert client.updates[-1]["CV"][0]["name"] == "x"


def test_configuration_prompts_and_field_validation() -> None:
    client = Client()
    repository = BaserowConfigurationRepository(client, 1)  # type: ignore[arg-type]
    client.rows = [
        {"Key": "qualification", "Prompt": "score", "Enabled": True},
        {"Key": "tailoring", "Prompt": "tailor", "Enabled": True},
    ]
    assert repository.prompts(2)["qualification"] == "score"
    repository.validate_job_table(1)
    client.rows.append({"Key": "tailoring", "Prompt": "duplicate", "Enabled": True})
    with pytest.raises(ConfigurationError, match="Duplicate"):
        repository.prompts(2)
    client.fields = [{"name": "Job ID"}]
    with pytest.raises(ConfigurationError, match="missing fields"):
        repository.validate_job_table(1)
