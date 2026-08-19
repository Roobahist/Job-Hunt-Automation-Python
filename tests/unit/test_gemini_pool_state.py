from __future__ import annotations

from typing import Any

import pytest

from job_hunt.domain.models import PromptDefinition
from job_hunt.integrations.gemini_pool import LocalCapacityError, PooledGeminiStructuredClient


class State:
    def __init__(self) -> None:
        self.counters: dict[tuple[str, str, str], int] = {}
        self.cooldowns: set[tuple[str, str]] = set()
        self.checkpoints: dict[str, dict[str, Any]] = {}

    def increment_window(
        self,
        scope: str,
        resource: str,
        window: str,
        *,
        ttl_seconds: int,
        amount: int = 1,
    ) -> int:
        del ttl_seconds
        key = (scope, resource, window)
        self.counters[key] = self.counters.get(key, 0) + amount
        return self.counters[key]

    def cooldown(self, scope: str, resource: str, *, seconds: float) -> None:
        del seconds
        self.cooldowns.add((scope, resource))

    def available(self, scope: str, resource: str) -> bool:
        return (scope, resource) not in self.cooldowns

    def remaining_cooldown(self, scope: str, resource: str) -> int:
        return 1 if (scope, resource) in self.cooldowns else 0

    def get_checkpoint(self, digest: str) -> dict[str, Any] | None:
        return self.checkpoints.get(digest)

    def set_checkpoint(self, digest: str, value: dict[str, Any], *, ttl_seconds: int) -> None:
        del ttl_seconds
        self.checkpoints[digest] = dict(value)


def definition() -> PromptDefinition:
    return PromptDefinition(
        key="stage",
        version=2,
        template="unused",
        output_structure={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        temperature=0.2,
    )


def test_local_rpm_budget_opens_candidate_cooldown() -> None:
    state = State()
    client = PooledGeminiStructuredClient(
        ["account-key"],
        ["best"],
        ["repair"],
        state=state,  # type: ignore[arg-type]
        limits={"best": {"rpm": 1}},
    )
    client._reserve_capacity("best", "account-key", "first")
    with pytest.raises(LocalCapacityError):
        client._reserve_capacity("best", "account-key", "second")
    candidate = client._candidate_id("best", "account-key")
    assert ("gemini-candidate", candidate) in state.cooldowns


def test_checkpoint_hit_skips_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    state = State()
    client = PooledGeminiStructuredClient(
        ["key"], ["best"], ["repair"], state=state  # type: ignore[arg-type]
    )
    prompt = "rendered prompt"
    digest = client._checkpoint_digest(prompt, definition())
    state.checkpoints[digest] = {"value": "cached"}

    def unexpected(*_: object, **__: object) -> object:
        raise AssertionError("provider should not be called on a checkpoint hit")

    monkeypatch.setattr(client, "_pooled_structured_call", unexpected)
    assert client.generate(prompt, definition()) == {"value": "cached"}
