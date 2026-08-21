from __future__ import annotations

import json

import httpx
import pytest

from job_hunt.config import LlmRoute, Settings
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.gemini import GeminiStructuredClient
from job_hunt.integrations.llm_routing import (
    CerebrasStructuredClient,
    RoutedStructuredClient,
    _cerebras_schema,
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


def test_route_parser_preserves_cross_provider_order() -> None:
    settings = Settings(llm_routes="cerebras:first,gemini:second,cerebras:third")
    assert [(route.provider, route.model) for route in settings.llm_route_specs()] == [
        ("cerebras", "first"),
        ("gemini", "second"),
        ("cerebras", "third"),
    ]


def test_repair_routes_are_independent_from_generation_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        llm_routes="cerebras:content-one,gemini:content-two",
        gemini_repair_models="legacy-one,legacy-two",
    )
    monkeypatch.setenv(
        "JOB_HUNT_LLM_REPAIR_ROUTES",
        "gemini:repair-one,cerebras:repair-two,gemini:repair-three",
    )

    assert [(route.provider, route.model) for route in settings.llm_route_specs()] == [
        ("cerebras", "content-one"),
        ("gemini", "content-two"),
    ]
    assert [(route.provider, route.model) for route in repair_route_specs(settings)] == [
        ("gemini", "repair-one"),
        ("cerebras", "repair-two"),
        ("gemini", "repair-three"),
    ]


def test_repair_routes_fall_back_to_legacy_gemini_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOB_HUNT_LLM_REPAIR_ROUTES", raising=False)
    settings = Settings(gemini_repair_models="gemini-repair-a,gemini-repair-b")
    assert [(route.provider, route.model) for route in repair_route_specs(settings)] == [
        ("gemini", "gemini-repair-a"),
        ("gemini", "gemini-repair-b"),
    ]


def test_routed_client_falls_through_immediately() -> None:
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


def test_cerebras_tries_multiple_keys_and_uses_strict_schema() -> None:
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer bad-key":
            return httpx.Response(429, json={"message": "rate limit"})
        payload = json.loads(request.content)
        schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"compatible":true}'}}],
                "usage": {"total_tokens": 4},
            },
        )

    http = httpx.Client(
        base_url="https://api.cerebras.ai/v1",
        transport=httpx.MockTransport(handler),
    )
    client = CerebrasStructuredClient(["bad-key", "good-key"], "test-model", client=http)
    assert client.generate("prompt", definition()) == {"compatible": True}
    assert seen_auth == ["Bearer bad-key", "Bearer good-key"]


def test_cerebras_invalid_content_uses_independent_repair_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"compatible":"yes"}'}}]},
        )

    repair = StubClient(result={"compatible": True})
    http = httpx.Client(
        base_url="https://api.cerebras.ai/v1",
        transport=httpx.MockTransport(handler),
    )
    client = CerebrasStructuredClient(
        ["key"],
        "content-model",
        client=http,
        repair_client=repair,
    )

    assert client.generate("original prompt", definition()) == {"compatible": True}
    assert repair.calls == 1
    assert "ORIGINAL OUTPUT" in repair.prompts[0]
    assert '{"compatible":"yes"}' in repair.prompts[0]


def test_cerebras_schema_normalizes_nested_objects_without_mutating_source() -> None:
    source = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }
        },
    }
    normalized = _cerebras_schema(source)
    assert normalized["additionalProperties"] is False
    assert normalized["properties"]["nested"]["additionalProperties"] is False
    assert "additionalProperties" not in source
