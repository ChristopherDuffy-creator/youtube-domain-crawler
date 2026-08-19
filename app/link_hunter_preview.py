from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataforseo import estimate_provider_proof_max_cost_usd
from app.models import Domain, DroppedDomain, ProviderQuery, SourceLink


def _dataforseo_checked_targets(db: Session) -> set[str]:
    return set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )


def _commoncrawl_signals(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(ProviderQuery.target, ProviderQuery.row_count).where(
            ProviderQuery.provider == "commoncrawl",
            ProviderQuery.endpoint == "url_index",
            ProviderQuery.status == "complete",
        )
    ).all()
    return {target: int(row_count or 0) for target, row_count in rows}


def _free_exact_link_signals(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Domain.name, func.count(SourceLink.id))
        .join(SourceLink, SourceLink.domain_id == Domain.id)
        .where(SourceLink.provider_live.is_(True))
        .group_by(Domain.name)
    ).all()
    return {name: int(count or 0) for name, count in rows}


def select_provider_proof_targets(db: Session, settings: Settings) -> list[str]:
    """Select paid-proof targets using only cached/free evidence."""
    already_checked = _dataforseo_checked_targets(db)
    commoncrawl = _commoncrawl_signals(db)
    exact_links = _free_exact_link_signals(db)
    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(250)
    ).all()
    candidates = [drop for drop in recent_drops if drop.name not in already_checked]
    original_position = {drop.name: position for position, drop in enumerate(candidates)}

    exact_positive = [drop for drop in candidates if exact_links.get(drop.name, 0) > 0]
    exact_positive.sort(
        key=lambda drop: (
            -exact_links[drop.name],
            -commoncrawl.get(drop.name, 0),
            original_position[drop.name],
        )
    )
    exact_names = {drop.name for drop in exact_positive}
    historical_positive = [
        drop
        for drop in candidates
        if drop.name not in exact_names and commoncrawl.get(drop.name, 0) > 0
    ]
    historical_positive.sort(
        key=lambda drop: (
            -commoncrawl[drop.name],
            original_position[drop.name],
        )
    )
    promoted = exact_names | {drop.name for drop in historical_positive}
    unknown = [
        drop for drop in candidates if drop.name not in promoted and drop.name not in commoncrawl
    ]
    negative = [
        drop
        for drop in candidates
        if drop.name not in promoted and drop.name in commoncrawl and commoncrawl[drop.name] <= 0
    ]
    ordered = exact_positive + historical_positive + unknown + negative
    return [drop.name for drop in ordered[: settings.link_hunter_proof_batch_size]]


def build_provider_proof_preview(db: Session, settings: Settings) -> dict[str, Any]:
    """Describe the next provider proof without making any network/provider calls."""
    commoncrawl = _commoncrawl_signals(db)
    exact_links = _free_exact_link_signals(db)
    targets = select_provider_proof_targets(db, settings)
    estimated_max_cost = estimate_provider_proof_max_cost_usd(
        len(targets), settings.link_hunter_backlinks_per_domain
    )
    max_source_pages = len(targets) * settings.link_hunter_backlinks_per_domain
    target_cc = {target: commoncrawl.get(target) for target in targets}
    target_exact = {target: exact_links.get(target, 0) for target in targets}

    return {
        "targets": targets,
        "target_count": len(targets),
        "backlinks_per_domain": settings.link_hunter_backlinks_per_domain,
        "max_source_pages": max_source_pages,
        "estimated_max_cost_usd": estimated_max_cost,
        "configured_cost_cap_usd": settings.link_hunter_proof_max_cost_usd,
        "within_cost_cap": estimated_max_cost <= settings.link_hunter_proof_max_cost_usd,
        "dataforseo_configured": settings.dataforseo_enabled,
        "link_hunter_enabled": settings.link_hunter_enabled,
        "paid_requests_made": 0,
        "free_exact_link_domain_count": len(exact_links),
        "free_exact_link_targets": [target for target in targets if exact_links.get(target, 0) > 0],
        "target_free_exact_links": target_exact,
        "commoncrawl_signal_count": len(commoncrawl),
        "commoncrawl_positive_count": sum(1 for value in commoncrawl.values() if value > 0),
        "commoncrawl_positive_targets": [
            target for target in targets if (commoncrawl.get(target) or 0) > 0
        ],
        "target_commoncrawl_hits": target_cc,
    }
