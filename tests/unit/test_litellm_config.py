from __future__ import annotations

from pathlib import Path

import pytest

from job_hunt.integrations.litellm_config import (
    ConfigGenerationError,
    build_litellm_config,
    classify_model,
    generate_litellm_config,
    group_models,
    indexed_env_values,
)


def test_indexed_env_values_orders_numbered_keys() -> None:
    env = {"GROQ_API_KEY_10": "ten", "GROQ_API_KEY_2": "two", "GROQ_API_KEY_1": "one"}
    assert indexed_env_values(env, "GROQ_API_KEY_") == [
        ("GROQ_API_KEY_1", "one"),
        ("GROQ_API_KEY_2", "two"),
        ("GROQ_API_KEY_10", "ten"),
    ]


def test_model_size_heuristic_assigns_capabilities() -> None:
    assert classify_model("openai/gpt-oss-120b") == "job-powerful"
    assert classify_model("qwen/qwen3-32b") == "job-balanced"
    assert classify_model("llama-3.1-8b-instant") == "job-fast"


def test_explicit_classification_overrides_heuristic_and_filters_non_chat() -> None:
    groups = group_models(
        ["openai/gpt-oss-120b", "whisper-large-v3", "llama-3.1-8b-instant"],
        env={"LITELLM_GROQ_FAST_MODELS": "openai/gpt-oss-120b"},
    )
    assert "openai/gpt-oss-120b" in groups["job-fast"]
    assert "openai/gpt-oss-120b" not in groups["job-powerful"]
    assert all("whisper" not in model for models in groups.values() for model in models)


def test_generator_expands_every_key_across_discovered_models() -> None:
    env = {
        "GROQ_API_KEY_1": "key-one",
        "GROQ_API_KEY_2": "key-two",
        "GEMINI_API_KEY_1": "gemini-one",
        "LITELLM_GEMINI_FAST_MODELS": "gemini-lite",
        "LITELLM_GEMINI_BALANCED_MODELS": "gemini-flash",
        "LITELLM_GEMINI_POWERFUL_MODELS": "gemini-pro",
    }
    config = build_litellm_config(
        env=env,
        discover_groq=lambda _: ["openai/gpt-oss-120b", "llama-3.1-8b-instant"],
    )
    deployments = config["model_list"]
    powerful_groq = [
        item
        for item in deployments
        if item["model_name"] == "job-powerful" and item["litellm_params"]["model"] == "groq/openai/gpt-oss-120b"
    ]
    assert len(powerful_groq) == 2
    assert {item["litellm_params"]["api_key"] for item in powerful_groq} == {
        "os.environ/GROQ_API_KEY_1",
        "os.environ/GROQ_API_KEY_2",
    }


def test_generated_config_contains_native_group_fallbacks() -> None:
    config = build_litellm_config(
        env={"GROQ_API_KEY_1": "key"},
        discover_groq=lambda _: ["openai/gpt-oss-120b", "qwen-32b", "llama-8b"],
    )
    assert config["router_settings"]["fallbacks"] == [
        {"job-powerful": ["job-balanced", "job-fast"]},
        {"job-balanced": ["job-fast"]},
    ]


def test_discovery_can_be_disabled_with_explicit_models() -> None:
    called = False

    def discover(_: str) -> list[str]:
        nonlocal called
        called = True
        return []

    config = build_litellm_config(
        env={
            "GROQ_API_KEY_1": "key",
            "LITELLM_GROQ_DISCOVER_MODELS": "false",
            "LITELLM_GROQ_POWERFUL_MODELS": "openai/gpt-oss-120b",
        },
        discover_groq=discover,
    )
    assert called is False
    assert config["model_list"][0]["model_name"] == "job-powerful"


def test_generation_requires_at_least_one_provider_key() -> None:
    with pytest.raises(ConfigGenerationError):
        build_litellm_config(env={}, discover_groq=lambda _: [])


def test_high_level_generator_writes_runtime_config(tmp_path: Path) -> None:
    destination = tmp_path / "litellm.runtime.yaml"
    config = generate_litellm_config(
        env={"GROQ_API_KEY_1": "key", "LITELLM_GROQ_FAST_MODELS": "llama-8b"},
        discover_groq=lambda _: [],
        destination=destination,
    )
    assert destination.exists()
    assert '"job-fast"' in destination.read_text()
    assert config["model_list"]
