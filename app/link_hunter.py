from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.dataforseo import DataForSEOClient, DataForSEOError
from app.models import (
    Domain,
    DroppedDomain,
    Opportunity,
    ProviderQuery,
    RunLog,
    SourceLink,
    SourcePage,
    SourceSite,
    utcnow,
)


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
    hostname = str(item.get("domain_from") or "").strip().lower()
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


def _save_opportunity(
    db: Session,
    domain: Domain,
    summary: dict[str, Any],
    saved_links: list[SourceLink],
) -> None:
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


def run_provider_proof(db: Session, settings: Settings) -> dict[str, Any]:
    """Run a deliberately tiny, cost-capped DataForSEO proof batch.

    This is not scheduled automatically. It exists for Phase B so we can inspect
    real returned backlinks before enabling the production Link Hunter pipeline.
    """
    if not settings.link_hunter_enabled:
        raise DataForSEOError("Link Hunter feature flag is disabled")
    if not settings.dataforseo_enabled:
        raise DataForSEOError("DataForSEO credentials are not configured")

    already_checked = set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "backlinks_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )
    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(100)
    ).all()
    targets = [drop.name for drop in recent_drops if drop.name not in already_checked][
        : settings.link_hunter_proof_batch_size
    ]

    client = DataForSEOClient(settings)
    counters: dict[str, Any] = {
        "targets": len(targets),
        "summary_calls": 0,
        "backlink_calls": 0,
        "domains_with_live_backlinks": 0,
        "links_saved": 0,
        "provider_cost_usd": 0.0,
        "errors": 0,
        "error_details": [],
    }

    for target in targets:
        try:
            summary_response = _provider_call(
                db,
                endpoint="backlinks_summary",
                target=target,
                callback=lambda target=target: client.backlink_summary(target),
            )
            counters["summary_calls"] += 1
            counters["provider_cost_usd"] += summary_response.task_cost_usd
            summary = summary_response.result
            if int(summary.get("referring_pages") or 0) <= 0:
                continue

            counters["domains_with_live_backlinks"] += 1
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
            _save_opportunity(db, domain, summary, saved_links)
            db.commit()
            counters["links_saved"] += len(saved_links)
        except Exception as exc:
            db.rollback()
            counters["errors"] += 1
            if len(counters["error_details"]) < 5:
                counters["error_details"].append(f"{target}: {exc}")

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
