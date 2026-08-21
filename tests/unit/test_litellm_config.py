from __future__ import annotations

from pathlib import Path

import pytest

from job_hunt.integrations.litellm_config import (
    ConfigGenerationError,
    build_litellm_config,
    classify_model,
    discover_with_key_fallback,
    generate_litellm_config,
    group_models,
    indexed_env_values,
    load_provider_registry,
)


def registry(*providers: dict[str, object]) -> dict[str, object]:
    return {"providers": list(providers)}


def provider(
    name: str,
    key_prefix: str,
    *,
    litellm_prefix: str | None = None,
    discovery_url: str | None = None,
    fast: list[str] | None = None,
    balanced: list[str] | None = None,
    powerful: list[str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "litellm_prefix": litellm_prefix or name,
        "api_key_prefix": key_prefix,
        "enabled": True,
        "discovery": {"enabled": discovery_url is not None, "url": discovery_url or ""},
        "models": {
            "fast": fast or [],
            "balanced": balanced or [],
            "powerful": powerful or [],
        },
        "exclude_models": [],
    }


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


def test_explicit_classification_and_non_chat_filtering() -> None:
    groups = group_models(
        ["openai/gpt-oss-120b", "whisper-large-v3", "llama-3.1-8b-instant"],
        explicit={"job-fast": ["openai/gpt-oss-120b"]},
    )
    assert "openai/gpt-oss-120b" in groups["job-fast"]
    assert all("whisper" not in model for models in groups.values() for model in models)


def test_missing_capability_bucket_borrows_nearest_available_models() -> None:
    groups = group_models(["openai/gpt-oss-120b"])
    assert groups["job-powerful"] == ["openai/gpt-oss-120b"]
    assert groups["job-balanced"] == ["openai/gpt-oss-120b"]
    assert groups["job-fast"] == ["openai/gpt-oss-120b"]


def test_excluded_models_never_enter_any_capability_pool() -> None:
    groups = group_models(
        ["openai/gpt-oss-120b", "llama-3.1-8b-instant"],
        excluded=["openai/gpt-oss-120b"],
    )
    assert all("openai/gpt-oss-120b" not in models for models in groups.values())


def test_discovery_falls_through_invalid_keys() -> None:
    calls: list[str] = []

    def discover(key: str) -> list[str]:
        calls.append(key)
        if key == "bad":
            raise RuntimeError("invalid key")
        return ["llama-8b"]

    models = discover_with_key_fallback(
        [("ANY_API_KEY_1", "bad"), ("ANY_API_KEY_2", "good")],
        discover,
    )
    assert models == ["llama-8b"]
    assert calls == ["bad", "good"]


def test_generator_expands_every_key_across_discovered_models() -> None:
    env = {"GROQ_API_KEY_1": "key-one", "GROQ_API_KEY_2": "key-two"}
    config = build_litellm_config(
        env=env,
        registry=registry(provider("groq", "GROQ_API_KEY_", discovery_url="https://example.test/models")),
        discoverer=lambda _key, _url: ["openai/gpt-oss-120b", "llama-3.1-8b-instant"],
    )
    deployments = config["model_list"]
    powerful = [
        item
        for item in deployments
        if item["model_name"] == "job-powerful" and item["litellm_params"]["model"] == "groq/openai/gpt-oss-120b"
    ]
    assert len(powerful) == 2
    assert {item["litellm_params"]["api_key"] for item in powerful} == {
        "os.environ/GROQ_API_KEY_1",
        "os.environ/GROQ_API_KEY_2",
    }


def test_arbitrary_curated_provider_requires_no_python_adapter() -> None:
    config = build_litellm_config(
        env={"OPENROUTER_API_KEY_1": "key"},
        registry=registry(
            provider(
                "openrouter",
                "OPENROUTER_API_KEY_",
                fast=["meta-llama/llama-3.1-8b-instruct:free"],
                balanced=["qwen/qwen3-30b-a3b:free"],
                powerful=["openai/gpt-oss-120b:free"],
            )
        ),
    )
    models = {item["litellm_params"]["model"] for item in config["model_list"]}
    assert "openrouter/meta-llama/llama-3.1-8b-instruct:free" in models
    assert "openrouter/qwen/qwen3-30b-a3b:free" in models
    assert "openrouter/openai/gpt-oss-120b:free" in models


def test_arbitrary_openai_compatible_provider_can_discover_models() -> None:
    calls: list[tuple[str, str]] = []

    def discover(key: str, url: str) -> list[str]:
        calls.append((key, url))
        return ["vendor-70b", "vendor-8b"]

    custom_provider = provider(
        "vendor",
        "VENDOR_API_KEY_",
        litellm_prefix="openai",
        discovery_url="https://vendor.test/v1/models",
    )
    custom_provider["litellm_params"] = {"api_base": "https://vendor.test/v1"}
    config = build_litellm_config(
        env={"VENDOR_API_KEY_1": "secret"},
        registry=registry(custom_provider),
        discoverer=discover,
    )
    assert calls == [("secret", "https://vendor.test/v1/models")]
    deployments = config["model_list"]
    models = {item["litellm_params"]["model"] for item in deployments}
    assert "openai/vendor-70b" in models
    assert "openai/vendor-8b" in models
    assert all(item["litellm_params"]["api_base"] == "https://vendor.test/v1" for item in deployments)


def test_provider_without_keys_is_skipped() -> None:
    config = build_litellm_config(
        env={"GEMINI_API_KEY_1": "key"},
        registry=registry(
            provider("unused", "UNUSED_API_KEY_", fast=["unused-8b"]),
            provider("gemini", "GEMINI_API_KEY_", fast=["gemini-lite"]),
        ),
    )
    models = {item["litellm_params"]["model"] for item in config["model_list"]}
    assert all(not model.startswith("unused/") for model in models)
    assert "gemini/gemini-lite" in models


def test_generated_config_contains_generation_and_repair_fallbacks() -> None:
    config = build_litellm_config(
        env={"GROQ_API_KEY_1": "key"},
        registry=registry(provider("groq", "GROQ_API_KEY_", discovery_url="https://example.test/models")),
        discoverer=lambda _key, _url: ["openai/gpt-oss-120b", "qwen-32b", "llama-8b"],
    )
    assert config["router_settings"]["routing_strategy"] == "latency-based-routing"
    assert config["router_settings"]["fallbacks"] == [
        {"job-powerful": ["job-balanced", "job-fast"]},
        {"job-balanced": ["job-fast"]},
        {"repair-fast": ["repair-balanced"]},
    ]


def test_generation_requires_at_least_one_configured_provider_key() -> None:
    with pytest.raises(ConfigGenerationError):
        build_litellm_config(
            env={},
            registry=registry(provider("groq", "GROQ_API_KEY_", fast=["llama-8b"])),
        )


def test_provider_registry_loads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text('{"providers":[{"name":"test"}]}', encoding="utf-8")
    assert load_provider_registry(path)["providers"][0]["name"] == "test"


def test_high_level_generator_writes_runtime_config(tmp_path: Path) -> None:
    destination = tmp_path / "litellm.runtime.yaml"
    config = generate_litellm_config(
        env={"CUSTOM_API_KEY_1": "key"},
        registry=registry(provider("custom", "CUSTOM_API_KEY_", fast=["model-8b"])),
        destination=destination,
    )
    assert destination.exists()
    assert '"job-fast"' in destination.read_text()
    assert config["model_list"]
