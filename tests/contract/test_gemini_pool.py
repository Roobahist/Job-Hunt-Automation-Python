from __future__ import annotations

from typing import Any

import pytest

from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.gemini_pool import PooledGeminiStructuredClient


def definition() -> PromptDefinition:
    return PromptDefinition(
        key="test",
        version=1,
        template="unused",
        output_structure={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        temperature=0.2,
    )


def reset_pool() -> None:
    PooledGeminiStructuredClient._unavailable_until.clear()
    PooledGeminiStructuredClient._invalid_key_until.clear()


def response(value: dict[str, Any]) -> tuple[dict[str, Any], str, None, dict[str, Any]]:
    return value, "json", None, {"model": "test", "account": "account", "latency_ms": 1}


def test_content_exhausts_all_keys_before_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_pool()
    client = PooledGeminiStructuredClient(["key-1", "key-2"], ["best", "second"], ["repair"])
    calls: list[tuple[str, str]] = []

    def call(model: str, key: str, *_: object, **__: object) -> tuple[dict[str, Any], str, None, dict[str, Any]]:
        calls.append((model, key))
        if model == "best":
            raise RuntimeError("RESOURCE_EXHAUSTED: quota exceeded")
        return response({"value": "ok"})

    monkeypatch.setattr(client, "_pooled_structured_call", call)
    assert client.generate("prompt", definition()) == {"value": "ok"}
    assert calls == [("best", "key-1"), ("best", "key-2"), ("second", "key-1")]


def test_invalid_key_is_skipped_for_lower_models(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_pool()
    client = PooledGeminiStructuredClient(["bad-key", "good-key"], ["best", "second"], ["repair"])
    calls: list[tuple[str, str]] = []

    def call(model: str, key: str, *_: object, **__: object) -> tuple[dict[str, Any], str, None, dict[str, Any]]:
        calls.append((model, key))
        if key == "bad-key":
            raise RuntimeError("API key not valid")
        if model == "best":
            raise RuntimeError("RESOURCE_EXHAUSTED: quota exceeded")
        return response({"value": "ok"})

    monkeypatch.setattr(client, "_pooled_structured_call", call)
    assert client.generate("prompt", definition()) == {"value": "ok"}
    assert calls == [("best", "bad-key"), ("best", "good-key"), ("second", "good-key")]


def test_invalid_content_output_uses_repair_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_pool()
    client = PooledGeminiStructuredClient(["key-1", "key-2"], ["best", "second"], ["fast-repair", "backup-repair"])
    calls: list[tuple[str, str]] = []

    def call(
        model: str, key: str, *_: object, **__: object
    ) -> tuple[dict[str, Any] | None, str, str | None, dict[str, Any]]:
        calls.append((model, key))
        metadata = {"model": model, "account": key, "latency_ms": 1}
        if model == "best":
            return {"wrong": "shape"}, '{"wrong":"shape"}', None, metadata
        if model == "fast-repair":
            return {"value": "fixed"}, '{"value":"fixed"}', None, metadata
        raise AssertionError("Unexpected candidate")

    monkeypatch.setattr(client, "_pooled_structured_call", call)
    assert client.generate("prompt", definition()) == {"value": "fixed"}
    assert calls == [("best", "key-1"), ("fast-repair", "key-1")]


def test_repair_rotates_keys_before_weaker_repair_model(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_pool()
    client = PooledGeminiStructuredClient(["key-1", "key-2"], ["best"], ["fast-repair", "backup-repair"])
    calls: list[tuple[str, str]] = []

    def call(
        model: str, key: str, *_: object, **__: object
    ) -> tuple[dict[str, Any] | None, str, str | None, dict[str, Any]]:
        calls.append((model, key))
        metadata = {"model": model, "account": key, "latency_ms": 1}
        if model == "best":
            return {"wrong": "shape"}, '{"wrong":"shape"}', None, metadata
        if model == "fast-repair" and key == "key-1":
            raise RuntimeError("429 quota exceeded")
        if model == "fast-repair" and key == "key-2":
            return {"value": "fixed"}, '{"value":"fixed"}', None, metadata
        raise AssertionError("Unexpected candidate")

    monkeypatch.setattr(client, "_pooled_structured_call", call)
    assert client.generate("prompt", definition()) == {"value": "fixed"}
    assert calls == [("best", "key-1"), ("fast-repair", "key-1"), ("fast-repair", "key-2")]


def test_repair_provider_error_does_not_abort_remaining_content_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_pool()
    client = PooledGeminiStructuredClient(["key-1", "key-2"], ["best"], ["repair"])
    content_calls: list[tuple[str, str]] = []
    repair_calls = 0

    def call(
        model: str, key: str, *_: object, **__: object
    ) -> tuple[dict[str, Any] | None, str, str | None, dict[str, Any]]:
        content_calls.append((model, key))
        metadata = {"model": model, "account": key, "latency_ms": 1}
        if key == "key-1":
            return {"wrong": "shape"}, '{"wrong":"shape"}', None, metadata
        return {"value": "ok"}, '{"value":"ok"}', None, metadata

    def repair(*_: object, **__: object) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal repair_calls
        repair_calls += 1
        raise ProviderError(
            "repair capacity exhausted",
            ErrorKind.RATE_LIMIT,
            retryable=True,
            provider="gemini",
            retry_after=300,
        )

    monkeypatch.setattr(client, "_pooled_structured_call", call)
    monkeypatch.setattr(client, "_pooled_repair", repair)

    assert client.generate("prompt", definition()) == {"value": "ok"}
    assert repair_calls == 1
    assert content_calls == [("best", "key-1"), ("best", "key-2")]
