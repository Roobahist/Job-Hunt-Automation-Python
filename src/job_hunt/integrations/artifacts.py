from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from job_hunt.domain.models import ArtifactBundle
from job_hunt.integrations.baserow import BaserowClient
from job_hunt.integrations.cloudinary import CloudinaryPublisher


class CloudinaryBaserowPublisher:
    """Upload once to Cloudinary, then import those stable URLs into Baserow."""

    def __init__(self, cloudinary: CloudinaryPublisher, baserow: BaserowClient) -> None:
        self.cloudinary = cloudinary
        self.baserow = baserow

    def publish(
        self, artifacts: ArtifactBundle, folder: str, tags: Sequence[str]
    ) -> Mapping[str, Any]:
        uploads = self.cloudinary.publish(artifacts, folder, tags)

        def imported(path_name: str) -> dict[str, Any]:
            return self.baserow.upload_via_url(str(uploads[path_name]["secure_url"]))

        return {
            "CV": [
                imported(path.name)
                for path in (artifacts.cv_json, artifacts.cv_tex, artifacts.cv_pdf)
            ],
            "Cover Letter": [
                imported(path.name)
                for path in (
                    artifacts.cover_letter_json,
                    artifacts.cover_letter_tex,
                    artifacts.cover_letter_pdf,
                )
            ],
        }
