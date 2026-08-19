from __future__ import annotations

import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataforseo import estimate_provider_proof_max_cost_usd
from app.models import (
    Candidate,
    Domain,
    DroppedDomain,
    FetchVerification,
    ProviderQuery,
    SourceLink,
    SourcePage,
)


_BLOCKED_AVAILABILITY = {"registered", "aftermarket", "premium"}


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


def _free_independent_site_signals(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Domain.name, func.count(func.distinct(SourcePage.site_id)))
        .join(SourceLink, SourceLink.domain_id == Domain.id)
        .join(SourcePage, SourcePage.id == SourceLink.source_page_id)
        .where(SourceLink.provider_live.is_(True))
        .group_by(Domain.name)
    ).all()
    return {name: int(count or 0) for name, count in rows}


def _free_verified_link_signals(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(Domain.name, func.count(FetchVerification.id))
        .join(SourceLink, SourceLink.domain_id == Domain.id)
        .join(FetchVerification, FetchVerification.source_link_id == SourceLink.id)
        .where(FetchVerification.link_present.is_(True))
        .group_by(Domain.name)
    ).all()
    return {name: int(count or 0) for name, count in rows}


def _youtube_signals(db: Session) -> dict[str, dict[str, float | int]]:
    rows = db.execute(
        select(
            Domain.name,
            Candidate.monthly_views,
            Candidate.video_count,
            Candidate.link_count,
        )
        .join(Candidate, Candidate.domain_id == Domain.id)
        .where(Candidate.monthly_views > 0)
    ).all()
    return {
        name: {
            "monthly_views": int(monthly_views or 0),
            "video_count": int(video_count or 0),
            "link_count": int(link_count or 0),
        }
        for name, monthly_views, video_count, link_count in rows
    }


def _availability_signals(db: Session) -> dict[str, str]:
    rows = db.execute(select(Domain.name, Domain.availability_status)).all()
    return {name: str(status or "unknown") for name, status in rows}


def _free_preproof_score(
    *,
    exact_links: int,
    independent_sites: int,
    verified_links: int,
    commoncrawl_hits: int,
    youtube_monthly_views: int,
    availability_status: str,
) -> float:
    """Score a proof target using only evidence already collected without DataForSEO spend."""
    exact_points = min(22.0, 8.0 * math.log2(1 + max(0, exact_links)))
    site_points = min(23.0, 6.0 * math.log2(1 + max(0, independent_sites)))
    verified_points = 0.0
    if verified_links > 0:
        verified_points = min(25.0, 20.0 + 2.5 * math.log2(max(1, verified_links)))
    commoncrawl_points = min(15.0, 4.0 * math.log2(1 + max(0, commoncrawl_hits)))
    youtube_points = min(10.0, 2.0 * math.log10(1 + max(0, youtube_monthly_views)))
    availability_points = {
        "available": 5.0,
        "likely_available": 4.0,
        "unknown": 0.0,
        "conflicting": -2.0,
    }.get(availability_status, 0.0)
    return round(
        exact_points
        + site_points
        + verified_points
        + commoncrawl_points
        + youtube_points
        + availability_points,
        2,
    )


