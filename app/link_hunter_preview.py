from __future__ import annotations

import math
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataforseo import estimate_provider_proof_max_cost_usd
from app.domain_lifecycle import bought_domain_names
from app.models import (
    BacklinkSummary,
    Candidate,
    Domain,
    DroppedDomain,
    FetchVerification,
    LinkObservation,
    ProviderQuery,
    SourceLink,
    SourcePage,
    WebScreening,
)
from app.provider_budget import (
    effective_provider_daily_limit_usd,
    effective_provider_run_limit_usd,
    provider_daily_budget_snapshot,
)
from app.web_hunter_upgrade import (
    _cached_summary_signals,
    _source_focus_signals,
    _summary_rescue_points,
    traffic_first_rerank_summary_targets,
)

_BLOCKED_AVAILABILITY = {"registered", "aftermarket", "premium", "reserved"}
_RECENT_FALLBACK_POOL = 10_000
_SCREENED_PRIORITY_POOL = 50_000
_SQL_IN_CHUNK = 9_000


def _chunks(values: list[str], size: int = _SQL_IN_CHUNK):
    """Yield bounded chunks for the few candidate-name filters we need.

    PostgreSQL limits a statement to 65,535 bind parameters.  Keep a margin
    below that limit because these selectors are also used by production
    bootstrap code that may add predicates in the future.
    """
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _dropped_domains_for_names(db: Session, names: set[str]) -> list[DroppedDomain]:
    """Fetch dropped rows in bounded batches, preserving the old ordering."""
    if not names:
        return []
    rows: list[DroppedDomain] = []
    for chunk in _chunks(sorted(names)):
        rows.extend(
            db.scalars(
                select(DroppedDomain)
                .where(DroppedDomain.name.in_(chunk))
                .order_by(DroppedDomain.first_seen_at.desc())
            ).all()
        )
    return rows


def _checked_targets_for_names(
    db: Session,
    names: set[str],
    *,
    endpoint: str,
) -> set[str]:
    """Return completed provider targets from a bounded candidate pool only."""
    if not names:
        return set()
    checked: set[str] = set()
    for chunk in _chunks(sorted(names)):
        checked.update(
            db.scalars(
                select(ProviderQuery.target).where(
                    ProviderQuery.provider == "dataforseo",
                    ProviderQuery.endpoint == endpoint,
                    ProviderQuery.status == "complete",
                    ProviderQuery.target.in_(chunk),
                )
            ).all()
        )
    return checked


def _summary_selection_checked_endpoint() -> str:
    """Do not repay for summaries; cached winners remain in the deep queue."""
    return "bulk_backlink_summary"


def _blocked_screening_for_names(db: Session, names: set[str]) -> set[str]:
    """Return blocked screening rows from a bounded candidate pool only."""
    if not names:
        return set()
    blocked: set[str] = set()
    for chunk in _chunks(sorted(names)):
        blocked.update(
            db.scalars(
                select(WebScreening.domain_name).where(
                    WebScreening.status == "blocked",
                    WebScreening.domain_name.in_(chunk),
                )
            ).all()
        )
    return blocked


def _dataforseo_checked_targets(
    db: Session,
    names: set[str] | None = None,
) -> set[str]:
    if names is not None:
        return _checked_targets_for_names(
            db,
            names,
            endpoint="bulk_backlink_summary",
        )
    return set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )


def _dataforseo_deep_checked_targets(
    db: Session,
    names: set[str] | None = None,
) -> set[str]:
    """Domains whose expensive detailed backlink proof already completed."""
    if names is not None:
        return _checked_targets_for_names(
            db,
            names,
            endpoint="backlinks",
        )
    return set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "backlinks",
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


