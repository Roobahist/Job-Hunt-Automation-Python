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


def test_litellm_router_retries_deployments_immediately_by_default() -> None:
    config = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "TEST_API_KEY_2": "two"},
        registry=_registry(),
    )
    assert config["router_settings"]["num_retries"] == 8
    assert config["router_settings"]["cooldown_time"] == 65


def test_litellm_router_retry_count_is_environment_configurable() -> None:
    config = build_litellm_config(
        env={"TEST_API_KEY_1": "one", "LITELLM_NUM_RETRIES": "3"},
        registry=_registry(),
    )
    assert config["router_settings"]["num_retries"] == 3


def test_litellm_router_rejects_negative_retry_count() -> None:
    with pytest.raises(ConfigGenerationError, match="LITELLM_NUM_RETRIES"):
        build_litellm_config(
            env={"TEST_API_KEY_1": "one", "LITELLM_NUM_RETRIES": "-1"},
            registry=_registry(),
        )
