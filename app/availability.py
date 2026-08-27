from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import dns.exception
import dns.resolver
import httpx

from app.config import Settings
from app.domain_tools import registrable_domain

RDAP_ROOT = "https://rdap.org/domain"
PORKBUN_ROOT = "https://api.porkbun.com/api/json/v3"
RDAP_MIN_INTERVAL_SECONDS = 0.25
RDAP_MAX_ATTEMPTS = 3
RDAP_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
RDAP_MAX_COOLDOWN_SECONDS = 15 * 60.0
_rdap_lock = threading.Lock()
_last_rdap_call = 0.0
_rdap_circuits: dict[str, tuple[int, float]] = {}
_porkbun_lock = threading.Lock()
_last_porkbun_call = 0.0


@dataclass(frozen=True)
class AvailabilityResult:
    status: str
    source: str
    rdap_status: str
    dns_status: str
    http_status: str = "unchecked"
    price_usd: float | None = None
    premium: bool = False
    error: str | None = None


class RdapCircuitOpen(RuntimeError):
    """Raised before an HTTP request when a registry has just rate-limited us."""


def _rdap_registry_key(domain: str) -> str:
    parsed = registrable_domain(domain)
    return parsed[1] if parsed is not None else "invalid"


def _rdap_circuit_error(domain: str) -> str | None:
    registry = _rdap_registry_key(domain)
    with _rdap_lock:
        state = _rdap_circuits.get(registry)
        if state is None:
            return None
        _, retry_at = state
        remaining = retry_at - time.monotonic()
        if remaining <= 0:
            _rdap_circuits.pop(registry, None)
            return None
    return f"RDAP circuit open for .{registry}; retry after {max(1, round(remaining))}s"


def _record_rdap_rate_limit(domain: str) -> None:
    """Back off only the rate-limited TLD while other registries keep moving."""
    registry = _rdap_registry_key(domain)
    with _rdap_lock:
        failures, _ = _rdap_circuits.get(registry, (0, 0.0))
        failures += 1
        cooldown = min(
            RDAP_MAX_COOLDOWN_SECONDS,
            RDAP_RATE_LIMIT_COOLDOWN_SECONDS * (2 ** min(failures - 1, 4)),
        )
        _rdap_circuits[registry] = (failures, time.monotonic() + cooldown)


def _record_rdap_success(domain: str) -> None:
    with _rdap_lock:
        _rdap_circuits.pop(_rdap_registry_key(domain), None)


def _reset_rdap_circuits_for_tests() -> None:
    """Keep stateful rate-limit tests isolated without changing production flow."""
    global _last_rdap_call
    with _rdap_lock:
        _rdap_circuits.clear()
        _last_rdap_call = 0.0


def classify_rdap_dns(rdap_status: str, dns_status: str) -> str:
    # A domain that resolves is necessarily registered, even when the shared
    # RDAP endpoint is rate-limited.  Treat DNS as decisive for rejection so
    # obvious live businesses never remain in the candidate queue as unknown.
    if dns_status == "resolves":
        return "registered"
    if rdap_status == "registered":
        return "registered"
    if rdap_status == "not_found" and dns_status == "nxdomain":
        return "likely_available"
    return "unknown"


def check_dns(domain: str) -> str:
    resolver = dns.resolver.Resolver(configure=True)
    resolver.lifetime = 5.0
    resolver.timeout = 3.0
    found = False
    for record_type in ("NS", "A", "AAAA", "SOA"):
        try:
            answer = resolver.resolve(domain, record_type, raise_on_no_answer=False)
            if answer.rrset is not None:
                found = True
                break
        except dns.resolver.NXDOMAIN:
            return "nxdomain"
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
            continue
        except OSError:
            continue
    return "resolves" if found else "unknown"


def _paced_rdap_get(domain: str) -> httpx.Response:
    global _last_rdap_call
    circuit_error = _rdap_circuit_error(domain)
    if circuit_error:
        raise RdapCircuitOpen(circuit_error)
    with _rdap_lock:
        wait_for = RDAP_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_rdap_call)
        if wait_for > 0:
            time.sleep(wait_for)
        _last_rdap_call = time.monotonic()
    return httpx.get(
            f"{RDAP_ROOT}/{domain}",
            headers={"Accept": "application/rdap+json", "User-Agent": "YouTubeDomainCrawler/0.1"},
            follow_redirects=True,
            timeout=15.0,
        )


def check_rdap(domain: str) -> tuple[str, str | None]:
    last_status: int | None = None
    last_error = "RDAP request failed"
    for attempt in range(RDAP_MAX_ATTEMPTS):
        circuit_error = _rdap_circuit_error(domain)
        if circuit_error:
            return "rate_limited", circuit_error
        try:
            response = _paced_rdap_get(domain)
        except RdapCircuitOpen as exc:
            return "rate_limited", str(exc)
        except httpx.HTTPError as exc:
            last_status = None
            last_error = str(exc)
        else:
            last_status = response.status_code
            if response.status_code == 200:
                _record_rdap_success(domain)
                return "registered", None
            if response.status_code == 404:
                _record_rdap_success(domain)
                return "not_found", None
            if response.status_code == 429:
                _record_rdap_rate_limit(domain)
                return "rate_limited", "RDAP rate limited"
            if response.status_code not in {429, 500, 502, 503, 504}:
                return "error", f"RDAP returned HTTP {response.status_code}"
            last_error = (
                "RDAP rate limited"
                if response.status_code == 429
                else f"RDAP returned HTTP {response.status_code}"
            )
        if attempt + 1 < RDAP_MAX_ATTEMPTS:
            time.sleep(2**attempt)
    if last_status == 429:
        return "rate_limited", last_error
    return "error", last_error


