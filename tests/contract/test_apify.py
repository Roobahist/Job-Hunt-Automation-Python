from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from job_hunt.errors import ProviderError
from job_hunt.integrations.apify import ApifyProvider


def client_with_items(items: list[dict[str, object]]) -> MagicMock:
    client = MagicMock()
    client.actor.return_value.call.return_value = {"defaultDatasetId": "dataset"}
    client.dataset.return_value.iterate_items.return_value = iter(items)
    return client


def test_apify_waits_for_actor_and_iterates_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    ApifyProvider._unavailable_until.clear()
    client = client_with_items([{"id": 1}, {"id": 2}])
    monkeypatch.setattr("job_hunt.integrations.apify.ApifyClient", lambda _: client)
    provider = ApifyProvider(["token"], "search", "single")
    assert list(provider.discover(["https://search"], max_items=2)) == [{"id": 1}, {"id": 2}]
    client.actor.assert_called_once_with("search")
    client.actor.return_value.call.assert_called_once_with(
        run_input={"startUrls": [{"url": "https://search"}]},
        max_items=2,
    )
    client.dataset.assert_called_once_with("dataset")


def test_apify_rotates_to_next_shared_token_on_capacity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ApifyProvider._unavailable_until.clear()
    exhausted = MagicMock()
    exhausted.actor.return_value.call.side_effect = RuntimeError("monthly usage limit exceeded")
    available = client_with_items([{"id": 9}])
    clients = {"first": exhausted, "second": available}
    monkeypatch.setattr("job_hunt.integrations.apify.ApifyClient", lambda token: clients[token])

    provider = ApifyProvider(["first", "second"], "search", "single")
    assert list(provider.discover(["https://search"], max_items=1)) == [{"id": 9}]
    exhausted.actor.assert_called_once_with("search")
    available.actor.assert_called_once_with("search")


def test_apify_fetches_one_linkedin_job_with_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    ApifyProvider._unavailable_until.clear()
    client = client_with_items([{"id": 9}])
    monkeypatch.setattr("job_hunt.integrations.apify.ApifyClient", lambda _: client)
    provider = ApifyProvider(["token"], "search", "single")
    assert provider.fetch_linkedin(9, country="ca", max_concurrency=2) == {"id": 9}
    run_input = client.actor.return_value.call.call_args.kwargs["run_input"]
    assert run_input["proxy"]["apifyProxyCountry"] == "CA"


def test_apify_empty_single_job_is_business_error(monkeypatch: pytest.MonkeyPatch) -> None:
    ApifyProvider._unavailable_until.clear()
    client = client_with_items([])
    monkeypatch.setattr("job_hunt.integrations.apify.ApifyClient", lambda _: client)
    provider = ApifyProvider(["token"], "search", "single")
    with pytest.raises(ProviderError, match="not found"):
        provider.fetch_linkedin(9, country="ca", max_concurrency=1)


def test_apify_missing_dataset_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    ApifyProvider._unavailable_until.clear()
    client = MagicMock()
    client.actor.return_value.call.return_value = {}
    monkeypatch.setattr("job_hunt.integrations.apify.ApifyClient", lambda _: client)
    provider = ApifyProvider(["token"], "search", "single")
    with pytest.raises(ProviderError, match="no dataset"):
        list(provider.discover(["https://search"], max_items=1))
