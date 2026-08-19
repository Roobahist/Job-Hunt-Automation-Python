from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from job_hunt.application.normalization import SubmissionNormalizer
from job_hunt.application.workflow import ApplicationWorkflow
from job_hunt.config import Settings, TenantRuntimeConfig, load_registry
from job_hunt.integrations.apify import ApifyProvider
from job_hunt.integrations.artifacts import CloudinaryBaserowPublisher
from job_hunt.integrations.baserow import BaserowClient, BaserowJobRepository
from job_hunt.integrations.cloudinary import CloudinaryPublisher
from job_hunt.integrations.configuration import BaserowConfigurationRepository
from job_hunt.integrations.gemini import GeminiStructuredClient, GeminiWorkflowAI
from job_hunt.integrations.telegram import TelegramNotifier
from job_hunt.tenants.registry import TenantContext, TenantRegistry


@dataclass(slots=True)
class TenantServices:
    context: TenantContext
    config: TenantRuntimeConfig
    baserow: BaserowClient
    config_repository: BaserowConfigurationRepository
    prompts: dict[str, str]
    normalizer: SubmissionNormalizer
    workflow: ApplicationWorkflow
    discovery: ApifyProvider


class Container:
    def __init__(self, settings: Settings | None = None, project_root: Path = Path(".")) -> None:
        self.settings = settings or Settings()
        self.project_root = project_root
        self.registry = TenantRegistry(load_registry(self.settings.registry_path), project_root)

    def tenant(self, key: str) -> TenantServices:
        context = self.registry.get(key)
        bootstrap = context.bootstrap
        baserow = BaserowClient(bootstrap.secret("baserow"), bootstrap.baserow_base_url)
        config_repository = BaserowConfigurationRepository(baserow, bootstrap.config_table_id)
        config = config_repository.load()
        if config.tenant_key not in {
            key,
            key.replace("_", "-"),
        } and not config.tenant_key.startswith(key):
            raise ValueError(
                f"Registry tenant {key} does not match Baserow tenant_key {config.tenant_key}"
            )
        config_repository.validate_job_table(config.baserow_table_ids["jobs"])
        prompts = config_repository.prompts(config.baserow_table_ids["prompts"])
        discovery = ApifyProvider(
            bootstrap.secret("apify"),
            config.apify_actor_ids["linkedinSearch"],
            config.apify_actor_ids["linkedinSingleJob"],
        )
        ai = GeminiWorkflowAI(
            GeminiStructuredClient(
                bootstrap.secret("gemini"),
                bootstrap.secret("gemini_backup", required=False),
                config.gemini_model,
            )
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
            CloudinaryBaserowPublisher(
                CloudinaryPublisher(bootstrap.secret("cloudinary")), baserow
            ),
            TelegramNotifier(bootstrap.secret("telegram")),
            self.settings.artifact_root,
        )
        normalizer = SubmissionNormalizer(discovery, ai, config.linkedin_job_url_template)
        return TenantServices(
            context, config, baserow, config_repository, prompts, normalizer, workflow, discovery
        )
