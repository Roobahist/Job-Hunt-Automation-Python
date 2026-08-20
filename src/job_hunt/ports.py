from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from job_hunt.domain.models import (
    ArtifactBundle,
    Job,
    PromptDefinition,
    Qualification,
    TailoredContent,
)


class JobRepository(Protocol):
    def find(self, job: Job) -> Mapping[str, Any] | None: ...
    def create(self, job: Job) -> Mapping[str, Any]: ...
    def reset(self, row_id: int, job: Job) -> Mapping[str, Any]: ...
    def save_qualification(self, row_id: int, result: Qualification, *, passed: bool) -> None: ...
    def save_artifacts(
        self,
        row_id: int,
        uploaded_files: Mapping[str, Any],
        *,
        job_url: str,
    ) -> None: ...


class Qualifier(Protocol):
    def qualify(
        self,
        job: Job,
        master_cv: Mapping[str, Any],
        prompts: Mapping[str, PromptDefinition],
    ) -> Qualification: ...


class Tailor(Protocol):
    def tailor(
        self,
        job: Job,
        master_cv: Mapping[str, Any],
        prompts: Mapping[str, PromptDefinition],
    ) -> TailoredContent: ...


class DocumentRenderer(Protocol):
    def render(self, content: TailoredContent, output_directory: Path, basename: str) -> ArtifactBundle: ...


class ArtifactPublisher(Protocol):
    def publish(self, artifacts: ArtifactBundle) -> Mapping[str, Any]: ...


class Notifier(Protocol):
    def send_documents(self, chat_id: str, artifacts: Iterable[Path], caption: str) -> str: ...


class DiscoveryProvider(Protocol):
    def discover(self, urls: Sequence[str], *, max_items: int) -> Iterable[Mapping[str, Any]]: ...
    def fetch_linkedin(self, job_id: int, *, country: str, max_concurrency: int) -> Mapping[str, Any]: ...
