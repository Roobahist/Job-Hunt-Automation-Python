from __future__ import annotations

from job_hunt.integrations.litellm_config import build_litellm_config


def _registry() -> dict[str, object]:
    return {
        "providers": [
            {
                "name": "groq",
                "litellm_prefix": "groq",
                "api_key_prefix": "GROQ_API_KEY_",
                "enabled": True,
                "discovery": {"enabled": True, "url": "https://example.test/models"},
                "models": {"fast": [], "balanced": [], "powerful": []},
                "exclude_models": [],
            }
        ]
    }


def _discover(_key: str, _url: str) -> list[str]:
    return ["llama-8b", "qwen-32b", "openai/gpt-oss-120b"]


def test_first_provider_failure_is_cooled_immediately() -> None:
    config = build_litellm_config(
        env={"GROQ_API_KEY_1": "one"},
        registry=_registry(),
        discoverer=_discover,
    )

    assert config["router_settings"]["allowed_fails"] == 0


def test_retry_budget_covers_every_reachable_deployment() -> None:
    config = build_litellm_config(
        env={"GROQ_API_KEY_1": "one", "GROQ_API_KEY_2": "two"},
        registry=_registry(),
        discoverer=_discover,
    )

    # Two keys x one deployment in each job capability group means six deployments
    # are reachable from job-powerful. The initial attempt plus five retries can visit all six.
    assert config["router_settings"]["num_retries"] == 5


def test_retry_budget_scales_when_an_api_key_is_added() -> None:
    two_keys = build_litellm_config(
        env={"GROQ_API_KEY_1": "one", "GROQ_API_KEY_2": "two"},
        registry=_registry(),
        discoverer=_discover,
    )
    three_keys = build_litellm_config(
        env={
            "GROQ_API_KEY_1": "one",
            "GROQ_API_KEY_2": "two",
            "GROQ_API_KEY_3": "three",
        },
        registry=_registry(),
        discoverer=_discover,
    )

    assert two_keys["router_settings"]["num_retries"] == 5
    assert three_keys["router_settings"]["num_retries"] == 8
