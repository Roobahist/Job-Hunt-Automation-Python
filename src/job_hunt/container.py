from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from redis import Redis

from job_hunt.application.compatibility import GeminiCompatibilityFilter
from job_hunt.application.normalization import SubmissionNormalizer
from job_hunt.application.workflow import ApplicationWorkflow
from job_hunt.config import Settings, TenantRuntimeConfig, load_registry
from job_hunt.domain.models import PromptDefinition
from job_hunt.integrations.apify import ApifyProvider
from job_hunt.integrations.artifacts import BaserowArtifactPublisher
from job_hunt.integrations.baserow import BaserowClient, BaserowJobRepository
from job_hunt.integrations.configuration import (
    MAHSA_PROMPT_KEYS,
    MOJTABA_PROMPT_KEYS,
    BaserowConfigurationRepository,
    validate_prompt_contract,
)
from job_hunt.integrations.gemini import GeminiWorkflowAI
from job_hunt.integrations.gemini_parallel import (
    ParallelGeminiWorkflowAI,
    ParallelMahsaGeminiWorkflowAI,
)
from job_hunt.integrations.gemini_pool import PooledGeminiStructuredClient
from job_hunt.integrations.telegram import TelegramNotifier
from job_hunt.state import RedisState
from job_hunt.tenants.registry import TenantContext, TenantRegistry


@dataclass(slots=True)
class TenantServices:
    context: TenantContext
    config: TenantRuntimeConfig
    baserow: BaserowClient
    config_repository: BaserowConfigurationRepository
    prompts: dict[str, PromptDefinition]
    repository: BaserowJobRepository
    normalizer: SubmissionNormalizer
    workflow: ApplicationWorkflow
    discovery: ApifyProvider
    notifier: TelegramNotifier

    def snapshot(self) -> dict[str, Any]:
        return {
            "config": self.config.model_dump(mode="json"),
            "prompts": {key: definition.model_dump(mode="json") for key, definition in self.prompts.items()},
        }


@dataclass(slots=True)
class TelegramRoute:
    tenant: str
    config: TenantRuntimeConfig
    repository: BaserowJobRepository
    notifier: TelegramNotifier


class Container:
    def __init__(self, settings: Settings | None = None, project_root: Path = Path(".")) -> None:
        self.settings = settings or Settings()
        self.project_root = project_root
        self.registry = TenantRegistry(
            load_registry(self.settings.registry_path),
            project_root,
            latex_compile_timeout_seconds=self.settings.latex_compile_timeout_seconds,
        )
        self.redis = Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.state = RedisState(self.redis)

    def _baserow(self, token: str, base_url: str) -> BaserowClient:
        return BaserowClient(
            token,
            base_url,
            timeout_seconds=self.settings.baserow_request_timeout_seconds,
        )

    def _telegram(self) -> TelegramNotifier:
        return TelegramNotifier(
            self.settings.shared_telegram_token(),
            timeout_seconds=self.settings.telegram_request_timeout_seconds,
        )

    def telegram_route(self, chat_id: str) -> TelegramRoute | None:
        notifier = self._telegram()
        for tenant, bootstrap in self.registry.bootstraps.items():
            if not bootstrap.enabled:
                continue
            baserow = self._baserow(bootstrap.secret("baserow"), bootstrap.baserow_base_url)
            config_repository = BaserowConfigurationRepository(baserow, bootstrap.config_table_id)
            config = config_repository.load()
            if str(config.telegram_chat_id) != chat_id:
                continue
            repository = BaserowJobRepository(
                baserow,
                config.baserow_table_ids["jobs"],
                config.status_option_ids,
                config.contract_type_option_ids,
            )
            return TelegramRoute(tenant, config, repository, notifier)
        return None

    def tenant(
        self,
        key: str,
        snapshot: dict[str, Any] | None = None,
        checkpoint_namespace: str | None = None,
    ) -> TenantServices:
        context = self.registry.get(key)
        bootstrap = context.bootstrap
        baserow = self._baserow(bootstrap.secret("baserow"), bootstrap.baserow_base_url)
        config_repository = BaserowConfigurationRepository(baserow, bootstrap.config_table_id)

        if snapshot is None:
            config = config_repository.load()
            config_repository.validate_job_table(config.baserow_table_ids["jobs"])
            prompts = config_repository.prompts(config.baserow_table_ids["prompts"])
        else:
            config = TenantRuntimeConfig.model_validate(snapshot.get("config"))
            raw_prompts = snapshot.get("prompts")
            if not isinstance(raw_prompts, dict):
                raise ValueError("Discovery snapshot has no prompt definitions")
            prompts = {
                str(prompt_key): PromptDefinition.model_validate(value) for prompt_key, value in raw_prompts.items()
            }

        if config.tenant_key not in {key, key.replace("_", "-")} and not config.tenant_key.startswith(key):
            raise ValueError(f"Registry tenant {key} does not match Baserow tenant_key {config.tenant_key}")

        discovery = ApifyProvider(
            self.settings.shared_apify_tokens(),
            config.apify_actor_ids["linkedinSearch"],
            config.apify_actor_ids["linkedinSingleJob"],
            quota_cooldown_seconds=self.settings.provider_quota_cooldown_seconds,
            state=self.state,
        )
        structured_client = PooledGeminiStructuredClient(
            self.settings.shared_gemini_keys(),
            self.settings.content_models(),
            self.settings.repair_models(),
            quota_cooldown_seconds=self.settings.gemini_quota_cooldown_seconds,
            request_timeout_seconds=self.settings.gemini_request_timeout_seconds,
            state=self.state.checkpoints(checkpoint_namespace),
            checkpoint_ttl_seconds=self.settings.llm_checkpoint_ttl_seconds,
            limits=self.settings.gemini_limits(),
        )
        compatibility_filter = GeminiCompatibilityFilter(structured_client)

        ai: GeminiWorkflowAI
        if bootstrap.renderer == "mahsa":
            validate_prompt_contract(prompts, MAHSA_PROMPT_KEYS, "mahsa")
            ai = ParallelMahsaGeminiWorkflowAI(
                structured_client,
                prompts,
                project_selection_count=config.project_selection_count,
                work_experience_selection_count=config.work_experience_selection_count,
                parallelism=self.settings.llm_parallelism,
            )
        else:
            validate_prompt_contract(prompts, MOJTABA_PROMPT_KEYS, "mojtaba")
            ai = ParallelGeminiWorkflowAI(
                structured_client,
                prompts,
                project_selection_count=config.project_selection_count,
                work_experience_selection_count=config.work_experience_selection_count,
                parallelism=self.settings.llm_parallelism,
            )

        repository = BaserowJobRepository(
            baserow,
            config.baserow_table_ids["jobs"],
            config.status_option_ids,
            config.contract_type_option_ids,
        )
        workflow = ApplicationWorkflow(
            repository,
            ai,
            ai,
            context.renderer,
            BaserowArtifactPublisher(baserow),
            self.settings.artifact_root,
            compatibility_filter=compatibility_filter,
        )
        normalizer = SubmissionNormalizer(discovery, ai, config.linkedin_job_url_template)
        return TenantServices(
            context,
            config,
            baserow,
            config_repository,
            prompts,
            repository,
            normalizer,
            workflow,
            discovery,
            self._telegram(),
        )
