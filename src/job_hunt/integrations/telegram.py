from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import ExitStack
from pathlib import Path

import httpx

from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.http import raise_provider_error


class TelegramNotifier:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}", timeout=60
        )

    def send_documents(self, chat_id: str, artifacts: Iterable[Path], caption: str) -> str:
        paths = list(artifacts)
        if not 2 <= len(paths) <= 10:
            raise ValueError("Telegram media groups require 2 to 10 documents")
        media = [
            {
                "type": "document",
                "media": f"attach://file_{index}",
                **({"caption": caption[:1024]} if index == 0 else {}),
            }
            for index, _ in enumerate(paths)
        ]
        with ExitStack() as stack:
            files = {
                f"file_{index}": (
                    path.name,
                    stack.enter_context(path.open("rb")),
                    "application/octet-stream",
                )
                for index, path in enumerate(paths)
            }
            try:
                response = self._client.post(
                    "/sendMediaGroup",
                    data={"chat_id": chat_id, "media": json.dumps(media)},
                    files=files,
                )
            except httpx.TransportError as exc:
                raise ProviderError(
                    f"Telegram network request failed: {exc}",
                    ErrorKind.TRANSIENT_PROVIDER,
                    retryable=True,
                    provider="telegram",
                ) from exc
        raise_provider_error("telegram", response)
        payload = response.json()
        return str(payload["result"][0]["message_id"])
