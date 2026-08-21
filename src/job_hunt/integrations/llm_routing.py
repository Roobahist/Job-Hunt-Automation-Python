from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from typing import Any

import httpx
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from job_hunt.config import LlmRoute, Settings
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError, ErrorKind, ProviderError
from job_hunt.integrations.gemini import GeminiStructuredClient, _compact, _validate_json_schema
from job_hunt.integrations.gemini_pool import PooledGeminiStructuredClient
from job_hunt.integrations.http import raise_provider_error
from job_hunt.logging import logger
from job_hunt.state import RedisState


class CerebrasStructuredClient(GeminiStructuredClient):
    """Structured-output client for Cerebras' OpenAI-compatible chat-completions API."""

    def __init__(
        self,
        keys: Sequence[str],
        model: str,
        *,
        base_url: str = "https://api.cerebras.ai/v1",
        timeout_seconds: int = 180,
        client: httpx.Client | None = None,
    ) -> None:
        self.keys = [key for key in keys if key]
        self.model = model
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds)
        if not self.keys:
            raise ValueError("At least one Cerebras API key is required")
        if not self.model:
            raise ValueError("A Cerebras model is required")

    @staticmethod
    def _key_id(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def _call(
        self,
        key: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
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
                    "schema": schema,
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
        if isinstance(content, dict):
            parsed = content
        elif isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    "Cerebras returned non-JSON structured output",
                    ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                    provider="cerebras",
                ) from exc
        else:
            raise ProviderError(
                "Cerebras returned empty structured output",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="cerebras",
            )
        validated = _validate_json_schema(parsed, schema)
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        return validated, {
            "provider": "cerebras",
            "model": self.model,
            "account": self._key_id(key),
            "latency_ms": latency_ms,
            "usage": usage if isinstance(usage, dict) else {},
        }

    def _repair(
        self,
        key: str,
        raw_output: str,
        schema: dict[str, Any],
        validation_error: str,
    ) -> dict[str, Any]:
        prompt = (
            "Repair the following output so it conforms exactly to the supplied JSON Schema. "
            "Preserve the original meaning and facts. Return only the repaired structured result.\n\n"
            f"VALIDATION ERROR:\n{validation_error}\n\n"
            f"JSON SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"ORIGINAL OUTPUT:\n{raw_output}"
        )
        result, _ = self._call(key, prompt, schema, 0.0)
        return result

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        failures: list[str] = []
        for key in self.keys:
            try:
                result, metadata = self._call(
                    key,
                    prompt,
                    definition.output_structure,
                    definition.temperature,
                )
                logger().info("llm_generation", prompt_key=definition.key, **metadata)
                return result
            except ConfigurationError:
                raise
            except JsonSchemaValidationError as exc:
                try:
                    repaired = self._repair(key, "", definition.output_structure, str(exc))
                    logger().info(
                        "llm_generation",
                        prompt_key=definition.key,
                        provider="cerebras",
                        model=self.model,
                        account=self._key_id(key),
                        repaired=True,
                    )
                    return repaired
                except Exception as repair_exc:
                    failures.append(f"{self._key_id(key)}: {repair_exc}")
            except Exception as exc:
                failures.append(f"{self._key_id(key)}: {exc}")
        raise ProviderError(
            f"Cerebras {self.model} failed across all configured keys: " + "; ".join(failures),
            ErrorKind.TRANSIENT_PROVIDER,
            retryable=True,
            provider="cerebras",
        )


class RoutedStructuredClient(GeminiStructuredClient):
    """Try provider/model routes in exactly the environment-configured order."""

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


def build_routed_structured_client(
    settings: Settings,
    *,
    state: RedisState | None = None,
) -> RoutedStructuredClient:
    gemini_keys = settings.shared_gemini_keys(required=False)
    cerebras_keys = settings.shared_cerebras_keys(required=False)
    gemini_limits = settings.gemini_limits()
    routes: list[tuple[LlmRoute, GeminiStructuredClient]] = []

    for route in settings.llm_route_specs():
        if route.provider == "gemini":
            if not gemini_keys:
                logger().warning("llm_route_skipped", provider="gemini", model=route.model, reason="no keys")
                continue
            routes.append(
                (
                    route,
                    PooledGeminiStructuredClient(
                        gemini_keys,
                        [route.model],
                        [route.model],
                        quota_cooldown_seconds=settings.gemini_quota_cooldown_seconds,
                        request_timeout_seconds=settings.gemini_request_timeout_seconds,
                        state=state,
                        checkpoint_ttl_seconds=settings.llm_checkpoint_ttl_seconds,
                        limits=gemini_limits,
                    ),
                )
            )
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
                    ),
                )
            )
            continue
        raise ConfigurationError(f"Unsupported LLM provider: {route.provider}")

    return RoutedStructuredClient(routes)
