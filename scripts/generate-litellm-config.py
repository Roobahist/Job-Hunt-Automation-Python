from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

from job_hunt.integrations.litellm_config import ConfigGenerationError, discover_groq_models, generate_litellm_config


def main() -> int:
    destination = Path(os.getenv("LITELLM_CONFIG_PATH", "config/litellm.runtime.yaml"))
    try:
        config = generate_litellm_config(
            env=os.environ,
            discover_groq=discover_groq_models,
            destination=destination,
        )
    except (ConfigGenerationError, httpx.HTTPError, ValueError) as exc:
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
