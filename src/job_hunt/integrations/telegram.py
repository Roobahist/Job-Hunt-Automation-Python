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

    @staticmethod
    def _processing_actions(job_url: str, row_id: int | None) -> dict[str, object]:
        rows: list[list[dict[str, object]]] = [[{"text": "Open Job", "url": job_url}]]
        if row_id is not None:
            rows.append(
                [
                    {
                        "text": "Skip Processing",
                        "callback_data": f"status:dropped:{row_id}",
                    }
                ]
            )
        return {"inline_keyboard": rows}

    @staticmethod
    def _message_id(payload: dict[str, object], error_message: str) -> str:
        result = payload.get("result")
        if not isinstance(result, dict) or "message_id" not in result:
            raise ProviderError(
                error_message,
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )
        return str(result["message_id"])

    def send_processing_message(
        self,
        chat_id: str,
        *,
        caption: str,
        job_url: str,
        row_id: int | None = None,
    ) -> str:
        placeholder = (
            b"Job processing is in progress. This placeholder will be replaced by "
            b"the application ZIP when complete.\n"
        )
        payload = self._post(
            "/sendDocument",
            data={
                "chat_id": chat_id,
                "caption": caption[:1024],
                "reply_markup": json.dumps(self._processing_actions(job_url, row_id)),
            },
            files={"document": ("processing.txt", placeholder, "text/plain")},
        )
        return self._message_id(payload, "Telegram returned no processing message")

    def edit_processing_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        caption: str,
        job_url: str,
        row_id: int | None = None,
    ) -> str:
        data: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "caption": caption[:1024],
        }
        if "Status: ✅ Complete" not in caption:
            data["reply_markup"] = self._processing_actions(job_url, row_id)
        payload = self._post("/editMessageCaption", json=data)
        return self._message_id(payload, "Telegram returned no edited processing message")

    def edit_final_caption(
        self,
        chat_id: str,
        message_id: str,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        payload = self._post(
            "/editMessageCaption",
            json={
                "chat_id": chat_id,
                "message_id": int(message_id),
                "caption": caption[:1024],
                "reply_markup": self._actions(job_url, row_id, run_id),
            },
        )
        return self._message_id(payload, "Telegram returned no edited final message")

    def finalize_processing_message(
        self,
        chat_id: str,
        message_id: str,
        document: Path,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        media = {
            "type": "document",
            "media": "attach://document",
            "caption": caption[:1024],
        }
        with document.open("rb") as handle:
            payload = self._post(
                "/editMessageMedia",
                data={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media": json.dumps(media),
                    "reply_markup": json.dumps(self._actions(job_url, row_id, run_id)),
                },
                files={"document": (document.name, handle, "application/zip")},
            )
        return self._message_id(payload, "Telegram returned no finalized application message")

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
        return self._message_id(payload, "Telegram returned no document message")

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
        return self._message_id(response, "Telegram returned no action message")

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
