from __future__ import annotations

from typing import Any

from job_hunt.config import TenantRuntimeConfig, parse_configuration_rows
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

    def prompts(self, table_id: int) -> dict[str, str]:
        prompts: dict[str, str] = {}
        for row in self.client.iter_rows(table_id):
            enabled = row.get("Enabled", row.get("enabled", True))
            if not enabled:
                continue
            key = str(row.get("Key", row.get("key", ""))).strip()
            content = str(row.get("Prompt", row.get("prompt", ""))).strip()
            if key and content:
                if key in prompts:
                    raise ConfigurationError(f"Duplicate enabled prompt: {key}")
                prompts[key] = content
        if "qualification" not in prompts:
            raise ConfigurationError("Missing required prompt: qualification")
        if not any(key != "qualification" for key in prompts):
            raise ConfigurationError("At least one CV or cover-letter tailoring prompt is required")
        return prompts

    def validate_job_table(self, table_id: int) -> None:
        fields: list[dict[str, Any]] = self.client.list_fields(table_id)
        names = {str(field["name"]) for field in fields}
        missing = REQUIRED_JOB_FIELDS - names
        if missing:
            raise ConfigurationError(f"Jobs table missing fields: {sorted(missing)}")
