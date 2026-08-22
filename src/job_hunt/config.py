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


class LlmRoute(BaseModel):
    provider: str
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOB_HUNT_", env_file=".env", extra="ignore")

    environment: str = "development"
    debug: bool = False
    registry_path: Path = Path("config/users.toml")
    artifact_root: Path = Path("runs")
    redis_url: str = "redis://localhost:6379/0"
    operator_token: str = ""
    run_ttl_seconds: int = 604800
    discovery_snapshot_ttl_seconds: int = Field(default=7200, ge=300)
    llm_checkpoint_ttl_seconds: int = Field(default=604800, ge=300)
    llm_parallelism: int = Field(default=3, ge=1, le=8)
    scheduler_timezone: str = "America/Edmonton"

    # Operational timing is environment-configurable. A task time limit of zero disables
    # Celery's global deadline so long document jobs are not killed mid-generation.
    litellm_request_timeout_seconds: int = Field(default=180, ge=1)
    telegram_request_timeout_seconds: int = Field(default=120, ge=1)
    baserow_request_timeout_seconds: int = Field(default=60, ge=1)
    latex_compile_timeout_seconds: int = Field(default=180, ge=1)
    job_lock_timeout_seconds: int = Field(default=1800, ge=60)
    task_soft_time_limit_seconds: int = Field(default=0, ge=0)
    task_time_limit_seconds: int = Field(default=0, ge=0)
    task_max_retries: int = Field(default=8, ge=0)
    rate_limit_fallback_seconds: int = Field(default=65, ge=1)
    transient_base_delay_seconds: int = Field(default=5, ge=1)
    transient_max_delay_seconds: int = Field(default=300, ge=1)

    apify_tokens: str = ""
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    # Job-hunt workers call a centralized LiteLLM Proxy using logical model aliases.
    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: str = ""
    llm_routes: str = ""
    llm_repair_routes: str = ""

    provider_quota_cooldown_seconds: int = Field(default=3600, ge=1)

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def shared_apify_tokens(self) -> list[str]:
        tokens = self._split_csv(self.apify_tokens)
        if not tokens:
            raise ConfigurationError("JOB_HUNT_APIFY_TOKENS must contain at least one token")
        return tokens

    def shared_litellm_key(self) -> str:
        if not self.litellm_api_key:
            raise ConfigurationError("JOB_HUNT_LITELLM_API_KEY must be set")
        return self.litellm_api_key

    def shared_telegram_token(self) -> str:
        if not self.telegram_bot_token:
            raise ConfigurationError("JOB_HUNT_TELEGRAM_BOT_TOKEN must be set")
        return self.telegram_bot_token

    def shared_telegram_webhook_secret(self) -> str:
        if not self.telegram_webhook_secret:
            raise ConfigurationError("JOB_HUNT_TELEGRAM_WEBHOOK_SECRET must be set")
        return self.telegram_webhook_secret

    @classmethod
    def _gateway_routes(cls, raw: str, variable_name: str) -> list[LlmRoute]:
        routes: list[LlmRoute] = []
        for alias in cls._split_csv(raw):
            if ":" in alias:
                raise ConfigurationError(
                    f"{variable_name} must contain LiteLLM model aliases, not provider:model entries: {alias}"
                )
            routes.append(LlmRoute(provider="litellm", model=alias))
        return routes

    def llm_route_specs(self) -> list[LlmRoute]:
        routes = self._gateway_routes(self.llm_routes, "JOB_HUNT_LLM_ROUTES")
        if not routes:
            raise ConfigurationError("JOB_HUNT_LLM_ROUTES must contain at least one LiteLLM model alias")
        return routes

    def llm_repair_route_specs(self) -> list[LlmRoute]:
        routes = self._gateway_routes(self.llm_repair_routes, "JOB_HUNT_LLM_REPAIR_ROUTES")
        if not routes:
            raise ConfigurationError("JOB_HUNT_LLM_REPAIR_ROUTES must contain at least one LiteLLM model alias")
        return routes


class SecretAliases(BaseModel):
    baserow: str
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
        if not alias:
            if required:
                raise ConfigurationError(f"Missing secret alias for {name} on tenant {self.key}")
            return ""
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
    project_selection_count: int | None = None
    work_experience_selection_count: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def table_contract(self) -> TenantRuntimeConfig:
        missing = {"jobs", "searchCriteria", "prompts"} - self.baserow_table_ids.keys()
        if missing:
            raise ValueError(f"baserow_table_ids missing: {sorted(missing)}")
        return self


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
        if not alias:
            if required:
                raise ConfigurationError(f"Missing secret alias for {name} on tenant {self.key}")
            return ""
        value = os.getenv(alias, "")
        if required and not value:
            raise ConfigurationError(f"Missing {alias} for tenant {self.key}")
        return value


def load_registry(path: Path) -> dict[str, TenantBootstrap]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle).get("users", {})
    result: dict[str, TenantBootstrap] = {}
    for key, value in raw.items():
        aliases = SecretAliases(
            baserow=value["baserow_token_env"],
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