def _latest_live_observation_signals(db: Session) -> dict[str, dict[str, int | float]]:
    """Return only each link's newest direct observation.

    Historic positive fetches are useful audit evidence, but they must not keep
    winning paid proof priority after a newer observation has shown the link is
    gone.  This is intentionally a cached/local signal: it adds no external
    requests to the summary funnel.
    """
    latest_per_link = (
        select(
            LinkObservation.source_link_id.label("source_link_id"),
            func.max(LinkObservation.observed_at).label("observed_at"),
        )
        .group_by(LinkObservation.source_link_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Domain.name,
            LinkObservation.link_present,
            LinkObservation.clickable,
            LinkObservation.survival_days,
        )
        .join(SourceLink, SourceLink.domain_id == Domain.id)
        .join(
            latest_per_link,
            latest_per_link.c.source_link_id == SourceLink.id,
        )
        .join(
            LinkObservation,
            (LinkObservation.source_link_id == latest_per_link.c.source_link_id)
            & (LinkObservation.observed_at == latest_per_link.c.observed_at),
        )
    ).all()
    signals: dict[str, dict[str, int | float]] = {}
    for name, link_present, clickable, survival_days in rows:
        row = signals.setdefault(
            str(name),
            {
                "observed_live_links": 0,
                "clickable_live_links": 0,
                "max_observed_survival_days": 0.0,
            },
        )
        if link_present:
            row["observed_live_links"] = int(row["observed_live_links"]) + 1
            row["max_observed_survival_days"] = max(
                float(row["max_observed_survival_days"]),
                max(0.0, float(survival_days or 0.0)),
            )
        if link_present and clickable:
            row["clickable_live_links"] = int(row["clickable_live_links"]) + 1
    return signals


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


def _screening_signals(db: Session) -> dict[str, dict[str, int | float | str]]:
    rows = db.execute(
        select(
            WebScreening.domain_name,
            WebScreening.status,
            WebScreening.quality_score,
            WebScreening.risk_score,
        )
        .where(WebScreening.status != "blocked")
        .order_by(WebScreening.quality_score.desc(), WebScreening.id.asc())
        .limit(_SCREENED_PRIORITY_POOL)
    ).all()
    return {
        name: {
            "status": str(status),
            "quality_score": float(quality_score or 0.0),
            "risk_score": float(risk_score or 0.0),
        }
        for name, status, quality_score, risk_score in rows
    }


def _free_rank_context(db: Session) -> dict[str, Any]:
    return {
        "commoncrawl": _commoncrawl_signals(db),
        "exact_links": _free_exact_link_signals(db),
        "independent_sites": _free_independent_site_signals(db),
        "verified_links": _free_verified_link_signals(db),
        "observations": _latest_live_observation_signals(db),
        "youtube": _youtube_signals(db),
        "availability": _availability_signals(db),
        "screening": _screening_signals(db),
        "cached_summary": _cached_summary_signals(db),
        "source_focus": _source_focus_signals(db),
    }


def _free_preproof_score(
    *,
    exact_links: int,
    independent_sites: int,
    verified_links: int,
    observed_live_links: int,
    clickable_live_links: int,
    max_observed_survival_days: float,
    commoncrawl_hits: int,
    youtube_monthly_views: int,
    youtube_video_count: int,
    youtube_link_count: int,
    availability_status: str,
    screening_quality: float = 0.0,
    screening_risk: float = 0.0,
) -> float:
    """Score a proof target using only evidence already collected without DataForSEO spend."""
    exact_points = min(22.0, 8.0 * math.log2(1 + max(0, exact_links)))
    site_points = min(23.0, 6.0 * math.log2(1 + max(0, independent_sites)))
    verified_points = 0.0
    if verified_links > 0:
        verified_points = min(25.0, 20.0 + 2.5 * math.log2(max(1, verified_links)))
    observed_points = min(8.0, 3.0 * math.log2(1 + max(0, observed_live_links)))
    clickable_points = min(12.0, 5.0 * math.log2(1 + max(0, clickable_live_links)))
    survival_points = min(5.0, math.log2(1 + max(0.0, max_observed_survival_days)))
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
    screening_points = min(10.0, max(0.0, screening_quality) * 0.1)
    screening_penalty = min(10.0, max(0.0, screening_risk) * 0.1)
    return round(
        exact_points
        + site_points
        + verified_points
        + observed_points
        + clickable_points
        + survival_points
        + commoncrawl_points
        + youtube_points
        + availability_points
        + screening_points
        - screening_penalty,
        2,
    )


