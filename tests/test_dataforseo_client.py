from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.dataforseo import DataForSEOClient, DataForSEOError


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.dataforseo.com/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("provider error", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHTTPClient:
    calls: list[tuple[str, list[dict[str, Any]]]] = []
    response_payload: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self) -> FakeHTTPClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, json: list[dict[str, Any]]) -> FakeHTTPResponse:
        self.__class__.calls.append((url, json))
        return FakeHTTPResponse(self.__class__.response_payload)


def _settings() -> Settings:
    return Settings(
        dataforseo_login="api-login",
        dataforseo_password="api-password",
    )


def _ok_payload(result: dict[str, Any], cost: float = 0.024) -> dict[str, Any]:
    return {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [
            {
                "id": "task-123",
                "status_code": 20000,
                "status_message": "Ok.",
                "cost": cost,
                "result": [result],
            }
        ],
    }


def test_bulk_backlink_summary_contract(monkeypatch) -> None:
    FakeHTTPClient.calls = []
    FakeHTTPClient.response_payload = _ok_payload(
        {
            "items_count": 1,
            "items": [
                {
                    "url": "example.com",
                    "referring_pages": 8,
                    "referring_main_domains": 4,
                }
            ],
        }
    )
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    response = DataForSEOClient(_settings()).bulk_backlink_summaries(["example.com"])

    assert response.task_id == "task-123"
    assert response.task_cost_usd == 0.024
    assert FakeHTTPClient.calls == [
        (
            "https://api.dataforseo.com/v3/backlinks/bulk_pages_summary/live",
            [
                {
                    "targets": ["example.com"],
                    "include_subdomains": True,
                    "rank_scale": "one_hundred",
                }
            ],
        )
    ]


def test_detailed_backlinks_contract(monkeypatch) -> None:
    FakeHTTPClient.calls = []
    FakeHTTPClient.response_payload = _ok_payload({"items_count": 0, "items": []})
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    DataForSEOClient(_settings()).backlinks("example.com", limit=25)

    url, payload = FakeHTTPClient.calls[0]
    assert url == "https://api.dataforseo.com/v3/backlinks/backlinks/live"
    assert payload == [
        {
            "target": "example.com",
            "mode": "as_is",
            "backlinks_status_type": "live",
            "include_subdomains": True,
            "exclude_internal_backlinks": True,
            "rank_scale": "one_hundred",
            "limit": 25,
            "order_by": ["page_from_rank,desc", "domain_from_rank,desc"],
        }
    ]


def test_bulk_page_traffic_contract(monkeypatch) -> None:
    FakeHTTPClient.calls = []
    FakeHTTPClient.response_payload = _ok_payload({"items_count": 0, "items": []}, cost=0.01)
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    DataForSEOClient(_settings()).bulk_traffic_estimation(["https://publisher.example/article"])

    assert FakeHTTPClient.calls == [
        (
            "https://api.dataforseo.com/v3/dataforseo_labs/google/bulk_traffic_estimation/live",
            [{"targets": ["https://publisher.example/article"]}],
        )
    ]


def test_provider_task_error_is_not_silently_accepted(monkeypatch) -> None:
    FakeHTTPClient.calls = []
    FakeHTTPClient.response_payload = {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [
            {
                "id": "bad-task",
                "status_code": 40501,
                "status_message": "Invalid Field",
                "cost": 0,
                "result": [],
            }
        ],
    }
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    with pytest.raises(DataForSEOError, match="Invalid Field"):
        DataForSEOClient(_settings()).backlinks("example.com")


def test_bulk_limits_are_enforced_before_provider_call(monkeypatch) -> None:
    FakeHTTPClient.calls = []
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    client = DataForSEOClient(_settings())

    with pytest.raises(ValueError):
        client.bulk_backlink_summaries([])
    with pytest.raises(ValueError):
        client.bulk_backlink_summaries([f"domain-{i}.example" for i in range(101)])
    with pytest.raises(ValueError):
        client.bulk_traffic_estimation([])
    with pytest.raises(ValueError):
        client.bulk_traffic_estimation([f"domain-{i}.example" for i in range(1001)])

    assert FakeHTTPClient.calls == []
