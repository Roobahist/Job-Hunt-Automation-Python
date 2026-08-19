from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from job_hunt.errors import ProviderError
from job_hunt.integrations.apify import ApifyProvider


def provider_with_client() -> tuple[ApifyProvider, MagicMock]:
    provider = ApifyProvider.__new__(ApifyProvider)
    provider.search_actor_id = "search"
    provider.single_actor_id = "single"
    provider.client = MagicMock()
    return provider, provider.client


def test_apify_waits_for_actor_and_iterates_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr("job_hunt.integrations.apify.ApifyClient", lambda _: client)
    provider = ApifyProvider("token", "search", "single")
    client.actor.return_value.call.return_value = {"defaultDatasetId": "dataset"}
    client.dataset.return_value.iterate_items.return_value = iter([{"id": 1}, {"id": 2}])
    assert list(provider.discover(["https://search"], max_items=2)) == [{"id": 1}, {"id": 2}]
    client.actor.assert_called_once_with("search")
    client.actor.return_value.call.assert_called_once_with(
        run_input={"startUrls": ["https://search"]}, max_items=2
    )
    client.dataset.assert_called_once_with("dataset")


def test_apify_fetches_one_linkedin_job_with_proxy() -> None:
    provider, client = provider_with_client()
    client.actor.return_value.call.return_value = {"defaultDatasetId": "dataset"}
    client.dataset.return_value.iterate_items.return_value = iter([{"id": 9}])
    assert provider.fetch_linkedin(9, country="ca", max_concurrency=2) == {"id": 9}
    run_input = client.actor.return_value.call.call_args.kwargs["run_input"]
    assert run_input["proxy"]["apifyProxyCountry"] == "CA"


def test_apify_empty_single_job_is_business_error() -> None:
    provider, client = provider_with_client()
    client.actor.return_value.call.return_value = {"defaultDatasetId": "dataset"}
    client.dataset.return_value.iterate_items.return_value = iter([])
    with pytest.raises(ProviderError, match="not found"):
        provider.fetch_linkedin(9, country="ca", max_concurrency=1)


def test_apify_missing_dataset_is_classified() -> None:
    provider, client = provider_with_client()
    client.actor.return_value.call.return_value = {}
    with pytest.raises(ProviderError, match="no dataset"):
        list(provider.discover([], max_items=1))
