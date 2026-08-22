from __future__ import annotations

"""Traffic-first production helpers for the web-wide Link Hunter.

Backlink volume remains useful evidence, but it is deliberately secondary to
verified live links, source-page traffic, modelled clicks and revenue.  The
crawler's goal is to find domains that can receive monetisable humans now, not
merely domains that accumulated many historical backlinks.
"""

import math
from dataclasses import replace
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Domain,
    LinkObservation,
    Opportunity,
    OpportunityEconomics,
    SourceLink,
)


def traffic_first_projection(
    original: Callable[..., Any],
    opportunity: Opportunity,
    domain: Domain,
    links: list[SourceLink],
    *,
    traffic: int,
    verified: bool,
    evidence_score: float,
    clickability_score: float = 0.0,
    screening_risk: float = 0.0,
):
    """Re-score an existing economic projection around real traffic evidence."""
    projection = original(
        opportunity,
        domain,
        links,
        traffic=traffic,
        verified=verified,
        evidence_score=evidence_score,
        clickability_score=clickability_score,
        screening_risk=screening_risk,
    )

    traffic = max(0, int(traffic or 0))
    clicks = max(0, int(projection.expected_clicks_monthly or 0))
    revenue_high = max(0.0, float(projection.monthly_revenue_high_usd or 0.0))
    risk = max(0.0, min(100.0, float(projection.risk_score or 0.0)))

    traffic_points = min(28.0, 7.5 * math.log10(traffic + 1))
    click_points = min(22.0, 10.0 * math.log10(clicks + 1))
    revenue_points = min(22.0, 10.0 * math.log10(revenue_high + 1.0))
    evidence_bonus = min(10.0, max(0.0, evidence_score) * 0.10)
    verified_points = 10.0 if verified else 0.0
    clickability_points = (
        min(5.0, max(0.0, min(100.0, clickability_score)) / 20.0)
        if verified
        else 0.0
    )
    availability_points = {
        "available": 3.0,
        "likely_available": 2.0,
        "conflicting": 0.5,
    }.get(str(domain.availability_status or "unknown"), 0.0)
    confidence_points = min(5.0, max(0.0, float(projection.confidence or 0.0)) * 5.0)
    risk_penalty = risk * 0.10

    buy_score = max(
        0.0,
        min(
            100.0,
            traffic_points
            + click_points
            + revenue_points
            + evidence_bonus
            + verified_points
            + clickability_points
            + availability_points
            + confidence_points
            - risk_penalty,
        ),
    )

    # A backlink asset can still be interesting SEO evidence, but it is not a
    # traffic-buy candidate until at least one monetisable click is modelled.
    if traffic <= 0 or clicks <= 0 or revenue_high <= 0:
        buy_score = min(buy_score, 24.9)
    if not verified:
        buy_score = min(buy_score, 39.9)

    return replace(projection, buy_score=round(buy_score, 1))


def traffic_first_rerank_summary_targets(
    targets: list[str],
    free_scores: dict[str, float],
    free_signals: dict[str, dict[str, int | float | str]],
    summaries: dict[str, dict[str, Any]],
    deep_limit: int,
) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """Choose deep-proof targets for likely quality, not raw backlink volume."""
    original_position = {target: position for position, target in enumerate(targets)}
    summary_scores: dict[str, float] = {}
    combined_scores: dict[str, float] = {}

    for target in targets:
        summary = summaries.get(target, {})
        signal = free_signals.get(target, {})
        referring_pages = max(0, int(summary.get("referring_pages") or 0))
        referring_domains = max(
            0,
            int(summary.get("referring_main_domains") or summary.get("referring_domains") or 0),
        )
        provider_rank = max(0.0, min(100.0, float(summary.get("rank") or 0.0)))

        # Counts saturate quickly.  A 6,000-page footprint should not beat a
        # smaller footprint merely because one site generated thousands of URLs.
        rank_points = min(20.0, provider_rank * 0.20)
        domain_points = min(10.0, 2.0 * math.log2(1 + referring_domains))
        page_points = min(5.0, 0.8 * math.log2(1 + referring_pages))
        diversity_points = (
            min(4.0, 12.0 * referring_domains / max(1, referring_pages))
            if referring_pages
            else 0.0
        )
        summary_score = rank_points + domain_points + page_points + diversity_points
        summary_scores[target] = round(summary_score, 2)

        verified_links = max(0, int(signal.get("verified_links", 0) or 0))
        exact_links = max(0, int(signal.get("exact_links", 0) or 0))
        commoncrawl_hits = max(0, int(signal.get("commoncrawl_hits", 0) or 0))
        youtube_views = max(0, int(signal.get("youtube_monthly_views", 0) or 0))
        screening_quality = max(0.0, float(signal.get("screening_quality", 0.0) or 0.0))
        screening_risk = max(0.0, float(signal.get("screening_risk", 0.0) or 0.0))

        verified_points = (
            min(18.0, 12.0 + 2.0 * math.log2(verified_links)) if verified_links else 0.0
        )
        exact_points = min(8.0, 2.0 * math.log2(1 + exact_links))
        youtube_points = min(12.0, 2.5 * math.log10(1 + youtube_views))
        commoncrawl_points = min(5.0, 1.5 * math.log2(1 + commoncrawl_hits))
        quality_points = min(5.0, screening_quality * 0.05)
        risk_penalty = min(8.0, screening_risk * 0.08)
        availability_points = {
            "available": 3.0,
            "likely_available": 2.0,
            "conflicting": 0.5,
        }.get(str(signal.get("availability", "unknown")), 0.0)

        # Keep only a tiny legacy-score influence as a tie-breaker.  This stops
        # backlink count from silently taking over the new ranking again.
        legacy_tiebreak = min(3.0, max(0.0, float(free_scores.get(target, 0.0))) * 0.03)
        combined_scores[target] = round(
            summary_score
            + verified_points
            + exact_points
            + youtube_points
            + commoncrawl_points
            + quality_points
            + availability_points
            + legacy_tiebreak
            - risk_penalty,
            2,
        )

    eligible = [
        target
        for target in targets
        if int(summaries.get(target, {}).get("referring_pages") or 0) > 0
    ]
    ordered = sorted(
        eligible,
        key=lambda target: (
            -combined_scores[target],
            -float(summaries.get(target, {}).get("rank") or 0.0),
            -int(free_signals.get(target, {}).get("verified_links", 0) or 0),
            -int(free_signals.get(target, {}).get("youtube_monthly_views", 0) or 0),
            -int(
                summaries.get(target, {}).get("referring_main_domains")
                or summaries.get(target, {}).get("referring_domains")
                or 0
            ),
            original_position[target],
        ),
    )
    return ordered[: max(0, deep_limit)], combined_scores, summary_scores


