from __future__ import annotations

import copy
import hashlib
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
from job_hunt.integrations.gemini_pool import PooledGeminiStructuredClient
from job_hunt.integrations.http import raise_provider_error
from job_hunt.logging import logger
from job_hunt.state import RedisState


def _cerebras_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(schema)

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict):
                for child in properties.values():
                    visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            variants = node.get(keyword)
            if isinstance(variants, list):
                for child in variants:
                    visit(child)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for child in definitions.values():
                visit(child)

    visit(normalized)
    return normalized


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
                f"{variable_name} entries must use provider:model, for example cerebras:gpt-oss-120b"
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


class CerebrasStructuredClient(GeminiStructuredClient):
    def __init__(
        self,
        keys: Sequence[str],
        model: str,
        *,
        base_url: str = "https://api.cerebras.ai/v1",
        timeout_seconds: int = 180,
        client: httpx.Client | None = None,
        repair_client: GeminiStructuredClient | None = None,
    ) -> None:
        self.keys = [key for key in keys if key]
        self.model = model
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds)
        self.repair_client = repair_client
        if not self.keys:
            raise ValueError("At least one Cerebras API key is required")
        if not self.model:
            raise ValueError("A Cerebras model is required")

    @staticmethod
    def _key_id(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

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
                f"Cerebras structured output failed validation: {validation_error}",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="cerebras",
            )
        return self.repair_client.generate(
            _repair_prompt(raw_output, schema, validation_error),
            _repair_definition(operation, schema),
        )

    def _call(
        self,
        key: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        *,
        operation: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "job_hunt_response",
                    "strict": True,
                    "schema": _cerebras_schema(schema),
                },
            },
        }
        started = time.perf_counter()
        try:
            response = self._client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        except httpx.TransportError as exc:
            raise ProviderError(
                f"Cerebras network request failed: {exc}",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="cerebras",
            ) from exc
        raise_provider_error("cerebras", response)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            body = response.json()
            choices = body["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(
                "Cerebras returned a malformed chat-completions response",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="cerebras",
            ) from exc

        raw_output = json.dumps(content, ensure_ascii=False) if isinstance(content, Mapping) else str(content or "")
        if isinstance(content, Mapping):
            parsed: object = dict(content)
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                fixed = self._repair_via_routes(raw_output, schema, str(exc), operation=operation)
                return fixed, {
                    "provider": "cerebras",
                    "model": self.model,
                    "account": self._key_id(key),
                    "latency_ms": latency_ms,
                    "repaired": True,
                }
        else:
            raise ProviderError(
                "Cerebras returned empty structured output",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="cerebras",
            )

        try:
            validated = _validate_json_schema(parsed, schema)
            was_repaired = False
        except JsonSchemaValidationError as exc:
            validated = self._repair_via_routes(raw_output, schema, str(exc), operation=operation)
            was_repaired = True
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return validated, {
            "provider": "cerebras",
            "model": self.model,
            "account": self._key_id(key),
            "latency_ms": latency_ms,
            "usage": usage if isinstance(usage, dict) else {},
            "repaired": was_repaired,
        }

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        failures: list[str] = []
        last_error: ProviderError | None = None
        for key in self.keys:
            try:
                result, metadata = self._call(
                    key,
                    prompt,
                    definition.output_structure,
                    definition.temperature,
                    operation=definition.key,
                )
                logger().info("llm_generation", prompt_key=definition.key, **metadata)
                return result
            except ConfigurationError:
                raise
            except ProviderError as exc:
                last_error = exc
                failures.append(f"{self._key_id(key)}: {exc}")
                continue
            except Exception as exc:
                failures.append(f"{self._key_id(key)}: {exc}")
                continue
        if last_error is not None and all("429" in failure or "rate" in failure.lower() for failure in failures):
            raise ProviderError(
                f"Cerebras {self.model} exhausted all configured keys: " + "; ".join(failures),
                ErrorKind.RATE_LIMIT,
                retryable=True,
                provider="cerebras",
                retry_after=last_error.retry_after,
            )
        raise ProviderError(
            f"Cerebras {self.model} failed across all configured keys: " + "; ".join(failures),
            ErrorKind.TRANSIENT_PROVIDER,
            retryable=True,
            provider="cerebras",
        )


