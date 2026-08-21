from __future__ import annotations

from types import SimpleNamespace

import litellm
import pytest

from job_hunt.config import LlmRoute, Settings
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.gemini import GeminiStructuredClient
from job_hunt.integrations.llm_routing import (
    LiteLLMStructuredClient,
    RoutedStructuredClient,
    _error_kind,
    _provider_model,
    repair_route_specs,
)


def definition() -> PromptDefinition:
    return PromptDefinition(
        key="test",
        version=1,
        template="unused",
        temperature=0,
        output_structure={
            "type": "object",
            "properties": {"compatible": {"type": "boolean"}},
            "required": ["compatible"],
        },
    )


class StubClient(GeminiStructuredClient):
    def __init__(self, result: dict[str, object] | None = None, error: ProviderError | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, definition: PromptDefinition) -> dict[str, object]:
        self.calls += 1
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class StubRouter:
    def __init__(self, content: str | None = '{"compatible":true}', error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict[str, object]] = []

    def completion(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 4}),
            _hidden_params={"model_id": "deployment-test"},
        )


def test_route_parser_preserves_cross_provider_order() -> None:
    settings = Settings(llm_routes="cerebras:first,gemini:second,cerebras:third")
    assert [(route.provider, route.model) for route in settings.llm_route_specs()] == [
        ("cerebras", "first"),
        ("gemini", "second"),
        ("cerebras", "third"),
    ]


def test_repair_routes_are_independent_from_generation_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(llm_routes="cerebras:content-one,gemini:content-two", gemini_repair_models="legacy")
    monkeypatch.setenv("JOB_HUNT_LLM_REPAIR_ROUTES", "gemini:repair-one,cerebras:repair-two")
    assert [(route.provider, route.model) for route in repair_route_specs(settings)] == [
        ("gemini", "repair-one"),
        ("cerebras", "repair-two"),
    ]


def test_repair_routes_fall_back_to_legacy_gemini_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNT_LLM_REPAIR_ROUTES", raising=False)
    settings = Settings(gemini_repair_models="gemini-repair-a,gemini-repair-b")
    assert [(route.provider, route.model) for route in repair_route_specs(settings)] == [
        ("gemini", "gemini-repair-a"),
        ("gemini", "gemini-repair-b"),
    ]


def test_routed_client_preserves_fallback_order() -> None:
    first = StubClient(error=ProviderError("rate limited", ErrorKind.RATE_LIMIT, retryable=True, provider="cerebras"))
    second = StubClient(result={"compatible": True})
    client = RoutedStructuredClient(
        [
            (LlmRoute(provider="cerebras", model="first"), first),
            (LlmRoute(provider="gemini", model="second"), second),
        ]
    )
    assert client.generate("prompt", definition()) == {"compatible": True}
    assert first.calls == 1
    assert second.calls == 1


def test_litellm_client_sends_schema_and_returns_validated_json() -> None:
    router = StubRouter()
    client = LiteLLMStructuredClient("gemini", "gemini-test", ["key"], router=router)
    assert client.generate("prompt", definition()) == {"compatible": True}
    call = router.calls[0]
    assert call["model"] == "job-hunt-gemini-gemini-test"
    assert call["response_format"]["type"] == "json_schema"  # type: ignore[index]


def test_litellm_client_uses_independent_repair_route_for_invalid_json() -> None:
    repair = StubClient(result={"compatible": True})
    client = LiteLLMStructuredClient(
        "gemini",
        "gemini-test",
        ["key"],
        router=StubRouter(content="not json"),
        repair_client=repair,
    )
    assert client.generate("prompt", definition()) == {"compatible": True}
    assert repair.calls == 1
    assert "ORIGINAL OUTPUT" in repair.prompts[0]


def test_provider_model_uses_litellm_provider_prefix() -> None:
    assert _provider_model("gemini", "gemini-3.5-flash") == "gemini/gemini-3.5-flash"
    assert _provider_model("cerebras", "gpt-oss-120b") == "cerebras/gpt-oss-120b"


def test_litellm_rate_limit_maps_to_domain_error() -> None:
    exc = litellm.RateLimitError(message="limited", model="test", llm_provider="gemini", response=None)
    assert _error_kind(exc) == ErrorKind.RATE_LIMIT
