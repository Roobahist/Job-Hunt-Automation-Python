from __future__ import annotations

import json

import httpx
import pytest

from job_hunt.config import LlmRoute, Settings
from job_hunt.domain.models import PromptDefinition
from job_hunt.errors import ConfigurationError, ErrorKind, ProviderError
from job_hunt.integrations.gemini import GeminiStructuredClient
from job_hunt.integrations.llm_routing import LiteLLMGatewayClient, RoutedStructuredClient, _error_kind


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


def gateway_transport(content: str = '{"compatible":true}', status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if status_code >= 400:
            return httpx.Response(status_code, text="upstream failed")
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "upstream/provider-model",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 4},
            },
        )

    return httpx.MockTransport(handler)


def test_generation_routes_are_litellm_aliases_in_order() -> None:
    settings = Settings(llm_routes="groq-gpt-oss,gemini-flash,gemini-lite")
    assert [(route.provider, route.model) for route in settings.llm_route_specs()] == [
        ("litellm", "groq-gpt-oss"),
        ("litellm", "gemini-flash"),
        ("litellm", "gemini-lite"),
    ]


def test_generation_routes_reject_old_provider_model_syntax() -> None:
    settings = Settings(llm_routes="gemini:gemini-3.5-flash")
    with pytest.raises(ConfigurationError):
        settings.llm_route_specs()


def test_repair_routes_are_independent() -> None:
    settings = Settings(llm_routes="content", llm_repair_routes="repair-fast,repair-backup")
    assert [route.model for route in settings.llm_repair_route_specs()] == ["repair-fast", "repair-backup"]


def test_routed_client_preserves_alias_fallback_order() -> None:
    first = StubClient(error=ProviderError("limited", ErrorKind.RATE_LIMIT, retryable=True, provider="litellm"))
    second = StubClient(result={"compatible": True})
    client = RoutedStructuredClient(
        [
            (LlmRoute(provider="litellm", model="first"), first),
            (LlmRoute(provider="litellm", model="second"), second),
        ]
    )
    assert client.generate("prompt", definition()) == {"compatible": True}
    assert first.calls == 1
    assert second.calls == 1


def test_gateway_client_sends_alias_schema_and_authentication() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "groq/openai/gpt-oss-120b",
                "choices": [{"message": {"content": '{"compatible":true}'}}],
                "usage": {},
            },
        )

    client = LiteLLMGatewayClient(
        "http://litellm:4000",
        "secret",
        "groq-gpt-oss",
        transport=httpx.MockTransport(handler),
    )
    assert client.generate("prompt", definition()) == {"compatible": True}
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    assert payload["model"] == "groq-gpt-oss"
    assert payload["response_format"]["type"] == "json_schema"


def test_gateway_client_uses_independent_repair_route_for_invalid_json() -> None:
    repair = StubClient(result={"compatible": True})
    client = LiteLLMGatewayClient(
        "http://litellm:4000",
        "secret",
        "content",
        repair_client=repair,
        transport=gateway_transport(content="not json"),
    )
    assert client.generate("prompt", definition()) == {"compatible": True}
    assert repair.calls == 1
    assert "ORIGINAL OUTPUT" in repair.prompts[0]


def test_gateway_rate_limit_maps_to_domain_error() -> None:
    client = LiteLLMGatewayClient(
        "http://litellm:4000",
        "secret",
        "content",
        transport=gateway_transport(status_code=429),
    )
    with pytest.raises(ProviderError) as caught:
        client.generate("prompt", definition())
    assert caught.value.kind == ErrorKind.RATE_LIMIT
    assert caught.value.retryable is True


def test_http_status_error_mapping() -> None:
    assert _error_kind(429) == ErrorKind.RATE_LIMIT
    assert _error_kind(401) == ErrorKind.AUTHENTICATION
    assert _error_kind(422) == ErrorKind.MALFORMED_PROVIDER_RESPONSE
    assert _error_kind(503) == ErrorKind.TRANSIENT_PROVIDER
