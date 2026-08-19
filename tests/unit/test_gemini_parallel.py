from __future__ import annotations

from threading import Barrier, Lock
from typing import Any

import pytest

from job_hunt.domain.models import Job
from job_hunt.integrations.gemini_parallel import ParallelGeminiWorkflowAI


def test_mojtaba_independent_branches_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    ai = ParallelGeminiWorkflowAI(object(), {}, parallelism=3)  # type: ignore[arg-type]
    barrier = Barrier(3, timeout=2)
    lock = Lock()
    started: list[str] = []

    def branch(name: str, result: object) -> object:
        with lock:
            started.append(name)
        barrier.wait()
        return result

    monkeypatch.setattr(
        ai,
        "_project_pipeline",
        lambda *_: branch("projects", ([0], [{"title": "P", "content": ["p"]}])),
    )
    monkeypatch.setattr(
        ai,
        "_work_pipeline",
        lambda *_: branch("work", ([0], [{"title": "W", "content": ["w"]}])),
    )
    monkeypatch.setattr(
        ai,
        "_skills_pipeline",
        lambda *_: branch("skills", [{"label": "Skills", "value": "Python"}]),
    )
    monkeypatch.setattr(ai, "_summary_pipeline", lambda *_: ["summary"])
    monkeypatch.setattr(ai, "_definition", lambda *_: object())
    monkeypatch.setattr(ai, "_run", lambda *_: {"paragraphs": ["1", "2", "3"]})

    job = Job(source="x", url="https://x", company_name="C", title="T", description="D")
    content = ai.tailor(
        job,
        {
            "summary": ["old"],
            "projects": [],
            "work_experience": [],
            "skills": [],
        },
        {},
    )
    assert sorted(started) == ["projects", "skills", "work"]
    assert content.cv["summary"] == ["summary"]
    assert content.cover_letter["paragraphs"] == ["1", "2", "3"]