def enforce_money_tier(
    db: Session | None,
    opportunity: Opportunity,
    *,
    traffic: int,
    verified: bool,
) -> None:
    """Prevent backlink-only or zero-money rows from entering ranked tiers."""
    if db is None:
        if not verified or int(traffic or 0) <= 0:
            opportunity.tier = "pending"
        return
    economics = db.scalar(
        select(OpportunityEconomics).where(OpportunityEconomics.domain_id == opportunity.domain_id)
    )
    has_money = bool(
        economics is not None
        and int(economics.expected_clicks_monthly or 0) > 0
        and float(economics.monthly_revenue_high_usd or 0.0) > 0.0
    )
    if not verified or int(traffic or 0) <= 0 or not has_money:
        opportunity.tier = "pending"


def regrade_existing_web_opportunities(
    db: Session,
    score_function: Callable[..., None],
    *,
    limit: int = 1_000,
) -> int:
    """Apply the traffic-first model to existing rows after a deployment."""
    rows = db.scalars(
        select(Opportunity).order_by(Opportunity.updated_at.desc()).limit(limit)
    ).all()
    updated = 0
    for opportunity in rows:
        domain = db.get(Domain, opportunity.domain_id)
        if domain is None:
            continue
        links = list(
            db.scalars(
                select(SourceLink)
                .where(SourceLink.domain_id == domain.id)
                .order_by(SourceLink.provider_rank.desc().nullslast())
                .limit(100)
            ).all()
        )
        clickability = 0.0
        if opportunity.best_source_page_id is not None:
            best_link = next(
                (
                    link
                    for link in links
                    if link.source_page_id == opportunity.best_source_page_id
                ),
                None,
            )
            if best_link is not None:
                observation = db.scalar(
                    select(LinkObservation)
                    .where(LinkObservation.source_link_id == best_link.id)
                    .order_by(LinkObservation.observed_at.desc())
                    .limit(1)
                )
                if observation is not None:
                    clickability = float(observation.clickability_score or 0.0)
        score_function(
            opportunity,
            domain,
            links,
            max(0, int(opportunity.source_page_traffic_estimate or 0)),
            bool(opportunity.verified_live_link),
            db=db,
            clickability_score=clickability,
        )
        updated += 1
    db.commit()
    return updated


def traffic_first_web_row_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Sort dashboard rows by money/traffic proof before backlink vanity metrics."""
    opportunity = row[0]
    economics = row[6] if len(row) > 6 else None
    tier_rank = {
        "priority": 0,
        "qualified": 1,
        "watchlist": 2,
        "pending": 3,
    }.get(str(opportunity.tier or "pending"), 4)
    revenue_high = float(economics.monthly_revenue_high_usd or 0.0) if economics else 0.0
    clicks = int(economics.expected_clicks_monthly or 0) if economics else 0
    return (
        tier_rank,
        -int(bool(opportunity.verified_live_link)),
        -revenue_high,
        -clicks,
        -int(opportunity.source_page_traffic_estimate or 0),
        -float(opportunity.score or 0.0),
        -int(opportunity.independent_site_count or 0),
    )
