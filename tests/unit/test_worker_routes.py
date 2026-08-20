from __future__ import annotations

from job_hunt.worker import celery_app


def test_expensive_and_notification_tasks_use_dedicated_queues() -> None:
    routes = celery_app.conf.task_routes
    assert routes["job_hunt.discover_tenant"]["queue"] == "fast"
    assert routes["job_hunt.process_submission"]["queue"] == "fast"
    assert routes["job_hunt.generate_documents"]["queue"] == "documents"
    assert routes["job_hunt.notify_documents"]["queue"] == "notifications"
    assert celery_app.conf.task_default_queue == "fast"
