from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.availability import AvailabilityResult, check_dns, check_domain
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.dataforseo import DataForSEOClient, DataForSEOError
from app.domain_lifecycle import get_or_create_unsuppressed_domain
from app.link_hunter_preview import (
    rerank_summary_screen_targets,
    select_cached_deep_proof_targets_with_ranking,
    select_provider_summary_targets_with_ranking,
)
from app.models import (
    BacklinkSummary,
    Domain,
    FetchVerification,
    LinkObservation,
    Opportunity,
    ProviderQuery,
    RunLog,
    SourceLink,
    SourceMetricSnapshot,
    SourcePage,
    SourceSite,
    WebScreening,
    utcnow,
)
from app.provider_budget import (
    acquire_provider_proof_lease,
    effective_provider_run_limit_usd,
    finalize_provider_daily_budget,
    provider_daily_budget_snapshot,
    release_provider_proof_lease,
    reserve_provider_daily_budget,
)
from app.storage_guard import database_storage_status, storage_guard_allows_writes
from app.web_hunter_upgrade import apply_source_focus_bonus, enforce_money_tier
from app.web_intelligence import project_opportunity_economics, save_opportunity_economics

logger = logging.getLogger(__name__)

MAX_VERIFY_BYTES = 2_000_000
MAX_VERIFY_REDIRECTS = 5


@dataclass(frozen=True)
class _AnchorEvidence:
    href: str
    text: str
    semantic_location: str
    nofollow: bool
    hidden: bool


@dataclass(frozen=True)
class _VerificationRequest:
    """All data needed to fetch one public source page outside the ORM session."""

    source_link_id: int
    source_url: str
    target_url: str
    target_domain: str
    first_seen_at: datetime


@dataclass(frozen=True)
class _VerificationFetchResult:
    """Network-only result that is safe to hand back to the single DB writer."""

    observed_at: datetime
    http_status: int | None
    final_url: str
    content_hash: str | None
    link_present: bool
    clickability_score: float
    clickable: bool
    semantic_location: str
    anchor_text: str
    nofollow: bool
    error: str | None


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.anchors: list[_AnchorEvidence] = []
        self._location_stack: list[str] = []
        self._anchor: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"article", "aside", "footer", "header", "main", "nav"}:
            self._location_stack.append(lowered)
        if lowered != "a":
            return
        attributes = {key.lower(): value or "" for key, value in attrs}
        value = attributes.get("href", "")
        if not value:
            return
        self.hrefs.append(value)
        rel = attributes.get("rel", "").lower().split()
        style = attributes.get("style", "").lower().replace(" ", "")
        hidden = (
            "display:none" in style
            or "visibility:hidden" in style
            or attributes.get("aria-hidden", "").lower() == "true"
            or "hidden" in attributes
        )
        self._anchor = {
            "href": value,
            "text": [],
            "semantic_location": self._location_stack[-1] if self._location_stack else "body",
            "nofollow": "nofollow" in rel,
            "hidden": hidden,
        }

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "a" and self._anchor is not None:
            self.anchors.append(
                _AnchorEvidence(
                    href=str(self._anchor["href"]),
                    text=" ".join(self._anchor["text"]).strip(),
                    semantic_location=str(self._anchor["semantic_location"]),
                    nofollow=bool(self._anchor["nofollow"]),
                    hidden=bool(self._anchor["hidden"]),
                )
            )
            self._anchor = None
        if (
            lowered in {"article", "aside", "footer", "header", "main", "nav"}
            and self._location_stack
            and self._location_stack[-1] == lowered
        ):
            self._location_stack.pop()


def _anchor_clickability(anchor: _AnchorEvidence) -> float:
    if anchor.hidden:
        return 0.0
    score = 45.0
    if anchor.text and not anchor.text.lower().startswith(("http://", "https://", "www.")):
        score += 12.0
    lowered = anchor.text.lower()
    if any(
        term in lowered
        for term in ("buy", "book", "download", "get", "join", "order", "shop", "sign up", "visit")
    ):
        score += 20.0
    score += {
        "article": 18.0,
        "main": 15.0,
        "body": 8.0,
        "aside": 0.0,
        "nav": -8.0,
        "header": -10.0,
        "footer": -18.0,
    }.get(anchor.semantic_location, 0.0)
    if anchor.nofollow:
        score -= 3.0
    return round(max(0.0, min(100.0, score)), 1)


def _normalize_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _canonical_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _href_points_to_domain(href: str, target_domain: str) -> bool:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    host = _normalize_host(href)
    target = _normalize_host(target_domain)
    return bool(host and target and (host == target or host.endswith(f".{target}")))