def _free_signal_row(name: str, context: dict[str, Any]) -> dict[str, int | float | str]:
    yt = context["youtube"].get(name, {})
    free_screen = context["screening"].get(name, {})
    observations = context["observations"].get(name, {})
    summary_signal = context.get("cached_summary", {}).get(name, {})
    focus_signal = context.get("source_focus", {}).get(name, {})
    return {
        "exact_links": int(context["exact_links"].get(name, 0) or 0),
        "independent_sites": int(context["independent_sites"].get(name, 0) or 0),
        "verified_links": int(context["verified_links"].get(name, 0) or 0),
        "observed_live_links": int(observations.get("observed_live_links", 0) or 0),
        "clickable_live_links": int(observations.get("clickable_live_links", 0) or 0),
        "max_observed_survival_days": float(observations.get("max_observed_survival_days", 0.0) or 0.0),
        "commoncrawl_hits": int(context["commoncrawl"].get(name, 0) or 0),
        "youtube_monthly_views": int(yt.get("monthly_views", 0) or 0),
        "youtube_video_count": int(yt.get("video_count", 0) or 0),
        "youtube_link_count": int(yt.get("link_count", 0) or 0),
        "availability": str(context["availability"].get(name, "unknown")),
        "screening_status": str(free_screen.get("status", "unscreened")),
        "screening_quality": float(free_screen.get("quality_score", 0.0) or 0.0),
        "screening_risk": float(free_screen.get("risk_score", 0.0) or 0.0),
        "summary_rescue_points": _summary_rescue_points(summary_signal),
        "cached_summary_rank": float(summary_signal.get("rank", 0.0) or 0.0),
        "cached_referring_pages": int(summary_signal.get("referring_pages", 0) or 0),
        "cached_referring_domains": int(summary_signal.get("referring_domains", 0) or 0),
        "source_focus_bonus": max(
            0.0,
            float(focus_signal.get("best_weight", 0.0) or 0.0),
        ),
        "source_focus_category": str(focus_signal.get("best_category", "") or ""),
        "source_focus_government": int(focus_signal.get("government", 0) or 0),
        "source_focus_academic": int(focus_signal.get("academic", 0) or 0),
        "source_focus_editorial": int(focus_signal.get("editorial", 0) or 0),
        "source_focus_community": int(focus_signal.get("community", 0) or 0),
    }


def _free_score_for_name(name: str, context: dict[str, Any]) -> tuple[float, dict[str, int | float | str]]:
    row = _free_signal_row(name, context)
    score = _free_preproof_score(
        exact_links=int(row["exact_links"]),
        independent_sites=int(row["independent_sites"]),
        verified_links=int(row["verified_links"]),
        observed_live_links=int(row["observed_live_links"]),
        clickable_live_links=int(row["clickable_live_links"]),
        max_observed_survival_days=float(row["max_observed_survival_days"]),
        commoncrawl_hits=int(row["commoncrawl_hits"]),
        youtube_monthly_views=int(row["youtube_monthly_views"]),
        youtube_video_count=int(row["youtube_video_count"]),
        youtube_link_count=int(row["youtube_link_count"]),
        availability_status=str(row["availability"]),
        screening_quality=float(row["screening_quality"]),
        screening_risk=float(row["screening_risk"]),
    )
    score = round(
        score + min(25.0, float(row["summary_rescue_points"])) + min(8.0, float(row["source_focus_bonus"])),
        2,
    )
    return score, row


def _rank_free_candidates(
    candidates: list[DroppedDomain],
    context: dict[str, Any],
) -> tuple[list[DroppedDomain], dict[str, float], dict[str, dict[str, int | float | str]]]:
    original_position = {drop.name: position for position, drop in enumerate(candidates)}

    scores: dict[str, float] = {}
    signals: dict[str, dict[str, int | float | str]] = {}
    for drop in candidates:
        score, row = _free_score_for_name(drop.name, context)
        signals[drop.name] = row
        scores[drop.name] = score

    ordered = sorted(
        candidates,
        key=lambda drop: (
            -scores[drop.name],
            -float(signals[drop.name]["summary_rescue_points"]),
            -float(signals[drop.name]["source_focus_bonus"]),
            -int(signals[drop.name]["verified_links"]),
            -int(signals[drop.name]["clickable_live_links"]),
            -int(signals[drop.name]["observed_live_links"]),
            -float(signals[drop.name]["max_observed_survival_days"]),
            -int(signals[drop.name]["independent_sites"]),
            -int(signals[drop.name]["exact_links"]),
            -int(signals[drop.name]["commoncrawl_hits"]),
            -int(signals[drop.name]["youtube_monthly_views"]),
            -float(signals[drop.name]["screening_quality"]),
            float(signals[drop.name]["screening_risk"]),
            original_position[drop.name],
        ),
    )
    return ordered, scores, signals


def _priority_candidate_names(context: dict[str, Any]) -> set[str]:
    names = {name for name, count in context["exact_links"].items() if int(count or 0) > 0}
    names.update(name for name, count in context["verified_links"].items() if int(count or 0) > 0)
    names.update(
        name
        for name, values in context["observations"].items()
        if int(values.get("observed_live_links", 0) or 0) > 0
    )
    names.update(name for name, count in context["commoncrawl"].items() if int(count or 0) > 0)
    names.update(
        name for name, values in context["youtube"].items() if int(values.get("monthly_views", 0)) > 0
    )
    names.update(context["screening"].keys())
    return names


