from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from langchain_google_genai import ChatGoogleGenerativeAI

from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError, ErrorKind, ProviderError
from job_hunt.integrations.gemini import (
    GeminiStructuredClient,
    _compact,
    _message_text,
    _validate_json_schema,
)
from job_hunt.logging import logger
from job_hunt.state import RedisState

_RETRY_SECONDS = re.compile(r"(?:retry|try again)[^0-9]{0,24}(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


class LocalCapacityError(RuntimeError):
    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = 429


def _langfuse_handler() -> object | None:
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except ImportError:
        logger().warning("langfuse_unavailable", reason="langfuse package is not installed")
        return None


class PooledGeminiStructuredClient(GeminiStructuredClient):
    """Use shared independent Gemini accounts and ordered model tiers for structured generation."""

    _lock: ClassVar[Lock] = Lock()
    _unavailable_until: ClassVar[dict[str, float]] = {}
    _invalid_key_until: ClassVar[dict[str, float]] = {}

    def __init__(
        self,
        keys: Sequence[str],
        content_models: Sequence[str],
        repair_models: Sequence[str],
        *,
        quota_cooldown_seconds: int = 3600,
        state: RedisState | None = None,
        checkpoint_ttl_seconds: int = 604800,
        limits: Mapping[str, Mapping[str, int]] | None = None,
    ) -> None:
        self.keys = [key for key in keys if key]
        self.content_models = [model.removeprefix("models/") for model in content_models if model]
        self.repair_models = [model.removeprefix("models/") for model in repair_models if model]
        self.quota_cooldown_seconds = quota_cooldown_seconds
        self.state = state
        self.checkpoint_ttl_seconds = checkpoint_ttl_seconds
        self.limits = {model.removeprefix("models/"): dict(values) for model, values in (limits or {}).items()}
        self.langfuse_handler = _langfuse_handler()
        if not self.keys:
            raise ValueError("At least one Gemini API key is required")
        if not self.content_models:
            raise ValueError("At least one Gemini content model is required")
        if not self.repair_models:
            raise ValueError("At least one Gemini repair model is required")

    @staticmethod
    def _key_id(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    @classmethod
    def _candidate_id(cls, model: str, key: str) -> str:
        return f"{model}:{cls._key_id(key)}"

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        if callable(code):
            try:
                code = code()
            except Exception:
                code = None
        if status == 429 or str(code).lower() in {"429", "resource_exhausted"}:
            return True
        text = str(exc).lower()
        markers = (
            "resource_exhausted",
            "resource exhausted",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "requests per day",
            "requests per minute",
            "tokens per minute",
            "limit exceeded",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status in {401, 403}:
            return True
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "api key not valid",
                "invalid api key",
                "permission denied",
                "unauthenticated",
            )
        )

    @staticmethod
    def _seconds_to_next_minute() -> int:
        return max(1, 61 - datetime.now().second)

    @staticmethod
    def _seconds_to_daily_reset() -> int:
        pacific = ZoneInfo("America/Los_Angeles")
        now = datetime.now(pacific)
        reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return max(60, int((reset - now).total_seconds()))

    def _reserve_capacity(self, model: str, key: str, prompt: str) -> None:
        if self.state is None:
            return
        limits = self.limits.get(model)
        if not limits:
            return
        account = self._key_id(key)
        candidate = self._candidate_id(model, key)
        now = datetime.now()
        minute_bucket = now.strftime("%Y%m%d%H%M")

        rpm = limits.get("rpm")
        if rpm:
            count = self.state.increment_window("gemini-rpm", candidate, minute_bucket, ttl_seconds=90)
            if count > rpm:
                retry_after = self._seconds_to_next_minute()
                self.state.cooldown("gemini-candidate", candidate, seconds=retry_after)
                raise LocalCapacityError("local requests-per-minute budget exhausted", retry_after)

        tpm = limits.get("tpm")
        if tpm:
            estimated_tokens = max(1, len(prompt) // 4)
            tokens = self.state.increment_window(
                "gemini-tpm",
                candidate,
                minute_bucket,
                ttl_seconds=90,
                amount=estimated_tokens,
            )
            if tokens > tpm:
                retry_after = self._seconds_to_next_minute()
                self.state.cooldown("gemini-candidate", candidate, seconds=retry_after)
                raise LocalCapacityError("local tokens-per-minute budget exhausted", retry_after)

        rpd = limits.get("rpd")
        if rpd:
            pacific = ZoneInfo("America/Los_Angeles")
            day_bucket = datetime.now(pacific).strftime("%Y%m%d")
            daily = self.state.increment_window(
                "gemini-rpd",
                account + ":" + model,
                day_bucket,
                ttl_seconds=self._seconds_to_daily_reset() + 60,
            )
            if daily > rpd:
                retry_after = self._seconds_to_daily_reset()
                self.state.cooldown("gemini-candidate", candidate, seconds=retry_after)
                raise LocalCapacityError("local requests-per-day budget exhausted", retry_after)

    def _quota_cooldown(self, exc: Exception) -> int:
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return max(1, int(retry_after))
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            raw = headers.get("retry-after")
            try:
                if raw is not None:
                    return max(1, int(float(raw)))
            except (TypeError, ValueError):
                pass
        text = str(exc)
        match = _RETRY_SECONDS.search(text)
        if match:
            return max(1, int(float(match.group(1))))
        lowered = text.lower()
        if any(marker in lowered for marker in ("requests per day", "daily quota", " rpd")):
            return self._seconds_to_daily_reset()
        return self.quota_cooldown_seconds

    def _ordered_candidates(self, models: Sequence[str]) -> list[tuple[str, str]]:
        return [(model, key) for model in models for key in self.keys]

    def _available(self, models: Sequence[str]) -> list[tuple[str, str]]:
        ordered = self._ordered_candidates(models)
        state = self.state
        if state is not None:
            return [
                candidate
                for candidate in ordered
                if state.available("gemini-key", self._key_id(candidate[1]))
                and state.available("gemini-candidate", self._candidate_id(*candidate))
            ]

        now = time.monotonic()
        with self._lock:
            return [
                candidate
                for candidate in ordered
                if self._invalid_key_until.get(self._key_id(candidate[1]), 0) <= now
                and self._unavailable_until.get(self._candidate_id(*candidate), 0) <= now
            ]

    def _next_available_delay(self, models: Sequence[str]) -> int:
        ordered = self._ordered_candidates(models)
        state = self.state
        if state is not None:
            delays = [
                max(
                    state.remaining_cooldown("gemini-key", self._key_id(key)),
                    state.remaining_cooldown("gemini-candidate", self._candidate_id(model, key)),
                )
                for model, key in ordered
            ]
            positive = [int(delay) for delay in delays if delay > 0]
            return max(1, min(positive)) if positive else 1

        now = time.monotonic()
        with self._lock:
            delays = [
                max(
                    self._invalid_key_until.get(self._key_id(key), 0),
                    self._unavailable_until.get(self._candidate_id(model, key), 0),
                )
                - now
                for model, key in ordered
            ]
        positive = [int(delay) + 1 for delay in delays if delay > 0]
        return max(1, min(positive)) if positive else 1

    def _cooldown(self, model: str, key: str, seconds: int) -> None:
        candidate_id = self._candidate_id(model, key)
        if self.state is not None:
            self.state.cooldown("gemini-candidate", candidate_id, seconds=seconds)
            return
        with self._lock:
            self._unavailable_until[candidate_id] = time.monotonic() + seconds

    def _disable_key(self, key: str) -> None:
        key_id = self._key_id(key)
        if self.state is not None:
            self.state.cooldown("gemini-key", key_id, seconds=self.quota_cooldown_seconds)
            return
        with self._lock:
            self._invalid_key_until[key_id] = time.monotonic() + self.quota_cooldown_seconds

    @staticmethod
    def _model_for_candidate(model_name: str, key: str, temperature: float) -> ChatGoogleGenerativeAI:
        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": key,
            "max_retries": 0,
            "timeout": 60.0,
        }
        if model_name not in {"gemini-3.6-flash", "gemini-3.5-flash-lite"}:
            kwargs["temperature"] = temperature
        return ChatGoogleGenerativeAI(**kwargs)

    def _pooled_structured_call(
        self,
        model_name: str,
        key: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
        *,
        operation: str,
    ) -> tuple[dict[str, Any] | None, str, str | None, dict[str, Any]]:
        self._reserve_capacity(model_name, key, prompt)
        runnable = self._model_for_candidate(model_name, key, temperature).with_structured_output(
            schema=schema,
            method="json_schema",
            include_raw=True,
        )
        config: dict[str, Any] = {
            "metadata": {
                "operation": operation,
                "model": model_name,
                "account": self._key_id(key),
                "langfuse_tags": ["job-hunt", operation],
            }
        }
        if self.langfuse_handler is not None:
            config["callbacks"] = [self.langfuse_handler]
        started = time.perf_counter()
        result = runnable.invoke(prompt, config=config)  # type: ignore[arg-type]
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not isinstance(result, dict):
            return (
                None,
                "",
                "LangChain returned an unexpected structured-output wrapper",
                {"model": model_name, "account": self._key_id(key), "latency_ms": latency_ms},
            )
        parsed = result.get("parsed")
        raw = result.get("raw")
        raw_text = _message_text(raw)
        parsing_error = result.get("parsing_error")
        usage = getattr(raw, "usage_metadata", None)
        metadata = {
            "model": model_name,
            "account": self._key_id(key),
            "latency_ms": latency_ms,
            "usage": usage if isinstance(usage, dict) else {},
        }
        if isinstance(parsed, dict):
            return parsed, raw_text or _compact(parsed), str(parsing_error) if parsing_error else None, metadata
        return None, raw_text, str(parsing_error) if parsing_error else "No parsed JSON object", metadata

    @staticmethod
    def _checkpoint_digest(prompt: str, definition: PromptDefinition) -> str:
        payload = json.dumps(
            {
                "key": definition.key,
                "version": definition.version,
                "prompt": prompt,
                "schema": definition.output_structure,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _pooled_repair(
        self,
        raw_output: str,
        schema: dict[str, Any],
        validation_error: str,
        *,
        operation: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        repair_prompt = (
            "Repair the following model output so it conforms exactly to the supplied JSON Schema. "
            "Preserve the original meaning and facts. Do not add facts that were not present. "
            "Return only the repaired structured result.\n\n"
            f"VALIDATION ERROR:\n{validation_error}\n\n"
            f"JSON SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"ORIGINAL OUTPUT:\n{raw_output}"
        )
        candidates = self._available(self.repair_models)
        if not candidates:
            raise ProviderError(
                "Gemini repair capacity is temporarily unavailable",
                ErrorKind.RATE_LIMIT,
                retryable=True,
                provider="gemini",
                retry_after=self._next_available_delay(self.repair_models),
            )

        failures: list[str] = []
        invalid_keys: set[str] = set()
        attempted = 0
        quota_failures = 0
        quota_delays: list[int] = []
        for model_name, key in candidates:
            if self._key_id(key) in invalid_keys:
                continue
            attempted += 1
            try:
                parsed, _, parsing_error, metadata = self._pooled_structured_call(
                    model_name,
                    key,
                    repair_prompt,
                    schema,
                    0.0,
                    operation=f"{operation}:repair",
                )
                if parsed is None:
                    failures.append(f"{model_name}/{self._key_id(key)}: {parsing_error or 'no parsed output'}")
                    continue
                metadata["repaired"] = True
                return _validate_json_schema(parsed, schema), metadata
            except ConfigurationError:
                raise
            except Exception as exc:
                failures.append(f"{model_name}/{self._key_id(key)}: {exc}")
                if self._is_quota_error(exc):
                    quota_failures += 1
                    cooldown = self._quota_cooldown(exc)
                    quota_delays.append(cooldown)
                    self._cooldown(model_name, key, cooldown)
                elif self._is_auth_error(exc):
                    self._disable_key(key)
                continue

        rate_limited = attempted > 0 and quota_failures == attempted
        raise ProviderError(
            "Gemini structure-repair capacity failed across all configured candidates: " + "; ".join(failures),
            ErrorKind.RATE_LIMIT if rate_limited else ErrorKind.MALFORMED_PROVIDER_RESPONSE,
            retryable=rate_limited,
            provider="gemini",
            retry_after=min(quota_delays) if rate_limited and quota_delays else None,
        )

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        checkpoint = self._checkpoint_digest(prompt, definition)
        if self.state is not None:
            cached = self.state.get_checkpoint(checkpoint)
            if cached is not None:
                logger().info(
                    "gemini_checkpoint_hit",
                    prompt_key=definition.key,
                    prompt_version=definition.version,
                )
                return _validate_json_schema(cached, definition.output_structure)

        candidates = self._available(self.content_models)
        if not candidates:
            raise ProviderError(
                "Gemini content capacity is temporarily unavailable",
                ErrorKind.RATE_LIMIT,
                retryable=True,
                provider="gemini",
                retry_after=self._next_available_delay(self.content_models),
            )

        failures: list[str] = []
        quota_failures = 0
        quota_delays: list[int] = []
        attempted = 0
        invalid_keys: set[str] = set()
        for model_name, key in candidates:
            if self._key_id(key) in invalid_keys:
                continue
            attempted += 1
            try:
                parsed, raw_output, parsing_error, metadata = self._pooled_structured_call(
                    model_name,
                    key,
                    prompt,
                    definition.output_structure,
                    definition.temperature,
                    operation=definition.key,
                )
                final: dict[str, Any] | None = None
                if parsed is not None:
                    try:
                        final = _validate_json_schema(parsed, definition.output_structure)
                    except JsonSchemaValidationError as exc:
                        parsing_error = str(exc)
                        raw_output = raw_output or _compact(parsed)

                if final is None and raw_output:
                    final, metadata = self._pooled_repair(
                        raw_output,
                        definition.output_structure,
                        parsing_error or "structured output did not validate",
                        operation=definition.key,
                    )

                if final is not None:
                    if self.state is not None:
                        self.state.set_checkpoint(checkpoint, final, ttl_seconds=self.checkpoint_ttl_seconds)
                    logger().info(
                        "gemini_generation",
                        prompt_key=definition.key,
                        prompt_version=definition.version,
                        model=metadata.get("model"),
                        account=metadata.get("account"),
                        repaired=bool(metadata.get("repaired", False)),
                        latency_ms=metadata.get("latency_ms"),
                        usage=metadata.get("usage", {}),
                    )
                    return final

                failures.append(
                    f"{model_name}/{self._key_id(key)}: {parsing_error or 'Gemini returned an empty response'}"
                )
            except ConfigurationError:
                raise
            except ProviderError:
                raise
            except Exception as exc:
                failures.append(f"{model_name}/{self._key_id(key)}: {exc}")
                if self._is_quota_error(exc):
                    quota_failures += 1
                    cooldown = self._quota_cooldown(exc)
                    quota_delays.append(cooldown)
                    self._cooldown(model_name, key, cooldown)
                    continue
                if self._is_auth_error(exc):
                    invalid_keys.add(self._key_id(key))
                    self._disable_key(key)
                    continue
                continue

        rate_limited = attempted > 0 and quota_failures == attempted
        raise ProviderError(
            "Gemini generation failed across all configured keys and content models: " + "; ".join(failures),
            ErrorKind.RATE_LIMIT if rate_limited else ErrorKind.MALFORMED_PROVIDER_RESPONSE,
            retryable=True,
            provider="gemini",
            retry_after=min(quota_delays) if rate_limited and quota_delays else None,
        )
