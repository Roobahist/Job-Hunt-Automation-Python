from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

import litellm
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from litellm import Router

from job_hunt.config import LlmRoute, Settings
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError, ErrorKind, ProviderError
from job_hunt.integrations.gemini import GeminiStructuredClient, _validate_json_schema
from job_hunt.logging import logger
from job_hunt.state import RedisState


def _parse_routes(raw: str, *, variable_name: str) -> list[LlmRoute]:
    routes: list[LlmRoute] = []
    for item in (value.strip() for value in raw.split(",")):
        if not item:
            continue
        provider, separator, model = item.partition(":")
        provider = provider.strip().lower()
        model = model.strip().removeprefix("models/")
        if not separator or not provider or not model:
            raise ConfigurationError(
                f"{variable_name} entries must use provider:model, for example gemini:gemini-3.5-flash-lite"
            )
        if provider not in {"cerebras", "gemini"}:
            raise ConfigurationError(f"Unsupported LLM provider in {variable_name}: {provider}")
        routes.append(LlmRoute(provider=provider, model=model))
    return routes


def repair_route_specs(settings: Settings) -> list[LlmRoute]:
    configured = os.getenv("JOB_HUNT_LLM_REPAIR_ROUTES", "")
    if configured.strip():
        return _parse_routes(configured, variable_name="JOB_HUNT_LLM_REPAIR_ROUTES")
    return [LlmRoute(provider="gemini", model=model) for model in settings.repair_models()]


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


