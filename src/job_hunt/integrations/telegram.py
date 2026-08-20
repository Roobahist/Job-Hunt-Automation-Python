from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

import httpx

from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.http import raise_provider_error


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: int = 120,
    ) -> None:
        self._client = client or httpx.Client(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=timeout_seconds,
        )

    def _post(self, path: str, **kwargs: Any) -> dict[str, object]:
        try:
            response = self._client.post(path, **kwargs)
        except httpx.TransportError as exc:
            raise ProviderError(
                f"Telegram network request failed: {exc}",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="telegram",
            ) from exc
        raise_provider_error("telegram", response)
        return cast(dict[str, object], response.json())

    @staticmethod
    def _actions(job_url: str, row_id: int, run_id: str) -> dict[str, object]:
        return {
            "inline_keyboard": [
                [{"text": "Open Job", "url": job_url}],
                [
                    {"text": "Mark Applied", "callback_data": f"status:applied:{row_id}"},
                    {"text": "Skip", "callback_data": f"status:dropped:{row_id}"},
                ],
                [{"text": "Regenerate", "callback_data": f"retry:{run_id}"}],
            ]
        }

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
            payload = self._post(
                "/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media)},
                files=files,
            )
        result = payload.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise ProviderError(
                "Telegram returned no media-group message",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )
        return str(result[0]["message_id"])

    def send_document_with_actions(
        self,
        chat_id: str,
        document: Path,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        with document.open("rb") as handle:
            payload = self._post(
                "/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "reply_markup": json.dumps(self._actions(job_url, row_id, run_id)),
                },
                files={"document": (document.name, handle, "application/octet-stream")},
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProviderError(
                "Telegram returned no document message",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )
        return str(result["message_id"])

    def send_application_actions(
        self,
        chat_id: str,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": caption,
            "reply_markup": self._actions(job_url, row_id, run_id),
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": int(reply_to_message_id)}
        response = self._post("/sendMessage", json=payload)
        result = response.get("result")
        if not isinstance(result, dict):
            raise ProviderError(
                "Telegram returned no action message",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )
        return str(result["message_id"])

    def send_application_bundle(
        self,
        chat_id: str,
        artifacts: Iterable[Path],
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        archives = [path for path in artifacts if path.suffix.casefold() == ".zip"]
        if len(archives) != 1:
            raise ValueError("Application notification requires exactly one ZIP archive")
        return self.send_document_with_actions(
            chat_id,
            archives[0],
            caption=caption,
            job_url=job_url,
            row_id=row_id,
            run_id=run_id,
        )

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self._post(
            "/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text[:200]},
        )
