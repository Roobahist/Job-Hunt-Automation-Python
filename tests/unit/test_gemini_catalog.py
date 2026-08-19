from __future__ import annotations

from types import SimpleNamespace

import pytest

from job_hunt.errors import ConfigurationError
from job_hunt.integrations.gemini_catalog import validate_gemini_models


def test_model_catalog_accepts_exposed_models(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(
        models=SimpleNamespace(
            list=lambda: [SimpleNamespace(name="models/best"), SimpleNamespace(name="models/repair")]
        )
    )
    monkeypatch.setattr("job_hunt.integrations.gemini_catalog.genai.Client", lambda api_key: client)
    validate_gemini_models("key", ["best", "models/repair"])


def test_model_catalog_rejects_unknown_models(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(models=SimpleNamespace(list=lambda: [SimpleNamespace(name="models/best")]))
    monkeypatch.setattr("job_hunt.integrations.gemini_catalog.genai.Client", lambda api_key: client)
    with pytest.raises(ConfigurationError, match="missing"):
        validate_gemini_models("key", ["best", "missing"])