def _key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _provider_model(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _error_kind(exc: Exception) -> ErrorKind:
    if isinstance(exc, litellm.RateLimitError):
        return ErrorKind.RATE_LIMIT
    if isinstance(exc, (litellm.AuthenticationError, litellm.PermissionDeniedError)):
        return ErrorKind.AUTHENTICATION
    if isinstance(exc, litellm.BadRequestError):
        return ErrorKind.MALFORMED_PROVIDER_RESPONSE
    return ErrorKind.TRANSIENT_PROVIDER


class LiteLLMStructuredClient(GeminiStructuredClient):
    """One logical provider:model route backed by a LiteLLM deployment pool."""

    def __init__(
        self,
        provider: str,
        model: str,
        keys: Sequence[str],
        *,
        timeout_seconds: int = 180,
        repair_client: GeminiStructuredClient | None = None,
        router: Router | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.keys = [key for key in keys if key]
        self.timeout_seconds = timeout_seconds
        self.repair_client = repair_client
        if not self.keys:
            raise ValueError(f"At least one {provider} API key is required")
        if not model:
            raise ValueError("A model is required")

        alias = self.route_name
        model_list = [
            {
                "model_name": alias,
                "litellm_params": {
                    "model": _provider_model(provider, model),
                    "api_key": key,
                    "timeout": timeout_seconds,
                },
                "model_info": {"id": f"{alias}:{_key_id(key)}"},
            }
            for key in self.keys
        ]
        self.router = router or Router(
            model_list=model_list,
            routing_strategy="simple-shuffle",
            num_retries=0,
            allowed_fails=1,
            cooldown_time=65,
            set_verbose=False,
        )

    @property
    def route_name(self) -> str:
        safe_model = self.model.replace("/", "-")
        return f"job-hunt-{self.provider}-{safe_model}"

    def _repair(
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
                provider=self.provider,
            )
        return self.repair_client.generate(
            _repair_prompt(raw_output, schema, validation_error),
            _repair_definition(operation, schema),
        )

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            response = self.router.completion(
                model=self.route_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=definition.temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "job_hunt_response",
                        "strict": True,
                        "schema": definition.output_structure,
                    },
                },
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            kind = _error_kind(exc)
            raise ProviderError(
                f"LiteLLM {self.provider}:{self.model} failed: {exc}",
                kind,
                retryable=kind in {ErrorKind.RATE_LIMIT, ErrorKind.TRANSIENT_PROVIDER},
                provider=self.provider,
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            message = response.choices[0].message
            content = message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderError(
                "LiteLLM returned a malformed chat-completions response",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider=self.provider,
            ) from exc

        raw_output = json.dumps(content, ensure_ascii=False) if isinstance(content, Mapping) else str(content or "")
        if isinstance(content, Mapping):
            parsed: object = dict(content)
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                parsed = self._repair(raw_output, definition.output_structure, str(exc), operation=definition.key)
        else:
            raise ProviderError(
                "LiteLLM returned empty structured output",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider=self.provider,
            )

        try:
            result = _validate_json_schema(parsed, definition.output_structure)
            repaired = False
        except JsonSchemaValidationError as exc:
            result = self._repair(raw_output, definition.output_structure, str(exc), operation=definition.key)
            repaired = True

        hidden = getattr(response, "_hidden_params", {}) or {}
        deployment = hidden.get("model_id") or hidden.get("api_base") or "litellm"
        usage = getattr(response, "usage", None)
        usage_data = usage.model_dump() if hasattr(usage, "model_dump") else {}
        logger().info(
            "llm_generation",
            prompt_key=definition.key,
            provider=self.provider,
            model=self.model,
            deployment=str(deployment),
            latency_ms=latency_ms,
            usage=usage_data,
            repaired=repaired,
            router="litellm",
        )
        return result


class RoutedStructuredClient(GeminiStructuredClient):
    """Preserves explicit model fallback order while LiteLLM manages keys inside each route."""

    def __init__(self, routes: Sequence[tuple[LlmRoute, GeminiStructuredClient]]) -> None:
        self.routes = list(routes)
        if not self.routes:
            raise ConfigurationError("No usable LLM routes are configured")

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        failures: list[str] = []
        all_rate_limited = True
        for route, client in self.routes:
            try:
                result = client.generate(prompt, definition)
                logger().info(
                    "llm_route_succeeded",
                    prompt_key=definition.key,
                    provider=route.provider,
                    model=route.model,
                    router="litellm",
                )
                return result
            except ConfigurationError:
                raise
            except ProviderError as exc:
                failures.append(f"{route.provider}:{route.model}: {exc}")
                all_rate_limited = all_rate_limited and exc.kind == ErrorKind.RATE_LIMIT
                logger().warning(
                    "llm_route_failed",
                    prompt_key=definition.key,
                    provider=route.provider,
                    model=route.model,
                    error_kind=exc.kind,
                    reason=str(exc),
                    router="litellm",
                )
            except Exception as exc:
                all_rate_limited = False
                failures.append(f"{route.provider}:{route.model}: {exc}")
                logger().warning(
                    "llm_route_failed",
                    prompt_key=definition.key,
                    provider=route.provider,
                    model=route.model,
                    reason=str(exc),
                    router="litellm",
                )
        kind = ErrorKind.RATE_LIMIT if all_rate_limited else ErrorKind.TRANSIENT_PROVIDER
        raise ProviderError(
            "LLM generation failed across all configured routes: " + "; ".join(failures),
            kind,
            retryable=True,
            provider="llm",
        )


def _keys_for(settings: Settings, provider: str) -> list[str]:
    if provider == "gemini":
        return settings.shared_gemini_keys(required=False)
    if provider == "cerebras":
        return settings.shared_cerebras_keys(required=False)
    raise ConfigurationError(f"Unsupported LLM provider: {provider}")


def _timeout_for(settings: Settings, provider: str) -> int:
    if provider == "gemini":
        return settings.gemini_request_timeout_seconds
    if provider == "cerebras":
        return settings.cerebras_request_timeout_seconds
    return 180


def _build_route_clients(
    settings: Settings,
    specs: Sequence[LlmRoute],
    *,
    repair_client: GeminiStructuredClient | None,
    generation: bool,
) -> list[tuple[LlmRoute, GeminiStructuredClient]]:
    routes: list[tuple[LlmRoute, GeminiStructuredClient]] = []
    for route in specs:
        keys = _keys_for(settings, route.provider)
        if not keys:
            logger().warning("llm_route_skipped", provider=route.provider, model=route.model, reason="no keys")
            continue
        routes.append(
            (
                route,
                LiteLLMStructuredClient(
                    route.provider,
                    route.model,
                    keys,
                    timeout_seconds=_timeout_for(settings, route.provider),
                    repair_client=repair_client if generation else None,
                ),
            )
        )
    return routes


def build_routed_structured_client(
    settings: Settings,
    *,
    state: RedisState | None = None,
) -> RoutedStructuredClient:
    del state
    repair_routes = _build_route_clients(
        settings,
        repair_route_specs(settings),
        repair_client=None,
        generation=False,
    )
    repair_client = RoutedStructuredClient(repair_routes)
    content_routes = _build_route_clients(
        settings,
        settings.llm_route_specs(),
        repair_client=repair_client,
        generation=True,
    )
    return RoutedStructuredClient(content_routes)
