from __future__ import annotations

import hashlib
import math
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.dataforseo import DataForSEOClient, DataForSEOError
from app.models import (
    Domain,
    DroppedDomain,
    FetchVerification,
    Opportunity,
    ProviderQuery,
    RunLog,
    SourceLink,
    SourceMetricSnapshot,
    SourcePage,
    SourceSite,
    utcnow,
)


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


def _normalize_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
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
        item_map = {
            _normalize_host(str(item.get("url") or item.get("target") or "")): item
            for item in items
        }
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


def _get_or_create_domain(db: Session, name: str) -> Domain:
    domain = db.scalar(select(Domain).where(Domain.name == name))
    if domain is None:
        domain = Domain(name=name)
        db.add(domain)
        db.flush()
    return domain


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
    opportunity.score = round(
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

    ordinary_available = domain.availability_status == "available" and not domain.premium
    if ordinary_available and verified and opportunity.score >= 80:
        opportunity.tier = "priority"
    elif ordinary_available and verified and opportunity.score >= 65:
        opportunity.tier = "qualified"
    elif opportunity.score >= 45:
        opportunity.tier = "watchlist"
    else:
        opportunity.tier = "pending"
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


def _verify_source_link(
    db: Session,
    link: SourceLink,
    target_domain: str,
    timeout_seconds: float,
) -> bool:
    page = db.get(SourcePage, link.source_page_id)
    if page is None:
        return False
    verification = db.scalar(
        select(FetchVerification).where(FetchVerification.source_link_id == link.id)
    )
    if verification is None:
        verification = FetchVerification(source_link_id=link.id)
        db.add(verification)

    verification.fetched_at = utcnow()
    try:
        response = httpx.get(
            page.url,
            follow_redirects=True,
            timeout=timeout_seconds,
            headers={"User-Agent": "Expandosaurus-Link-Hunter/0.3 (+link verification)"},
        )
        verification.http_status = response.status_code
        verification.final_url = str(response.url)
        verification.content_hash = hashlib.sha256(response.content).hexdigest()
        parser = _HrefParser()
        parser.feed(response.text)
        verification.link_present = any(
            _href_points_to_domain(href, target_domain) for href in parser.hrefs
        )
        verification.error = None
    except Exception as exc:
        verification.http_status = None
        verification.final_url = page.url
        verification.link_present = False
        verification.error = str(exc)[:2000]
    db.commit()
    return verification.link_present


def run_provider_proof(db: Session, settings: Settings) -> dict[str, Any]:
    """Run a deliberately tiny, cost-capped DataForSEO proof batch.

    This is never scheduled automatically. It validates bulk backlink summaries,
    detailed backlinks, source-page traffic and direct source-page link presence.
    """
    if not settings.link_hunter_enabled:
        raise DataForSEOError("Link Hunter feature flag is disabled")
    if not settings.dataforseo_enabled:
        raise DataForSEOError("DataForSEO credentials are not configured")

    already_checked = set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )
    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(250)
    ).all()
    targets = [drop.name for drop in recent_drops if drop.name not in already_checked][
        : settings.link_hunter_proof_batch_size
    ]

    counters: dict[str, Any] = {
        "targets": len(targets),
        "summary_calls": 0,
        "backlink_calls": 0,
        "traffic_calls": 0,
        "domains_with_live_backlinks": 0,
        "links_saved": 0,
        "source_pages_traffic_checked": 0,
        "source_links_verified": 0,
        "provider_cost_usd": 0.0,
        "cost_cap_hit": False,
        "errors": 0,
        "error_details": [],
    }
    if not targets:
        return counters

    client = DataForSEOClient(settings)
    domain_batches: list[tuple[Domain, Opportunity, list[SourceLink]]] = []

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
    except Exception as exc:
        counters["errors"] += 1
        counters["error_details"].append(f"bulk summary: {exc}")
        counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
        return counters

    for target in targets:
        if counters["provider_cost_usd"] >= settings.link_hunter_proof_max_cost_usd:
            counters["cost_cap_hit"] = True
            break
        summary = summary_map.get(_normalize_host(target), {})
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
            domain_batches.append((domain, opportunity, saved_links))
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
    if page_urls and counters["provider_cost_usd"] < settings.link_hunter_proof_max_cost_usd:
        try:
            traffic_response = _provider_call(
                db,
                endpoint="bulk_traffic_estimation",
                target=f"{len(page_urls)} source pages",
                callback=lambda: client.bulk_traffic_estimation(page_urls),
            )
            counters["traffic_calls"] = 1
            counters["provider_cost_usd"] += traffic_response.task_cost_usd
            for item in traffic_response.result.get("items") or []:
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
                        for item in traffic_response.result.get("items") or []
                        if _canonical_url(str(item.get("target") or ""))
                        == _canonical_url(page_url)
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
            _score_opportunity(opportunity, domain, links, 0, False)
            continue
        best_link = max(
            links,
            key=lambda link: (
                traffic_map.get(
                    _canonical_url(
                        (db.get(SourcePage, link.source_page_id) or SourcePage(url="")).url
                    ),
                    0,
                ),
                float(link.provider_rank or 0.0),
            ),
        )
        best_page = db.get(SourcePage, best_link.source_page_id)
        best_traffic = traffic_map.get(_canonical_url(best_page.url if best_page else ""), 0)
        opportunity.best_source_page_id = best_link.source_page_id
        verified = _verify_source_link(
            db,
            best_link,
            domain.name,
            settings.link_hunter_verify_timeout_seconds,
        )
        if verified:
            counters["source_links_verified"] += 1
        _score_opportunity(opportunity, domain, links, best_traffic, verified)
        db.commit()

    counters["provider_cost_usd"] = round(float(counters["provider_cost_usd"]), 6)
    return counters


def run_provider_proof_job() -> dict[str, Any]:
    """Run Phase B manually and record it in the shared operational run ledger."""
    settings = get_settings()
    with SessionLocal() as db:
        run = RunLog(job="link_hunter_proof", started_at=utcnow(), status="running", counters={})
        db.add(run)
        db.commit()
        db.refresh(run)
        try:
            counters = run_provider_proof(db, settings)
            run.status = "complete" if not counters.get("errors") else "partial"
            run.counters = counters
            run.finished_at = utcnow()
            db.commit()
            return counters
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            if run is not None:
                run.status = "failed"
                run.error = str(exc)[:2000]
                run.finished_at = utcnow()
                db.commit()
            raise
