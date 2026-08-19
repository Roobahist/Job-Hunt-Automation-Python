from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from threading import Lock
from typing import Any

from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from langchain_google_genai import ChatGoogleGenerativeAI

from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError, ErrorKind, ProviderError
from job_hunt.integrations.gemini import _compact, _message_text, _validate_json_schema


class PooledGeminiStructuredClient:
    """Use shared Gemini accounts and ordered model tiers for structured generation."""

    _lock = Lock()
    _unavailable_until: dict[str, float] = {}

    def __init__(
        self,
        keys: Sequence[str],
        content_models: Sequence[str],
        repair_models: Sequence[str],
        *,
        quota_cooldown_seconds: int = 3600,
    ) -> None:
        self.keys = [key for key in keys if key]
        self.content_models = [model.removeprefix("models/") for model in content_models if model]
        self.repair_models = [model.removeprefix("models/") for model in repair_models if model]
        self.quota_cooldown_seconds = quota_cooldown_seconds
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

    def _available(self, models: Sequence[str]) -> list[tuple[str, str]]:
        now = time.monotonic()
        ordered = [(model, key) for model in models for key in self.keys]
        with self._lock:
            available = [
                candidate
                for candidate in ordered
                if self._unavailable_until.get(self._candidate_id(*candidate), 0) <= now
            ]
            if available:
                return available
            # Capacity may have reset while every local circuit is still open. Probe the
            # candidate whose cooldown expires first rather than failing without a request.
            return [
                min(
                    ordered,
                    key=lambda candidate: self._unavailable_until.get(
                        self._candidate_id(*candidate), 0
                    ),
                )
            ]

    def _cooldown(self, model: str, key: str) -> None:
        with self._lock:
            self._unavailable_until[self._candidate_id(model, key)] = (
                time.monotonic() + self.quota_cooldown_seconds
            )

    @staticmethod
    def _model(
        model_name: str,
        key: str,
        temperature: float,
    ) -> ChatGoogleGenerativeAI:
        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": key,
            "max_retries": 0,
        }
        # Google's latest-model API no longer uses sampling temperature on these models.
        if model_name not in {"gemini-3.6-flash", "gemini-3.5-flash-lite"}:
            kwargs["temperature"] = temperature
        return ChatGoogleGenerativeAI(**kwargs)

    def _structured_call(
        self,
        model_name: str,
        key: str,
        prompt: str,
        schema: dict[str, Any],
        temperature: float,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        runnable = self._model(model_name, key, temperature).with_structured_output(
            schema=schema,
            method="json_schema",
            include_raw=True,
        )
        result = runnable.invoke(prompt)
        if not isinstance(result, dict):
            return None, "", "LangChain returned an unexpected structured-output wrapper"
        parsed = result.get("parsed")
        raw_text = _message_text(result.get("raw"))
        parsing_error = result.get("parsing_error")
        if isinstance(parsed, dict):
            return parsed, raw_text or _compact(parsed), str(parsing_error) if parsing_error else None
        return None, raw_text, str(parsing_error) if parsing_error else "No parsed JSON object"

    def _repair(
        self,
        raw_output: str,
        schema: dict[str, Any],
        validation_error: str,
    ) -> dict[str, Any]:
        repair_prompt = (
            "Repair the following model output so it conforms exactly to the supplied JSON Schema. "
            "Preserve the original meaning and facts. Do not add facts that were not present. "
            "Return only the repaired structured result.\n\n"
            f"VALIDATION ERROR:\n{validation_error}\n\n"
            f"JSON SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"ORIGINAL OUTPUT:\n{raw_output}"
        )
        failures: list[str] = []
        for model_name, key in self._available(self.repair_models):
            try:
                parsed, _, parsing_error = self._structured_call(
                    model_name, key, repair_prompt, schema, 0.0
                )
                if parsed is None:
                    failures.append(
                        f"{model_name}/{self._key_id(key)}: {parsing_error or 'no parsed output'}"
                    )
                    continue
                return _validate_json_schema(parsed, schema)
            except ConfigurationError:
                raise
            except Exception as exc:
                failures.append(f"{model_name}/{self._key_id(key)}: {exc}")
                if self._is_quota_error(exc) or self._is_auth_error(exc):
                    self._cooldown(model_name, key)
                continue
        raise ProviderError(
            "Gemini structure-repair capacity failed across all configured candidates: "
            + "; ".join(failures),
            ErrorKind.MALFORMED_PROVIDER_RESPONSE,
            provider="gemini",
        )

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, Any]:
        failures: list[str] = []
        for model_name, key in self._available(self.content_models):
            try:
                parsed, raw_output, parsing_error = self._structured_call(
                    model_name,
                    key,
                    prompt,
                    definition.output_structure,
                    definition.temperature,
                )
                if parsed is not None:
                    try:
                        return _validate_json_schema(parsed, definition.output_structure)
                    except JsonSchemaValidationError as exc:
                        parsing_error = str(exc)
                        raw_output = raw_output or _compact(parsed)

                if raw_output:
                    return self._repair(
                        raw_output,
                        definition.output_structure,
                        parsing_error or "structured output did not validate",
                    )
                failures.append(
                    f"{model_name}/{self._key_id(key)}: "
                    f"{parsing_error or 'Gemini returned an empty response'}"
                )
            except ConfigurationError:
                raise
            except ProviderError:
                raise
            except Exception as exc:
                failures.append(f"{model_name}/{self._key_id(key)}: {exc}")
                if self._is_quota_error(exc) or self._is_auth_error(exc):
                    self._cooldown(model_name, key)
                    continue
                # A model/provider-specific failure can still be recovered by another account
                # or lower-ranked model. Schema/configuration failures are raised separately.
                continue

        raise ProviderError(
            "Gemini generation failed across all configured keys and content models: "
            + "; ".join(failures),
            ErrorKind.RATE_LIMIT
            if failures and all("quota" in failure.lower() for failure in failures)
            else ErrorKind.MALFORMED_PROVIDER_RESPONSE,
            retryable=True,
            provider="gemini",
        )
