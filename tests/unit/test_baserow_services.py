from __future__ import annotations

import json
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
            source="x",
            external_id="1",
            url="https://x/1",
            company_name="C",
            title="T",
            description="D",
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


def test_repository_create_find_reset_and_updates() -> None:
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
    client.rows = [{"id": 3, "Job ID": job.internal_id, "Link": job.url}]
    assert repo.find(job)["id"] == 3  # type: ignore[index]
    reset = repo.reset(3, job)
    assert "CV" not in reset and "Cover Letter" not in reset and reset["Status"] == 10
    repo.save_qualification(3, Qualification(score=20, should_apply=False, reasoning="low"), passed=False)
    assert client.updates[-1]["Status"] == 11
    repo.save_artifacts(3, {"CV": [{"name": "x"}]})
    assert client.updates[-1]["CV"][0]["name"] == "x"
    assert client.updates[-1]["Status"] == 12
    repo.set_status(3, "applied")
    assert client.updates[-1]["Status"] == 13


def test_configuration_reads_live_prompt_contract_and_validates_fields() -> None:
    client = Client()
    repository = BaserowConfigurationRepository(client, 1)  # type: ignore[arg-type]
    client.rows = [prompt_row(key) for key in sorted(MAHSA_PROMPT_KEYS)]
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


def test_profile_contracts_reject_missing_profile_specific_prompts() -> None:
    client = Client()
    repository = BaserowConfigurationRepository(client, 1)  # type: ignore[arg-type]
    client.rows = [prompt_row(key) for key in sorted(MOJTABA_PROMPT_KEYS)]
    prompts = repository.prompts(2)
    with pytest.raises(ConfigurationError, match="mahsa"):
        validate_prompt_contract(prompts, MAHSA_PROMPT_KEYS, "mahsa")
