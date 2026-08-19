from __future__ import annotations

import csv
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from job_hunt.errors import ConfigurationError


_DEFAULT_GEMINI_LIMITS = {
    "gemini-3.6-flash": {"rpm": 5, "tpm": 250000, "rpd": 20},
    "gemini-3.5-flash": {"rpm": 5, "tpm": 250000, "rpd": 20},
    "gemini-3-flash-preview": {"rpm": 5, "tpm": 250000, "rpd": 20},
    "gemini-2.5-flash": {"rpm": 5, "tpm": 250000, "rpd": 20},
    "gemini-3.5-flash-lite": {"rpm": 15, "tpm": 250000, "rpd": 500},
    "gemini-3.1-flash-lite": {"rpm": 15, "tpm": 250000, "rpd": 500},
    "gemini-2.5-flash-lite": {"rpm": 10, "tpm": 250000, "rpd": 20},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOB_HUNT_", env_file=".env", extra="ignore")

    environment: str = "development"
    registry_path: Path = Path("config/users.toml")
    artifact_root: Path = Path("runs")
    redis_url: str = "redis://localhost:6379/0"
    operator_token: str = ""
    run_ttl_seconds: int = 604800
    discovery_snapshot_ttl_seconds: int = Field(default=7200, ge=300)
    llm_checkpoint_ttl_seconds: int = Field(default=604800, ge=300)
    llm_parallelism: int = Field(default=3, ge=1, le=8)

    # Provider capacity is shared by every tenant. Each configured key belongs to a separate
    # free-tier account, while all tenants consume the same ordered account pool.
    apify_tokens: str = ""
    gemini_api_keys: str = ""
    gemini_content_models: str = (
        "gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"
    )
    gemini_repair_models: str = (
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"
    )
    gemini_limits_json: str = json.dumps(_DEFAULT_GEMINI_LIMITS, separators=(",", ":"))
    provider_quota_cooldown_seconds: int = Field(default=3600, ge=60)

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def shared_apify_tokens(self) -> list[str]:
        tokens = self._split_csv(self.apify_tokens)
        if not tokens:
            raise ConfigurationError("JOB_HUNT_APIFY_TOKENS must contain at least one token")
        return tokens

    def shared_gemini_keys(self) -> list[str]:
        keys = self._split_csv(self.gemini_api_keys)
        if not keys:
            raise ConfigurationError("JOB_HUNT_GEMINI_API_KEYS must contain at least one API key")
        return keys

    def content_models(self) -> list[str]:
        models = [
            model.removeprefix("models/")
            for model in self._split_csv(self.gemini_content_models)
        ]
        if not models:
            raise ConfigurationError(
                "JOB_HUNT_GEMINI_CONTENT_MODELS must contain at least one model"
            )
        return models

    def repair_models(self) -> list[str]:
        models = [
            model.removeprefix("models/")
            for model in self._split_csv(self.gemini_repair_models)
        ]
        if not models:
            raise ConfigurationError(
                "JOB_HUNT_GEMINI_REPAIR_MODELS must contain at least one model"
            )
        return models

    def gemini_limits(self) -> dict[str, dict[str, int]]:
        try:
            raw = json.loads(self.gemini_limits_json)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("JOB_HUNT_GEMINI_LIMITS_JSON must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("JOB_HUNT_GEMINI_LIMITS_JSON must be a JSON object")
        result: dict[str, dict[str, int]] = {}
        for model, limits in raw.items():
            if not isinstance(model, str) or not isinstance(limits, dict):
                raise ConfigurationError("Every Gemini limits entry must map a model to an object")
            normalized: dict[str, int] = {}
            for name in ("rpm", "tpm", "rpd"):
                value = limits.get(name)
                if value is not None:
                    try:
                        integer = int(value)
                    except (TypeError, ValueError) as exc:
                        raise ConfigurationError(
                            f"Gemini limit {model}.{name} must be an integer"
                        ) from exc
                    if integer < 1:
                        raise ConfigurationError(
                            f"Gemini limit {model}.{name} must be positive"
                        )
                    normalized[name] = integer
            result[model.removeprefix("models/")] = normalized
        return result


class SecretAliases(BaseModel):
    baserow: str
    telegram: str
    fillout: str


class TenantBootstrap(BaseModel):
    key: str
    enabled: bool = True
    renderer: str
    config_table_id: int = Field(ge=0)
    baserow_base_url: str = "https://api.baserow.io"
    tenant_root: Path
    secrets: SecretAliases

    def secret(self, name: str, *, required: bool = True) -> str:
        alias = getattr(self.secrets, name)
        value = os.getenv(alias, "")
        if required and not value:
            raise ConfigurationError(f"Missing {alias} for tenant {self.key}")
        return value


class TenantRuntimeConfig(BaseModel):
    tenant_key: str
    tenant_enabled: bool = True
    applicant_filename: str
    baserow_table_ids: dict[str, int]
    contract_type_option_ids: dict[str, int] = Field(default_factory=dict)
    status_option_ids: dict[str, int]
    fillout_form_id: str
    fillout_field_ids: dict[str, str]
    apify_actor_ids: dict[str, str]
    apify_proxy_country: str = "ca"
    apify_max_concurrency: int = Field(default=1, ge=1)
    linkedin_max_items: int = Field(default=150, ge=1)
    linkedin_schedule_interval_hours: int = Field(default=1, ge=1)
    linkedin_base_search_url: str
    linkedin_job_url_template: str
    company_exclusions: list[str] = Field(default_factory=list)
    title_exclusions: list[str] = Field(default_factory=list)
    qualification_threshold: int = Field(default=33, ge=0, le=100)
    telegram_chat_id: str
    # Retained so existing Baserow configuration rows remain valid. Artifacts now upload
    # directly to Baserow and model selection is application-wide.
    cloudinary_folder_prefix: str = "job-applications"
    cloudinary_tags: list[str] = Field(default_factory=list)
    gemini_model: str | None = None
    project_selection_count: int | None = None
    work_experience_selection_count: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def table_contract(self) -> TenantRuntimeConfig:
        missing = {"jobs", "searchCriteria", "prompts"} - self.baserow_table_ids.keys()
        if missing:
            raise ValueError(f"baserow_table_ids missing: {sorted(missing)}")
        return self


def load_registry(path: Path) -> dict[str, TenantBootstrap]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle).get("users", {})
    result: dict[str, TenantBootstrap] = {}
    for key, value in raw.items():
        aliases = SecretAliases(
            baserow=value["baserow_token_env"],
            telegram=value["telegram_token_env"],
            fillout=value["fillout_secret_env"],
        )
        result[key] = TenantBootstrap(
            key=key,
            secrets=aliases,
            **{k: v for k, v in value.items() if not k.endswith("_env")},
        )
    return result


def decode_config_value(value_type: str, value: str) -> Any:
    match value_type:
        case "boolean":
            return value.strip().lower() in {"true", "1", "yes"}
        case "number":
            number = float(value)
            return int(number) if number.is_integer() else number
        case "json":
            return json.loads(value)
        case _:
            return value


def parse_configuration_rows(rows: list[Mapping[str, Any]]) -> TenantRuntimeConfig:
    values: dict[str, Any] = {}
    for row in rows:
        if not bool(row.get("enabled", True)):
            continue
        key = str(row.get("configKey", "")).strip()
        if key:
            values[key] = decode_config_value(str(row.get("valueType", "text")), str(row["value"]))
    try:
        return TenantRuntimeConfig.model_validate(values)
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid tenant configuration: {exc}") from exc


def read_seed(path: Path) -> TenantRuntimeConfig:
    with path.open(newline="", encoding="utf-8") as handle:
        return parse_configuration_rows(list(csv.DictReader(handle)))
