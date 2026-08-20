from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from job_hunt.domain.identity import assign_identity
from job_hunt.domain.models import (
    AiContentSubmission,
    EntryType,
    ExternalSubmission,
    Job,
    JobSubmission,
    LinkedInSubmission,
    UrlSubmission,
)
from job_hunt.ports import DiscoveryProvider
from job_hunt.security import fetch_public_text

_LINKEDIN_JOB_ID = re.compile(r"(?:^|[-/])(\d{6,})(?:/)?$")


def linkedin_job_id_from_url(url: str) -> int | None:
    parts = urlsplit(url)
    if not (parts.hostname or "").lower().endswith("linkedin.com"):
        return None
    if "/jobs/view/" not in parts.path:
        return None
    match = _LINKEDIN_JOB_ID.search(parts.path.rstrip("/"))
    return int(match.group(1)) if match else None


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_datetime(int(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def job_from_provider(data: Mapping[str, Any], *, source: str = "linkedin") -> Job:
    external_id = data.get("id") or data.get("jobId") or data.get("job_id")
    url = data.get("url") or data.get("link") or data.get("jobUrl")
    company = data.get("companyName") or data.get("company") or data.get("company_name")
    title = data.get("title") or data.get("jobTitle") or data.get("job_title")
    description = data.get("description") or data.get("jobDescription") or data.get("job_description")
    published = (
        data.get("publishedAt")
        or data.get("published_at")
        or data.get("postedAt")
        or data.get("posted_at")
        or data.get("datePosted")
        or data.get("date_posted")
        or data.get("postedDate")
        or data.get("publishedDate")
    )
    return assign_identity(
        Job(
            source=source,
            external_id=str(external_id) if external_id else None,
            url=str(url or ""),
            company_name=str(company or ""),
            title=str(title or ""),
            description=str(description or ""),
            location=str(data.get("location") or "") or None,
            contract_type=str(data.get("contractType") or data.get("contract_type") or "") or None,
            published_at=_parse_datetime(published),
        )
    )


class SubmissionNormalizer:
    def __init__(self, discovery: DiscoveryProvider, extractor: Any, linkedin_template: str) -> None:
        self.discovery = discovery
        self.extractor = extractor
        self.linkedin_template = linkedin_template

    def _linkedin(self, job_id: int, *, country: str, max_concurrency: int) -> Job:
        raw = self.discovery.fetch_linkedin(job_id, country=country, max_concurrency=max_concurrency)
        enriched = dict(raw) | {
            "id": raw.get("id") or raw.get("jobId") or raw.get("job_id") or job_id,
            "url": raw.get("url") or raw.get("link") or self.linkedin_template.format(jobId=job_id),
        }
        return job_from_provider(enriched)

    def normalize(self, submission: JobSubmission, *, country: str, max_concurrency: int) -> Job:
        if isinstance(submission, ExternalSubmission):
            return assign_identity(
                Job(
                    source=submission.source,
                    external_id=submission.external_job_id,
                    url=str(submission.job_url),
                    company_name=submission.company_name,
                    title=submission.job_title,
                    description=submission.job_description,
                    location=submission.location,
                    contract_type=submission.contract_type,
                    published_at=submission.published_at,
                )
            )
        if isinstance(submission, LinkedInSubmission):
            return self._linkedin(
                submission.linkedin_job_id,
                country=country,
                max_concurrency=max_concurrency,
            )
        if isinstance(submission, UrlSubmission):
            source_url = str(submission.job_url)
            linkedin_job_id = linkedin_job_id_from_url(source_url)
            if linkedin_job_id is not None:
                return self._linkedin(
                    linkedin_job_id,
                    country=country,
                    max_concurrency=max_concurrency,
                )
            content = fetch_public_text(source_url)
            return assign_identity(self.extractor.extract_job(content, source_url))
        if isinstance(submission, AiContentSubmission):
            return assign_identity(
                self.extractor.extract_job(submission.page_content, str(submission.source_url or ""))
            )
        raise TypeError(f"Unsupported submission: {submission.entry_type}")


def fillout_submission(payload: Mapping[str, Any], field_ids: Mapping[str, str]) -> JobSubmission:
    source = payload.get("submission", payload)
    if not isinstance(source, Mapping):
        source = payload
    flattened: dict[str, Any] = dict(source)
    questions = source.get("questions", [])
    if isinstance(questions, list):
        for question in questions:
            if not isinstance(question, Mapping):
                continue
            question_id = question.get("id") or question.get("questionId")
            if question_id:
                flattened[str(question_id)] = question.get("value")

    def value(name: str) -> Any:
        configured_id = field_ids.get(name)
        if configured_id and configured_id in flattened:
            return flattened[configured_id]
        return flattened.get(name)

    entry = str(value("entryType") or "").strip().lower().replace(" ", "_")
    if entry == EntryType.LINKEDIN:
        return LinkedInSubmission(entry_type=EntryType.LINKEDIN, linkedin_job_id=int(value("linkedinJobId")))
    if entry == EntryType.EXTERNAL:
        return ExternalSubmission(
            entry_type=EntryType.EXTERNAL,
            company_name=value("companyName"),
            job_title=value("jobTitle"),
            job_description=value("jobDescription"),
            job_url=value("jobUrl"),
            location=value("location"),
            contract_type=value("contractType"),
            published_at=value("publishedAt"),
            external_job_id=value("externalJobId"),
        )
    if entry == EntryType.AI_CONTENT:
        return AiContentSubmission(
            entry_type=EntryType.AI_CONTENT,
            page_content=value("pageContent"),
            source_url=value("jobUrl"),
        )
    return UrlSubmission(entry_type=EntryType.URL, job_url=value("jobUrl"))
