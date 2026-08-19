from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.hackernews import HackerNewsSearchClient, HackerNewsSearchError


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://hn.algolia.com/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("search error", request=request, response=response)


class FakeClient:
    responses: list[FakeResponse] = []
    calls: list[tuple[str, dict[str, str]]] = []
    instances: list[FakeClient] = []

    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        self.__class__.calls.append((url, params))
        return self.__class__.responses.pop(0)


def _reset() -> None:
    FakeClient.responses = []
    FakeClient.calls = []
    FakeClient.instances = []


def test_hn_search_is_small_anonymous_and_proxy_safe(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [
        FakeResponse({"hits": [{"objectID": "123"}], "nbHits": 7})
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = HackerNewsSearchClient().search_domain("Example.com", hits_per_page=50)

    assert result.total_hits == 7
    assert len(result.hits) == 1
    assert FakeClient.calls == [
        (
            "https://hn.algolia.com/api/v1/search",
            {
                "query": "example.com",
                "tags": "(story,comment)",
                "hitsPerPage": "50",
            },
        )
    ]
    assert FakeClient.instances[0].kwargs["trust_env"] is False


def test_hn_malformed_hits_are_rejected(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [FakeResponse({"hits": {"bad": "shape"}})]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(HackerNewsSearchError, match="malformed hits"):
        HackerNewsSearchClient().search_domain("example.com")
