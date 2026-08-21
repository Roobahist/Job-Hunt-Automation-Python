from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from job_hunt.config import LlmRoute, Settings
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError, ErrorKind, ProviderError
from job_hunt.integrations.gemini import GeminiStructuredClient, _validate_json_schema
from job_hunt.logging import logger
from job_hunt.state import RedisState

_DEFAULT_OPERATION_GROUPS = {
    "compatibility": "job-fast",
    "content_extraction": "job-fast",
    "qualification": "job-balanced",
    "project_selection": "job-balanced",
    "project_rewrite": "job-powerful",
    "work_experience_selection": "job-balanced",
    "work_experience_rewrite": "job-powerful",
    "skills": "job-balanced",
    "summary": "job-powerful",
    "cover_letter": "job-powerful",
}


def _repair_prompt(raw_output: str, schema: dict[str, Any], validation_error: str) -> str:
    return (
        "Repair the following model output so it conforms exactly to the supplied JSON Schema. "
        "Preserve the original meaning and facts. Do not add facts that were not present. "
        "Return only the repaired structured result.\n\n"
        f"VALIDATION ERROR:\n{validation_error}\n\n"
        f"JSON SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"ORIGINAL OUTPUT:\n{raw_output}"
    )


def _repair_definition(operation: str, schema: dict[str, Any]) -> PromptDefinition:
    return PromptDefinition(
        key=f"{operation}:repair",
        version=1.0,
        template="repair structured output",
        output_structure=schema,
        temperature=0.0,
    )


def _error_kind(status_code: int) -> ErrorKind:
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    if status_code in {401, 403}:
        return ErrorKind.AUTHENTICATION
    if status_code in {400, 404, 422}:
        return ErrorKind.MALFORMED_PROVIDER_RESPONSE
    return ErrorKind.TRANSIENT_PROVIDER


def _operation_groups() -> dict[str, str]:
    configured = os.getenv("JOB_HUNT_LLM_OPERATION_GROUPS_JSON", "").strip()
    if not configured:
        return dict(_DEFAULT_OPERATION_GROUPS)
    try:
        parsed = json.loads(configured)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("JOB_HUNT_LLM_OPERATION_GROUPS_JSON must be valid JSON") from exc
    if not isinstance(parsed, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()):
        raise ConfigurationError("JOB_HUNT_LLM_OPERATION_GROUPS_JSON must map operation names to LiteLLM aliases")
    return {**_DEFAULT_OPERATION_GROUPS, **parsed}


def capability_group_for_operation(operation: str, *, repair: bool = False) -> str:
    if repair:
        return os.getenv("JOB_HUNT_LLM_REPAIR_GROUP", "repair-fast").strip() or "repair-fast"
    overrides = _operation_groups()
    normalized = operation.lower().replace("-", "_")
    for marker, group in overrides.items():
        if marker.lower().replace("-", "_") in normalized:
            return group
    return os.getenv("JOB_HUNT_LLM_DEFAULT_GROUP", "job-balanced").strip() or "job-balanced"


