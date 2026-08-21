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
CAPABILITY_GROUPS = ("job-fast", "job-balanced", "job-powerful")
REPAIR_GROUP_MAP = {"job-fast": "repair-fast", "job-balanced": "repair-balanced"}
DEFAULT_PROVIDER_REGISTRY_PATH = Path("config/llm-providers.json")


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


def load_provider_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigGenerationError(f"LiteLLM provider registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigGenerationError(f"LiteLLM provider registry is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise ConfigGenerationError("LiteLLM provider registry must contain a providers array")
    return payload


def discover_models(
    api_key: str,
    *,
    url: str,
    client_factory: Callable[[], httpx.Client] = httpx.Client,
) -> list[str]:
    with client_factory() as client:
        response = client.get(
            url,
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


def discover_with_key_fallback(
    keys: Sequence[tuple[str, str]],
    discover: Callable[[str], Sequence[str]],
) -> list[str]:
    failures: list[str] = []
    for key_name, key in keys:
        try:
            models = list(discover(key))
            if models:
                return models
            failures.append(f"{key_name}: empty model catalog")
        except Exception as exc:
            failures.append(f"{key_name}: {exc}")
    raise ConfigGenerationError("Unable to discover provider models with configured keys: " + "; ".join(failures))


def is_chat_candidate(model: str, blocklist: Sequence[str] = CHAT_MODEL_BLOCKLIST) -> bool:
    lowered = model.lower()
    return not any(marker.lower() in lowered for marker in blocklist)


def classify_model(model: str) -> str:
    lowered = model.lower()
    powerful_markers = ("120b", "90b", "70b", "72b", "65b", "405b")
    balanced_markers = ("34b", "32b", "31b", "27b", "24b", "20b", "17b", "14b")
    if any(marker in lowered for marker in powerful_markers):
        return "job-powerful"
    if any(marker in lowered for marker in balanced_markers):
        return "job-balanced"
    return "job-fast"


def _fill_missing_groups(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    if not any(groups.values()):
        return groups
    preferred_sources = {
        "job-fast": ("job-balanced", "job-powerful"),
        "job-balanced": ("job-powerful", "job-fast"),
        "job-powerful": ("job-balanced", "job-fast"),
    }
    for group in CAPABILITY_GROUPS:
        if groups[group]:
            continue
        for source in preferred_sources[group]:
            if groups[source]:
                groups[group] = list(groups[source])
                break
    return groups


def group_models(
    discovered: Sequence[str],
    *,
    explicit: Mapping[str, Sequence[str]] | None = None,
    excluded: Sequence[str] = (),
    blocklist: Sequence[str] = CHAT_MODEL_BLOCKLIST,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {group: [] for group in CAPABILITY_GROUPS}
    explicit_groups = explicit or {}
    excluded_set = set(excluded)
    explicitly_classified = {model for models in explicit_groups.values() for model in models}
    for group in CAPABILITY_GROUPS:
        groups[group].extend(model for model in explicit_groups.get(group, ()) if model not in excluded_set)
    for model in discovered:
        if model in excluded_set or model in explicitly_classified or not is_chat_candidate(model, blocklist):
            continue
        groups[classify_model(model)].append(model)
    deduplicated = {group: sorted(set(models)) for group, models in groups.items()}
    return _fill_missing_groups(deduplicated)


def deployment(
    model_name: str,
    provider_model: str,
    key_env_name: str,
    *,
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(extra_params or {})
    params.update(
        {
            "model": provider_model,
            "api_key": f"os.environ/{key_env_name}",
        }
    )
    return {"model_name": model_name, "litellm_params": params}


def _provider_groups(provider: Mapping[str, Any]) -> dict[str, list[str]]:
    raw = provider.get("models", {})
    if not isinstance(raw, Mapping):
        raise ConfigGenerationError(f"Provider {provider.get('name', '<unknown>')} models must be an object")
    return {
        "job-fast": [str(item) for item in raw.get("fast", [])],
        "job-balanced": [str(item) for item in raw.get("balanced", [])],
        "job-powerful": [str(item) for item in raw.get("powerful", [])],
    }


def _provider_enabled(provider: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    flag = str(provider.get("enabled_env", "")).strip()
    if not flag:
        return bool(provider.get("enabled", True))
    raw = env.get(flag, str(provider.get("enabled", True))).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _provider_model_prefix(provider: Mapping[str, Any]) -> str:
    prefix = str(provider.get("litellm_prefix", provider.get("name", ""))).strip().strip("/")
    if not prefix:
        raise ConfigGenerationError("Each provider must define name or litellm_prefix")
    return prefix


def _provider_keys(provider: Mapping[str, Any], env: Mapping[str, str]) -> list[tuple[str, str]]:
    prefix = str(provider.get("api_key_prefix", "")).strip()
    if not prefix:
        raise ConfigGenerationError(f"Provider {provider.get('name', '<unknown>')} must define api_key_prefix")
    return indexed_env_values(env, prefix)


def _discover_provider_models(
    provider: Mapping[str, Any],
    keys: Sequence[tuple[str, str]],
    *,
    discoverer: Callable[[str, str], Sequence[str]],
) -> list[str]:
    discovery = provider.get("discovery")
    if not isinstance(discovery, Mapping) or not discovery.get("enabled", False):
        return []
    url = str(discovery.get("url", "")).strip()
    if not url:
        raise ConfigGenerationError(f"Provider {provider.get('name', '<unknown>')} discovery requires a url")
    return discover_with_key_fallback(keys, lambda key: discoverer(key, url))


def _provider_extra_params(provider: Mapping[str, Any]) -> dict[str, Any]:
    raw = provider.get("litellm_params", {})
    if not isinstance(raw, Mapping):
        raise ConfigGenerationError(f"Provider {provider.get('name', '<unknown>')} litellm_params must be an object")
    return {str(key): value for key, value in raw.items()}


def add_provider_deployments(
    model_list: list[dict[str, Any]],
    provider: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    discoverer: Callable[[str, str], Sequence[str]],
) -> None:
    if not _provider_enabled(provider, env):
        return
    keys = _provider_keys(provider, env)
    if not keys:
        return

    discovered = _discover_provider_models(provider, keys, discoverer=discoverer)
    explicit = _provider_groups(provider)
    excluded = [str(item) for item in provider.get("exclude_models", [])]
    blocklist = [str(item) for item in provider.get("blocklist", CHAT_MODEL_BLOCKLIST)]
    groups = group_models(discovered, explicit=explicit, excluded=excluded, blocklist=blocklist)
    provider_prefix = _provider_model_prefix(provider)
    extra_params = _provider_extra_params(provider)

    for group, models in groups.items():
        for model in models:
            provider_model = model if model.startswith(f"{provider_prefix}/") else f"{provider_prefix}/{model}"
            for key_name, _ in keys:
                model_list.append(
                    deployment(group, provider_model, key_name, extra_params=extra_params)
                )


def add_repair_deployments(model_list: list[dict[str, Any]]) -> None:
    source_entries = list(model_list)
    for entry in source_entries:
        group = str(entry["model_name"])
        repair_group = REPAIR_GROUP_MAP.get(group)
        if repair_group is None:
            continue
        model_list.append(
            {
                "model_name": repair_group,
                "litellm_params": dict(entry["litellm_params"]),
            }
        )


def build_litellm_config(
    *,
    env: Mapping[str, str],
    registry: Mapping[str, Any],
    discoverer: Callable[[str, str], Sequence[str]] = lambda key, url: discover_models(key, url=url),
) -> dict[str, Any]:
    providers = registry.get("providers")
    if not isinstance(providers, list):
        raise ConfigGenerationError("LiteLLM provider registry must contain a providers array")

    model_list: list[dict[str, Any]] = []
    for provider in providers:
        if not isinstance(provider, Mapping):
            raise ConfigGenerationError("Every provider registry entry must be an object")
        add_provider_deployments(model_list, provider, env=env, discoverer=discoverer)

    if not model_list:
        raise ConfigGenerationError("No LiteLLM chat-model deployments were generated from configured provider keys")
    add_repair_deployments(model_list)

    present_groups = {str(item["model_name"]) for item in model_list}
    fallbacks: list[dict[str, list[str]]] = []
    if "job-powerful" in present_groups:
        chain = [group for group in ("job-balanced", "job-fast") if group in present_groups]
        if chain:
            fallbacks.append({"job-powerful": chain})
    if "job-balanced" in present_groups and "job-fast" in present_groups:
        fallbacks.append({"job-balanced": ["job-fast"]})
    if "repair-fast" in present_groups and "repair-balanced" in present_groups:
        fallbacks.append({"repair-fast": ["repair-balanced"]})

    return {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": env.get("LITELLM_ROUTING_STRATEGY", "latency-based-routing"),
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
    registry: Mapping[str, Any],
    destination: Path,
    discoverer: Callable[[str, str], Sequence[str]] = lambda key, url: discover_models(key, url=url),
) -> dict[str, Any]:
    config = build_litellm_config(env=env, registry=registry, discoverer=discoverer)
    write_config(config, destination)
    return config