def _rank_free_candidates(
    db: Session,
    candidates: list[DroppedDomain],
) -> tuple[list[DroppedDomain], dict[str, float], dict[str, dict[str, int | str]]]:
    commoncrawl = _commoncrawl_signals(db)
    exact_links = _free_exact_link_signals(db)
    independent_sites = _free_independent_site_signals(db)
    verified_links = _free_verified_link_signals(db)
    youtube = _youtube_signals(db)
    availability = _availability_signals(db)
    original_position = {drop.name: position for position, drop in enumerate(candidates)}

    scores: dict[str, float] = {}
    signals: dict[str, dict[str, int | str]] = {}
    for drop in candidates:
        yt = youtube.get(drop.name, {})
        row = {
            "exact_links": exact_links.get(drop.name, 0),
            "independent_sites": independent_sites.get(drop.name, 0),
            "verified_links": verified_links.get(drop.name, 0),
            "commoncrawl_hits": commoncrawl.get(drop.name, 0),
            "youtube_monthly_views": int(yt.get("monthly_views", 0)),
            "youtube_video_count": int(yt.get("video_count", 0)),
            "youtube_link_count": int(yt.get("link_count", 0)),
            "availability": availability.get(drop.name, "unknown"),
        }
        signals[drop.name] = row
        scores[drop.name] = _free_preproof_score(
            exact_links=int(row["exact_links"]),
            independent_sites=int(row["independent_sites"]),
            verified_links=int(row["verified_links"]),
            commoncrawl_hits=int(row["commoncrawl_hits"]),
            youtube_monthly_views=int(row["youtube_monthly_views"]),
            availability_status=str(row["availability"]),
        )

    ordered = sorted(
        candidates,
        key=lambda drop: (
            -scores[drop.name],
            -int(signals[drop.name]["verified_links"]),
            -int(signals[drop.name]["independent_sites"]),
            -int(signals[drop.name]["exact_links"]),
            -int(signals[drop.name]["commoncrawl_hits"]),
            original_position[drop.name],
        ),
    )
    return ordered, scores, signals


def _select_provider_proof_targets_with_ranking(
    db: Session, settings: Settings
) -> tuple[list[str], dict[str, float], dict[str, dict[str, int | str]], int]:
    already_checked = _dataforseo_checked_targets(db)
    availability = _availability_signals(db)
    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(250)
    ).all()
    unchecked = [drop for drop in recent_drops if drop.name not in already_checked]
    blocked = [
        drop for drop in unchecked if availability.get(drop.name, "unknown") in _BLOCKED_AVAILABILITY
    ]
    candidates = [drop for drop in unchecked if drop not in blocked]
    ordered, scores, signals = _rank_free_candidates(db, candidates)
    targets = [drop.name for drop in ordered[: settings.link_hunter_proof_batch_size]]
    return targets, scores, signals, len(blocked)


def select_provider_proof_targets(db: Session, settings: Settings) -> list[str]:
    """Select the highest-ranked paid-proof targets using only cached/free evidence."""
    targets, _, _, _ = _select_provider_proof_targets_with_ranking(db, settings)
    return targets


def _proof_readiness(
    *, settings: Settings, targets: list[str], estimated_max_cost_usd: float, free_positive_count: int
) -> dict[str, Any]:
    """Return zero-cost activation diagnostics without exposing secrets."""
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
    if targets and free_positive_count == 0:
        warnings.append("no_free_positive_signal_in_batch")
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
    commoncrawl = _commoncrawl_signals(db)
    exact_links = _free_exact_link_signals(db)
    targets, scores, signals, blocked_count = _select_provider_proof_targets_with_ranking(db, settings)
    estimated_max_cost = estimate_provider_proof_max_cost_usd(
        len(targets), settings.link_hunter_backlinks_per_domain
    )
    max_source_pages = len(targets) * settings.link_hunter_backlinks_per_domain
    target_cc = {target: commoncrawl.get(target) for target in targets}
    target_exact = {target: exact_links.get(target, 0) for target in targets}
    free_positive_count = sum(
        1
        for target in targets
        if exact_links.get(target, 0) > 0 or (commoncrawl.get(target) or 0) > 0
    )

    return {
        "targets": targets,
        "target_count": len(targets),
        "selection_strategy": "free_preproof_score",
        "target_free_scores": {target: scores.get(target, 0.0) for target in targets},
        "target_free_rank_signals": {target: signals.get(target, {}) for target in targets},
        "known_unavailable_targets_skipped": blocked_count,
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
        **_proof_readiness(
            settings=settings,
            targets=targets,
            estimated_max_cost_usd=estimated_max_cost,
            free_positive_count=free_positive_count,
        ),
    }
