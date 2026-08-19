from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from job_hunt.config import TenantBootstrap
from job_hunt.errors import ConfigurationError
from job_hunt.rendering.documents import TenantDocumentRenderer
from job_hunt.rendering.profiles import (
    CoverLetterRenderer,
    CvRenderer,
    MahsaCoverLetterRenderer,
    MahsaCvRenderer,
    MojtabaCoverLetterRenderer,
    MojtabaCvRenderer,
)


@dataclass(frozen=True, slots=True)
class TenantContext:
    bootstrap: TenantBootstrap
    master_cv: dict[str, Any]
    renderer: TenantDocumentRenderer


class TenantRegistry:
    def __init__(
        self, bootstraps: dict[str, TenantBootstrap], project_root: Path = Path(".")
    ) -> None:
        self.bootstraps = bootstraps
        self.project_root = project_root

    def get(self, key: str) -> TenantContext:
        try:
            bootstrap = self.bootstraps[key]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown tenant: {key}") from exc
        if not bootstrap.enabled:
            raise ConfigurationError(f"Tenant is disabled: {key}")
        root = self.project_root / bootstrap.tenant_root
        try:
            master_cv = json.loads((root / "master_cv.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Could not load master CV for {key}: {exc}") from exc
        if bootstrap.renderer == "mahsa":
            cv: CvRenderer = MahsaCvRenderer()
            cover: CoverLetterRenderer = MahsaCoverLetterRenderer()
        elif bootstrap.renderer == "mojtaba":
            cv, cover = MojtabaCvRenderer(), MojtabaCoverLetterRenderer()
        else:
            raise ConfigurationError(f"Unknown renderer profile: {bootstrap.renderer}")
        renderer = TenantDocumentRenderer(
            cv,
            cover,
            root / "templates/cv_template.tex",
            root / "templates/cover_letter_template.tex",
        )
        return TenantContext(bootstrap, master_cv, renderer)
