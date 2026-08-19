from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataforseo import estimate_provider_proof_max_cost_usd
from app.models import DroppedDomain, ProviderQuery


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


def select_provider_proof_targets(db: Session, settings: Settings) -> list[str]:
    """Select the next paid-proof targets using only cached/free evidence."""
    already_checked = _dataforseo_checked_targets(db)
    signals = _commoncrawl_signals(db)
    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(250)
    ).all()
    candidates = [drop for drop in recent_drops if drop.name not in already_checked]
    original_position = {drop.name: position for position, drop in enumerate(candidates)}

    positive = [drop for drop in candidates if signals.get(drop.name, 0) > 0]
    positive.sort(
        key=lambda drop: (
            -signals[drop.name],
            original_position[drop.name],
        )
    )
    unknown = [drop for drop in candidates if drop.name not in signals]
    negative = [drop for drop in candidates if drop.name in signals and signals[drop.name] <= 0]
    ordered = positive + unknown + negative
    return [drop.name for drop in ordered[: settings.link_hunter_proof_batch_size]]


def _proof_readiness(
    *,
    settings: Settings,
    targets: list[str],
    estimated_max_cost_usd: float,
    positive_target_count: int,
) -> dict[str, Any]:
    """Return zero-cost activation diagnostics without reading or returning secrets."""
    blockers: list[str] = []
    warnings: list[str] = []

    if not settings.dataforseo_enabled:
        blockers.append("dataforseo_credentials_not_configured")
    if not targets:
        blockers.append("no_unchecked_targets")
    if estimated_max_cost_usd > settings.link_hunter_proof_max_cost_usd:
        blockers.append("estimated_cost_exceeds_configured_cap")
    if settings.link_hunter_enabled:
        warnings.append("link_hunter_already_enabled")
    if targets and positive_target_count == 0:
        warnings.append("no_commoncrawl_positive_target_in_batch")

    return {
        "ready_for_controlled_proof": not blockers,
        "activation_blockers": blockers,
        "activation_warnings": warnings,
        "requires_explicit_spend_approval": True,
        "credentials_present": settings.dataforseo_enabled,
        "credentials_exposed": False,
    }


def build_provider_proof_preview(db: Session, settings: Settings) -> dict[str, Any]:
    """Describe the next provider proof without making any network/provider calls."""
    signals = _commoncrawl_signals(db)
    targets = select_provider_proof_targets(db, settings)
    estimated_max_cost = estimate_provider_proof_max_cost_usd(
        len(targets), settings.link_hunter_backlinks_per_domain
    )
    max_source_pages = len(targets) * settings.link_hunter_backlinks_per_domain
    target_signals = {target: signals.get(target) for target in targets}
    positive_targets = [target for target in targets if (signals.get(target) or 0) > 0]
    readiness = _proof_readiness(
        settings=settings,
        targets=targets,
        estimated_max_cost_usd=estimated_max_cost,
        positive_target_count=len(positive_targets),
    )

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
        "commoncrawl_signal_count": len(signals),
        "commoncrawl_positive_count": sum(1 for value in signals.values() if value > 0),
        "commoncrawl_positive_targets": positive_targets,
        "target_commoncrawl_hits": target_signals,
        **readiness,
    }