def _select_provider_summary_targets_with_ranking(
    db: Session, settings: Settings
) -> tuple[
    list[str],
    dict[str, float],
    dict[str, dict[str, int | float | str]],
    int,
    dict[str, Any],
]:
    context = _free_rank_context(db)
    availability: dict[str, str] = context["availability"]

    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(_RECENT_FALLBACK_POOL)
    ).all()
    priority_names = _priority_candidate_names(context)
    priority_drops = _dropped_domains_for_names(db, priority_names)
    priority_drops.sort(key=lambda drop: drop.first_seen_at, reverse=True)

    candidate_map = {drop.name: drop for drop in recent_drops}
    for drop in priority_drops:
        candidate_map.setdefault(drop.name, drop)
    pooled = list(candidate_map.values())
    pooled_names = {drop.name for drop in pooled}
    bought_names = bought_domain_names(db, pooled_names)
    already_checked = _checked_targets_for_names(
        db,
        pooled_names,
        endpoint=_summary_selection_checked_endpoint(),
    )
    unchecked = [
        drop for drop in pooled if drop.name not in already_checked and drop.name not in bought_names
    ]

    locally_blocked = _blocked_screening_for_names(
        db,
        {drop.name for drop in unchecked},
    )

    blocked_names = {
        drop.name
        for drop in unchecked
        if availability.get(drop.name, "unknown") in _BLOCKED_AVAILABILITY or drop.name in locally_blocked
    }
    candidates = [drop for drop in unchecked if drop.name not in blocked_names]
    ordered, scores, signals = _rank_free_candidates(candidates, context)
    targets = [drop.name for drop in ordered[: settings.link_hunter_summary_batch_size]]
    return targets, scores, signals, len(blocked_names), context


def select_provider_summary_targets(db: Session, settings: Settings) -> list[str]:
    """Select the large cheap-screen batch using only cached/free evidence."""
    targets, _, _, _, _ = _select_provider_summary_targets_with_ranking(db, settings)
    return targets


def select_provider_summary_targets_with_ranking(
    db: Session, settings: Settings
) -> tuple[
    list[str],
    dict[str, float],
    dict[str, dict[str, int | float | str]],
    int,
    dict[str, Any],
]:
    """Return the summary screen and its cached/free ranking evidence."""
    return _select_provider_summary_targets_with_ranking(db, settings)


def _summary_record_payload(summary: BacklinkSummary) -> dict[str, Any]:
    payload = dict(summary.raw_summary or {})
    payload.setdefault("backlinks", int(summary.backlinks or 0))
    payload.setdefault("referring_pages", int(summary.referring_pages or 0))
    payload.setdefault("referring_domains", int(summary.referring_domains or 0))
    payload.setdefault("referring_main_domains", int(summary.referring_main_domains or 0))
    payload.setdefault("rank", float(summary.rank or 0.0))
    return payload


