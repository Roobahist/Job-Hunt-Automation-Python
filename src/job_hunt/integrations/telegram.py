from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

import httpx

from job_hunt.errors import ErrorKind, ProviderError
from job_hunt.integrations.http import raise_provider_error

_DROPPED_STATUS_MARKER = "Status: ⛔ Dropped"


def _should_cleanup(caption: str) -> bool:
    return _DROPPED_STATUS_MARKER in caption


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

    def delete_message(self, chat_id: str, message_id: str) -> None:
        payload = self._post(
            "/deleteMessage",
            json={"chat_id": chat_id, "message_id": int(message_id)},
        )
        if payload.get("result") is not True:
            raise ProviderError(
                "Telegram did not confirm message deletion",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )

    def send_processing_message(
        self,
        chat_id: str,
        *,
        caption: str,
        job_url: str,
        row_id: int | None = None,
    ) -> str:
        # Dropped jobs should never leave a placeholder behind. Technical failures
        # remain visible so the underlying error can be diagnosed and retried.
        if _should_cleanup(caption):
            return ""
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
        if _should_cleanup(caption):
            self.delete_message(chat_id, message_id)
            return message_id
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
        return self._message_id(payload, "Telegram returned no edited final caption")

    def finalize_processing_message(
        self,
        chat_id: str,
        message_id: str,
        archive: Path,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        with archive.open("rb") as handle:
            payload = self._post(
                "/editMessageMedia",
                data={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "media": json.dumps(
                        {
                            "type": "document",
                            "media": "attach://document",
                            "caption": caption[:1024],
                        }
                    ),
                    "reply_markup": json.dumps(self._actions(job_url, row_id, run_id)),
                },
                files={"document": (archive.name, handle, "application/zip")},
            )
        return self._message_id(payload, "Telegram returned no finalized processing message")

    def answer_callback(self, callback_id: str, text: str) -> None:
        self._post("/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text})

    def send_documents(self, chat_id: str, artifacts: Iterable[Path], caption: str) -> str:
        paths = list(artifacts)
        if not 2 <= len(paths) <= 10:
            raise ValueError("Telegram media groups support 2 to 10 documents")
        media = []
        with ExitStack() as stack:
            files: dict[str, tuple[str, Any, str]] = {}
            for index, path in enumerate(paths):
                handle = stack.enter_context(path.open("rb"))
                name = f"file_{index}"
                files[name] = (path.name, handle, "application/octet-stream")
                media.append(
                    {
                        "type": "document",
                        "media": f"attach://{name}",
                        "caption": caption[:1024] if index == 0 else "",
                    }
                )
            payload = self._post(
                "/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media)},
                files=files,
            )
        result = payload.get("result")
        if not isinstance(result, list) or not result:
            raise ProviderError(
                "Telegram returned no media group messages",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )
        first = result[0]
        if not isinstance(first, dict) or "message_id" not in first:
            raise ProviderError(
                "Telegram returned malformed media group result",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="telegram",
            )
        return str(first["message_id"])

    def send_document_with_actions(
        self,
        chat_id: str,
        archive: Path,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        with archive.open("rb") as handle:
            payload = self._post(
                "/sendDocument",
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "reply_markup": json.dumps(self._actions(job_url, row_id, run_id)),
                },
                files={"document": (archive.name, handle, "application/zip")},
            )
        return self._message_id(payload, "Telegram returned no application archive message")

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
        paths = list(artifacts)
        archives = [path for path in paths if path.suffix.casefold() == ".zip"]
        if len(archives) != 1:
            raise ValueError("Telegram application bundle requires exactly one ZIP archive")
        return self.send_document_with_actions(
            chat_id,
            archives[0],
            caption=caption,
            job_url=job_url,
            row_id=row_id,
            run_id=run_id,
        )

    def send_application_actions(
        self,
        chat_id: str,
        *,
        caption: str,
        job_url: str,
        row_id: int,
        run_id: str,
    ) -> str:
        payload = self._post(
            "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": caption[:4096],
                "reply_markup": self._actions(job_url, row_id, run_id),
            },
        )
        return self._message_id(payload, "Telegram returned no application action message")
