from __future__ import annotations

import pytest

from job_hunt.integrations.litellm_config import ConfigGenerationError, build_litellm_config


def _registry() -> dict[str, object]:
    return {
        "providers": [
            {
                "name": "test",
                "litellm_prefix": "openai",
                "api_key_prefix": "TEST_API_KEY_",
                "enabled": True,
                "discovery": {"enabled": False},
                "models": {
                    "fast": ["fast-model"],
                    "balanced": ["balanced-model"],
                    "powerful": ["powerful-model"],
                },
                "exclude_models": [],
            }
        ]
    }


def _multi_provider_registry() -> dict[str, object]:
    return {
        "providers": [
            {
                "name": "alpha",
                "litellm_prefix": "openai",
                "api_key_prefix": "ALPHA_API_KEY_",
                "enabled": True,
                "discovery": {"enabled": False},
                "models": {"fast": ["alpha-fast"], "balanced": [], "powerful": []},
                "exclude_models": [],
            },
            {
                "name": "beta",
                "litellm_prefix": "groq",
                "api_key_prefix": "BETA_API_KEY_",
                "enabled": True,
                "discovery": {"enabled": False},
                "models": {"fast": ["beta-fast"], "balanced": [], "powerful": []},
                "exclude_models": [],
            },
        ]
    }


def test_litellm_gets_enough_retries_to_exhaust_reachable_deployments() -> None:
    config = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "TEST_API_KEY_2": "two"},
        registry=_registry(),
    )
    router = config["router_settings"]

    assert router["num_retries"] == 5
    assert router["allowed_fails"] == 0
    assert router["cooldown_time"] == 65


def test_each_provider_key_is_a_separate_litellm_deployment() -> None:
    config = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "TEST_API_KEY_2": "two", "TEST_API_KEY_3": "three"},
        registry=_registry(),
    )
    job_fast = [entry for entry in config["model_list"] if entry["model_name"] == "job-fast"]
    assert len(job_fast) == 3
    assert {entry["litellm_params"]["api_key"] for entry in job_fast} == {
        "os.environ/TEST_API_KEY_1",
        "os.environ/TEST_API_KEY_2",
        "os.environ/TEST_API_KEY_3",
    }


def test_retry_budget_scales_when_api_keys_are_added() -> None:
    two_keys = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "TEST_API_KEY_2": "two"},
        registry=_registry(),
    )
    three_keys = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "TEST_API_KEY_2": "two", "TEST_API_KEY_3": "three"},
        registry=_registry(),
    )

    assert two_keys["router_settings"]["num_retries"] == 5
    assert three_keys["router_settings"]["num_retries"] == 8


def test_models_and_keys_from_multiple_providers_share_the_capability_pool() -> None:
    config = build_litellm_config(
        env={
            "ALPHA_API_KEY_1": "a1",
            "ALPHA_API_KEY_2": "a2",
            "BETA_API_KEY_1": "b1",
            "BETA_API_KEY_2": "b2",
            "BETA_API_KEY_3": "b3",
        },
        registry=_multi_provider_registry(),
    )
    job_fast = [entry for entry in config["model_list"] if entry["model_name"] == "job-fast"]
    assert len(job_fast) == 5
    assert {entry["litellm_params"]["model"] for entry in job_fast} == {
        "openai/alpha-fast",
        "groq/beta-fast",
    }
    assert {entry["litellm_params"]["api_key"] for entry in job_fast} == {
        "os.environ/ALPHA_API_KEY_1",
        "os.environ/ALPHA_API_KEY_2",
        "os.environ/BETA_API_KEY_1",
        "os.environ/BETA_API_KEY_2",
        "os.environ/BETA_API_KEY_3",
    }


def test_capability_fallback_graph_is_delegated_to_litellm() -> None:
    config = build_litellm_config(
        env={"TEST_API_KEY_1": "one"},
        registry=_registry(),
    )
    assert config["router_settings"]["fallbacks"] == [
        {"job-powerful": ["job-balanced", "job-fast"]},
        {"job-balanced": ["job-fast"]},
        {"repair-fast": ["repair-balanced"]},
    ]


def test_router_strategy_and_cooldown_remain_environment_configurable() -> None:
    config = build_litellm_config(
        env={
            "TEST_API_KEY_1": "one",
            "LITELLM_COOLDOWN_SECONDS": "90",
            "LITELLM_ROUTING_STRATEGY": "simple-shuffle",
        },
        registry=_registry(),
    )
    router = config["router_settings"]
    assert router["allowed_fails"] == 0
    assert router["cooldown_time"] == 90
    assert router["routing_strategy"] == "simple-shuffle"
    assert router["num_retries"] == 2


def test_stale_allowed_fails_env_cannot_disable_immediate_failover() -> None:
    config = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "LITELLM_ALLOWED_FAILS": "99"},
        registry=_registry(),
    )
    assert config["router_settings"]["allowed_fails"] == 0


@pytest.mark.parametrize("value", ["-1", "bad"])
def test_invalid_cooldown_fails_config_generation(value: str) -> None:
    with pytest.raises(ConfigGenerationError, match="LITELLM_COOLDOWN_SECONDS"):
        build_litellm_config(
            env={"TEST_API_KEY_1": "one", "LITELLM_COOLDOWN_SECONDS": value},
            registry=_registry(),
        )