class RoutedStructuredClient(GeminiStructuredClient):
    def __init__(self, routes: Sequence[tuple[LlmRoute, GeminiStructuredClient]]) -> None:
        self.routes = list(routes)
        if not self.routes:
            raise ConfigurationError("No usable LLM routes are configured")

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        failures: list[str] = []
        retry_delays: list[float] = []
        all_rate_limited = True
        for route, client in self.routes:
            try:
                result = client.generate(prompt, definition)
                logger().info(
                    "llm_route_succeeded",
                    prompt_key=definition.key,
                    provider=route.provider,
                    model=route.model,
                )
                return result
            except ConfigurationError:
                raise
            except ProviderError as exc:
                failures.append(f"{route.provider}:{route.model}: {exc}")
                all_rate_limited = all_rate_limited and exc.kind == ErrorKind.RATE_LIMIT
                if exc.retry_after:
                    retry_delays.append(float(exc.retry_after))
                logger().warning(
                    "llm_route_failed",
                    prompt_key=definition.key,
                    provider=route.provider,
                    model=route.model,
                    error_kind=exc.kind,
                    reason=str(exc),
                )
                continue
            except Exception as exc:
                all_rate_limited = False
                failures.append(f"{route.provider}:{route.model}: {exc}")
                logger().warning(
                    "llm_route_failed",
                    prompt_key=definition.key,
                    provider=route.provider,
                    model=route.model,
                    reason=str(exc),
                )
                continue
        kind = ErrorKind.RATE_LIMIT if all_rate_limited else ErrorKind.TRANSIENT_PROVIDER
        raise ProviderError(
            "LLM generation failed across all configured routes: " + "; ".join(failures),
            kind,
            retryable=True,
            provider="llm",
            retry_after=min(retry_delays) if retry_delays else None,
        )


class RoutedGeminiContentClient(PooledGeminiStructuredClient):
    def __init__(self, *args: Any, repair_client: GeminiStructuredClient, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.repair_client = repair_client

    def _pooled_repair(
        self,
        raw_output: str,
        schema: dict[str, Any],
        validation_error: str,
        *,
        operation: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        result = self.repair_client.generate(
            _repair_prompt(raw_output, schema, validation_error),
            _repair_definition(operation, schema),
        )
        return result, {"repaired": True, "model": "repair-route", "account": "repair-route"}


def _build_route_clients(
    settings: Settings,
    specs: Sequence[LlmRoute],
    *,
    state: RedisState | None,
    repair_client: GeminiStructuredClient | None,
    generation: bool,
) -> list[tuple[LlmRoute, GeminiStructuredClient]]:
    gemini_keys = settings.shared_gemini_keys(required=False)
    cerebras_keys = settings.shared_cerebras_keys(required=False)
    gemini_limits = settings.gemini_limits()
    routes: list[tuple[LlmRoute, GeminiStructuredClient]] = []

    for route in specs:
        if route.provider == "gemini":
            if not gemini_keys:
                logger().warning("llm_route_skipped", provider="gemini", model=route.model, reason="no keys")
                continue
            common: dict[str, Any] = {
                "quota_cooldown_seconds": settings.gemini_quota_cooldown_seconds,
                "request_timeout_seconds": settings.gemini_request_timeout_seconds,
                "state": state,
                "checkpoint_ttl_seconds": settings.llm_checkpoint_ttl_seconds,
                "limits": gemini_limits,
            }
            if generation:
                if repair_client is None:
                    raise ConfigurationError("Generation routes require an independent repair route client")
                client: GeminiStructuredClient = RoutedGeminiContentClient(
                    gemini_keys,
                    [route.model],
                    [route.model],
                    repair_client=repair_client,
                    **common,
                )
            else:
                client = PooledGeminiStructuredClient(
                    gemini_keys,
                    [route.model],
                    [route.model],
                    **common,
                )
            routes.append((route, client))
            continue
        if route.provider == "cerebras":
            if not cerebras_keys:
                logger().warning("llm_route_skipped", provider="cerebras", model=route.model, reason="no keys")
                continue
            routes.append(
                (
                    route,
                    CerebrasStructuredClient(
                        cerebras_keys,
                        route.model,
                        base_url=settings.cerebras_base_url,
                        timeout_seconds=settings.cerebras_request_timeout_seconds,
                        repair_client=repair_client if generation else None,
                    ),
                )
            )
            continue
        raise ConfigurationError(f"Unsupported LLM provider: {route.provider}")
    return routes


def build_routed_structured_client(
    settings: Settings,
    *,
    state: RedisState | None = None,
) -> RoutedStructuredClient:
    repair_routes = _build_route_clients(
        settings,
        repair_route_specs(settings),
        state=state,
        repair_client=None,
        generation=False,
    )
    repair_client = RoutedStructuredClient(repair_routes)
    content_routes = _build_route_clients(
        settings,
        settings.llm_route_specs(),
        state=state,
        repair_client=repair_client,
        generation=True,
    )
    return RoutedStructuredClient(content_routes)
