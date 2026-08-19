from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from job_hunt.domain.models import ArtifactBundle
from job_hunt.integrations.baserow import BaserowClient


class BaserowArtifactPublisher:
    """Persist final application documents directly in Baserow.

    JSON and TeX sources remain inside the local ZIP bundle for debugging and reproducibility,
    while the Jobs table only receives the two files used during the application process.
    """

    def __init__(self, baserow: BaserowClient) -> None:
        self.baserow = baserow

    def publish(
        self, artifacts: ArtifactBundle, folder: str, tags: Sequence[str]
    ) -> Mapping[str, Any]:
        del folder, tags
        return {
            "CV": [self.baserow.upload_file(artifacts.cv_pdf)],
            "Cover Letter": [self.baserow.upload_file(artifacts.cover_letter_pdf)],
        }
