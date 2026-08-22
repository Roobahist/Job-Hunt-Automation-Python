from __future__ import annotations

import httpx

import job_hunt.integrations.baserow as baserow_module
from job_hunt.integrations.baserow import BaserowClient
from job_hunt.retry import retry_transient


def test_iter_rows_retries_non_json_page(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text="", headers={"content-type": "text/html"}, request=request)
        return httpx.Response(
            200,
            json={"results": [{"id": 7, "Title": "Data Scientist"}], "next": None},
            request=request,
        )

    def immediate_retry(operation, *args, **kwargs):  # type: ignore[no-untyped-def]
        return retry_transient(operation, *args, attempts=3, base_delay=0, sleep=lambda _: None, **kwargs)

    monkeypatch.setattr(baserow_module, "retry_transient", immediate_retry)
    client = httpx.Client(base_url="https://api.baserow.io", transport=httpx.MockTransport(handler))
    baserow = BaserowClient("token", client=client)

    rows = list(baserow.iter_rows(123))

    assert rows == [{"id": 7, "Title": "Data Scientist"}]
    assert calls == 2
