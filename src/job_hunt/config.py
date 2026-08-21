from __future__ import annotations

import csv
import json
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

    gemini_request_timeout_seconds: int = Field(default=180, ge=1)
    cerebras_request_timeout_seconds: int = Field(default=180, ge=1)
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
    gemini_api_keys: str = ""
    cerebras_api_keys: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    # The application now talks only to a centralized LiteLLM Proxy. Provider credentials
    # and provider-specific model names belong to config/litellm.yaml and the proxy environment.
    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: str = ""
    llm_routes: str = ""
    llm_repair_routes: str = ""

    # Legacy Gemini settings are retained for migration/config compatibility only.
    gemini_content_models: str = (
        "gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-flash,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"
    )
    gemini_repair_models: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite"
    gemini_limits_json: str = json.dumps(_DEFAULT_GEMINI_LIMITS, separators=(",", ":"))
    provider_quota_cooldown_seconds: int = Field(default=3600, ge=1)
    gemini_quota_cooldown_seconds: int = Field(default=65, ge=1)
    cerebras_quota_cooldown_seconds: int = Field(default=65, ge=1)

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def shared_apify_tokens(self) -> list[str]:
        tokens = self._split_csv(self.apify_tokens)
        if not tokens:
            raise ConfigurationError("JOB_HUNT_APIFY_TOKENS must contain at least one token")
        return tokens

    def shared_gemini_keys(self, *, required: bool = True) -> list[str]:
        keys = self._split_csv(self.gemini_api_keys)
        if required and not keys:
            raise ConfigurationError("JOB_HUNT_GEMINI_API_KEYS must contain at least one API key")
        return keys

    def shared_cerebras_keys(self, *, required: bool = True) -> list[str]:
        keys = self._split_csv(self.cerebras_api_keys)
        if required and not keys:
            raise ConfigurationError("JOB_HUNT_CEREBRAS_API_KEYS must contain at least one API key")
        return keys

    def shared_telegram_token(self) -> str:
        if not self.telegram_bot_token:
            raise ConfigurationError("JOB_HUNT_TELEGRAM_BOT_TOKEN must be set")
        return self.telegram_bot_token

    def shared_telegram_webhook_secret(self) -> str:
        if not self.telegram_webhook_secret:
            raise ConfigurationError("JOB_HUNT_TELEGRAM_WEBHOOK_SECRET must be set")
        return self.telegram_webhook_secret

    def shared_litellm_key(self) -> str:
        if not self.litellm_api_key:
            raise ConfigurationError("JOB_HUNT_LITELLM_API_KEY must be set")
        return self.litellm_api_key

    @staticmethod
    def _gateway_routes(raw: str, variable_name: str) -> list[LlmRoute]:
        routes: list[LlmRoute] = []
        for alias in Settings._split_csv(raw):
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

    def content_models(self) -> list[str]:
        models = [model.removeprefix("models/") for model in self._split_csv(self.gemini_content_models)]
        if not models:
            raise ConfigurationError("JOB_HUNT_GEMINI_CONTENT_MODELS must contain at least one model")
        return models

    def repair_models(self) -> list[str]:
        models = [model.removeprefix("models/") for model in self._split_csv(self.gemini_repair_models)]
        if not models:
            raise ConfigurationError("JOB_HUNT_GEMINI_REPAIR_MODELS must contain at least one model")
        return models

    def gemini_limits(self) -> dict[str, dict[str, int]]:
        try:
            parsed = json.loads(self.gemini_limits_json)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("JOB_HUNT_GEMINI_LIMITS_JSON must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ConfigurationError("JOB_HUNT_GEMINI_LIMITS_JSON must be a JSON object")
        return parsed

    @model_validator(mode="after")
    def validate_task_limits(self) -> Settings:
        if self.task_soft_time_limit_seconds and self.task_time_limit_seconds:
            if self.task_soft_time_limit_seconds >= self.task_time_limit_seconds:
                raise ValueError("task_soft_time_limit_seconds must be less than task_time_limit_seconds")
        return self


class TenantRuntimeConfig(BaseModel):
    tenant_key: str
    telegram_chat_id: str | int
    baserow_table_ids: dict[str, int]
    status_option_ids: dict[str, int]
    contract_type_option_ids: dict[str, int]
    apify_actor_ids: dict[str, str]
    linkedin_job_url_template: str
    project_selection_count: int = 3
    work_experience_selection_count: int = 3


def load_registry(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)
