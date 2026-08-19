from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class EntryType(StrEnum):
    LINKEDIN = "linkedin"
    EXTERNAL = "external"
    AI_CONTENT = "ai_content"
    URL = "url"


class LinkedInSubmission(BaseModel):
    entry_type: Literal[EntryType.LINKEDIN]
    linkedin_job_id: int = Field(gt=0)


class ExternalSubmission(BaseModel):
    entry_type: Literal[EntryType.EXTERNAL]
    company_name: str = Field(min_length=1)
    job_title: str = Field(min_length=1)
    job_description: str = Field(min_length=1)
    job_url: HttpUrl
    location: str | None = None
    contract_type: str | None = None
    published_at: datetime | None = None
    external_job_id: str | None = None
    source: str = "external"


class AiContentSubmission(BaseModel):
    entry_type: Literal[EntryType.AI_CONTENT]
    page_content: str = Field(min_length=1)
    source_url: HttpUrl | None = None


class UrlSubmission(BaseModel):
    entry_type: Literal[EntryType.URL]
    job_url: HttpUrl


JobSubmission = Annotated[
    LinkedInSubmission | ExternalSubmission | AiContentSubmission | UrlSubmission,
    Field(discriminator="entry_type"),
]


class Job(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source: str
    external_id: str | None = None
    url: str
    company_name: str
    title: str
    description: str
    location: str | None = None
    contract_type: str | None = None
    published_at: datetime | None = None
    identity: str = ""
    internal_id: int = 0

    @model_validator(mode="after")
    def require_content(self) -> Job:
        if not self.company_name or not self.title or not self.description:
            raise ValueError("company_name, title, and description are required")
        return self


class Qualification(BaseModel):
    score: int = Field(ge=0, le=100)
    should_apply: bool
    reasoning: str = Field(min_length=1)

    def passes(self, threshold: int, *, force: bool = False) -> bool:
        return force or (self.score >= threshold and self.should_apply)


class TailoredContent(BaseModel):
    cv: dict[str, object]
    cover_letter: dict[str, object]


class ArtifactBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_directory: Path
    cv_json: Path
    cv_tex: Path
    cv_pdf: Path
    cover_letter_json: Path
    cover_letter_tex: Path
    cover_letter_pdf: Path
    archive: Path

    def all_paths(self) -> tuple[Path, ...]:
        return (
            self.archive,
            self.cv_json,
            self.cv_tex,
            self.cv_pdf,
            self.cover_letter_json,
            self.cover_letter_tex,
            self.cover_letter_pdf,
        )

    def notification_paths(self) -> tuple[Path, ...]:
        return (
            self.archive,
            self.cv_tex,
            self.cv_pdf,
            self.cover_letter_tex,
            self.cover_letter_pdf,
        )


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    tenant: str
    kind: str
    state: RunState = RunState.QUEUED
    stage: str = "queued"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    task_id: str | None = None
    original_run_id: UUID | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    error: dict[str, object] | None = None


class EnqueueResponse(BaseModel):
    run_id: UUID
    status: Literal["queued"] = "queued"


class RetryResponse(BaseModel):
    original_run_id: UUID
    run_id: UUID
    status: Literal["queued"] = "queued"
