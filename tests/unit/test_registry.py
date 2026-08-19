from pathlib import Path

import pytest

from job_hunt.config import load_registry
from job_hunt.errors import ConfigurationError
from job_hunt.rendering.profiles import MahsaCvRenderer, MojtabaCvRenderer
from job_hunt.tenants.registry import TenantRegistry


def test_registry_builds_each_tenant_renderer() -> None:
    registry = TenantRegistry(load_registry(Path("config/users.toml")))
    assert isinstance(registry.get("mahsa").renderer.cv_renderer, MahsaCvRenderer)
    assert isinstance(registry.get("mojtaba").renderer.cv_renderer, MojtabaCvRenderer)
    with pytest.raises(ConfigurationError, match="Unknown tenant"):
        registry.get("nobody")
