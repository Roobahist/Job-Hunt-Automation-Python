from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from job_hunt.config import TenantRuntimeConfig, parse_configuration_rows
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError
from job_hunt.integrations.baserow import BaserowClient

REQUIRED_JOB_FIELDS = {
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

REQUIRED_PROMPT_KEYS = {
    "cv_project_selection",
    "cv_project_rewrite",
    "cv_work_experience_selection",
    "cv_work_experience_rewrite",
    "cv_skills_tailoring",
    "cv_summary_rewrite",
    "cover_letter_generation",
    "job_page_content_extraction",
    "qualification_scoring",
}

_LEGACY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {}},
    "additionalProperties": True,
}


def _field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _scalar(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("value", "name"):
            if value.get(key) is not None:
                return value[key]
        return value.get("id")
    if isinstance(value, list) and len(value) == 1:
        return _scalar(value[0])
    return value


def _enabled(value: Any) -> bool:
    value = _scalar(value)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "enabled"}
    return False


def _output_schema(value: Any, key: str) -> dict[str, Any]:
    value = _scalar(value)
    if isinstance(value, dict):
        schema = value
    elif isinstance(value, str) and value.strip():
        try:
            schema = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Prompt '{key}' has invalid Output Structure JSON") from exc
    else:
        raise ConfigurationError(f"Prompt '{key}' has no Output Structure JSON Schema")
    if not isinstance(schema, dict):
        raise ConfigurationError(f"Prompt '{key}' Output Structure must be a JSON object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConfigurationError(
            f"Prompt '{key}' has invalid Output Structure JSON Schema: {exc.message}"
        ) from exc
    return schema


def _is_modern_row(row: dict[str, Any]) -> bool:
    return any(
        name in row
        for name in (
            "Prompt Key",
            "Version",
            "Prompt Template",
            "Output Structure",
            "Temperature",
            "Status",
        )
    )


class BaserowConfigurationRepository:
    def __init__(self, client: BaserowClient, configuration_table_id: int) -> None:
        self.client = client
        self.configuration_table_id = configuration_table_id

    def load(self) -> TenantRuntimeConfig:
        if self.configuration_table_id <= 0:
            raise ConfigurationError(
                "Set a positive Baserow configuration table ID in config/users.toml"
            )
        return parse_configuration_rows(list(self.client.iter_rows(self.configuration_table_id)))

    def prompts(self, table_id: int) -> dict[str, PromptDefinition]:
        prompts: dict[str, PromptDefinition] = {}
        modern_contract_seen = False
        for raw_row in self.client.iter_rows(table_id):
            row = dict(raw_row)
            if not _enabled(_field(row, "Enabled", "enabled")):
                continue

            modern = _is_modern_row(row)
            modern_contract_seen = modern_contract_seen or modern
            status_value = _scalar(_field(row, "Status", "status"))
            if modern and str(status_value or "").strip() != "Active":
                continue

            key = str(
                _scalar(_field(row, "Prompt Key", "Key", "prompt_key", "key")) or ""
            ).strip()
            if not key:
                continue
            if key in prompts:
                label = "active " if modern else "enabled "
                raise ConfigurationError(f"Duplicate {label}prompt: {key}")

            template = str(
                _scalar(
                    _field(
                        row,
                        "Prompt Template",
                        "Prompt",
                        "Template",
                        "prompt_template",
                        "prompt",
                    )
                )
                or ""
            ).strip()
            if not template:
                raise ConfigurationError(f"Prompt '{key}' has no Prompt Template")

            try:
                if modern:
                    version = float(_scalar(_field(row, "Version", "Prompt Version", "version")))
                    temperature = float(_scalar(_field(row, "Temperature", "temperature")))
                    schema = _output_schema(
                        _field(
                            row,
                            "Output Structure",
                            "Output Schema",
                            "output_structure",
                            "output_schema",
                        ),
                        key,
                    )
                else:
                    version = 1.0
                    temperature = 0.2
                    schema = _LEGACY_SCHEMA
                prompt = PromptDefinition(
                    key=key,
                    version=version,
                    template=template,
                    output_structure=schema,
                    temperature=temperature,
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ConfigurationError(f"Invalid active prompt '{key}': {exc}") from exc
            prompts[key] = prompt

        if "qualification_scoring" in prompts or modern_contract_seen:
            missing = REQUIRED_PROMPT_KEYS - prompts.keys()
            if missing:
                raise ConfigurationError(f"Missing active prompts: {sorted(missing)}")
        else:
            if "qualification" not in prompts:
                raise ConfigurationError("Missing required prompt: qualification")
            if not any(key != "qualification" for key in prompts):
                raise ConfigurationError(
                    "At least one CV or cover-letter tailoring prompt is required"
                )
        return prompts

    def validate_job_table(self, table_id: int) -> None:
        fields: list[dict[str, Any]] = self.client.list_fields(table_id)
        names = {str(field["name"]) for field in fields}
        missing = REQUIRED_JOB_FIELDS - names
        if missing:
            raise ConfigurationError(f"Jobs table missing fields: {sorted(missing)}")
