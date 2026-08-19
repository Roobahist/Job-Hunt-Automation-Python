from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Mapping, Sequence
from threading import Lock
from typing import Any

from apify_client import ApifyClient

from job_hunt.errors import ErrorKind, ProviderError


class ApifyProvider:
    _lock = Lock()
    _unavailable_until: dict[str, float] = {}

    def __init__(
        self,
        tokens: Sequence[str],
        search_actor_id: str,
        single_actor_id: str,
        *,
        quota_cooldown_seconds: int = 3600,
    ) -> None:
        self.tokens = [token for token in tokens if token]
        if not self.tokens:
            raise ValueError("At least one Apify token is required")
        self.search_actor_id = search_actor_id
        self.single_actor_id = single_actor_id
        self.quota_cooldown_seconds = quota_cooldown_seconds

    @staticmethod
    def _token_id(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()[:12]

    @staticmethod
    def _is_capacity_error(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status in {401, 402, 403, 429}:
            return True
        text = str(exc).lower()
        markers = (
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "usage limit",
            "limit exceeded",
            "insufficient",
            "payment required",
            "not enough",
            "monthly usage",
            "account limit",
        )
        return any(marker in text for marker in markers)

    def _available_tokens(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            available = [
                token
                for token in self.tokens
                if self._unavailable_until.get(self._token_id(token), 0) <= now
            ]
            if available:
                return available
            # If every account is cooling down, retry the one whose cooldown expires first.
            return [
                min(
                    self.tokens,
                    key=lambda token: self._unavailable_until.get(self._token_id(token), 0),
                )
            ]

    def _cooldown(self, token: str) -> None:
        with self._lock:
            self._unavailable_until[self._token_id(token)] = (
                time.monotonic() + self.quota_cooldown_seconds
            )

    @staticmethod
    def _run_with_client(
        client: ApifyClient,
        actor_id: str,
        run_input: Mapping[str, Any],
        max_items: int | None,
    ) -> list[dict[str, Any]]:
        run = client.actor(actor_id).call(run_input=dict(run_input), max_items=max_items)
        if not run or not run.get("defaultDatasetId"):
            raise ProviderError(
                "Apify run returned no dataset",
                ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                provider="apify",
            )
        return list(client.dataset(run["defaultDatasetId"]).iterate_items())

    def _run(
        self, actor_id: str, run_input: Mapping[str, Any], max_items: int | None = None
    ) -> Iterable[dict[str, Any]]:
        failures: list[str] = []
        for token in self._available_tokens():
            try:
                yield from self._run_with_client(
                    ApifyClient(token), actor_id, run_input, max_items
                )
                return
            except ProviderError:
                raise
            except Exception as exc:
                failures.append(f"{self._token_id(token)}: {exc}")
                if self._is_capacity_error(exc):
                    self._cooldown(token)
                    continue
                raise ProviderError(
                    f"Apify request failed: {exc}",
                    ErrorKind.TRANSIENT_PROVIDER,
                    retryable=True,
                    provider="apify",
                ) from exc
        raise ProviderError(
            "Apify capacity exhausted for all configured accounts: " + "; ".join(failures),
            ErrorKind.RATE_LIMIT,
            retryable=True,
            provider="apify",
        )

    def discover(self, urls: Sequence[str], *, max_items: int) -> Iterable[Mapping[str, Any]]:
        return self._run(self.search_actor_id, {"startUrls": list(urls)}, max_items=max_items)

    def fetch_linkedin(
        self, job_id: int, *, country: str, max_concurrency: int
    ) -> Mapping[str, Any]:
        items = list(
            self._run(
                self.single_actor_id,
                {
                    "urls": [f"https://www.linkedin.com/jobs/view/{job_id}"],
                    "proxy": {"useApifyProxy": True, "apifyProxyCountry": country.upper()},
                    "maxConcurrency": max_concurrency,
                },
                max_items=1,
            )
        )
        if not items:
            raise ProviderError("LinkedIn job was not found", ErrorKind.BUSINESS, provider="apify")
        return items[0]