def select_cached_deep_proof_targets_with_ranking(
    db: Session,
    settings: Settings,
    *,
    limit: int | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[
    list[str],
    dict[str, float],
    dict[str, float],
    dict[str, float],
    dict[str, dict[str, int | float | str]],
]:
    """Rank every cached live summary that has never received detailed proof.

    This is the permanent winner queue: a name is not discarded merely because
    it failed to make the top five in the same batch that first summarised it.
    """
    rank_context = context or _free_rank_context(db)
    detailed_proof_exists = exists(
        select(ProviderQuery.id).where(
            ProviderQuery.provider == "dataforseo",
            ProviderQuery.endpoint == "backlinks",
            ProviderQuery.status == "complete",
            ProviderQuery.target == Domain.name,
        )
    )
    blocked_screening_exists = exists(
        select(WebScreening.id).where(
            WebScreening.status == "blocked",
            WebScreening.domain_name == Domain.name,
        )
    )
    rows = db.execute(
        select(BacklinkSummary, Domain)
        .join(Domain, Domain.id == BacklinkSummary.domain_id)
        .where(
            BacklinkSummary.provider == "dataforseo",
            BacklinkSummary.referring_pages > 0,
            Domain.excluded_reason.is_(None),
            ~detailed_proof_exists,
            ~blocked_screening_exists,
            or_(
                Domain.availability_status.is_(None),
                ~Domain.availability_status.in_(_BLOCKED_AVAILABILITY),
            ),
        )
    ).yield_per(1_000)
    if not rows:
        return [], {}, {}, {}, {}

    combined_scores: dict[str, float] = {}
    summary_scores: dict[str, float] = {}
    free_scores: dict[str, float] = {}
    free_signals: dict[str, dict[str, int | float | str]] = {}
    sort_rows: list[tuple[str, float, float, float, int, int]] = []
    for summary, domain in rows:
        name = domain.name
        free_score, signal = _free_score_for_name(name, rank_context)
        summary_score = _summary_signal_score(_summary_record_payload(summary))
        combined = round(free_score + summary_score, 2)
        free_scores[name] = free_score
        free_signals[name] = signal
        summary_scores[name] = summary_score
        combined_scores[name] = combined
        sort_rows.append(
            (
                name,
                combined,
                free_score,
                summary_score,
                int(summary.referring_main_domains or summary.referring_domains or 0),
                int(summary.referring_pages or 0),
            )
        )

    sort_rows.sort(key=lambda row: (-row[1], -row[2], -row[3], -row[4], -row[5], row[0]))
    ordered = [row[0] for row in sort_rows]
    if limit is not None:
        ordered = ordered[: max(0, limit)]
    return ordered, combined_scores, summary_scores, free_scores, free_signals


def select_provider_proof_targets(db: Session, settings: Settings) -> list[str]:
    """Return the provisional deep targets before paid summary evidence exists.

    Kept as a compatibility helper for callers that only need the zero-cost
    preview. The paid workflow always reranks the full summary screen before it
    chooses its actual deep-proof targets.
    """
    return select_provider_summary_targets(db, settings)[: settings.link_hunter_proof_batch_size]


def _summary_signal_score(summary: dict[str, Any]) -> float:
    """Turn cheap aggregate backlink evidence into a deliberately capped score."""
    referring_pages = max(0, int(summary.get("referring_pages") or 0))
    referring_domains = max(
        0,
        int(summary.get("referring_main_domains") or summary.get("referring_domains") or 0),
    )
    backlinks = max(0, int(summary.get("backlinks") or 0))
    rank = max(0.0, min(100.0, float(summary.get("rank") or 0.0)))

    rank_points = min(8.0, rank * 0.08)
    domain_points = min(10.0, 2.2 * math.log2(1 + referring_domains))
    page_points = min(7.0, 1.4 * math.log2(1 + referring_pages))
    backlink_points = min(3.0, 0.5 * math.log2(1 + backlinks))
    diversity_points = min(2.0, 2.0 * referring_domains / referring_pages) if referring_pages else 0.0
    return round(
        rank_points + domain_points + page_points + backlink_points + diversity_points,
        2,
    )


def rerank_summary_screen_targets(
    targets: list[str],
    free_scores: dict[str, float],
    free_signals: dict[str, dict[str, int | float | str]],
    summaries: dict[str, dict[str, Any]],
    deep_limit: int,
) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """Choose detailed-proof targets using the canonical traffic-first policy."""
    return traffic_first_rerank_summary_targets(
        targets,
        free_scores,
        free_signals,
        summaries,
        deep_limit,
    )


def _proof_readiness(
    *,
    settings: Settings,
    targets: list[str],
    work_available: bool,
    estimated_max_cost_usd: float,
    free_positive_count: int,
    daily_budget: dict[str, float | int | str],
) -> dict[str, Any]:
    """Return zero-cost activation diagnostics without exposing secrets."""
    run_cap = effective_provider_run_limit_usd(settings)
    blockers: list[str] = []
    warnings: list[str] = []
    if not settings.dataforseo_enabled:
        blockers.append("dataforseo_credentials_not_configured")
    if not work_available:
        blockers.append("no_queued_work")
    if estimated_max_cost_usd > run_cap:
        blockers.append("estimated_cost_exceeds_configured_cap")
    # The database reservation holds the full configured per-run envelope.
    # Stop before Railway paid mode is enabled when that reservation cannot fit.
    if work_available and float(daily_budget.get("remaining_usd") or 0.0) + 1e-9 < run_cap:
        blockers.append("daily_budget_exhausted")
    if settings.link_hunter_enabled:
        warnings.append("link_hunter_already_enabled")
    if targets and free_positive_count == 0:
        warnings.append("no_free_positive_signal_in_new_summary_batch")
    return {
        "ready_for_controlled_proof": not blockers,
        "activation_blockers": blockers,
        "activation_warnings": warnings,
        "requires_explicit_spend_approval": True,
        "credentials_present": settings.dataforseo_enabled,
        "credentials_exposed": False,
    }


def _has_meaningful_free_signal(signal: dict[str, int | float | str]) -> bool:
    return any(
        int(signal.get(key, 0) or 0) > 0
        for key in (
            "exact_links",
            "independent_sites",
            "verified_links",
            "observed_live_links",
            "clickable_live_links",
            "commoncrawl_hits",
            "youtube_monthly_views",
        )
    )


def build_provider_proof_preview(db: Session, settings: Settings) -> dict[str, Any]:
    """Describe the next provider proof without making any network/provider calls."""
    targets, scores, signals, blocked_count, context = _select_provider_summary_targets_with_ranking(
        db, settings
    )
    cached_targets, cached_combined, _, _, _ = select_cached_deep_proof_targets_with_ranking(
        db,
        settings,
        context=context,
    )
    commoncrawl: dict[str, int] = context["commoncrawl"]
    exact_links: dict[str, int] = context["exact_links"]

    # Preview a global queue: cached paid summaries compete with newly queued
    # names. Fresh names only have free evidence until the bulk summary returns.
    provisional_scores = dict(cached_combined)
    for target in targets:
        provisional_scores[target] = float(scores.get(target, 0.0))
    provisional_pool = list(dict.fromkeys([*cached_targets, *targets]))
    provisional_position = {target: index for index, target in enumerate(provisional_pool)}
    provisional_pool.sort(
        key=lambda target: (
            -provisional_scores.get(target, 0.0),
            provisional_position[target],
        )
    )
    deep_target_count = min(len(provisional_pool), settings.link_hunter_proof_batch_size)
    provisional_deep_targets = provisional_pool[:deep_target_count]
    estimated_max_cost = estimate_provider_proof_max_cost_usd(
        len(targets), deep_target_count, settings.link_hunter_backlinks_per_domain
    )
    max_source_pages = deep_target_count * settings.link_hunter_backlinks_per_domain
    target_cc = {target: commoncrawl.get(target) for target in targets}
    target_exact = {target: exact_links.get(target, 0) for target in targets}
    free_positive_count = sum(1 for target in targets if _has_meaningful_free_signal(signals.get(target, {})))
    daily_budget = provider_daily_budget_snapshot(db, settings)
    work_available_count = len(targets) + len(cached_targets)

    run_cap = effective_provider_run_limit_usd(settings)
    daily_cap = effective_provider_daily_limit_usd(settings)
    return {
        "targets": targets,
        "target_count": len(targets),
        "summary_targets": targets,
        "summary_target_count": len(targets),
        "summary_targets_sample": targets[:10],
        "cached_deep_target_count": len(cached_targets),
        "cached_deep_targets_sample": cached_targets[:10],
        "work_available_count": work_available_count,
        "deep_proof_target_count": deep_target_count,
        "provisional_deep_targets": provisional_deep_targets,
        "selection_strategy": "free_preproof_score",
        "deep_selection_strategy": "global_cached_summary_winner_queue",
        "target_free_scores": {target: scores.get(target, 0.0) for target in targets},
        "target_free_rank_signals": {target: signals.get(target, {}) for target in targets},
        "known_unavailable_targets_skipped": blocked_count,
        "backlinks_per_domain": settings.link_hunter_backlinks_per_domain,
        "max_source_pages": max_source_pages,
        "estimated_max_cost_usd": estimated_max_cost,
        "configured_cost_cap_usd": run_cap,
        "daily_cost_cap_usd": daily_cap,
        "daily_budget": daily_budget,
        "within_cost_cap": estimated_max_cost <= run_cap,
        "dataforseo_configured": settings.dataforseo_enabled,
        "link_hunter_enabled": settings.link_hunter_enabled,
        "paid_requests_made": 0,
        "free_exact_link_domain_count": len(exact_links),
        "free_exact_link_targets": [target for target in targets if exact_links.get(target, 0) > 0],
        "target_free_exact_links": target_exact,
        "commoncrawl_signal_count": len(commoncrawl),
        "commoncrawl_positive_count": sum(1 for value in commoncrawl.values() if value > 0),
        "commoncrawl_positive_targets": [target for target in targets if (commoncrawl.get(target) or 0) > 0],
        "target_commoncrawl_hits": target_cc,
        **_proof_readiness(
            settings=settings,
            targets=targets,
            work_available=bool(work_available_count),
            estimated_max_cost_usd=estimated_max_cost,
            free_positive_count=free_positive_count,
            daily_budget=daily_budget,
        ),
    }
