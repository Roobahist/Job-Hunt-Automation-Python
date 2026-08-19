from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

_SECRET_PATTERN = re.compile(r"(authorization|token|api[_-]?key|secret|password)", re.I)


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event):
        if _SECRET_PATTERN.search(key):
            event[key] = "[REDACTED]"
        elif isinstance(event[key], str) and len(event[key]) > 2000:
            event[key] = event[key][:2000] + "…[truncated]"
    return event


def configure_logging(*, json_logs: bool) -> None:
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def logger() -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger())
