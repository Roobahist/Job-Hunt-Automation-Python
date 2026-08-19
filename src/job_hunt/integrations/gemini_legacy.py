from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from job_hunt.domain.identity import assign_identity
from job_hunt.domain.models import Job, PromptDefinition, Qualification, TailoredContent
from job_hunt.errors import ConfigurationError
from job_hunt.integrations.gemini import GeminiStructuredClient

_QUALIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "should_apply": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["score", "should_apply", "reasoning"],
    "additionalProperties": False,
}

_TAILORED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cv": {"type": "object"},
        "cover_letter": {"type": "object"},
    },
    "required": ["cv", "cover_letter"],
    "additionalProperties": False,
}

_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "external_id": {"type": ["string", "null"]},
        "company_name": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "location": {"type": ["string", "null"]},
        "contract_type": {"type": ["string", "null"]},
    },
    "required": ["company_name", "title", "description"],
    "additionalProperties": False,
}


def _definition(source: PromptDefinition, schema: dict[str, Any]) -> PromptDefinition:
    return PromptDefinition(
        key=source.key,
        version=source.version,
        template=source.template,
        output_structure=schema,
        temperature=source.temperature,
    )


class LegacyGeminiWorkflowAI:
    """Compatibility adapter for tenants that still use the original simple prompt table."""

    def __init__(
        self,
        client: GeminiStructuredClient,
        prompts: Mapping[str, PromptDefinition],
    ) -> None:
        self.client = client
        self.prompts = dict(prompts)

    def qualify(
        self,
        job: Job,
        master_cv: Mapping[str, object],
        prompts: Mapping[str, PromptDefinition],
    ) -> Qualification:
        try:
            source = prompts["qualification"]
        except KeyError as exc:
            raise ConfigurationError("Missing required prompt: qualification") from exc
        prompt = (
            f"{source.template}\n\nMASTER CV:\n{json.dumps(master_cv)}"
            f"\n\nJOB:\n{job.model_dump_json()}"
        )
        generated = self.client.generate(prompt, _definition(source, _QUALIFICATION_SCHEMA))
        return Qualification.model_validate(generated)

    def tailor(
        self,
        job: Job,
        master_cv: Mapping[str, object],
        prompts: Mapping[str, PromptDefinition],
    ) -> TailoredContent:
        tailoring = [
            f"[{key}]\n{definition.template}"
            for key, definition in prompts.items()
            if key != "qualification"
        ]
        if not tailoring:
            raise ConfigurationError("At least one legacy tailoring prompt is required")
        source = next(definition for key, definition in prompts.items() if key != "qualification")
        prompt = (
            "\n\n".join(tailoring)
            + "\n\nReturn both cv and cover_letter.\nMASTER CV:\n"
            + json.dumps(master_cv)
            + "\nJOB:\n"
            + job.model_dump_json()
        )
        generated = self.client.generate(prompt, _definition(source, _TAILORED_SCHEMA))
        return TailoredContent.model_validate(generated)

    def extract_job(self, content: str, source_url: str) -> Job:
        source = self.prompts.get("job_page_content_extraction")
        if source is None:
            source = PromptDefinition(
                key="legacy_job_page_content_extraction",
                version=1,
                template="Extract the job posting into the required schema. Do not invent missing facts.",
                output_structure=_EXTRACTION_SCHEMA,
                temperature=0.2,
            )
        prompt = f"{source.template}\n\n{content}"
        generated = self.client.generate(prompt, _definition(source, _EXTRACTION_SCHEMA))
        generated.setdefault("source", "web")
        generated["url"] = source_url
        return assign_identity(Job.model_validate(generated))