def _porkbun_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError:
        return {"status": "ERROR", "message": response.text[:300]}


def check_porkbun(domain: str, settings: Settings) -> AvailabilityResult:
    global _last_porkbun_call
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "YouTubeDomainCrawler/0.1",
    }
    try:
        # The default account limit is one check per 10 seconds. This lock also
        # protects calls made by concurrent availability workers.
        with _porkbun_lock:
            wait_for = settings.porkbun_min_interval_seconds - (
                time.monotonic() - _last_porkbun_call
            )
            if wait_for > 0:
                time.sleep(wait_for)
            response = httpx.post(
                f"{PORKBUN_ROOT}/domain/checkDomain/{domain}",
                headers=headers,
                json={
                    "apikey": settings.porkbun_api_key,
                    "secretapikey": settings.porkbun_secret_api_key,
                },
                timeout=20.0,
            )
            _last_porkbun_call = time.monotonic()
    except httpx.HTTPError as exc:
        return AvailabilityResult(
            status="unknown",
            source="porkbun",
            rdap_status="unchecked",
            dns_status="unchecked",
            error=str(exc),
        )
    payload = _porkbun_payload(response)
    if response.status_code == 429:
        return AvailabilityResult(
            status="unknown",
            source="porkbun",
            rdap_status="unchecked",
            dns_status="unchecked",
            error="Porkbun rate limited",
        )
    if response.status_code >= 400 or str(payload.get("status", "")).upper() == "ERROR":
        message = str(payload.get("message") or f"HTTP {response.status_code}")
        if "invalid tld" in message.lower():
            return AvailabilityResult(
                status="unsupported",
                source="porkbun",
                rdap_status="unchecked",
                dns_status="unchecked",
                error=message,
            )
        return AvailabilityResult(
            status="unknown",
            source="porkbun",
            rdap_status="unchecked",
            dns_status="unchecked",
            error=message,
        )

    result = payload.get("response", payload)
    available = str(result.get("avail", "no")).lower() in {"yes", "true", "1"}
    try:
        price = float(result.get("price")) if result.get("price") is not None else None
    except (TypeError, ValueError):
        price = None
    premium_flag = str(result.get("premium", "no")).lower() in {"yes", "true", "1"}
    purchase_type = str(result.get("type", "registration")).lower()
    expensive = price is not None and price > settings.max_ordinary_registration_usd
    premium = premium_flag or expensive

    if not available:
        status = "registered"
    elif purchase_type != "registration":
        status = "aftermarket"
    elif premium:
        status = "premium"
    else:
        status = "available"

    return AvailabilityResult(
        status=status,
        source="porkbun",
        rdap_status="unchecked",
        dns_status="unchecked",
        price_usd=price,
        premium=premium,
    )


def check_domain(
    domain: str, settings: Settings, exact_registrar_check: bool = False
) -> AvailabilityResult:
    parsed_domain = registrable_domain(domain)
    if parsed_domain is None or parsed_domain[0] != domain.lower().strip().strip("."):
        return AvailabilityResult(
            status="unknown",
            source="validation",
            rdap_status="skipped",
            dns_status="skipped",
            error="Invalid registrable domain",
        )
    domain = parsed_domain[0]
    dns_status = check_dns(domain)
    if dns_status == "resolves":
        return AvailabilityResult(
            status="registered",
            source="dns",
            rdap_status="skipped",
            dns_status=dns_status,
        )

    # Candidates entering the review pipeline need a registrar-authoritative
    # answer. Do not let a shared RDAP rate limit prevent that exact check: the
    # Porkbun result is both the availability decision and the live ordinary
    # registration price. RDAP remains the free first pass for the wider
    # discovery ledger.
    if settings.registrar_enabled and exact_registrar_check:
        registrar_result = check_porkbun(domain, settings)
        if registrar_result.status != "unknown":
            return registrar_result

        # A failed registrar request cannot prove availability, but RDAP can
        # still prove that the name is registered and safely remove it.
        rdap_status, rdap_error = check_rdap(domain)
        status = classify_rdap_dns(rdap_status, dns_status)
        if status == "registered":
            return AvailabilityResult(
                status="registered",
                source="rdap_dns",
                rdap_status=rdap_status,
                dns_status=dns_status,
                error=rdap_error,
            )
        return AvailabilityResult(
            status="unknown",
            source="porkbun",
            rdap_status=rdap_status,
            dns_status=dns_status,
            error=registrar_result.error or rdap_error or "Exact registrar check failed",
        )

    # RDAP is the scarce/rate-limited check.  Only spend it on names for which
    # DNS did not already prove an active registration.
    rdap_status, rdap_error = check_rdap(domain)
    status = classify_rdap_dns(rdap_status, dns_status)
    return AvailabilityResult(
        status=status,
        source="rdap_dns",
        rdap_status=rdap_status,
        dns_status=dns_status,
        error=rdap_error,
    )
