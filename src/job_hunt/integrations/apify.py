from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from apify_client import ApifyClient

from job_hunt.errors import ErrorKind, ProviderError


class ApifyProvider:
    def __init__(self, token: str, search_actor_id: str, single_actor_id: str) -> None:
        self.client = ApifyClient(token)
        self.search_actor_id = search_actor_id
        self.single_actor_id = single_actor_id

    def _run(
        self, actor_id: str, run_input: Mapping[str, Any], max_items: int | None = None
    ) -> Iterable[dict[str, Any]]:
        try:
            run = self.client.actor(actor_id).call(run_input=dict(run_input), max_items=max_items)
            if not run or not run.get("defaultDatasetId"):
                raise ProviderError(
                    "Apify run returned no dataset",
                    ErrorKind.MALFORMED_PROVIDER_RESPONSE,
                    provider="apify",
                )
            yield from self.client.dataset(run["defaultDatasetId"]).iterate_items()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Apify request failed: {exc}",
                ErrorKind.TRANSIENT_PROVIDER,
                retryable=True,
                provider="apify",
            ) from exc

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