class LiteLLMGatewayClient(GeminiStructuredClient):
    """OpenAI-compatible client for one logical capability group exposed by LiteLLM Proxy."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_seconds: int = 180,
        repair_client: GeminiStructuredClient | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not model:
            raise ValueError("A LiteLLM model alias is required")
        self.model = model
        self.repair_client = repair_client
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def _repair_via_routes(
        self,
        raw_output: str,
        schema: dict[str, Any],
        validation_error: str,
        *,
        operation: str,
    ) -> dict[str, Any]:
        if self.repair_client is None:
            raise ProviderError(
                f"Structured output failed validation: {validation_error}",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="litellm",
            )
        return self.repair_client.generate(
            _repair_prompt(raw_output, schema, validation_error),
            _repair_definition(operation, schema),
        )

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": definition.temperature,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "job_hunt_response",
                            "strict": True,
                            "schema": definition.output_structure,
                        },
                    },
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"LiteLLM gateway request failed: {exc}",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="litellm",
            ) from exc

        if response.is_error:
            kind = _error_kind(response.status_code)
            raise ProviderError(
                f"LiteLLM route {self.model} failed with HTTP {response.status_code}: {response.text[:500]}",
                kind,
                retryable=kind in {ErrorKind.RATE_LIMIT, ErrorKind.TRANSIENT_PROVIDER},
                provider="litellm",
                status_code=response.status_code,
            )

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "LiteLLM gateway returned a malformed chat-completions response",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="litellm",
            ) from exc

        raw_output = json.dumps(content, ensure_ascii=False) if isinstance(content, Mapping) else str(content or "")
        if isinstance(content, Mapping):
            parsed: object = dict(content)
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                return self._repair_via_routes(
                    raw_output,
                    definition.output_structure,
                    str(exc),
                    operation=definition.key,
                )
        else:
            raise ProviderError(
                "LiteLLM gateway returned empty structured output",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="litellm",
            )

        try:
            result = _validate_json_schema(parsed, definition.output_structure)
            repaired = False
        except JsonSchemaValidationError as exc:
            result = self._repair_via_routes(
                raw_output,
                definition.output_structure,
                str(exc),
                operation=definition.key,
            )
            repaired = True

        logger().info(
            "llm_generation",
            prompt_key=definition.key,
            provider="litellm",
            model_group=self.model,
            upstream_model=payload.get("model"),
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=payload.get("usage", {}),
            repaired=repaired,
            router="litellm-proxy",
        )
        return result


class CapabilityRoutedStructuredClient(GeminiStructuredClient):
    """Selects one capability group; LiteLLM owns deployment choice and inter-group fallbacks."""

    def __init__(
        self,
        routes: Sequence[tuple[LlmRoute, GeminiStructuredClient]],
        *,
        repair: bool = False,
    ) -> None:
        self.routes = {route.model: client for route, client in routes}
        self.repair = repair
        if not self.routes:
            raise ConfigurationError("No usable LiteLLM capability groups are configured")

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        group = capability_group_for_operation(definition.key, repair=self.repair)
        client = self.routes.get(group)
        if client is None:
            raise ConfigurationError(
                f"LiteLLM capability group {group!r} is not configured for {'repair' if self.repair else 'generation'}"
            )
        try:
            result = client.generate(prompt, definition)
        except ProviderError as exc:
            logger().warning(
                "llm_group_failed",
                prompt_key=definition.key,
                group=group,
                error_kind=exc.kind,
                reason=str(exc),
                router="litellm-proxy",
            )
            raise
        logger().info(
            "llm_group_succeeded",
            prompt_key=definition.key,
            group=group,
            repair=self.repair,
            router="litellm-proxy",
        )
        return result


RoutedStructuredClient = CapabilityRoutedStructuredClient


def _build_gateway_routes(
    settings: Settings,
    specs: Sequence[LlmRoute],
    *,
    repair_client: GeminiStructuredClient | None,
) -> list[tuple[LlmRoute, GeminiStructuredClient]]:
    return [
        (
            route,
            LiteLLMGatewayClient(
                settings.litellm_base_url,
                settings.shared_litellm_key(),
                route.model,
                timeout_seconds=settings.litellm_request_timeout_seconds,
                repair_client=repair_client,
            ),
        )
        for route in specs
    ]


def build_routed_structured_client(
    settings: Settings,
    *,
    state: RedisState | None = None,
) -> CapabilityRoutedStructuredClient:
    del state
    repair_client = CapabilityRoutedStructuredClient(
        _build_gateway_routes(settings, settings.llm_repair_route_specs(), repair_client=None),
        repair=True,
    )
    return CapabilityRoutedStructuredClient(
        _build_gateway_routes(settings, settings.llm_route_specs(), repair_client=repair_client),
    )
