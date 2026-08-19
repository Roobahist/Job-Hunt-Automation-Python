from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from job_hunt.domain.models import Job
from job_hunt.logging import logger
from job_hunt.ports import (
    ArtifactPublisher,
    DocumentRenderer,
    JobRepository,
    Notifier,
    Qualifier,
    Tailor,
)
from job_hunt.retry import retry_transient


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    row_id: int
    passed: bool
    artifacts_published: bool
    notification_id: str | None = None


class ApplicationWorkflow:
    def __init__(
        self,
        repository: JobRepository,
        qualifier: Qualifier,
        tailor: Tailor,
        renderer: DocumentRenderer,
        publisher: ArtifactPublisher,
        notifier: Notifier,
        artifact_root: Path,
    ) -> None:
        self.repository = repository
        self.qualifier = qualifier
        self.tailor = tailor
        self.renderer = renderer
        self.publisher = publisher
        self.notifier = notifier
        self.artifact_root = artifact_root

    def process(
        self,
        job: Job,
        *,
        run_id: UUID,
        master_cv: Mapping[str, Any],
        prompts: Mapping[str, str],
        threshold: int,
        force: bool,
        applicant_filename: str,
        cloudinary_folder: str,
        cloudinary_tags: list[str],
        telegram_chat_id: str,
    ) -> WorkflowResult:
        log = logger().bind(run_id=str(run_id), job_identity=job.identity)
        existing = retry_transient(self.repository.find, job)
        row = (
            retry_transient(self.repository.reset, int(existing["id"]), job)
            if existing
            else retry_transient(self.repository.create, job)
        )
        row_id = int(row["id"])
        log.info("job_persisted", stage="persist", row_id=row_id, reprocessed=bool(existing))
        qualification = retry_transient(self.qualifier.qualify, job, master_cv, prompts)
        passed = qualification.passes(threshold, force=force)
        retry_transient(self.repository.save_qualification, row_id, qualification, passed=passed)
        log.info(
            "job_qualified",
            stage="qualification",
            score=qualification.score,
            should_apply=qualification.should_apply,
            passed=passed,
        )
        if not passed:
            return WorkflowResult(row_id, False, False)
        tailored = retry_transient(self.tailor.tailor, job, master_cv, prompts)
        basename = f"{applicant_filename}-{job.company_name}-{job.internal_id}".replace("/", "-")
        artifacts = self.renderer.render(tailored, self.artifact_root / str(run_id), basename)
        uploaded = retry_transient(
            self.publisher.publish,
            artifacts,
            f"{cloudinary_folder}/{job.internal_id}",
            cloudinary_tags,
        )
        retry_transient(self.repository.save_artifacts, row_id, uploaded)
        notification_id = retry_transient(
            self.notifier.send_documents,
            telegram_chat_id,
            artifacts.notification_paths(),
            f"{job.title} at {job.company_name}",
        )
        log.info("job_completed", stage="notify", row_id=row_id, notification_id=notification_id)
        return WorkflowResult(row_id, True, True, notification_id)
