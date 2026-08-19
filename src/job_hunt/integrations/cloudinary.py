from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

import cloudinary  # type: ignore[import-untyped]
import cloudinary.uploader  # type: ignore[import-untyped]

from job_hunt.domain.models import ArtifactBundle
from job_hunt.errors import ErrorKind, ProviderError


class CloudinaryPublisher:
    def __init__(self, cloudinary_url: str) -> None:
        self.cloudinary_url = cloudinary_url

    def publish(
        self, artifacts: ArtifactBundle, folder: str, tags: Sequence[str]
    ) -> Mapping[str, Any]:
        previous = os.environ.get("CLOUDINARY_URL")
        os.environ["CLOUDINARY_URL"] = self.cloudinary_url
        cloudinary.config(secure=True)
        result: dict[str, Any] = {}
        try:
            for path in artifacts.all_paths():
                response = cloudinary.uploader.upload(
                    str(path),
                    resource_type="raw",
                    folder=folder,
                    public_id=path.name,
                    tags=list(tags),
                    overwrite=True,
                )
                result[path.name] = response
            return result
        except Exception as exc:
            raise ProviderError(
                f"Cloudinary upload failed: {exc}",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="cloudinary",
            ) from exc
        finally:
            if previous is None:
                os.environ.pop("CLOUDINARY_URL", None)
            else:
                os.environ["CLOUDINARY_URL"] = previous
