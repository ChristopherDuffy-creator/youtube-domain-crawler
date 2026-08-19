from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.stackexchange import StackExchangeClient, StackExchangeError


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://api.stackexchange.com/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("api error", request=request, response=response)


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


def test_exact_url_search_contract_is_small_and_proxy_safe(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [
        FakeResponse(
            {
                "items": [{"question_id": 1, "body": "<p>body</p>"}],
                "quota_remaining": 9999,
                "backoff": 0,
            }
        )
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    result = StackExchangeClient().search_url(
        site="stackoverflow",
        domain="Example.com",
        min_views=1000,
        page_size=20,
    )

    assert len(result.items) == 1
    assert result.quota_remaining == 9999
    assert result.backoff_seconds == 0
    assert FakeClient.calls == [
        (
            "https://api.stackexchange.com/2.3/search/advanced",
            {
                "site": "stackoverflow",
                "url": "*example.com*",
                "views": "1000",
                "sort": "votes",
                "order": "desc",
                "pagesize": "20",
                "filter": "withbody",
            },
        )
    ]
    assert FakeClient.instances[0].kwargs["trust_env"] is False


def test_returned_backoff_is_respected_before_next_method_call(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [
        FakeResponse({"items": [], "quota_remaining": 9000, "backoff": 3}),
        FakeResponse({"items": [], "quota_remaining": 8999}),
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monotonic = iter([10.0, 11.0])
    monkeypatch.setattr("app.stackexchange.time.monotonic", lambda: next(monotonic))
    sleeps: list[float] = []
    monkeypatch.setattr("app.stackexchange.time.sleep", sleeps.append)

    client = StackExchangeClient()
    first = client.search_url(site="stackoverflow", domain="example.com")
    second = client.search_url(site="stackoverflow", domain="another.com")

    assert first.backoff_seconds == 3
    assert second.quota_remaining == 8999
    assert sleeps == [2.0]


def test_stackexchange_error_wrapper_is_rejected(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [
        FakeResponse(
            {
                "error_id": 502,
                "error_name": "throttle_violation",
                "error_message": "too many requests",
            }
        )
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(StackExchangeError, match="too many requests"):
        StackExchangeClient().search_url(site="stackoverflow", domain="example.com")
