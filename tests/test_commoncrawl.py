from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.commoncrawl import CommonCrawlClient, CommonCrawlError


class FakeResponse:
    def __init__(self, *, payload: Any = None, text: str = "", status_code: int = 200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://index.commoncrawl.org/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("Common Crawl error", request=request, response=response)


class FakeClient:
    responses: list[FakeResponse] = []
    calls: list[tuple[str, dict[str, str] | None]] = []
    instances: list[FakeClient] = []

    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def get(self, url: str, params: dict[str, str] | None = None) -> FakeResponse:
        self.__class__.calls.append((url, params))
        return self.__class__.responses.pop(0)


def _reset() -> None:
    FakeClient.responses = []
    FakeClient.calls = []
    FakeClient.instances = []


def test_latest_indexes_uses_collection_catalog_without_proxy_environment(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [
        FakeResponse(
            payload=[
                {"id": "CC-MAIN-2026-30"},
                {"id": "CC-MAIN-2026-25"},
                {"id": "not-a-crawl"},
            ]
        )
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    indexes = CommonCrawlClient().latest_indexes(2)

    assert indexes == ["CC-MAIN-2026-30", "CC-MAIN-2026-25"]
    assert FakeClient.calls == [("https://index.commoncrawl.org/collinfo.json", None)]
    assert FakeClient.instances[0].kwargs["trust_env"] is False


def test_domain_presence_checks_limited_domain_pattern_and_treats_404_as_no_capture(
    monkeypatch,
) -> None:
    _reset()
    FakeClient.responses = [
        FakeResponse(text='{"url":"https://example.com/"}\n'),
        FakeResponse(status_code=404),
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr("app.commoncrawl.time.sleep", lambda _: None)

    presence = CommonCrawlClient(min_interval_seconds=0.1).domain_presence(
        "Example.com",
        ["CC-MAIN-2026-30", "CC-MAIN-2026-25"],
    )

    assert presence.domain == "example.com"
    assert presence.indexes_checked == ("CC-MAIN-2026-30", "CC-MAIN-2026-25")
    assert presence.indexes_with_capture == ("CC-MAIN-2026-30",)
    assert presence.capture_index_count == 1
    assert FakeClient.calls == [
        (
            "https://index.commoncrawl.org/CC-MAIN-2026-30-index",
            {"url": "example.com/*", "output": "json", "limit": "1"},
        ),
        (
            "https://index.commoncrawl.org/CC-MAIN-2026-25-index",
            {"url": "example.com/*", "output": "json", "limit": "1"},
        ),
    ]


def test_malformed_collection_catalog_is_rejected(monkeypatch) -> None:
    _reset()
    FakeClient.responses = [FakeResponse(payload={"bad": "shape"})]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(CommonCrawlError, match="malformed"):
        CommonCrawlClient().latest_indexes(2)
