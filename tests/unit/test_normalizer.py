from __future__ import annotations

from job_hunt.application.normalization import SubmissionNormalizer, fillout_submission
from job_hunt.domain.models import (
    AiContentSubmission,
    EntryType,
    Job,
    LinkedInSubmission,
    UrlSubmission,
)


class Discovery:
    def fetch_linkedin(self, job_id: int, **_: object) -> dict[str, object]:
        return {"companyName": "C", "title": "T", "description": "D"}


class Extractor:
    def extract_job(self, _: str, url: str) -> Job:
        return Job(source="web", url=url, company_name="C", title="T", description="D")


def test_normalizer_linkedin_ai_and_url(monkeypatch: object) -> None:
    normalizer = SubmissionNormalizer(Discovery(), Extractor(), "https://linkedin/{jobId}")  # type: ignore[arg-type]
    linkedin = normalizer.normalize(
        LinkedInSubmission(entry_type=EntryType.LINKEDIN, linkedin_job_id=5),
        country="ca",
        max_concurrency=1,
    )
    assert linkedin.external_id == "5"
    assert linkedin.url == "https://linkedin/5"
    ai = normalizer.normalize(
        AiContentSubmission(entry_type=EntryType.AI_CONTENT, page_content="posting", source_url="https://x/job"),
        country="ca",
        max_concurrency=1,
    )
    assert ai.identity == "https://x/job"


def test_url_normalizer_fetches_public_text(monkeypatch: object) -> None:
    monkeypatch.setattr("job_hunt.application.normalization.fetch_public_text", lambda _: "posting")  # type: ignore[attr-defined]
    normalizer = SubmissionNormalizer(Discovery(), Extractor(), "https://linkedin/{jobId}")  # type: ignore[arg-type]
    job = normalizer.normalize(
        UrlSubmission(entry_type=EntryType.URL, job_url="https://x/job"),
        country="ca",
        max_concurrency=1,
    )
    assert job.company_name == "C"


def test_fillout_ai_and_url_branches() -> None:
    ai = fillout_submission({"entryType": "ai_content", "pageContent": "text", "jobUrl": "https://x/job"}, {})
    assert isinstance(ai, AiContentSubmission)
    url = fillout_submission({"entryType": "url", "jobUrl": "https://x/job"}, {})
    assert isinstance(url, UrlSubmission)


def test_fillout_semantic_keys_work_with_configured_field_ids() -> None:
    submission = fillout_submission(
        {"entryType": "linkedin", "linkedinJobId": "123"},
        {
            "entryType": "opaque-entry-id",
            "linkedinJobId": "opaque-linkedin-id",
        },
    )
    assert isinstance(submission, LinkedInSubmission)
    assert submission.linkedin_job_id == 123


def test_fillout_configured_ids_still_take_precedence() -> None:
    submission = fillout_submission(
        {
            "entryType": "url",
            "jobUrl": "https://semantic.example/job",
            "opaque-entry-id": "linkedin",
            "opaque-linkedin-id": "456",
        },
        {
            "entryType": "opaque-entry-id",
            "linkedinJobId": "opaque-linkedin-id",
        },
    )
    assert isinstance(submission, LinkedInSubmission)
    assert submission.linkedin_job_id == 456
