from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.dataforseo import (
    DataForSEOClient,
    DataForSEOError,
    estimate_provider_proof_max_cost_usd,
)


class FakeHTTPResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.dataforseo.com/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("provider error", request=request, response=response)

    def json(self) -> Any:
        return self._payload


class FakeHTTPClient:
    calls: list[tuple[str, list[dict[str, Any]]]] = []
    instances: list[FakeHTTPClient] = []
    response_payload: Any = {}

    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def __enter__(self) -> FakeHTTPClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, json: list[dict[str, Any]]) -> FakeHTTPResponse:
        self.__class__.calls.append((url, json))
        return FakeHTTPResponse(self.__class__.response_payload)


def _reset_fake() -> None:
    FakeHTTPClient.calls = []
    FakeHTTPClient.instances = []


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


def test_default_proof_has_a_conservative_preflight_cost_envelope() -> None:
    assert estimate_provider_proof_max_cost_usd(100, 5, 25) == pytest.approx(0.1791)
    assert estimate_provider_proof_max_cost_usd(5, 5, 25) == pytest.approx(0.17568)
    assert estimate_provider_proof_max_cost_usd(0, 0, 25) == 0.0


def test_proof_spend_cap_is_enforced_before_first_http_call(monkeypatch) -> None:
    _reset_fake()
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    settings = Settings(
        dataforseo_login="api-login",
        dataforseo_password="api-password",
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=0.10,
    )
    client = DataForSEOClient(settings)

    with pytest.raises(DataForSEOError, match="exceeds configured cap"):
        client.bulk_backlink_summaries([f"proof-{i}.example" for i in range(5)])

    assert FakeHTTPClient.instances == []
    assert FakeHTTPClient.calls == []


def test_raised_configuration_cannot_bypass_the_approved_run_cap(monkeypatch) -> None:
    _reset_fake()
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    settings = Settings(
        dataforseo_login="api-login",
        dataforseo_password="api-password",
        link_hunter_proof_batch_size=6,
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=1.0,
    )
    client = DataForSEOClient(settings)

    with pytest.raises(DataForSEOError, match=r"cap \$0.1800"):
        client.bulk_backlink_summaries([f"proof-{i}.example" for i in range(100)])

    assert FakeHTTPClient.instances == []
    assert FakeHTTPClient.calls == []


def test_proof_rejects_source_page_volume_that_exceeds_one_traffic_batch(monkeypatch) -> None:
    _reset_fake()
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    settings = Settings(
        dataforseo_login="api-login",
        dataforseo_password="api-password",
        link_hunter_proof_batch_size=11,
        link_hunter_backlinks_per_domain=100,
        link_hunter_proof_max_cost_usd=5.0,
    )
    client = DataForSEOClient(settings)

    with pytest.raises(DataForSEOError, match="more than 1,000 source pages"):
        client.bulk_backlink_summaries([f"proof-{i}.example" for i in range(11)])

    assert FakeHTTPClient.instances == []
    assert FakeHTTPClient.calls == []


def test_one_bulk_call_can_screen_100_domains_inside_the_approved_cap(monkeypatch) -> None:
    _reset_fake()
    FakeHTTPClient.response_payload = _ok_payload({"items_count": 0, "items": []}, cost=0.0276)
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    targets = [f"screen-{i}.example" for i in range(100)]

    response = DataForSEOClient(_settings()).bulk_backlink_summaries(targets)

    assert response.task_cost_usd == pytest.approx(0.0276)
    assert len(FakeHTTPClient.calls) == 1
    assert FakeHTTPClient.calls[0][1][0]["targets"] == targets


def test_bulk_backlink_summary_contract(monkeypatch) -> None:
    _reset_fake()
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


def test_transport_does_not_inherit_proxy_environment(monkeypatch) -> None:
    _reset_fake()
    FakeHTTPClient.response_payload = _ok_payload({"items_count": 0, "items": []})
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    DataForSEOClient(_settings()).backlink_summary("example.com")

    assert len(FakeHTTPClient.instances) == 1
    kwargs = FakeHTTPClient.instances[0].kwargs
    assert kwargs["trust_env"] is False
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["headers"]["User-Agent"] == "Expandosaurus-Link-Hunter/0.3"


def test_detailed_backlinks_contract(monkeypatch) -> None:
    _reset_fake()
    FakeHTTPClient.response_payload = _ok_payload({"items_count": 0, "items": []})
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    DataForSEOClient(_settings()).backlinks("example.com", limit=25)

    url, payload = FakeHTTPClient.calls[0]
    assert url == "https://api.dataforseo.com/v3/backlinks/backlinks/live"
    assert payload == [
        {
            "target": "example.com",
            "mode": "one_per_domain",
            "backlinks_status_type": "live",
            "include_subdomains": True,
            "exclude_internal_backlinks": True,
            "rank_scale": "one_hundred",
            "filters": ["page_from_status_code", "=", 200],
            "limit": 25,
            "order_by": ["page_from_rank,desc", "domain_from_rank,desc"],
        }
    ]


def test_bulk_page_traffic_contract(monkeypatch) -> None:
    _reset_fake()
    FakeHTTPClient.response_payload = _ok_payload({"items_count": 0, "items": []}, cost=0.01)
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)

    DataForSEOClient(_settings()).bulk_traffic_estimation(["https://publisher.example/article"])

    assert FakeHTTPClient.calls == [
        (
            "https://api.dataforseo.com/v3/dataforseo_labs/google/bulk_traffic_estimation/live",
            [
                {
                    "targets": ["https://publisher.example/article"],
                    "item_types": ["organic"],
                }
            ],
        )
    ]


def test_provider_task_error_is_not_silently_accepted(monkeypatch) -> None:
    _reset_fake()
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


def test_malformed_provider_shapes_are_rejected(monkeypatch) -> None:
    _reset_fake()
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)
    client = DataForSEOClient(_settings())

    FakeHTTPClient.response_payload = []
    with pytest.raises(DataForSEOError, match="malformed response"):
        client.backlink_summary("example.com")

    FakeHTTPClient.response_payload = {"status_code": 20000, "tasks": {"bad": "shape"}}
    with pytest.raises(DataForSEOError, match="no valid task"):
        client.backlink_summary("example.com")

    FakeHTTPClient.response_payload = {
        "status_code": 20000,
        "tasks": [{"status_code": 20000, "result": {"bad": "shape"}}],
    }
    with pytest.raises(DataForSEOError, match="malformed results"):
        client.backlink_summary("example.com")


def test_bulk_limits_are_enforced_before_provider_call(monkeypatch) -> None:
    _reset_fake()
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
