from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from job_hunt.domain.models import Job, PromptDefinition, Qualification
from job_hunt.ports import CompatibilityFilter, JobRepository, Qualifier


@dataclass(frozen=True, slots=True)
class TriageDecision:
    row_id: int
    title: str
    company_name: str
    action: str
    reason: str
    score: int | None = None


@dataclass(frozen=True, slots=True)
class TriageSummary:
    scanned: int
    processed: int
    kept: int
    dropped: int
    compatibility_dropped: int
    qualification_dropped: int
    scored: int
    reused_scores: int
    errors: int
    decisions: tuple[TriageDecision, ...]


def _select_option_id(value: object) -> int | None:
    if isinstance(value, Mapping):
        candidate = value.get("id")
        return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _display_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        candidate = value.get("value", value.get("name"))
        return str(candidate).strip() if candidate is not None else None
    text = str(value).strip()
    return text or None


def _external_id(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


def _existing_score(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        score = int(value)
    elif isinstance(value, str) and value.strip():
        score = int(float(value.strip()))
    else:
        return None
    if not 0 <= score <= 100:
        raise ValueError(f"Score must be between 0 and 100, got {score}")
    return score


def job_from_baserow_row(row: Mapping[str, Any]) -> Job:
    row_id = row.get("id")
    if not isinstance(row_id, int) or isinstance(row_id, bool):
        raise ValueError("Baserow row is missing an integer id")
    return Job(
        source="baserow-maintenance",
        external_id=_external_id(row.get("Job ID")),
        url=str(row.get("Link") or "").strip(),
        company_name=str(row.get("Company Name") or "").strip(),
        title=str(row.get("Title") or "").strip(),
        description=str(row.get("Job Description") or "").strip(),
        location=_display_value(row.get("Location")),
        contract_type=_display_value(row.get("Contract Type")),
        internal_id=row_id,
    )


def triage_new_jobs(
    *,
    rows: Iterable[Mapping[str, Any]],
    new_status_id: int,
    threshold: int,
    compatibility_filter: CompatibilityFilter,
    qualifier: Qualifier,
    repository: JobRepository,
    prompts: Mapping[str, PromptDefinition],
    master_cv: Mapping[str, Any],
    dry_run: bool = False,
    limit: int | None = None,
) -> TriageSummary:
    decisions: list[TriageDecision] = []
    scanned = processed = kept = dropped = compatibility_dropped = qualification_dropped = 0
    scored = reused_scores = errors = 0

    for row in rows:
        scanned += 1
        if _select_option_id(row.get("Status")) != new_status_id:
            continue
        if limit is not None and processed >= limit:
            break
        processed += 1

        row_id_value = row.get("id")
        row_id = row_id_value if isinstance(row_id_value, int) and not isinstance(row_id_value, bool) else -1
        title = str(row.get("Title") or "").strip()
        company_name = str(row.get("Company Name") or "").strip()

        try:
            job = job_from_baserow_row(row)
            compatible = compatibility_filter.compatible(job, prompts)
            if not compatible:
                if not dry_run:
                    repository.set_status(job.internal_id, "dropped")
                dropped += 1
                compatibility_dropped += 1
                decisions.append(
                    TriageDecision(
                        row_id=job.internal_id,
                        title=job.title,
                        company_name=job.company_name,
                        action="would_drop" if dry_run else "dropped",
                        reason="compatibility",
                    )
                )
                continue

            existing_score = _existing_score(row.get("Score"))
            if existing_score is not None:
                score = existing_score
                reused_scores += 1
            else:
                qualification = qualifier.qualify(job, master_cv, prompts)
                score = qualification.score
                scored += 1
                if not dry_run:
                    repository.save_qualification(job.internal_id, qualification)

            if score < threshold:
                if not dry_run:
                    repository.set_status(job.internal_id, "dropped")
                dropped += 1
                qualification_dropped += 1
                decisions.append(
                    TriageDecision(
                        row_id=job.internal_id,
                        title=job.title,
                        company_name=job.company_name,
                        action="would_drop" if dry_run else "dropped",
                        reason=f"score_below_threshold_{threshold}",
                        score=score,
                    )
                )
                continue

            kept += 1
            decisions.append(
                TriageDecision(
                    row_id=job.internal_id,
                    title=job.title,
                    company_name=job.company_name,
                    action="keep_new",
                    reason=f"score_at_or_above_threshold_{threshold}",
                    score=score,
                )
            )
        except Exception as exc:
            errors += 1
            decisions.append(
                TriageDecision(
                    row_id=row_id,
                    title=title,
                    company_name=company_name,
                    action="error",
                    reason=str(exc),
                )
            )

    return TriageSummary(
        scanned=scanned,
        processed=processed,
        kept=kept,
        dropped=dropped,
        compatibility_dropped=compatibility_dropped,
        qualification_dropped=qualification_dropped,
        scored=scored,
        reused_scores=reused_scores,
        errors=errors,
        decisions=tuple(decisions),
    )
