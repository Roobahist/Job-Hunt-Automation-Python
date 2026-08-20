from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from job_hunt.domain.identity import assign_identity
from job_hunt.domain.models import Job, Qualification
from job_hunt.errors import ConfigurationError
from job_hunt.integrations.baserow import BaserowJobRepository
from job_hunt.integrations.configuration import (
    COMMON_PROMPT_KEYS,
    MAHSA_PROMPT_KEYS,
    MOJTABA_PROMPT_KEYS,
    BaserowConfigurationRepository,
    validate_prompt_contract,
)


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
            source="linkedin",
            external_id="4452378707",
            url="https://linkedin.com/jobs/view/4452378707",
            company_name="C",
            title="T",
            description="D",
            published_at=datetime(2026, 8, 19, tzinfo=UTC),
        )
    )


def prompt_row(key: str, *, version: float = 1, status: str = "Active") -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    return {
        "Prompt Key": key,
        "Version": version,
        "Prompt Template": f"prompt for {key}",
        "Output Structure": json.dumps(schema),
        "Temperature": 0.2,
        "Status": status,
        "Enabled": True,
    }


def test_repository_create_find_and_narrow_updates() -> None:
    client = Client()
    repo = BaserowJobRepository(
        client,
        1,
        {"new": 10, "dropped": 11, "toApply": 12, "applied": 13},
        {"fullTime": 14},
    )  # type: ignore[arg-type]
    job = sample_job()
    created = repo.create(job)
    assert created["Status"] == 10
    assert created["Job ID"] == 4452378707
    assert created["Date"] == "2026-08-19"
    assert created["Link"] == job.url

    client.rows = [{"id": 3, "Job ID": 4452378707, "Link": job.url}]
    assert repo.find(job)["id"] == 3  # type: ignore[index]

    updates_before_reset = len(client.updates)
    reset = repo.reset(3, job)
    assert reset == {"id": 3}
    assert len(client.updates) == updates_before_reset

    repo.save_qualification(
        3,
        Qualification(score=20, should_apply=False, reasoning="low"),
    )
    assert client.updates[-1] == {"Score": 20, "Apply": False}
    assert "Link" not in client.updates[-1]
    assert "Status" not in client.updates[-1]

    repo.save_artifacts(
        3,
        {
            "CV": [{"name": "cv.pdf"}],
            "Cover Letter": [{"name": "cl.pdf"}],
        },
    )
    assert set(client.updates[-1]) == {"CV", "Cover Letter"}
    assert "Link" not in client.updates[-1]
    assert "Status" not in client.updates[-1]

    repo.set_status(3, "applied")
    assert client.updates[-1]["Status"] == 13


def test_configuration_reads_live_prompt_contract_and_validates_fields() -> None:
    client = Client()
    repository = BaserowConfigurationRepository(client, 1)  # type: ignore[arg-type]
    all_profile_keys = MAHSA_PROMPT_KEYS | MOJTABA_PROMPT_KEYS
    client.rows = [prompt_row(key) for key in sorted(all_profile_keys)]
    client.rows.append(prompt_row("cv_summary_rewrite", version=99, status="Draft"))
    prompts = repository.prompts(2)
    summary = prompts["cv_summary_rewrite"]
    assert summary.version == 1
    assert summary.temperature == 0.2
    assert summary.output_structure["additionalProperties"] is False
    validate_prompt_contract(prompts, MAHSA_PROMPT_KEYS, "mahsa")
    validate_prompt_contract(prompts, MOJTABA_PROMPT_KEYS, "mojtaba")
    repository.validate_job_table(1)

    client.rows.append(prompt_row("cv_summary_rewrite", version=2))
    with pytest.raises(ConfigurationError, match="Duplicate active prompt"):
        repository.prompts(2)

    client.fields = [{"name": "Job ID"}]
    with pytest.raises(ConfigurationError, match="missing fields"):
        repository.validate_job_table(1)


def test_configuration_rejects_missing_common_or_invalid_schema() -> None:
    client = Client()
    repository = BaserowConfigurationRepository(client, 1)  # type: ignore[arg-type]
    client.rows = [prompt_row(key) for key in sorted(MAHSA_PROMPT_KEYS)]
    client.rows[0]["Output Structure"] = "not json"
    with pytest.raises(ConfigurationError, match="invalid Output Structure"):
        repository.prompts(2)

    client.rows = [prompt_row(key) for key in sorted(COMMON_PROMPT_KEYS - {"qualification_scoring"})]
    with pytest.raises(ConfigurationError, match="Missing active prompts"):
        repository.prompts(2)


def test_profile_contracts_are_independent() -> None:
    client = Client()
    repository = BaserowConfigurationRepository(client, 1)  # type: ignore[arg-type]

    client.rows = [prompt_row(key) for key in sorted(MAHSA_PROMPT_KEYS)]
    mahsa_prompts = repository.prompts(2)
    validate_prompt_contract(mahsa_prompts, MAHSA_PROMPT_KEYS, "mahsa")
    assert "cv_project_selection" not in MAHSA_PROMPT_KEYS
    assert "cv_project_rewrite" not in MAHSA_PROMPT_KEYS
    with pytest.raises(ConfigurationError, match="mojtaba"):
        validate_prompt_contract(mahsa_prompts, MOJTABA_PROMPT_KEYS, "mojtaba")

    client.rows = [prompt_row(key) for key in sorted(MOJTABA_PROMPT_KEYS)]
    mojtaba_prompts = repository.prompts(2)
    validate_prompt_contract(mojtaba_prompts, MOJTABA_PROMPT_KEYS, "mojtaba")
    with pytest.raises(ConfigurationError, match="mahsa"):
        validate_prompt_contract(mojtaba_prompts, MAHSA_PROMPT_KEYS, "mahsa")
