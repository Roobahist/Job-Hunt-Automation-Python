from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from job_hunt.application.normalization import job_from_provider
from job_hunt.domain.models import Job


def build_search_url(base_url: str, criteria: Mapping[str, Any]) -> str:
    generated = criteria.get("Generated URL") or criteria.get("generatedUrl")
    if generated:
        return str(generated)
    params = {
        "keywords": criteria.get("Keywords") or criteria.get("keywords"),
        "location": criteria.get("Location") or criteria.get("location"),
        "f_TPR": criteria.get("Date Filter") or criteria.get("dateFilter"),
        "f_JT": criteria.get("Job Type") or criteria.get("jobType"),
    }
    return (
        base_url.rstrip("?")
        + "?"
        + urlencode({key: value for key, value in params.items() if value})
    )


def excluded(job: Job, company_terms: Sequence[str], title_terms: Sequence[str]) -> bool:
    company = job.company_name.casefold()
    title = job.title.casefold()
    return any(term.casefold() in company for term in company_terms) or any(
        term.casefold() in title for term in title_terms
    )


def normalize_discovery(
    rows: Iterable[Mapping[str, Any]], company_terms: Sequence[str], title_terms: Sequence[str]
) -> list[Job]:
    unique: dict[str, Job] = {}
    for row in rows:
        try:
            job = job_from_provider(row)
        except ValueError:
            continue
        if not excluded(job, company_terms, title_terms):
            unique.setdefault(job.identity, job)
    return list(unique.values())
