from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx

from job_hunt.domain.models import Job, Qualification
from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.http import raise_provider_error


class BaserowClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.baserow.io",
        client: httpx.Client | None = None,
        *,
        timeout_seconds: int = 60,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
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

    def iter_rows(self, table_id: int, **params: object) -> Iterator[dict[str, Any]]:
        url: str | None = f"/api/database/rows/table/{table_id}/"
        query: dict[str, Any] = {"user_field_names": "true", "size": 200, **params}
        while url:
            response = self._request("GET", url, params=query)
            payload = response.json()
            yield from payload.get("results", [])
            url = payload.get("next")
            query = {}

    def get_row(self, table_id: int, row_id: int) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"/api/database/rows/table/{table_id}/{row_id}/?user_field_names=true",
        )
        return cast(dict[str, Any], response.json())

    def find_equal(self, table_id: int, field: str, value: object) -> dict[str, Any] | None:
        rows = list(self.iter_rows(table_id, **{f"filter__{field}__equal": value, "size": 2}))
        return rows[0] if rows else None

    def create_row(self, table_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST", f"/api/database/rows/table/{table_id}/?user_field_names=true", json=dict(values)
        )
        return cast(dict[str, Any], response.json())

    def update_row(self, table_id: int, row_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
        response = self._request(
            "PATCH",
            f"/api/database/rows/table/{table_id}/{row_id}/?user_field_names=true",
            json=dict(values),
        )
        return cast(dict[str, Any], response.json())

    def upload_via_url(self, url: str) -> dict[str, Any]:
        response = self._request("POST", "/api/user-files/upload-via-url/", json={"url": url})
        return cast(dict[str, Any], response.json())

    def upload_file(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                response = self._request(
                    "POST",
                    "/api/user-files/upload-file/",
                    files={"file": (path.name, handle, "application/octet-stream")},
                )
        except OSError as exc:
            raise ProviderError(
                f"Could not read artifact for Baserow upload: {path}",
                ErrorKind.VALIDATION,
                provider="baserow",
            ) from exc
        return cast(dict[str, Any], response.json())

    def list_fields(self, table_id: int) -> list[dict[str, Any]]:
        response = self._request("GET", f"/api/database/fields/table/{table_id}/")
        return cast(list[dict[str, Any]], response.json())


class BaserowJobRepository:
    def __init__(
        self,
        client: BaserowClient,
        table_id: int,
        status_options: Mapping[str, int],
        contract_type_options: Mapping[str, int] | None = None,
    ) -> None:
        self.client = client
        self.table_id = table_id
        self.status_options = status_options
        self.contract_type_options = contract_type_options or {}

    @staticmethod
    def _display_job_id(job: Job) -> object:
        if job.external_id:
            return int(job.external_id) if job.external_id.isdigit() else job.external_id
        return job.internal_id

    def find(self, job: Job) -> Mapping[str, Any] | None:
        if job.external_id:
            found = self.client.find_equal(self.table_id, "Job ID", self._display_job_id(job))
            if found:
                return found
        return self.client.find_equal(self.table_id, "Link", job.url)

    def create(self, job: Job) -> Mapping[str, Any]:
        return self.client.create_row(
            self.table_id,
            self._job_fields(job) | {"Status": self.status_options["new"]},
        )

    def reset(self, row_id: int, job: Job) -> Mapping[str, Any]:
        return {"id": row_id}

    def clear_qualification(self, row_id: int) -> None:
        self.client.update_row(
            self.table_id,
            row_id,
            {
                "Score": None,
                "Apply": False,
            },
        )

    def save_qualification(self, row_id: int, result: Qualification) -> None:
        self.client.update_row(
            self.table_id,
            row_id,
            {
                "Score": result.score,
                "Apply": result.should_apply,
            },
        )

    def save_artifacts(self, row_id: int, uploaded_files: Mapping[str, Any]) -> None:
        self.client.update_row(self.table_id, row_id, dict(uploaded_files))

    def set_status(self, row_id: int, status_key: str) -> None:
        if status_key not in self.status_options:
            raise KeyError(f"Unknown status key: {status_key}")
        self.client.update_row(
            self.table_id,
            row_id,
            {"Status": self.status_options[status_key]},
        )

    def has_status(self, row_id: int, status_key: str) -> bool:
        if status_key not in self.status_options:
            raise KeyError(f"Unknown status key: {status_key}")
        row = self.client.get_row(self.table_id, row_id)
        current = row.get("Status")
        expected_id = self.status_options[status_key]
        if isinstance(current, Mapping):
            return current.get("id") == expected_id
        if isinstance(current, int):
            return current == expected_id
        return False

    def _job_fields(self, job: Job) -> dict[str, Any]:
        normalized_contract = "".join(
            character for character in (job.contract_type or "").casefold() if character.isalnum()
        )
        aliases = {
            "fulltime": "fullTime",
            "parttime": "partTime",
            "contract": "contract",
            "temporary": "temporary",
            "internship": "internship",
            "volunteer": "volunteer",
        }
        contract_key = aliases.get(normalized_contract)
        contract_value: object = self.contract_type_options.get(contract_key, "") if contract_key else ""
        return {
            "Job ID": self._display_job_id(job),
            "Company Name": job.company_name,
            "Title": job.title,
            "Location": job.location or "",
            "Job Description": job.description,
            "Contract Type": contract_value,
            "Link": job.url,
            "Date": job.published_at.date().isoformat() if job.published_at else None,
        }
