from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.http import raise_provider_error, retry_after_seconds
from job_hunt.integrations.telegram import TelegramNotifier


def test_http_error_classification_and_retry_after() -> None:
    response = httpx.Response(429, headers={"Retry-After": "7"}, text="slow")
    assert retry_after_seconds(response) == 7
    with pytest.raises(ProviderError) as caught:
        raise_provider_error("test", response)
    assert caught.value.kind is ErrorKind.RATE_LIMIT
    assert caught.value.retryable
    with pytest.raises(ProviderError) as auth:
        raise_provider_error("test", httpx.Response(401, text="no"))
    assert auth.value.kind is ErrorKind.AUTHENTICATION
    with pytest.raises(ProviderError) as upstream:
        raise_provider_error("test", httpx.Response(503, text="down"))
    assert upstream.value.kind is ErrorKind.TRANSIENT_PROVIDER
    assert upstream.value.retryable
    with pytest.raises(ProviderError) as business:
        raise_provider_error("test", httpx.Response(400, text="invalid"))
    assert business.value.kind is ErrorKind.BUSINESS
    assert not business.value.retryable


@respx.mock
def test_telegram_uses_one_multipart_media_group(tmp_path: Path) -> None:
    files = []
    for index in range(5):
        path = tmp_path / f"{index}.pdf"
        path.write_bytes(b"pdf")
        files.append(path)
    route = respx.post("https://api.telegram.org/botsecret/sendMediaGroup").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [{"message_id": 12}]})
    )
    result = TelegramNotifier("secret").send_documents("42", files, "caption")
    assert result == "12"
    body = route.calls[0].request.content
    assert body.count(b'Content-Disposition: form-data; name="file_') == 5
    assert b"sendMediaGroup" not in body


def test_telegram_enforces_api_album_size(tmp_path: Path) -> None:
    one = tmp_path / "one"
    one.write_text("x")
    with pytest.raises(ValueError, match="2 to 10"):
        TelegramNotifier("x").send_documents("1", [one], "x")
