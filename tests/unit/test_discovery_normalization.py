from __future__ import annotations

from job_hunt.application.discovery import (
    build_search_url,
    excluded,
    normalize_discovery,
    search_criteria_active,
)
from job_hunt.application.normalization import fillout_submission, job_from_provider
from job_hunt.domain.models import ExternalSubmission, LinkedInSubmission


def provider_row(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": 1,
        "url": "https://example.com/jobs/1",
        "companyName": "Good Company",
        "title": "Data Analyst",
        "description": "Analyze data",
    }
    values.update(updates)
    return values


def test_search_url_prefers_generated_or_encodes_fields() -> None:
    assert build_search_url("https://x", {"Generated URL": "https://ready"}) == "https://ready"
    url = build_search_url("https://x/search/", {"Keywords": "data science", "Location": "Calgary"})
    assert url == "https://x/search/?keywords=data+science&location=Calgary"


def test_search_criteria_requires_active_flag() -> None:
    assert search_criteria_active({"Active": True})
    assert search_criteria_active({"Active": "yes"})
    assert search_criteria_active({"active": 1})
    assert not search_criteria_active({"Active": False})
    assert not search_criteria_active({"Active": "off"})
    assert not search_criteria_active({})


def test_discovery_filters_and_deduplicates() -> None:
    rows = [
        provider_row(),
        provider_row(),
        provider_row(id=2, url="https://x/2", title="Senior Lead"),
    ]
    jobs = normalize_discovery(rows, [], ["senior", "lead"])
    assert len(jobs) == 1
    assert excluded(job_from_provider(provider_row(companyName="Mercor")), ["mercor"], [])


def test_fillout_normalizes_linkedin_and_external() -> None:
    linkedin = fillout_submission({"entryType": "linkedin", "linkedinJobId": "42"}, {})
    assert isinstance(linkedin, LinkedInSubmission)
    external = fillout_submission(
        {
            "entryType": "external",
            "companyName": "C",
            "jobTitle": "T",
            "jobDescription": "D",
            "jobUrl": "https://example.com/j",
        },
        {},
    )
    assert isinstance(external, ExternalSubmission)


def test_fillout_flattens_official_questions_shape() -> None:
    payload = {
        "submission": {
            "questions": [
                {"id": "entry-id", "value": "linkedin"},
                {"questionId": "job-id", "value": "42"},
            ]
        }
    }
    submission = fillout_submission(
        payload,
        {"entryType": "entry-id", "linkedinJobId": "job-id"},
    )
    assert isinstance(submission, LinkedInSubmission)
    assert submission.linkedin_job_id == 42
