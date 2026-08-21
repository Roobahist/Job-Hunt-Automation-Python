from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

CHAT_MODEL_BLOCKLIST = (
    "whisper",
    "speech",
    "tts",
    "guard",
    "moderation",
    "embedding",
    "embed",
)


class ConfigGenerationError(RuntimeError):
    pass


def indexed_env_values(env: Mapping[str, str], prefix: str) -> list[tuple[str, str]]:
    values: list[tuple[int, str, str]] = []
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for name, value in env.items():
        match = pattern.match(name)
        if match and value.strip():
            values.append((int(match.group(1)), name, value.strip()))
    values.sort(key=lambda item: item[0])
    return [(name, value) for _, name, value in values]


def csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def discover_groq_models(api_key: str, *, client_factory: Callable[[], httpx.Client] = httpx.Client) -> list[str]:
    with client_factory() as client:
        response = client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    models = payload.get("data", []) if isinstance(payload, dict) else []
    return sorted(
        {
            str(item.get("id", "")).strip()
            for item in models
            if isinstance(item, dict) and str(item.get("id", "")).strip()
        }
    )


def is_chat_candidate(model: str) -> bool:
    lowered = model.lower()
    return not any(marker in lowered for marker in CHAT_MODEL_BLOCKLIST)


def classify_model(model: str) -> str:
    lowered = model.lower()
    powerful_markers = ("120b", "90b", "70b", "72b", "65b", "405b")
    balanced_markers = ("34b", "32b", "31b", "27b", "24b", "20b", "17b", "14b")
    if any(marker in lowered for marker in powerful_markers):
        return "job-powerful"
    if any(marker in lowered for marker in balanced_markers):
        return "job-balanced"
    return "job-fast"


def group_models(discovered: Sequence[str], *, env: Mapping[str, str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {"job-fast": [], "job-balanced": [], "job-powerful": []}
    explicit = {
        "job-fast": csv_values(env.get("LITELLM_GROQ_FAST_MODELS", "")),
        "job-balanced": csv_values(env.get("LITELLM_GROQ_BALANCED_MODELS", "")),
        "job-powerful": csv_values(env.get("LITELLM_GROQ_POWERFUL_MODELS", "")),
    }
    explicitly_classified = {model for models in explicit.values() for model in models}
    for group, models in explicit.items():
        groups[group].extend(models)
    for model in discovered:
        if model in explicitly_classified or not is_chat_candidate(model):
            continue
        groups[classify_model(model)].append(model)
    return {group: sorted(set(models)) for group, models in groups.items()}


def deployment(model_name: str, provider_model: str, key_env_name: str) -> dict[str, Any]:
    return {
        "model_name": model_name,
        "litellm_params": {
            "model": provider_model,
            "api_key": f"os.environ/{key_env_name}",
        },
    }


def add_groq_deployments(
    model_list: list[dict[str, Any]],
    groups: Mapping[str, Sequence[str]],
    key_names: Sequence[str],
) -> None:
    for group, models in groups.items():
        for model in models:
            for key_name in key_names:
                model_list.append(deployment(group, f"groq/{model}", key_name))


def add_gemini_deployments(
    model_list: list[dict[str, Any]],
    *,
    env: Mapping[str, str],
    key_names: Sequence[str],
) -> None:
    groups = {
        "job-fast": csv_values(env.get("LITELLM_GEMINI_FAST_MODELS", "gemini-3.5-flash-lite")),
        "job-balanced": csv_values(env.get("LITELLM_GEMINI_BALANCED_MODELS", "gemini-3.5-flash")),
        "job-powerful": csv_values(env.get("LITELLM_GEMINI_POWERFUL_MODELS", "gemini-3.5-flash")),
    }
    for group, models in groups.items():
        for model in models:
            for key_name in key_names:
                model_list.append(deployment(group, f"gemini/{model}", key_name))


def build_litellm_config(
    *,
    env: Mapping[str, str],
    discover_groq: Callable[[str], Sequence[str]],
) -> dict[str, Any]:
    groq_keys = indexed_env_values(env, "GROQ_API_KEY_")
    gemini_keys = indexed_env_values(env, "GEMINI_API_KEY_")
    if not groq_keys and not gemini_keys:
        raise ConfigGenerationError("At least one GROQ_API_KEY_N or GEMINI_API_KEY_N must be configured")

    discovered: Sequence[str] = []
    discovery_enabled = env.get("LITELLM_GROQ_DISCOVER_MODELS", "true").strip().lower() not in {"0", "false", "no"}
    if groq_keys and discovery_enabled:
        discovered = discover_groq(groq_keys[0][1])
    groups = group_models(discovered, env=env)

    model_list: list[dict[str, Any]] = []
    add_groq_deployments(model_list, groups, [name for name, _ in groq_keys])
    add_gemini_deployments(model_list, env=env, key_names=[name for name, _ in gemini_keys])
    if not model_list:
        raise ConfigGenerationError("No LiteLLM chat-model deployments were generated")

    present_groups = {str(item["model_name"]) for item in model_list}
    fallbacks: list[dict[str, list[str]]] = []
    if "job-powerful" in present_groups:
        chain = [group for group in ("job-balanced", "job-fast") if group in present_groups]
        if chain:
            fallbacks.append({"job-powerful": chain})
    if "job-balanced" in present_groups and "job-fast" in present_groups:
        fallbacks.append({"job-balanced": ["job-fast"]})

    return {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": env.get("LITELLM_ROUTING_STRATEGY", "simple-shuffle"),
            "num_retries": 0,
            "allowed_fails": 1,
            "cooldown_time": int(env.get("LITELLM_COOLDOWN_SECONDS", "65")),
            "fallbacks": fallbacks,
        },
        "litellm_settings": {"drop_params": True},
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
    }


def write_config(config: Mapping[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def generate_litellm_config(
    *,
    env: Mapping[str, str],
    discover_groq: Callable[[str], Sequence[str]],
    destination: Path,
) -> dict[str, Any]:
    config = build_litellm_config(env=env, discover_groq=discover_groq)
    write_config(config, destination)
    return config
