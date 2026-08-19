from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from job_hunt.domain.models import ArtifactBundle, Job, Qualification
from job_hunt.errors import ProviderError
from job_hunt.integrations.artifacts import CloudinaryBaserowPublisher
from job_hunt.integrations.cloudinary import CloudinaryPublisher
from job_hunt.integrations.gemini import GeminiStructuredClient, GeminiWorkflowAI


def bundle(tmp_path: Path) -> ArtifactBundle:
    paths = [
        tmp_path / name
        for name in ("cv.json", "cv.tex", "cv.pdf", "cl.json", "cl.tex", "cl.pdf", "all.zip")
    ]
    for path in paths:
        path.write_text("x")
    return ArtifactBundle(
        run_directory=tmp_path,
        cv_json=paths[0],
        cv_tex=paths[1],
        cv_pdf=paths[2],
        cover_letter_json=paths[3],
        cover_letter_tex=paths[4],
        cover_letter_pdf=paths[5],
        archive=paths[6],
    )


def test_cloudinary_uses_raw_signed_uploads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr("job_hunt.integrations.cloudinary.cloudinary.config", lambda **_: None)
    monkeypatch.setattr(
        "job_hunt.integrations.cloudinary.cloudinary.uploader.upload",
        lambda path, **kwargs: (
            calls.append((path, kwargs)) or {"secure_url": "https://cdn/" + Path(path).name}
        ),
    )
    old = os.environ.pop("CLOUDINARY_URL", None)
    try:
        result = CloudinaryPublisher("cloudinary://key:secret@cloud").publish(
            bundle(tmp_path), "folder", ["tag"]
        )
    finally:
        if old:
            os.environ["CLOUDINARY_URL"] = old
    assert len(result) == 7
    assert all(call[1]["resource_type"] == "raw" and call[1]["overwrite"] for call in calls)
    assert "CLOUDINARY_URL" not in os.environ or os.environ["CLOUDINARY_URL"] == old


def test_cloudinary_baserow_import_mapping(tmp_path: Path) -> None:
    artifact = bundle(tmp_path)

    class Cloud:
        def publish(self, artifacts: ArtifactBundle, *_: object) -> dict[str, dict[str, str]]:
            return {
                path.name: {"secure_url": "https://cdn/" + path.name}
                for path in artifacts.all_paths()
            }

    class Base:
        def upload_via_url(self, url: str) -> dict[str, str]:
            return {"name": url.rsplit("/", 1)[-1]}

    result = CloudinaryBaserowPublisher(Cloud(), Base()).publish(artifact, "f", [])  # type: ignore[arg-type]
    assert len(result["CV"]) == len(result["Cover Letter"]) == 3


def test_gemini_structured_primary_and_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    clients: list[str] = []

    class Models:
        def __init__(self, key: str) -> None:
            self.key = key

        def generate_content(self, **_: object) -> object:
            if self.key == "bad":
                raise RuntimeError("temporary")
            return SimpleNamespace(
                parsed={"score": 80, "should_apply": True, "reasoning": "match"}, text=None
            )

    def client(api_key: str) -> object:
        clients.append(api_key)
        return SimpleNamespace(models=Models(api_key))

    monkeypatch.setattr("job_hunt.integrations.gemini.genai.Client", client)
    structured = GeminiStructuredClient("bad", "good", "models/test")
    assert structured.generate("prompt", Qualification).score == 80
    assert clients == ["bad", "good"]


def test_gemini_all_keys_fail_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "job_hunt.integrations.gemini.genai.Client",
        lambda **_: SimpleNamespace(
            models=SimpleNamespace(
                generate_content=lambda **__: (_ for _ in ()).throw(ValueError("bad"))
            )
        ),
    )
    with pytest.raises(ProviderError, match="all configured keys"):
        GeminiStructuredClient("bad", "", "test").generate("x", Qualification)


def test_gemini_workflow_ai_builds_all_domain_outputs() -> None:
    class Structured:
        def generate(self, prompt: str, schema: type[Any]) -> Any:
            if schema is Qualification:
                return Qualification(score=50, should_apply=True, reasoning="yes")
            if schema.__name__ == "_TailoredResponse":
                return schema(cv={"summary": []}, cover_letter={"paragraphs": []})
            return schema(source="web", company_name="C", title="T", description="D")

    ai = GeminiWorkflowAI(Structured())  # type: ignore[arg-type]
    job = Job(source="x", url="https://x/1", company_name="C", title="T", description="D")
    assert ai.qualify(job, {}, {"qualification": "score"}).score == 50
    assert "summary" in ai.tailor(job, {}, {"tailoring": "tailor"}).cv
    assert ai.extract_job("posting", "https://x/1").identity == "https://x/1"
