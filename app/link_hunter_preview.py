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
_RECENT_FALLBACK_POOL = 1000


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


def _youtube_signals(db: Session) -> dict[str, dict[str, int]]:
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


def _free_rank_context(db: Session) -> dict[str, Any]:
    return {
        "commoncrawl": _commoncrawl_signals(db),
        "exact_links": _free_exact_link_signals(db),
        "independent_sites": _free_independent_site_signals(db),
        "verified_links": _free_verified_link_signals(db),
        "youtube": _youtube_signals(db),
        "availability": _availability_signals(db),
    }


def _free_preproof_score(
    *,
    exact_links: int,
    independent_sites: int,
    verified_links: int,
    commoncrawl_hits: int,
    youtube_monthly_views: int,
    youtube_video_count: int,
    youtube_link_count: int,
    availability_status: str,
) -> float:
    """Score a proof target using only evidence already collected without DataForSEO spend."""
    exact_points = min(22.0, 8.0 * math.log2(1 + max(0, exact_links)))
    site_points = min(23.0, 6.0 * math.log2(1 + max(0, independent_sites)))
    verified_points = 0.0
    if verified_links > 0:
        verified_points = min(25.0, 20.0 + 2.5 * math.log2(max(1, verified_links)))
    commoncrawl_points = min(15.0, 4.0 * math.log2(1 + max(0, commoncrawl_hits)))
    youtube_points = min(
        12.0,
        1.6 * math.log10(1 + max(0, youtube_monthly_views))
        + 1.2 * math.log2(1 + max(0, youtube_video_count))
        + 0.6 * math.log2(1 + max(0, youtube_link_count)),
    )
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
    candidates: list[DroppedDomain],
    context: dict[str, Any],
) -> tuple[list[DroppedDomain], dict[str, float], dict[str, dict[str, int | str]]]:
    commoncrawl: dict[str, int] = context["commoncrawl"]
    exact_links: dict[str, int] = context["exact_links"]
    independent_sites: dict[str, int] = context["independent_sites"]
    verified_links: dict[str, int] = context["verified_links"]
    youtube: dict[str, dict[str, int]] = context["youtube"]
    availability: dict[str, str] = context["availability"]
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
            youtube_video_count=int(row["youtube_video_count"]),
            youtube_link_count=int(row["youtube_link_count"]),
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
            -int(signals[drop.name]["youtube_monthly_views"]),
            original_position[drop.name],
        ),
    )
    return ordered, scores, signals


def _priority_candidate_names(context: dict[str, Any]) -> set[str]:
    names = {
        name for name, count in context["exact_links"].items() if int(count or 0) > 0
    }
    names.update(
        name for name, count in context["verified_links"].items() if int(count or 0) > 0
    )
    names.update(
        name for name, count in context["commoncrawl"].items() if int(count or 0) > 0
    )
    names.update(
        name
        for name, values in context["youtube"].items()
        if int(values.get("monthly_views", 0)) > 0
    )
    return names


def _select_provider_proof_targets_with_ranking(
    db: Session, settings: Settings
) -> tuple[
    list[str],
    dict[str, float],
    dict[str, dict[str, int | str]],
    int,
    dict[str, Any],
]:
    already_checked = _dataforseo_checked_targets(db)
    context = _free_rank_context(db)
    availability: dict[str, str] = context["availability"]

    recent_drops = db.scalars(
        select(DroppedDomain)
        .order_by(DroppedDomain.first_seen_at.desc())
        .limit(_RECENT_FALLBACK_POOL)
    ).all()
    priority_names = _priority_candidate_names(context)
    priority_drops: list[DroppedDomain] = []
    if priority_names:
        priority_drops = db.scalars(
            select(DroppedDomain)
            .where(DroppedDomain.name.in_(priority_names))
            .order_by(DroppedDomain.first_seen_at.desc())
        ).all()

    candidate_map = {drop.name: drop for drop in recent_drops}
    for drop in priority_drops:
        candidate_map.setdefault(drop.name, drop)
    pooled = list(candidate_map.values())
    unchecked = [drop for drop in pooled if drop.name not in already_checked]

    blocked_names = {
        drop.name
        for drop in unchecked
        if availability.get(drop.name, "unknown") in _BLOCKED_AVAILABILITY
    }
    candidates = [drop for drop in unchecked if drop.name not in blocked_names]
    ordered, scores, signals = _rank_free_candidates(candidates, context)
    targets = [drop.name for drop in ordered[: settings.link_hunter_proof_batch_size]]
    return targets, scores, signals, len(blocked_names), context


def select_provider_proof_targets(db: Session, settings: Settings) -> list[str]:
    """Select the highest-ranked paid-proof targets using only cached/free evidence."""
    targets, _, _, _, _ = _select_provider_proof_targets_with_ranking(db, settings)
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


def _has_meaningful_free_signal(signal: dict[str, int | str]) -> bool:
    return any(
        int(signal.get(key, 0) or 0) > 0
        for key in (
            "exact_links",
            "independent_sites",
            "verified_links",
            "commoncrawl_hits",
            "youtube_monthly_views",
        )
    )


def build_provider_proof_preview(db: Session, settings: Settings) -> dict[str, Any]:
    """Describe the next provider proof without making any network/provider calls."""
    targets, scores, signals, blocked_count, context = _select_provider_proof_targets_with_ranking(
        db, settings
    )
    commoncrawl: dict[str, int] = context["commoncrawl"]
    exact_links: dict[str, int] = context["exact_links"]
    estimated_max_cost = estimate_provider_proof_max_cost_usd(
        len(targets), settings.link_hunter_backlinks_per_domain
    )
    max_source_pages = len(targets) * settings.link_hunter_backlinks_per_domain
    target_cc = {target: commoncrawl.get(target) for target in targets}
    target_exact = {target: exact_links.get(target, 0) for target in targets}
    free_positive_count = sum(
        1 for target in targets if _has_meaningful_free_signal(signals.get(target, {}))
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
