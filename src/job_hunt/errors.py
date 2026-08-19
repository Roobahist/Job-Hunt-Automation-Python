from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    RATE_LIMIT = "rate_limit"
    TRANSIENT_PROVIDER = "transient_provider"
    MALFORMED_PROVIDER_RESPONSE = "malformed_provider_response"
    DOCUMENT_RENDERING = "document_rendering"
    BUSINESS = "business"


@dataclass(slots=True)
class WorkflowError(Exception):
    message: str
    kind: ErrorKind
    retryable: bool = False
    provider: str | None = None
    status_code: int | None = None
    retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


class ConfigurationError(WorkflowError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorKind.CONFIGURATION)


class DocumentRenderingError(WorkflowError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorKind.DOCUMENT_RENDERING)


class ProviderError(WorkflowError):
    pass