def _normalized_link_identity(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw:
        return "", "/"
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = _normalize_host(raw)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return host, path


def _href_matches_provider_target(href: str, target_url: str, target_domain: str) -> bool:
    if not _href_points_to_domain(href, target_domain):
        return False
    target_host, target_path = _normalized_link_identity(target_url)
    href_host, href_path = _normalized_link_identity(href)
    if not target_host:
        return False
    if target_path == "/":
        return True
    return href_host == target_host and href_path == target_path


def _validate_public_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must be public HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("source URL credentials are not allowed")

    host = parsed.hostname.lower().strip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("local source URL is not allowed")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("non-public source IP is not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"source hostname did not resolve: {host}") from exc
    addresses = {answer[4][0] for answer in answers if answer[4]}
    if not addresses:
        raise ValueError(f"source hostname did not resolve: {host}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("source hostname returned an invalid address") from exc
        if not ip.is_global:
            raise ValueError("source hostname resolves to a non-public address")
    return parsed.geturl()


def _fetch_public_page(url: str, timeout_seconds: float) -> tuple[int, str, bytes, str]:
    current = _validate_public_url(url)
    headers = {"User-Agent": "Expandosaurus-Link-Hunter/0.3 (+link verification)"}
    with httpx.Client(
        follow_redirects=False,
        timeout=timeout_seconds,
        headers=headers,
        trust_env=False,
    ) as client:
        for _ in range(MAX_VERIFY_REDIRECTS + 1):
            _validate_public_url(current)
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("source page returned redirect without Location")
                    current = _validate_public_url(urljoin(current, location))
                    continue

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    remaining = MAX_VERIFY_BYTES - size
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    size += min(len(chunk), remaining)
                    if size >= MAX_VERIFY_BYTES:
                        break
                content = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                text = content.decode(encoding, errors="replace")
                return response.status_code, str(response.url), content, text
    raise ValueError("source page exceeded redirect limit")


def _finish_provider_query(
    db: Session,
    query: ProviderQuery,
    *,
    status: str,
    task_id: str = "",
    cost_usd: float = 0.0,
    row_count: int = 0,
    error: str | None = None,
) -> None:
    query.status = status
    query.provider_task_id = task_id
    query.cost_usd = cost_usd
    query.row_count = row_count
    query.error = error[:2000] if error else None
    query.completed_at = utcnow()
    db.commit()


def _provider_call(
    db: Session,
    *,
    endpoint: str,
    target: str,
    callback: Any,
) -> Any:
    query = ProviderQuery(provider="dataforseo", endpoint=endpoint, target=target, status="running")
    db.add(query)
    db.commit()
    db.refresh(query)
    try:
        response = callback()
        result = response.result
        row_count = int(result.get("items_count") or result.get("referring_pages") or 0)
        _finish_provider_query(
            db,
            query,
            status="complete",
            task_id=response.task_id,
            cost_usd=response.task_cost_usd,
            row_count=row_count,
        )
        return response
    except Exception as exc:
        db.rollback()
        query = db.get(ProviderQuery, query.id)
        if query is not None:
            _finish_provider_query(db, query, status="failed", error=str(exc))
        raise


def _bulk_provider_call(
    db: Session,
    *,
    endpoint: str,
    targets: list[str],
    callback: Any,
) -> Any:
    queries: list[ProviderQuery] = []
    for target in targets:
        query = ProviderQuery(provider="dataforseo", endpoint=endpoint, target=target, status="running")
        db.add(query)
        queries.append(query)
    db.commit()
    try:
        response = callback()
        items = response.result.get("items") or []
        item_map = {_normalize_host(str(item.get("url") or item.get("target") or "")): item for item in items}
        split_cost = response.task_cost_usd / max(len(queries), 1)
        for query in queries:
            item = item_map.get(_normalize_host(query.target), {})
            query.status = "complete"
            query.provider_task_id = response.task_id
            query.cost_usd = split_cost
            query.row_count = int(item.get("referring_pages") or item.get("backlinks") or 0)
            query.completed_at = utcnow()
        db.commit()
        return response
    except Exception as exc:
        db.rollback()
        for query in queries:
            current = db.get(ProviderQuery, query.id)
            if current is not None:
                current.status = "failed"
                current.error = str(exc)[:2000]
                current.completed_at = utcnow()
        db.commit()
        raise


def _get_or_create_domain(db: Session, name: str) -> Domain | None:
    return get_or_create_unsuppressed_domain(db, name)


def _dns_prefilter_targets(
    db: Session,
    targets: list[str],
) -> tuple[list[str], int]:
    """Reject live names for free before any paid bulk-summary request."""
    if not targets:
        return [], 0
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        dns_results = dict(zip(targets, pool.map(check_dns, targets), strict=True))
    survivors: list[str] = []
    blocked = 0
    for target in targets:
        dns_status = dns_results[target]
        domain = _get_or_create_domain(db, target)
        if domain is None:
            blocked += 1
            continue
        domain.dns_status = dns_status
        if dns_status == "resolves":
            domain.availability_status = "registered"
            domain.availability_source = "dns"
            domain.rdap_status = "skipped"
            domain.last_checked_at = utcnow()
            blocked += 1
        else:
            survivors.append(target)
    db.commit()
    return survivors, blocked


def _get_or_create_site(db: Session, hostname: str) -> SourceSite:
    site = db.scalar(select(SourceSite).where(SourceSite.hostname == hostname))
    if site is None:
        site = SourceSite(hostname=hostname, source_type="web")
        db.add(site)
        db.flush()
    else:
        site.last_seen_at = utcnow()
    return site


def _get_or_create_page(db: Session, site: SourceSite, item: dict[str, Any]) -> SourcePage | None:
    url = str(item.get("url_from") or "").strip()
    if not url:
        return None
    page = db.scalar(select(SourcePage).where(SourcePage.url == url))
    if page is None:
        page = SourcePage(site_id=site.id, url=url)
        db.add(page)
        db.flush()
    page.title = str(item.get("page_from_title") or "")
    page.language = str(item.get("page_from_language") or "")[:16]
    page.http_status = item.get("page_from_status_code")
    page.page_rank = item.get("page_from_rank")
    page.domain_rank = item.get("domain_from_rank")
    page.last_seen_at = utcnow()
    return page


def _save_backlink(db: Session, domain: Domain, item: dict[str, Any]) -> SourceLink | None:
    hostname = _normalize_host(str(item.get("domain_from") or ""))
    if not hostname:
        return None
    site = _get_or_create_site(db, hostname)
    page = _get_or_create_page(db, site, item)
    if page is None:
        return None

    target_url = str(item.get("url_to") or domain.name)
    link = db.scalar(
        select(SourceLink).where(
            SourceLink.source_page_id == page.id,
            SourceLink.domain_id == domain.id,
            SourceLink.target_url == target_url,
        )
    )
    if link is None:
        link = SourceLink(
            source_page_id=page.id,
            domain_id=domain.id,
            target_url=target_url,
        )
        db.add(link)
    link.anchor_text = str(item.get("anchor") or "")
    link.context_before = str(item.get("text_pre") or "")
    link.context_after = str(item.get("text_post") or "")
    link.semantic_location = str(item.get("semantic_location") or "")[:64]
    link.dofollow = bool(item.get("dofollow"))
    link.provider_live = not bool(item.get("is_lost"))
    link.provider_rank = item.get("rank")
    link.spam_score = item.get("backlink_spam_score")
    link.last_seen_at = utcnow()
    return link


def _save_metric_snapshot(
    db: Session,
    page: SourcePage,
    item: dict[str, Any],
) -> int:
    captured = utcnow()
    snapshot = db.scalar(
        select(SourceMetricSnapshot).where(
            SourceMetricSnapshot.source_page_id == page.id,
            SourceMetricSnapshot.provider == "dataforseo",
            SourceMetricSnapshot.capture_date == captured.date(),
        )
    )
    if snapshot is None:
        snapshot = SourceMetricSnapshot(
            source_page_id=page.id,
            provider="dataforseo",
            capture_date=captured.date(),
        )
        db.add(snapshot)
    metrics = item.get("metrics") or {}
    organic = metrics.get("organic") or {}
    traffic = max(0, int(round(float(organic.get("etv") or 0.0))))
    snapshot.captured_at = captured
    snapshot.organic_traffic_estimate = traffic
    snapshot.page_rank = page.page_rank
    snapshot.domain_rank = page.domain_rank
    snapshot.raw_metrics = metrics
    return traffic


def _apply_availability(domain: Domain, result: AvailabilityResult) -> None:
    domain.availability_status = result.status
    domain.availability_source = result.source
    domain.rdap_status = result.rdap_status
    domain.dns_status = result.dns_status
    domain.http_status = result.http_status
    domain.registrar_price_usd = result.price_usd
    domain.premium = result.premium
    domain.check_error = result.error
    domain.last_checked_at = utcnow()


def _commercial_signal(saved_links: list[SourceLink]) -> float:
    if not saved_links:
        return 0.0
    terms = {
        "buy",
        "book",
        "deal",
        "discount",
        "download",
        "order",
        "price",
        "register",
        "service",
        "shop",
        "signup",
        "software",
        "tool",
        "training",
    }
    text = " ".join(
        f"{link.anchor_text} {link.context_before} {link.context_after}" for link in saved_links
    ).lower()
    hits = sum(1 for term in terms if term in text)
    return min(1.0, hits / 4.0)


def _guess_niche(saved_links: list[SourceLink]) -> str:
    text = " ".join(
        f"{link.anchor_text} {link.context_before} {link.context_after}" for link in saved_links
    ).lower()
    groups = {
        "finance": ("mortgage", "loan", "insurance", "invest", "finance", "tax"),
        "software": ("software", "app", "plugin", "hosting", "wordpress", "saas"),
        "education": ("course", "training", "learn", "tutorial", "school"),
        "ecommerce": ("shop", "store", "product", "discount", "coupon", "buy"),
        "home": ("home", "renovation", "plumbing", "garden", "landscape"),
        "fitness": ("fitness", "workout", "gym", "yoga", "training plan"),
        "travel": ("travel", "hotel", "flight", "tour", "holiday"),
        "food": ("recipe", "baking", "cake", "food", "meal"),
        "pets": ("dog", "cat", "pet", "grooming"),
        "automotive": ("car", "auto", "vehicle", "detailing", "repair"),
    }
    scores = {name: sum(1 for term in terms if term in text) for name, terms in groups.items()}
    best = max(scores, key=scores.get) if scores else ""
    return best if scores.get(best, 0) else ""


def _score_opportunity(
    opportunity: Opportunity,
    domain: Domain,
    saved_links: list[SourceLink],
    traffic: int,
    verified: bool,
    *,
    db: Session | None = None,
    clickability_score: float = 0.0,
) -> None:
    independent_sites = max(0, opportunity.independent_site_count)
    referring_pages = max(0, opportunity.referring_page_count)
    link_strength = max(0.0, min(100.0, opportunity.link_strength))
    traffic_points = min(35.0, math.log10(traffic + 1) * 7.0)
    site_points = min(20.0, math.sqrt(independent_sites) * 4.0)
    referring_points = min(10.0, math.log10(referring_pages + 1) * 4.0)
    strength_points = link_strength * 0.2
    dofollow_ratio = (
        sum(1 for link in saved_links if link.dofollow) / len(saved_links) if saved_links else 0.0
    )
    dofollow_points = dofollow_ratio * 5.0
    live_points = 10.0 if verified else 0.0
    spam_values = [float(link.spam_score) for link in saved_links if link.spam_score is not None]
    spam_penalty = min(15.0, (sum(spam_values) / len(spam_values)) * 0.3) if spam_values else 0.0

    opportunity.commercial_intent = _commercial_signal(saved_links)
    commercial_points = opportunity.commercial_intent * 5.0
    opportunity.source_page_traffic_estimate = traffic
    opportunity.verified_live_link = verified
    opportunity.niche = _guess_niche(saved_links)
    evidence_score = round(
        max(
            0.0,
            traffic_points
            + site_points
            + referring_points
            + strength_points
            + dofollow_points
            + live_points
            + commercial_points
            - spam_penalty,
        ),
        1,
    )
    screening_risk = 0.0
    if db is not None:
        screening_risk = float(
            db.scalar(select(WebScreening.risk_score).where(WebScreening.domain_name == domain.name).limit(1))
            or 0.0
        )
    projection = project_opportunity_economics(
        opportunity,
        domain,
        saved_links,
        traffic=traffic,
        verified=verified,
        evidence_score=evidence_score,
        clickability_score=clickability_score,
        screening_risk=screening_risk,
    )
    opportunity.score = projection.buy_score
    if db is not None:
        save_opportunity_economics(db, domain, projection)

    ordinary_available = domain.availability_status == "available" and not domain.premium
    if ordinary_available and verified and opportunity.score >= 80:
        opportunity.tier = "priority"
    elif ordinary_available and verified and opportunity.score >= 65:
        opportunity.tier = "qualified"
    elif opportunity.score >= 45:
        opportunity.tier = "watchlist"
    else:
        opportunity.tier = "pending"
    apply_source_focus_bonus(
        db,
        opportunity,
        domain,
        saved_links,
        traffic=traffic,
        verified=verified,
    )
    enforce_money_tier(
        db,
        opportunity,
        traffic=traffic,
        verified=verified,
    )
    opportunity.updated_at = utcnow()


def _save_opportunity(
    db: Session,
    domain: Domain,
    summary: dict[str, Any],
    saved_links: list[SourceLink],
) -> Opportunity:
    opportunity = db.scalar(select(Opportunity).where(Opportunity.domain_id == domain.id))
    if opportunity is None:
        opportunity = Opportunity(domain_id=domain.id)
        db.add(opportunity)

    opportunity.tier = "pending"
    opportunity.referring_page_count = int(summary.get("referring_pages") or 0)
    opportunity.independent_site_count = int(
        summary.get("referring_main_domains") or summary.get("referring_domains") or 0
    )
    opportunity.link_strength = max(
        (float(link.provider_rank or 0.0) for link in saved_links),
        default=0.0,
    )
    opportunity.best_source_page_id = saved_links[0].source_page_id if saved_links else None
    opportunity.updated_at = utcnow()
    return opportunity


def _save_backlink_summary(
    db: Session,
    domain: Domain,
    summary: dict[str, Any],
) -> BacklinkSummary:
    record = db.scalar(
        select(BacklinkSummary).where(
            BacklinkSummary.domain_id == domain.id,
            BacklinkSummary.provider == "dataforseo",
        )
    )
    if record is None:
        record = BacklinkSummary(domain_id=domain.id, provider="dataforseo")
        db.add(record)
    record.backlinks = max(0, int(summary.get("backlinks") or 0))
    record.referring_pages = max(0, int(summary.get("referring_pages") or 0))
    record.referring_domains = max(0, int(summary.get("referring_domains") or 0))
    record.referring_main_domains = max(
        0,
        int(summary.get("referring_main_domains") or record.referring_domains),
    )
    record.rank = max(0.0, float(summary.get("rank") or 0.0))
    record.raw_summary = summary
    record.last_refreshed_at = utcnow()
    return record


def _save_summary_opportunity(
    db: Session,
    domain: Domain,
    summary: dict[str, Any],
    combined_score: float,
) -> Opportunity | None:
    record = _save_backlink_summary(db, domain, summary)
    if record.referring_pages <= 0:
        return None
    opportunity = db.scalar(select(Opportunity).where(Opportunity.domain_id == domain.id))
    if opportunity is None:
        opportunity = Opportunity(domain_id=domain.id)
        db.add(opportunity)
    opportunity.referring_page_count = max(
        int(opportunity.referring_page_count or 0),
        record.referring_pages,
    )
    opportunity.independent_site_count = max(
        int(opportunity.independent_site_count or 0),
        record.referring_main_domains or record.referring_domains,
    )
    opportunity.link_strength = max(float(opportunity.link_strength or 0.0), record.rank)
    if not opportunity.verified_live_link:
        opportunity.score = round(
            min(39.9, max(float(opportunity.score or 0.0), combined_score)),
            1,
        )
        opportunity.tier = "pending"
    opportunity.updated_at = utcnow()
    return opportunity


def _fetch_source_link_evidence(
    request: _VerificationRequest,
    timeout_seconds: float,
) -> _VerificationFetchResult:
    """Fetch and parse a source page without touching the SQLAlchemy session.

    The refresh job can fan these requests out safely because the only work in
    worker threads is bounded public HTTP I/O and HTML parsing.  Database
    writes remain serialized by the caller, so the persistent evidence ledger
    retains its existing transaction and deduplication behaviour.
    """
    observed_at = utcnow()
    try:
        status_code, final_url, content, text = _fetch_public_page(
            request.source_url,
            timeout_seconds,
        )
        parser = _HrefParser()
        parser.feed(text)
        matches = [
            anchor
            for anchor in parser.anchors
            if _href_matches_provider_target(
                urljoin(final_url, anchor.href),
                request.target_url,
                request.target_domain,
            )
        ]
        best_anchor = max(matches, key=_anchor_clickability) if matches else None
        link_present = bool(matches and 200 <= status_code < 400)
        clickability_score = _anchor_clickability(best_anchor) if best_anchor else 0.0
        return _VerificationFetchResult(
            observed_at=observed_at,
            http_status=status_code,
            final_url=final_url,
            content_hash=hashlib.sha256(content).hexdigest(),
            link_present=link_present,
            clickability_score=clickability_score,
            clickable=bool(link_present and clickability_score > 0),
            semantic_location=best_anchor.semantic_location if best_anchor else "missing",
            anchor_text=best_anchor.text if best_anchor else "",
            nofollow=best_anchor.nofollow if best_anchor else False,
            error=None,
        )
    except Exception as exc:
        return _VerificationFetchResult(
            observed_at=observed_at,
            http_status=None,
            final_url=request.source_url,
            content_hash=None,
            link_present=False,
            clickability_score=0.0,
            clickable=False,
            semantic_location="missing",
            anchor_text="",
            nofollow=False,
            error=str(exc)[:2000],
        )


def _record_source_link_evidence(
    db: Session,
    link: SourceLink,
    request: _VerificationRequest,
    result: _VerificationFetchResult,
) -> bool:
    """Persist one completed fetch result in the canonical evidence ledger."""
    verification = db.scalar(select(FetchVerification).where(FetchVerification.source_link_id == link.id))
    if verification is None:
        verification = FetchVerification(source_link_id=link.id)
        db.add(verification)

    observation = LinkObservation(
        source_link_id=link.id,
        observed_at=result.observed_at,
        final_url=result.final_url,
    )
    db.add(observation)
    verification.fetched_at = result.observed_at
    verification.http_status = result.http_status
    verification.final_url = result.final_url
    verification.content_hash = result.content_hash
    verification.link_present = result.link_present
    verification.error = result.error
    observation.http_status = result.http_status
    observation.final_url = result.final_url
    observation.link_present = result.link_present
    observation.clickability_score = result.clickability_score
    observation.clickable = result.clickable
    observation.semantic_location = result.semantic_location
    observation.anchor_text = result.anchor_text
    observation.nofollow = result.nofollow
    observation.error = result.error

    first_seen = request.first_seen_at
    if first_seen.tzinfo is None:
        first_seen = first_seen.replace(tzinfo=UTC)
    observation.survival_days = round(
        max(0.0, (result.observed_at - first_seen).total_seconds() / 86400),
        2,
    )
    observation.content_hash = result.content_hash
    db.commit()
    return bool(observation.link_present and observation.clickable)


def _verify_source_link(
    db: Session,
    link: SourceLink,
    target_domain: str,
    timeout_seconds: float,
    cache_hours: int = 24,
) -> bool:
    page = db.get(SourcePage, link.source_page_id)
    if page is None:
        return False
    verification = db.scalar(select(FetchVerification).where(FetchVerification.source_link_id == link.id))
    fetched_at = verification.fetched_at if verification is not None else None
    if fetched_at is not None and fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    if (
        fetched_at is not None
        and fetched_at >= datetime.now(UTC) - timedelta(hours=cache_hours)
        and verification is not None
        and verification.error is None
    ):
        latest = db.scalar(
            select(LinkObservation)
            .where(LinkObservation.source_link_id == link.id)
            .order_by(LinkObservation.observed_at.desc())
            .limit(1)
        )
        return bool(verification.link_present and (latest is None or latest.clickable))

    request = _VerificationRequest(
        source_link_id=link.id,
        source_url=page.url,
        target_url=link.target_url,
        target_domain=target_domain,
        first_seen_at=link.first_seen_at,
    )
    result = _fetch_source_link_evidence(request, timeout_seconds)
    return _record_source_link_evidence(db, link, request, result)


def refresh_web_link_observations(
    db: Session,
    settings: Settings,
    batch_size: int,
) -> dict[str, int]:
    """Recheck due deep-proof links for free and extend their survival history."""
    cutoff = datetime.now(UTC) - timedelta(hours=settings.link_hunter_verification_cache_hours)
    rows = db.execute(
        select(Opportunity, Domain, SourceLink, SourcePage, FetchVerification)
        .join(Domain, Domain.id == Opportunity.domain_id)
        .join(
            SourceLink,
            (SourceLink.domain_id == Domain.id)
            & (SourceLink.source_page_id == Opportunity.best_source_page_id),
        )
        .join(SourcePage, SourcePage.id == SourceLink.source_page_id)
        .outerjoin(FetchVerification, FetchVerification.source_link_id == SourceLink.id)
        .where((FetchVerification.id.is_(None)) | (FetchVerification.fetched_at < cutoff))
        .order_by(FetchVerification.fetched_at.asc(), Opportunity.score.desc())
        .limit(batch_size)
    ).all()
    counters = {"due": len(rows), "refreshed": 0, "verified": 0, "missing": 0, "errors": 0}
    requests = [
        _VerificationRequest(
            source_link_id=best_link.id,
            source_url=page.url,
            target_url=best_link.target_url,
            target_domain=domain.name,
            first_seen_at=best_link.first_seen_at,
        )
        for _, domain, best_link, page, _ in rows
    ]
    fetched: dict[int, _VerificationFetchResult] = {}
    if requests:
        worker_count = min(settings.link_hunter_link_refresh_workers, len(requests))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = pool.map(
                lambda request: _fetch_source_link_evidence(
                    request,
                    settings.link_hunter_verify_timeout_seconds,
                ),
                requests,
            )
            fetched = {
                request.source_link_id: result for request, result in zip(requests, results, strict=True)
            }

    for (opportunity, domain, best_link, _, _), request in zip(rows, requests, strict=True):
        try:
            verified = _record_source_link_evidence(db, best_link, request, fetched[best_link.id])
            observation = db.scalar(
                select(LinkObservation)
                .where(LinkObservation.source_link_id == best_link.id)
                .order_by(LinkObservation.observed_at.desc())
                .limit(1)
            )
            links = db.scalars(select(SourceLink).where(SourceLink.domain_id == domain.id)).all()
            _score_opportunity(
                opportunity,
                domain,
                list(links),
                max(0, int(opportunity.source_page_traffic_estimate or 0)),
                verified,
                db=db,
                clickability_score=(
                    float(observation.clickability_score) if observation is not None else 0.0
                ),
            )
            db.commit()
            counters["refreshed"] += 1
            counters["verified" if verified else "missing"] += 1
        except Exception:
            db.rollback()
            counters["errors"] += 1
    return counters


def _cached_summary_payload(db: Session, target: str) -> dict[str, Any]:
    row = db.execute(
        select(BacklinkSummary, Domain)
        .join(Domain, Domain.id == BacklinkSummary.domain_id)
        .where(
            Domain.name == target,
            BacklinkSummary.provider == "dataforseo",
        )
        .limit(1)
    ).first()
    if row is None:
        return {}
    summary, _ = row
    payload = dict(summary.raw_summary or {})
    payload.setdefault("backlinks", int(summary.backlinks or 0))
    payload.setdefault("referring_pages", int(summary.referring_pages or 0))
    payload.setdefault("referring_domains", int(summary.referring_domains or 0))
    payload.setdefault("referring_main_domains", int(summary.referring_main_domains or 0))
    payload.setdefault("rank", float(summary.rank or 0.0))
    return payload


def run_provider_proof(db: Session, settings: Settings) -> dict[str, Any]:
    """Run the cost-capped provider funnel with a permanent global winner queue.

    New names receive the cheap 100-domain bulk summary. Every positive summary
    remains eligible for later detailed proof until it has actually received a
    completed backlinks call, so strong candidates can never be stranded simply
    because four stronger names happened to be in their original batch.
    """
    if not settings.link_hunter_enabled:
        raise DataForSEOError("Link Hunter feature flag is disabled")
    if not settings.dataforseo_enabled:
        raise DataForSEOError("DataForSEO credentials are not configured")

    storage_status = database_storage_status(db, settings)
    if not storage_status.write_allowed:
        logger.warning(
            "Database storage guard blocked link_hunter_proof before provider calls: %s",
            storage_status.reason,
        )
        return {
            "targets": 0,
            "summary_targets": 0,
            "summary_screened": 0,
            "deep_proof_target_count": 0,
            "deep_proof_targets": [],
            "summary_calls": 0,
            "backlink_calls": 0,
            "traffic_calls": 0,
            "source_links_verified": 0,
            "provider_cost_usd": 0.0,
            "errors": 0,
            "storage_guard_blocked": True,
            "database_storage": storage_status.as_dict(),
        }

    run_cost_cap = effective_provider_run_limit_usd(settings)
    targets, free_scores, free_signals, _, _ = select_provider_summary_targets_with_ranking(db, settings)
    counters: dict[str, Any] = {
        "targets": len(targets),
        "summary_targets": len(targets),
        "free_dns_screened": 0,
        "free_dns_blocked": 0,
        "winner_queue_candidates": 0,
        "winner_queue_dns_screened": 0,
        "winner_queue_dns_blocked": 0,
        "summary_screened": 0,
        "summary_domains_with_live_backlinks": 0,
        "deep_proof_target_count": 0,
        "deep_proof_targets": [],
        "summary_calls": 0,
        "backlink_calls": 0,
        "traffic_calls": 0,
        "availability_checks": 0,
        "registrar_checks": 0,
        "registered_or_unavailable": 0,
        "domains_with_live_backlinks": 0,
        "links_saved": 0,
        "source_pages_traffic_checked": 0,
        "source_links_verified": 0,
        "provider_cost_usd": 0.0,
        "cost_cap_hit": False,
        "errors": 0,
        "error_details": [],
    }

    client: DataForSEOClient | None = None
    domain_batches: list[tuple[Domain, Opportunity, list[SourceLink]]] = []
    summary_map: dict[str, dict[str, Any]] = {}

    # Stage 1: only names never summarised before incur the cheap bulk call.
    if targets:
        original_target_count = len(targets)
        targets, dns_blocked = _dns_prefilter_targets(db, targets)
        counters["free_dns_screened"] = original_target_count
        counters["free_dns_blocked"] = dns_blocked
        counters["summary_targets"] = len(targets)
        counters["registered_or_unavailable"] = dns_blocked

    if targets:
        client = DataForSEOClient(settings)
        try:
            summary_response = _bulk_provider_call(
                db,
                endpoint="bulk_backlink_summary",
                targets=targets,
                callback=lambda: client.bulk_backlink_summaries(targets),
            )
            counters["summary_calls"] = 1
            counters["provider_cost_usd"] += summary_response.task_cost_usd
            summary_map = {
                _normalize_host(str(item.get("url") or "")): item
                for item in summary_response.result.get("items") or []
            }
            counters["summary_screened"] = len(targets)
            counters["summary_domains_with_live_backlinks"] = sum(
                1
                for target in targets
                if int(summary_map.get(_normalize_host(target), {}).get("referring_pages") or 0) > 0
            )
        except Exception as exc:
            counters["errors"] += 1
            counters["error_details"].append(f"bulk summary: {exc}")
            counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
            return counters

        normalized_summaries = {target: summary_map.get(_normalize_host(target), {}) for target in targets}
        _, combined_scores, _ = rerank_summary_screen_targets(
            targets,
            free_scores,
            free_signals,
            normalized_summaries,
            0,
        )
        for target in targets:
            domain = _get_or_create_domain(db, target)
            if domain is None:
                continue
            _save_summary_opportunity(
                db,
                domain,
                normalized_summaries.get(target, {}),
                combined_scores.get(target, 0.0),
            )
        db.commit()

    # Stage 2: globally rerank every positive cached summary that has never had
    # detailed proof. The queue survives across batches and restarts.
    winner_targets, winner_scores, winner_summary_scores, winner_free_scores, _ = (
        select_cached_deep_proof_targets_with_ranking(db, settings)
    )
    counters["winner_queue_candidates"] = len(winner_targets)
    if not winner_targets:
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    # DNS is free. Look ahead far enough to keep all five paid slots full even
    # when high-ranked cached names have since been registered.
    deep_targets: list[str] = []
    cursor = 0
    dns_candidate_cap = min(len(winner_targets), max(100, settings.link_hunter_proof_batch_size * 20))
    while len(deep_targets) < settings.link_hunter_proof_batch_size and cursor < dns_candidate_cap:
        window_size = min(25, dns_candidate_cap - cursor)
        window = winner_targets[cursor : cursor + window_size]
        cursor += len(window)
        if not window:
            break
        survivors, blocked = _dns_prefilter_targets(db, window)
        counters["winner_queue_dns_screened"] += len(window)
        counters["winner_queue_dns_blocked"] += blocked
        counters["registered_or_unavailable"] += blocked
        for target in survivors:
            if target not in deep_targets:
                deep_targets.append(target)
            if len(deep_targets) >= settings.link_hunter_proof_batch_size:
                break

    deep_targets = deep_targets[: settings.link_hunter_proof_batch_size]
    counters["deep_proof_target_count"] = len(deep_targets)
    counters["deep_proof_targets"] = deep_targets
    counters["deep_proof_scores"] = {
        target: {
            "cached_free_preproof": winner_free_scores.get(target, 0.0),
            "cached_bulk_summary": winner_summary_scores.get(target, 0.0),
            "global_combined": winner_scores.get(target, 0.0),
        }
        for target in deep_targets
    }
    if not deep_targets:
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    if client is None:
        client = DataForSEOClient(settings)

    for target in deep_targets:
        if counters["provider_cost_usd"] >= run_cost_cap:
            counters["cost_cap_hit"] = True
            break
        summary = _cached_summary_payload(db, target)
        if int(summary.get("referring_pages") or 0) <= 0:
            continue
        counters["domains_with_live_backlinks"] += 1
        try:
            backlink_response = _provider_call(
                db,
                endpoint="backlinks",
                target=target,
                callback=lambda target=target: client.backlinks(
                    target, settings.link_hunter_backlinks_per_domain
                ),
            )
            counters["backlink_calls"] += 1
            counters["provider_cost_usd"] += backlink_response.task_cost_usd

            domain = _get_or_create_domain(db, target)
            if domain is None:
                continue
            saved_links: list[SourceLink] = []
            for item in backlink_response.result.get("items") or []:
                if item.get("is_lost"):
                    continue
                link = _save_backlink(db, domain, item)
                if link is not None:
                    saved_links.append(link)
            opportunity = _save_opportunity(db, domain, summary, saved_links)
            db.commit()
            counters["links_saved"] += len(saved_links)

            availability = check_domain(target, settings, exact_registrar_check=False)
            _apply_availability(domain, availability)
            counters["availability_checks"] += 1
            if availability.status == "registered":
                counters["registered_or_unavailable"] += 1
                _score_opportunity(
                    opportunity,
                    domain,
                    saved_links,
                    traffic=0,
                    verified=False,
                    db=db,
                )
                db.commit()
                continue

            domain_batches.append((domain, opportunity, saved_links))
            db.commit()
        except Exception as exc:
            db.rollback()
            counters["errors"] += 1
            if len(counters["error_details"]) < 5:
                counters["error_details"].append(f"{target}: {exc}")

    page_urls = sorted(
        {
            page.url
            for _, _, links in domain_batches
            for link in links
            if (page := db.get(SourcePage, link.source_page_id)) is not None
        }
    )
    traffic_map: dict[str, int] = {}
    if page_urls and counters["provider_cost_usd"] < run_cost_cap:
        try:
            traffic_response = _provider_call(
                db,
                endpoint="bulk_traffic_estimation",
                target=f"{len(page_urls)} source pages",
                callback=lambda: client.bulk_traffic_estimation(page_urls),
            )
            counters["traffic_calls"] = 1
            counters["provider_cost_usd"] += traffic_response.task_cost_usd
            traffic_items = traffic_response.result.get("items") or []
            for item in traffic_items:
                key = _canonical_url(str(item.get("target") or ""))
                if key:
                    metrics = item.get("metrics") or {}
                    organic = metrics.get("organic") or {}
                    traffic_map[key] = max(0, int(round(float(organic.get("etv") or 0.0))))
            for page_url in page_urls:
                page = db.scalar(select(SourcePage).where(SourcePage.url == page_url))
                if page is None:
                    continue
                matching_item = next(
                    (
                        item
                        for item in traffic_items
                        if _canonical_url(str(item.get("target") or "")) == _canonical_url(page_url)
                    ),
                    {"metrics": {}},
                )
                _save_metric_snapshot(db, page, matching_item)
            db.commit()
            counters["source_pages_traffic_checked"] = len(page_urls)
        except Exception as exc:
            db.rollback()
            counters["errors"] += 1
            if len(counters["error_details"]) < 5:
                counters["error_details"].append(f"traffic: {exc}")
    elif page_urls:
        counters["cost_cap_hit"] = True

    for domain, opportunity, links in domain_batches:
        if not links:
            _score_opportunity(opportunity, domain, links, 0, False, db=db)
            db.commit()
            continue

        def _best_link_key(link: SourceLink) -> tuple[int, float]:
            page = db.get(SourcePage, link.source_page_id)
            page_url = page.url if page is not None else ""
            return (
                traffic_map.get(_canonical_url(page_url), 0),
                float(link.provider_rank or 0.0),
            )

        best_link = max(links, key=_best_link_key)
        best_page = db.get(SourcePage, best_link.source_page_id)
        best_traffic = traffic_map.get(_canonical_url(best_page.url if best_page else ""), 0)
        opportunity.best_source_page_id = best_link.source_page_id
        verified = _verify_source_link(
            db,
            best_link,
            domain.name,
            settings.link_hunter_verify_timeout_seconds,
            settings.link_hunter_verification_cache_hours,
        )
        if verified:
            counters["source_links_verified"] += 1
        latest_observation = db.scalar(
            select(LinkObservation)
            .where(LinkObservation.source_link_id == best_link.id)
            .order_by(LinkObservation.observed_at.desc())
            .limit(1)
        )
        clickability_score = float(
            latest_observation.clickability_score if latest_observation is not None else 0.0
        )
        _score_opportunity(
            opportunity,
            domain,
            links,
            best_traffic,
            verified,
            db=db,
            clickability_score=clickability_score,
        )

        if (
            verified
            and opportunity.score >= 45
            and domain.availability_status in {"likely_available", "conflicting", "unknown"}
            and settings.registrar_enabled
        ):
            exact_availability = check_domain(domain.name, settings, exact_registrar_check=True)
            _apply_availability(domain, exact_availability)
            counters["registrar_checks"] += 1
            _score_opportunity(
                opportunity,
                domain,
                links,
                best_traffic,
                verified,
                db=db,
                clickability_score=clickability_score,
            )
        db.commit()

    counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
    return counters


def run_provider_proof_job() -> dict[str, Any]:
    """Run one externally-triggered paid proof with a durable single-flight guard."""
    settings = get_settings()
    with SessionLocal() as db:
        if not storage_guard_allows_writes(db, settings, "link_hunter_proof"):
            return {
                "targets": 0,
                "summary_screened": 0,
                "deep_proof_target_count": 0,
                "provider_cost_usd": 0.0,
                "errors": 0,
                "daily_budget_skipped": False,
                "storage_guard_blocked": True,
                "database_storage": database_storage_status(db, settings).as_dict(),
            }
        lease = acquire_provider_proof_lease(db)
        if lease is None:
            budget = provider_daily_budget_snapshot(db, settings)
            return {
                "targets": 0,
                "summary_screened": 0,
                "deep_proof_target_count": 0,
                "provider_cost_usd": 0.0,
                "errors": 0,
                "daily_budget_skipped": False,
                "run_in_progress": True,
                "daily_budget": budget,
            }

        run: RunLog | None = None
        try:
            reservation = reserve_provider_daily_budget(db, settings)
            if reservation is None:
                budget = provider_daily_budget_snapshot(db, settings)
                counters = {
                    "targets": 0,
                    "summary_screened": 0,
                    "deep_proof_target_count": 0,
                    "provider_cost_usd": 0.0,
                    "errors": 0,
                    "daily_budget_skipped": True,
                    "daily_budget": budget,
                }
                run = RunLog(
                    job="link_hunter_proof",
                    started_at=utcnow(),
                    finished_at=utcnow(),
                    status="skipped",
                    counters=counters,
                )
                db.add(run)
                db.commit()
                return counters

            run = RunLog(job="link_hunter_proof", started_at=utcnow(), status="running", counters={})
            db.add(run)
            db.commit()
            db.refresh(run)
            counters = run_provider_proof(db, settings)
            release_unused = not bool(counters.get("errors"))
            finalize_provider_daily_budget(
                db,
                reservation,
                float(counters.get("provider_cost_usd") or 0.0),
                release_unused=release_unused,
            )
            counters["daily_budget_skipped"] = False
            counters["daily_budget"] = provider_daily_budget_snapshot(db, settings)
            run.status = "complete" if not counters.get("errors") else "partial"
            run.counters = counters
            run.finished_at = utcnow()
            db.commit()
            return counters
        except Exception as exc:
            db.rollback()
            if run is not None:
                run = db.get(RunLog, run.id)
                if run is not None:
                    run.status = "failed"
                    run.error = str(exc)[:2000]
                    run.finished_at = utcnow()
                    db.commit()
            raise
        finally:
            release_provider_proof_lease(db, lease)
