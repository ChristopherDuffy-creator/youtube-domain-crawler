from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from app.availability import PORKBUN_ROOT, AvailabilityResult, check_porkbun
from app.config import Settings
from app.domain_tools import registrable_domain


PILOT_DOMAINS = frozenset({
    "satvic.yoga",
    "teamgerardiperformance.com",
    "craftsheaven.club",
})


class AcquisitionError(RuntimeError):
    """Raised when a domain purchase fails a safety or registrar check."""


@dataclass(frozen=True)
class RegistrationQuote:
    domain: str
    price_usd: float
    cost_cents: int
    premium: bool


@dataclass(frozen=True)
class RegistrationResult:
    domain: str
    cost_cents: int
    dry_run: bool
    order_id: int | None = None
    balance_cents: int | None = None
    request_id: str | None = None


def _validate_domain(domain: str) -> str:
    parsed = registrable_domain(domain)
    normalized = domain.lower().strip().strip(".")
    if parsed is None or parsed[0] != normalized:
        raise AcquisitionError(f"Invalid registrable domain: {domain}")
    return normalized


def _price_to_cents(price_usd: float) -> int:
    return int(round(price_usd * 100))


def quote_registration(domain: str, settings: Settings) -> RegistrationQuote:
    """Get an exact registrar quote and reject non-standard purchases."""
    domain = _validate_domain(domain)
    if not settings.registrar_enabled:
        raise AcquisitionError("Porkbun API credentials are not configured")

    result: AvailabilityResult = check_porkbun(domain, settings)
    if result.status != "available":
        raise AcquisitionError(f"{domain} is not a normal available registration ({result.status})")
    if result.price_usd is None:
        raise AcquisitionError(f"Porkbun returned no registration price for {domain}")
    if result.premium:
        raise AcquisitionError(f"Refusing premium/aftermarket registration for {domain}")
    if result.price_usd > settings.max_ordinary_registration_usd:
        raise AcquisitionError(
            f"{domain} quote ${result.price_usd:.2f} exceeds configured "
            f"${settings.max_ordinary_registration_usd:.2f} registration cap"
        )

    return RegistrationQuote(
        domain=domain,
        price_usd=result.price_usd,
        cost_cents=_price_to_cents(result.price_usd),
        premium=False,
    )


def _idempotency_key(domain: str, cost_cents: int, dry_run: bool) -> str:
    mode = "dry" if dry_run else "live"
    digest = hashlib.sha256(f"expandosaurus:{domain}:{cost_cents}:{mode}".encode()).hexdigest()
    return f"expandosaurus-{digest[:40]}"


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        raise AcquisitionError(f"Porkbun returned invalid JSON (HTTP {response.status_code})") from exc


def register_domain(
    domain: str,
    settings: Settings,
    *,
    dry_run: bool = True,
    allow_live_purchase: bool = False,
    max_cost_cents: int | None = None,
) -> RegistrationResult:
    """Register one domain with price-lock and double-charge protection.

    A live registration is impossible unless the caller explicitly sets
    ``allow_live_purchase=True``. The default path is always a Porkbun dry-run.
    The function re-quotes immediately before create so stale dashboard prices
    cannot silently turn into a more expensive purchase.
    """
    domain = _validate_domain(domain)
    if domain not in PILOT_DOMAINS:
        raise AcquisitionError(f"{domain} is not in the approved 3-site pilot allowlist")
    if not dry_run and not allow_live_purchase:
        raise AcquisitionError("Live purchase blocked: explicit live-purchase approval is required")

    quote = quote_registration(domain, settings)
    if max_cost_cents is not None and quote.cost_cents > max_cost_cents:
        raise AcquisitionError(
            f"Price changed for {domain}: quote {quote.cost_cents}c exceeds approved {max_cost_cents}c"
        )

    body: dict[str, Any] = {
        "apikey": settings.porkbun_api_key,
        "secretapikey": settings.porkbun_secret_api_key,
        "cost": quote.cost_cents,
        "agreeToTerms": "yes",
    }
    if dry_run:
        body["dryRun"] = True

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Expandosaurus/1.0",
        "Idempotency-Key": _idempotency_key(domain, quote.cost_cents, dry_run),
    }
    try:
        response = httpx.post(
            f"{PORKBUN_ROOT}/domain/create/{domain}",
            headers=headers,
            json=body,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise AcquisitionError(f"Porkbun registration request failed for {domain}: {exc}") from exc

    payload = _payload(response)
    if response.status_code >= 400 or str(payload.get("status", "")).upper() != "SUCCESS":
        message = payload.get("message") or payload.get("code") or f"HTTP {response.status_code}"
        raise AcquisitionError(f"Porkbun rejected {domain}: {message}")

    order_id = payload.get("orderId")
    balance = payload.get("balance")
    return RegistrationResult(
        domain=str(payload.get("domain") or domain),
        cost_cents=int(payload.get("cost") or quote.cost_cents),
        dry_run=dry_run,
        order_id=int(order_id) if order_id is not None else None,
        balance_cents=int(balance) if balance is not None else None,
        request_id=str(payload.get("requestId")) if payload.get("requestId") else None,
    )
