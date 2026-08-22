from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from job_hunt.domain.models import Job, Qualification
from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.http import raise_provider_error
from job_hunt.retry import retry_transient


class BaserowClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.baserow.io",
        client: httpx.Client | None = None,
        *,
        timeout_seconds: int = 60,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._base_origin = urlsplit(self._base_url)
        self._client = client or httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Token {token}"},
            timeout=timeout_seconds,
        )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            raise ProviderError(
                f"Baserow network request failed: {exc}",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="baserow",
            ) from exc
        raise_provider_error("baserow", response)
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            preview = response.text[:200].replace("\n", " ").strip() or "<empty body>"
            raise ProviderError(
                f"Baserow returned a non-JSON response (status={response.status_code}, "
                f"content_type={content_type}, body={preview!r})",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                retryable=True,
                provider="baserow",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                "Baserow returned JSON that was not an object",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                retryable=True,
                provider="baserow",
                status_code=response.status_code,
            )
        return cast(dict[str, Any], payload)

    def _row_page(self, url: str, query: Mapping[str, Any]) -> dict[str, Any]:
        # Pagination URLs can already contain their query string. Passing params={}
        # to HTTPX replaces that query string, so only provide params when non-empty.
        request_kwargs: dict[str, Any] = {"params": dict(query)} if query else {}
        response = self._request("GET", url, **request_kwargs)
        payload = self._json_object(response)
        results = payload.get("results")
        if not isinstance(results, list):
            raise ProviderError(
                "Baserow row page is missing a results array",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                retryable=True,
                provider="baserow",
                status_code=response.status_code,
            )
        return payload

    def _pagination_url(self, value: object) -> str | None:
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        parsed = urlsplit(raw)
        if not parsed.scheme and not parsed.netloc:
            return raw
        if parsed.hostname != self._base_origin.hostname:
            raise ProviderError(
                f"Baserow pagination URL changed host from {self._base_origin.hostname!r} to {parsed.hostname!r}",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="baserow",
            )
        path = parsed.path or "/"
        return f"{path}?{parsed.query}" if parsed.query else path

    def iter_rows(self, table_id: int, **params: object) -> Iterator[dict[str, Any]]:
        url: str | None = f"/api/database/rows/table/{table_id}/"
        query: dict[str, Any] = {"user_field_names": "true", "size": 200, **params}
        while url:
            payload = retry_transient(self._row_page, url, query)
            yield from payload["results"]
            url = self._pagination_url(payload.get("next"))
            query = {}

    def get_row(self, table_id: int, row_id: int) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/database/rows/table/{table_id}/{row_id}/?user_field_names=true",
        )
        return self._json_object(response)

    def find_equal(self, table_id: int, field: str, value: object) -> dict[str, Any] | None:
        rows = list(self.iter_rows(table_id, **{f"filter__{field}__equal": value, "size": 2}))
        return rows[0] if rows else None

    def create_row(self, table_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST", f"/api/database/rows/table/{table_id}/?user_field_names=true", json=dict(values)
        )
        return self._json_object(response)

    def update_row(self, table_id: int, row_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"/api/database/rows/table/{table_id}/{row_id}/?user_field_names=true",
            json=dict(values),
        )
        return self._json_object(response)

    def upload_file(self, file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as handle:
            response = self._request(
                "POST",
                "/api/user-files/upload-file/",
                files={"file": (file_path.name, handle)},
            )
        return self._json_object(response)


class BaserowJobRepository:
    def __init__(
        self,
        client: BaserowClient,
        table_id: int,
        status_option_ids: Mapping[str, int],
        contract_type_option_ids: Mapping[str, int],
    ) -> None:
        self.client = client
        self.table_id = table_id
        self.status_option_ids = dict(status_option_ids)
        self.contract_type_option_ids = dict(contract_type_option_ids)

    def find_existing(self, job: Job) -> dict[str, Any] | None:
        if job.external_id:
            existing = self.client.find_equal(self.table_id, "Job ID", job.external_id)
            if existing:
                return existing
        if job.url:
            return self.client.find_equal(self.table_id, "Link", job.url)
        return None

    def persist(self, job: Job) -> dict[str, Any]:
        existing = self.find_existing(job)
        values: dict[str, Any] = {
            "Company": job.company_name,
            "Job Title": job.title,
            "Description": job.description,
            "Link": job.url,
            "Location": job.location or "",
            "Status": self.status_option_ids["new"],
        }
        if job.external_id:
            values["Job ID"] = job.external_id
        if job.contract_type:
            option_id = self.contract_type_option_ids.get(job.contract_type.casefold())
            if option_id:
                values["Contract Type"] = option_id
        if job.published_at:
            values["Published"] = job.published_at.isoformat()
        if existing:
            return self.client.update_row(self.table_id, int(existing["id"]), values)
        return self.client.create_row(self.table_id, values)

    def update_status(self, row_id: int, status_key: str) -> dict[str, Any]:
        return self.client.update_row(self.table_id, row_id, {"Status": self.status_option_ids[status_key]})

    def save_qualification(self, row_id: int, qualification: Qualification) -> dict[str, Any]:
        return self.client.update_row(
            self.table_id,
            row_id,
            {
                "Match Score": qualification.score,
                "Qualification Reasoning": qualification.reasoning,
            },
        )
