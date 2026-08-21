from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from job_hunt.integrations.litellm_config import (
    ConfigGenerationError,
    DEFAULT_PROVIDER_REGISTRY_PATH,
    build_litellm_config,
    discover_models,
    load_provider_registry,
    write_config,
)


def main() -> int:
    destination = Path(os.getenv("LITELLM_CONFIG_PATH", "config/litellm.runtime.yaml"))
    registry_path = Path(os.getenv("LITELLM_PROVIDER_REGISTRY_PATH", str(DEFAULT_PROVIDER_REGISTRY_PATH)))
    stdout_mode = os.getenv("LITELLM_CONFIG_STDOUT", "false").strip().lower() in {"1", "true", "yes"}
    try:
        registry = load_provider_registry(registry_path)
        config = build_litellm_config(
            env=os.environ,
            registry=registry,
            discoverer=lambda key, url: discover_models(key, url=url),
        )
        if stdout_mode:
            sys.stdout.write(json.dumps(config, indent=2) + "\n")
            return 0
        write_config(config, destination)
    except (ConfigGenerationError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"LiteLLM config generation failed: {exc}", file=sys.stderr)
        return 1

    groups: dict[str, int] = {}
    for entry in config["model_list"]:
        group = str(entry["model_name"])
        groups[group] = groups.get(group, 0) + 1
    print(f"Generated {destination}: {groups}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
