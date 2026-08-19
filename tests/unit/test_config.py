from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_hunt.config import decode_config_value, load_registry, parse_configuration_rows, read_seed
from job_hunt.errors import ConfigurationError


def minimum_rows() -> list[dict[str, object]]:
    values: dict[str, tuple[str, object]] = {
        "tenant_key": ("text", "test"),
        "applicant_filename": ("text", "Test"),
        "baserow_table_ids": ("json", {"jobs": 1, "searchCriteria": 2, "prompts": 3}),
        "status_option_ids": ("json", {"new": 1, "dropped": 2}),
        "fillout_form_id": ("text", "form"),
        "fillout_field_ids": ("json", {}),
        "apify_actor_ids": ("json", {"linkedinSearch": "a", "linkedinSingleJob": "b"}),
        "linkedin_base_search_url": ("text", "https://linkedin.example/search"),
        "linkedin_job_url_template": ("text", "https://linkedin.example/{jobId}"),
        "telegram_chat_id": ("text", "1"),
        "gemini_model": ("text", "gemini-test"),
    }
    return [
        {
            "configKey": key,
            "valueType": kind,
            "value": json.dumps(value) if kind == "json" else value,
            "enabled": True,
        }
        for key, (kind, value) in values.items()
    ]


def test_decode_and_parse_configuration() -> None:
    assert decode_config_value("boolean", "YES") is True
    assert decode_config_value("number", "3") == 3
    assert decode_config_value("number", "3.5") == 3.5
    config = parse_configuration_rows(minimum_rows())
    assert config.baserow_table_ids["jobs"] == 1
    assert config.qualification_threshold == 33


def test_invalid_configuration_is_actionable() -> None:
    with pytest.raises(ConfigurationError, match="Invalid tenant configuration"):
        parse_configuration_rows([])


def test_registry_and_real_seed_files(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = load_registry(Path("config/users.toml"))
    assert set(registry) == {"mahsa", "mojtaba"}
    monkeypatch.setenv("MAHSA_BASEROW_TOKEN", "secret")
    assert registry["mahsa"].secret("baserow") == "secret"
    assert read_seed(Path("config/seeds/mahsa.csv")).tenant_key == "mahsa_azar"
    assert read_seed(Path("config/seeds/mojtaba.csv")).tenant_key == "mojtaba_kanani"
