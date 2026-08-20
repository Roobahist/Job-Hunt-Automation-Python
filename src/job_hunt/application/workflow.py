from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from job_hunt.domain.models import Job, PromptDefinition
from job_hunt.logging import logger
from job_hunt.ports import ArtifactPublisher, CompatibilityFilter, DocumentRenderer, JobRepository, Qualifier, Tailor
from job_hunt.retry import retry_transient

ProgressCallback = Callable[[str, str], None]
PersistedCallback = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    row_id: int
    passed: bool
    artifacts_published: bool
    score: int | None = None
    notification_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualificationResult:
    row_id: int
    passed: bool
    score: int


class ApplicationWorkflow:
    def __init__(
        self,
        repository: JobRepository,
        qualifier: Qualifier,
        tailor: Tailor,
        renderer: DocumentRenderer,
        publisher: ArtifactPublisher,
        artifact_root: Path,
        *,
        compatibility_filter: CompatibilityFilter | None = None,
    ) -> None:
        self.repository = repository
        self.compatibility_filter = compatibility_filter
        self.qualifier = qualifier
        self.tailor = tailor
        self.renderer = renderer
        self.publisher = publisher
        self.artifact_root = artifact_root

    @staticmethod
    def _progress(callback: ProgressCallback | None, stage: str, event: str) -> None:
        if callback is not None:
            callback(stage, event)

    def _dropped(self, row_id: int) -> bool:
        return bool(retry_transient(self.repository.has_status, row_id, "dropped"))

    @staticmethod
    def _skipped(row_id: int, score: int) -> WorkflowResult:
        return WorkflowResult(row_id=row_id, passed=False, artifacts_published=False, score=score)

    def persist_and_qualify(
        self,
        job: Job,
        *,
        run_id: UUID,
        master_cv: Mapping[str, Any],
        prompts: Mapping[str, PromptDefinition],
        threshold: int,
        force: bool,
        progress: ProgressCallback | None = None,
        persisted: PersistedCallback | None = None,
    ) -> QualificationResult:
        log = logger().bind(run_id=str(run_id), job_identity=job.identity)
        self._progress(progress, "persistence", "start")
        existing = retry_transient(self.repository.find, job)
        row = (
            retry_transient(self.repository.reset, int(existing["id"]), job)
            if existing
            else retry_transient(self.repository.create, job)
        )
        row_id = int(row["id"])
        if force:
            # A forced/manual submission is an explicit user override. Reset a
            # previously dropped row before any downstream live-status checks run.
            retry_transient(self.repository.set_status, row_id, "new")
        if persisted is not None:
            persisted(row_id)
        self._progress(progress, "persistence", "finish")
        log.info("job_persisted", stage="persist", row_id=row_id, reprocessed=bool(existing), forced=force)

        if self.compatibility_filter is not None and not force:
            self._progress(progress, "compatibility_filter", "start")
            compatible = self.compatibility_filter.compatible(job, prompts)
            self._progress(progress, "compatibility_filter", "finish")
            log.info("job_compatibility_checked", stage="compatibility_filter", compatible=compatible)
            if not compatible:
                retry_transient(self.repository.set_status, row_id, "dropped")
                log.info("job_compatibility_filtered", stage="compatibility_filter", row_id=row_id)
                return QualificationResult(row_id=row_id, passed=False, score=cast(int, None))
        elif force:
            log.info("job_compatibility_bypassed", stage="compatibility_filter", row_id=row_id)

        self._progress(progress, "qualification", "start")
        qualification = self.qualifier.qualify(job, master_cv, prompts)
        passed = qualification.passes(threshold, force=force)
        retry_transient(self.repository.save_qualification, row_id, qualification)
        if qualification.score < threshold and not force:
            retry_transient(self.repository.set_status, row_id, "dropped")
            passed = False
        self._progress(progress, "qualification", "finish")
        log.info(
            "job_qualified",
            stage="qualification",
            score=qualification.score,
            should_apply=qualification.should_apply,
            passed=passed,
            forced=force,
        )
        return QualificationResult(row_id=row_id, passed=passed, score=qualification.score)

    def generate_documents(
        self,
        job: Job,
        *,
        run_id: UUID,
        row_id: int,
        score: int,
        master_cv: Mapping[str, Any],
        prompts: Mapping[str, PromptDefinition],
        applicant_filename: str,
        progress: ProgressCallback | None = None,
    ) -> WorkflowResult:
        log = logger().bind(run_id=str(run_id), job_identity=job.identity)
        if self._dropped(row_id):
            log.info("job_documents_skipped", stage="documents", row_id=row_id, reason="status_dropped")
            return self._skipped(row_id, score)

        self._progress(progress, "tailoring", "start")
        tailored = self.tailor.tailor(job, master_cv, prompts)
        self._progress(progress, "tailoring", "finish")
        if self._dropped(row_id):
            log.info("job_documents_skipped", stage="post_tailoring", row_id=row_id, reason="status_dropped")
            return self._skipped(row_id, score)

        source_id = job.external_id or str(job.internal_id)
        archive_basename = f"{applicant_filename}-{job.company_name}-{source_id}".replace("/", "-")
        self._progress(progress, "rendering", "start")
        artifacts = self.renderer.render(
            tailored,
            self.artifact_root / str(run_id),
            archive_basename,
            applicant_filename=applicant_filename,
        )
        self._progress(progress, "rendering", "finish")
        if self._dropped(row_id):
            log.info("job_documents_skipped", stage="post_rendering", row_id=row_id, reason="status_dropped")
            return self._skipped(row_id, score)

        self._progress(progress, "artifact_upload", "start")
        uploaded = retry_transient(self.publisher.publish, artifacts)
        retry_transient(self.repository.save_artifacts, row_id, uploaded)
        self._progress(progress, "artifact_upload", "finish")
        log.info("job_documents_ready", stage="publish", row_id=row_id)
        return WorkflowResult(
            row_id=row_id,
            passed=True,
            artifacts_published=True,
            score=score,
            notification_paths=tuple(str(path) for path in artifacts.notification_paths()),
        )
