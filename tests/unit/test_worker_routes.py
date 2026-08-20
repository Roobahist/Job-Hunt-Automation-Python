from __future__ import annotations

from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.worker import _retry_countdown, celery_app


def test_expensive_and_notification_tasks_use_dedicated_queues() -> None:
    routes = celery_app.conf.task_routes
    assert routes["job_hunt.discover_tenant"]["queue"] == "fast"
    assert routes["job_hunt.process_submission"]["queue"] == "fast"
    assert routes["job_hunt.generate_documents"]["queue"] == "documents"
    assert routes["job_hunt.notify_documents"]["queue"] == "notifications"
    assert celery_app.conf.task_default_queue == "fast"


def test_retry_countdown_uses_provider_retry_after() -> None:
    exc = ProviderError(
        "capacity exhausted",
        ErrorKind.RATE_LIMIT,
        retryable=True,
        provider="gemini",
        retry_after=42,
    )
    assert _retry_countdown(exc, retries=0) == 42


def test_retry_countdown_gives_rate_limits_a_full_window_without_hint() -> None:
    exc = ProviderError(
        "local requests-per-minute budget exhausted",
        ErrorKind.RATE_LIMIT,
        retryable=True,
        provider="gemini",
    )
    assert _retry_countdown(exc, retries=0) == 65


def test_retry_countdown_uses_bounded_exponential_delay_for_other_transients() -> None:
    exc = ProviderError(
        "temporary provider failure",
        ErrorKind.TRANSIENT_PROVIDER,
        retryable=True,
        provider="test",
    )
    assert _retry_countdown(exc, retries=0) == 5
    assert _retry_countdown(exc, retries=3) == 40
    assert _retry_countdown(exc, retries=10) == 300
