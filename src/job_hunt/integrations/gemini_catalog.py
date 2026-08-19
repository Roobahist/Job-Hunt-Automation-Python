from __future__ import annotations

from collections.abc import Iterable

from google import genai

from job_hunt.errors import ConfigurationError


def validate_gemini_models(api_key: str, configured: Iterable[str]) -> None:
    client = genai.Client(api_key=api_key)
    available = {
        str(model.name).removeprefix("models/") for model in client.models.list() if getattr(model, "name", None)
    }
    requested = {model.removeprefix("models/") for model in configured}
    missing = sorted(requested - available)
    if missing:
        raise ConfigurationError("Configured Gemini model IDs are not exposed by this account: " + ", ".join(missing))
