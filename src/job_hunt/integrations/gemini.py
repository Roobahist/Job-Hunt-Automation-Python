from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from job_hunt.domain.identity import assign_identity
from job_hunt.domain.models import Job, Qualification, TailoredContent
from job_hunt.errors import ErrorKind, ProviderError

T = TypeVar("T", bound=BaseModel)


class _TailoredResponse(BaseModel):
    cv: dict[str, object]
    cover_letter: dict[str, object]


class _ExtractedJob(BaseModel):
    source: str = "web"
    external_id: str | None = None
    company_name: str
    title: str
    description: str
    location: str | None = None
    contract_type: str | None = None


class GeminiStructuredClient:
    def __init__(self, primary_key: str, backup_key: str, model: str) -> None:
        self.keys = [key for key in (primary_key, backup_key) if key]
        self.model = model.removeprefix("models/")
        if not self.keys:
            raise ValueError("At least one Gemini API key is required")

    def generate(self, prompt: str, schema: type[T]) -> T:
        failures: list[str] = []
        for key in self.keys:
            try:
                response = genai.Client(api_key=key).models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema,
                        temperature=0.2,
                    ),
                )
                if response.parsed:
                    return schema.model_validate(response.parsed)
                if not response.text:
                    raise ValueError("Gemini returned an empty response")
                return schema.model_validate_json(response.text)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                failures.append(f"malformed response: {exc}")
            except Exception as exc:
                failures.append(str(exc))
        raise ProviderError(
            "Gemini failed for all configured keys: " + "; ".join(failures),
            ErrorKind.MALFORMED_PROVIDER_RESPONSE,
            provider="gemini",
        )


class GeminiWorkflowAI:
    def __init__(self, client: GeminiStructuredClient) -> None:
        self.client = client

    def qualify(
        self, job: Job, master_cv: Mapping[str, object], prompts: Mapping[str, str]
    ) -> Qualification:
        system = prompts["qualification"]
        return self.client.generate(
            f"{system}\n\nMASTER CV:\n{json.dumps(master_cv)}\n\nJOB:\n{job.model_dump_json()}",
            Qualification,
        )

    def tailor(
        self, job: Job, master_cv: Mapping[str, object], prompts: Mapping[str, str]
    ) -> TailoredContent:
        tailoring_prompts = [
            f"[{key}]\n{value}" for key, value in prompts.items() if key != "qualification"
        ]
        if not tailoring_prompts:
            raise ValueError("At least one tailoring prompt is required")
        system = "\n\n".join(tailoring_prompts)
        prompt = (
            f"{system}\n\nReturn both cv and cover_letter.\n"
            f"MASTER CV:\n{json.dumps(master_cv)}\nJOB:\n{job.model_dump_json()}"
        )
        generated = self.client.generate(
            prompt,
            _TailoredResponse,
        )
        return TailoredContent.model_validate(generated.model_dump())

    def extract_job(self, content: str, source_url: str) -> Job:
        extracted = self.client.generate(
            "Extract the job posting into the required schema. Do not invent missing facts.\n\n"
            + content,
            _ExtractedJob,
        )
        return assign_identity(Job(url=source_url, **extracted.model_dump()))
