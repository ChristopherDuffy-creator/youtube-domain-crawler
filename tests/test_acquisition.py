from __future__ import annotations

import pytest

from app import acquisition
from app.acquisition import AcquisitionError, register_domain
from app.availability import AvailabilityResult
from app.config import Settings


def _settings() -> Settings:
    return Settings(
        porkbun_api_key="pk_test",
        porkbun_secret_api_key="sk_test",
        max_ordinary_registration_usd=50,
    )


def _available(price: float = 11.08) -> AvailabilityResult:
    return AvailabilityResult(
        status="available",
        source="porkbun",
        rdap_status="unchecked",
        dns_status="unchecked",
        price_usd=price,
        premium=False,
    )


def test_live_purchase_requires_explicit_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquisition, "check_porkbun", lambda domain, settings: _available())
    with pytest.raises(AcquisitionError, match="explicit live-purchase approval"):
        register_domain("satvic.yoga", _settings(), dry_run=False)


def test_non_pilot_domain_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquisition, "check_porkbun", lambda domain, settings: _available())
    with pytest.raises(AcquisitionError, match="pilot allowlist"):
        register_domain("example.com", _settings())


def test_quote_cap_blocks_expensive_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquisition, "check_porkbun", lambda domain, settings: _available(75.0))
    with pytest.raises(AcquisitionError, match="registration cap"):
        acquisition.quote_registration("petworthy.co", _settings())


def test_dry_run_sends_price_lock_and_idempotency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquisition, "check_porkbun", lambda domain, settings: _available(11.08))
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "SUCCESS", "domain": "satvic.yoga", "cost": 1108, "requestId": "req-1"}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(acquisition.httpx, "post", fake_post)
    result = register_domain("satvic.yoga", _settings(), dry_run=True, max_cost_cents=1108)

    assert result.dry_run is True
    assert result.cost_cents == 1108
    assert captured["url"].endswith("/domain/create/satvic.yoga")
    assert captured["json"]["cost"] == 1108
    assert captured["json"]["dryRun"] is True
    assert captured["json"]["agreeToTerms"] == "yes"
    assert captured["headers"]["Idempotency-Key"].startswith("expandosaurus-")


def test_price_change_above_approved_quote_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acquisition, "check_porkbun", lambda domain, settings: _available(12.00))
    with pytest.raises(AcquisitionError, match="Price changed"):
        register_domain("craftsheaven.club", _settings(), dry_run=True, max_cost_cents=1108)
