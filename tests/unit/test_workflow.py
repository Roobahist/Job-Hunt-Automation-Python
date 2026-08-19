from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from job_hunt.application.workflow import ApplicationWorkflow
from job_hunt.domain.identity import assign_identity
from job_hunt.domain.models import Job, Qualification, TailoredContent
from job_hunt.errors import ErrorKind, WorkflowError


class Repository:
    def __init__(self, existing: bool = False) -> None:
        self.existing = {"id": 7} if existing else None
        self.calls: list[tuple[str, object]] = []

    def find(self, job: Job) -> dict[str, int] | None:
        self.calls.append(("find", job.identity))
        return self.existing

    def create(self, job: Job) -> dict[str, int]:
        self.calls.append(("create", job.internal_id))
        return {"id": 8}

    def reset(self, row_id: int, job: Job) -> dict[str, int]:
        self.calls.append(("reset", row_id))
        return {"id": row_id}

    def save_qualification(self, row_id: int, result: Qualification, *, passed: bool) -> None:
        self.calls.append(("qualification", passed))

    def save_artifacts(self, row_id: int, uploaded_files: object) -> None:
        self.calls.append(("artifacts", row_id))


class AI:
    def __init__(self, qualification: Qualification) -> None:
        self.qualification = qualification

    def qualify(self, *_: object) -> Qualification:
        return self.qualification

    def tailor(self, *_: object) -> TailoredContent:
        return TailoredContent(cv={}, cover_letter={})


class Renderer:
    def render(self, *_: object) -> Any:
        paths = [Path("/tmp/application.zip"), Path("/tmp/cv.pdf"), Path("/tmp/cl.pdf")]
        return SimpleNamespace(notification_paths=lambda: paths)


class Publisher:
    def __init__(self) -> None:
        self.calls = 0

    def publish(self, *_: object) -> dict[str, object]:
        self.calls += 1
        return {"CV": []}


def make_workflow(repo: Repository, result: Qualification) -> tuple[ApplicationWorkflow, Publisher]:
    publisher = Publisher()
    return (
        ApplicationWorkflow(repo, AI(result), AI(result), Renderer(), publisher, Path("runs")),
        publisher,
    )


def application_job() -> Job:
    return assign_identity(
        Job(
            source="x",
            external_id="1",
            url="https://x/jobs/1",
            company_name="C",
            title="T",
            description="D",
        )
    )


def process(workflow: ApplicationWorkflow, *, force: bool = False) -> object:
    return workflow.process(
        application_job(),
        run_id=uuid4(),
        master_cv={},
        prompts={},
        threshold=33,
        force=force,
        applicant_filename="Person",
    )


def test_below_threshold_stops_expensive_side_effects() -> None:
    repo = Repository()
    workflow, publisher = make_workflow(repo, Qualification(score=32, should_apply=True, reasoning="low"))
    result = process(workflow)
    assert not result.passed  # type: ignore[attr-defined]
    assert publisher.calls == 0
    assert ("qualification", False) in repo.calls


def test_should_apply_does_not_block_documents_above_threshold() -> None:
    repo = Repository()
    workflow, publisher = make_workflow(repo, Qualification(score=90, should_apply=False, reasoning="metadata only"))
    result = process(workflow)
    assert result.passed  # type: ignore[attr-defined]
    assert publisher.calls == 1
    assert len(result.notification_paths) == 3  # type: ignore[attr-defined]
    assert ("qualification", True) in repo.calls


def test_force_processes_and_existing_job_resets_first() -> None:
    repo = Repository(existing=True)
    workflow, publisher = make_workflow(repo, Qualification(score=1, should_apply=False, reasoning="no"))
    result = process(workflow, force=True)
    assert result.passed  # type: ignore[attr-defined]
    assert ("reset", 7) in repo.calls
    assert publisher.calls == 1


def test_permanent_error_is_not_retried() -> None:
    class BrokenRepository(Repository):
        def find(self, job: Job) -> None:
            raise WorkflowError("bad", ErrorKind.VALIDATION)

    workflow, _ = make_workflow(BrokenRepository(), Qualification(score=1, should_apply=False, reasoning="no"))
    with pytest.raises(WorkflowError):
        process(workflow)
