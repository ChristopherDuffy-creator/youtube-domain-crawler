from dataclasses import dataclass
from typing import Any

from app.availability import AvailabilityResult, check_domain, check_porkbun, classify_rdap_dns
from app.config import Settings


def test_rdap_dns_classification_is_conservative() -> None:
    assert classify_rdap_dns("registered", "resolves") == "registered"
    assert classify_rdap_dns("registered", "nxdomain") == "registered"
    assert classify_rdap_dns("not_found", "nxdomain") == "likely_available"
    assert classify_rdap_dns("not_found", "resolves") == "conflicting"
    assert classify_rdap_dns("error", "nxdomain") == "unknown"


@dataclass
class FakeResponse:
    payload: dict[str, Any]
    status_code: int = 200
    text: str = ""

    def json(self) -> dict[str, Any]:
        return self.payload


def registrar_settings() -> Settings:
    return Settings(
        porkbun_api_key="public-key",
        porkbun_secret_api_key="secret-key",
        max_ordinary_registration_usd=50,
        porkbun_min_interval_seconds=0,
    )


def test_porkbun_nested_response_confirms_ordinary_registration(monkeypatch) -> None:
    def fake_post(*args, **kwargs) -> FakeResponse:
        return FakeResponse(
            {
                "status": "SUCCESS",
                "response": {
                    "avail": "yes",
                    "type": "registration",
                    "price": "12.34",
                    "premium": "no",
                },
            }
        )

    monkeypatch.setattr("app.availability.httpx.post", fake_post)
    result = check_porkbun("available-example.com", registrar_settings())
    assert result.status == "available"
    assert result.price_usd == 12.34
    assert result.premium is False


def test_porkbun_rejects_premium_or_expensive_registration(monkeypatch) -> None:
    def fake_post(*args, **kwargs) -> FakeResponse:
        return FakeResponse(
            {
                "status": "SUCCESS",
                "response": {
                    "avail": "yes",
                    "type": "registration",
                    "price": "500.00",
                    "premium": "yes",
                },
            }
        )

    monkeypatch.setattr("app.availability.httpx.post", fake_post)
    result = check_porkbun("premium-example.com", registrar_settings())
    assert result.status == "premium"
    assert result.premium is True


def test_porkbun_marks_unavailable_as_registered(monkeypatch) -> None:
    def fake_post(*args, **kwargs) -> FakeResponse:
        return FakeResponse(
            {
                "status": "SUCCESS",
                "response": {
                    "avail": "no",
                    "type": "registration",
                    "price": "12.34",
                    "premium": "no",
                },
            }
        )

    monkeypatch.setattr("app.availability.httpx.post", fake_post)
    result = check_porkbun("registered-example.com", registrar_settings())
    assert result.status == "registered"


def test_exact_registrar_is_only_used_after_likely_available_and_traffic_gate(monkeypatch) -> None:
    monkeypatch.setattr("app.availability.check_rdap", lambda domain: ("not_found", None))
    monkeypatch.setattr("app.availability.check_dns", lambda domain: "nxdomain")
    calls: list[str] = []

    def fake_registrar(domain: str, settings: Settings) -> AvailabilityResult:
        calls.append(domain)
        return AvailabilityResult(
            status="available",
            source="porkbun",
            rdap_status="unchecked",
            dns_status="unchecked",
            price_usd=10.0,
        )

    monkeypatch.setattr("app.availability.check_porkbun", fake_registrar)
    config = registrar_settings()

    preliminary = check_domain("candidate-example.com", config, exact_registrar_check=False)
    assert preliminary.status == "likely_available"
    assert calls == []

    exact = check_domain("candidate-example.com", config, exact_registrar_check=True)
    assert exact.status == "available"
    assert calls == ["candidate-example.com"]
