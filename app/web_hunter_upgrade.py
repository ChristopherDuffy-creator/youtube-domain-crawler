from __future__ import annotations

"""Traffic-first production helpers for the web-wide Link Hunter.

Backlink volume remains useful evidence, but it is deliberately secondary to
verified live links, source-page traffic, modelled clicks and revenue. The
crawler's goal is to find domains that can receive monetisable humans now, not
merely domains that accumulated many historical backlinks.

The production bootstrap imports this module after the core Link Hunter modules
are loaded. In that production-only path we also install a narrow rescue/focus
layer that:

* reconsiders strong summary-only cases until they receive a detailed proof;
* gives modest preference to government and academic links;
* also favours high-authority editorial publishers and established community/Q&A
  sources; and
* never lets those authority signals override the traffic-first money gates.
"""

import math
import sys
from dataclasses import replace
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BacklinkSummary,
    Domain,
    LinkObservation,
    Opportunity,
    OpportunityEconomics,
    ProviderQuery,
    SourceLink,
    SourcePage,
    SourceSite,
)

_FOCUS_GOVERNMENT = "government"
_FOCUS_ACADEMIC = "academic"
_FOCUS_EDITORIAL = "editorial"
_FOCUS_COMMUNITY = "community"
_FOCUS_NONE = ""


def _normalized_hostname(value: str) -> str:
    hostname = (value or "").strip().lower().strip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def source_focus_category(
    hostname: str,
    *,
    source_type: str = "",
    domain_rank: float = 0.0,
    semantic_location: str = "",
) -> tuple[str, float]:
    """Classify a source into one of the four preferred authority/traffic groups.

    The returned weight is deliberately small. It is a tie-breaker for proof
    selection and a modest final-score bonus, never a replacement for verified
    source traffic and clickability.
    """
    host = _normalized_hostname(hostname)
    source_type = (source_type or "").strip().lower()
    semantic_location = (semantic_location or "").strip().lower()
    rank = max(0.0, min(100.0, float(domain_rank or 0.0)))

    if not host:
        return _FOCUS_NONE, 0.0

    # Covers .gov, .gov.uk, .gov.ie, .gov.au and similar government namespaces.
    if host.endswith(".gov") or ".gov." in host or host.endswith(".gc.ca"):
        return _FOCUS_GOVERNMENT, 8.0

    # US .edu plus common international academic namespaces such as .ac.uk and
    # .edu.au. University/college hostnames only count when provider authority is
    # non-trivial so a commercial site containing the word is not over-promoted.
    academic_namespace = host.endswith(".edu") or ".edu." in host or ".ac." in host
    academic_name = ("university" in host or "college" in host) and rank >= 30.0
    if academic_namespace or academic_name:
        return _FOCUS_ACADEMIC, 7.0

    community_hosts = {
        "news.ycombinator.com",
        "reddit.com",
        "quora.com",
        "stackexchange.com",
        "stackoverflow.com",
        "superuser.com",
    }
    if (
        source_type in {"hackernews", "stackexchange"}
        or host in community_hosts
        or host.endswith(".stackexchange.com")
        or host.endswith(".reddit.com")
    ):
        return _FOCUS_COMMUNITY, 4.0

    # The fourth bucket is a strong editorial/publisher page. DataForSEO domain
    # rank is the guardrail; article/main/content placement strengthens the case.
    editorial_location = semantic_location in {"article", "main", "content", "body"}
    if rank >= 70.0 or (rank >= 60.0 and editorial_location):
        return _FOCUS_EDITORIAL, 5.0

    return _FOCUS_NONE, 0.0


def _source_focus_for_links(
    db: Session,
    links: list[SourceLink],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "best_category": _FOCUS_NONE,
        "best_weight": 0.0,
        "government": 0,
        "academic": 0,
        "editorial": 0,
        "community": 0,
    }
    seen_sites: set[tuple[str, str]] = set()
    for link in links:
        if not link.provider_live:
            continue
        page = db.get(SourcePage, link.source_page_id)
        if page is None:
            continue
        site = db.get(SourceSite, page.site_id)
        if site is None:
            continue
        category, weight = source_focus_category(
            site.hostname,
            source_type=site.source_type,
            domain_rank=float(page.domain_rank or 0.0),
            semantic_location=link.semantic_location,
        )
        if not category:
            continue
        identity = (category, _normalized_hostname(site.hostname))
        if identity not in seen_sites:
            result[category] = int(result.get(category, 0) or 0) + 1
            seen_sites.add(identity)
        if weight > float(result["best_weight"]):
            result["best_category"] = category
            result["best_weight"] = weight
    return result


def _source_focus_signals(db: Session) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        select(
            Domain.name,
            SourceSite.hostname,
            SourceSite.source_type,
            SourcePage.domain_rank,
            SourceLink.semantic_location,
        )
        .join(SourceLink, SourceLink.domain_id == Domain.id)
        .join(SourcePage, SourcePage.id == SourceLink.source_page_id)
        .join(SourceSite, SourceSite.id == SourcePage.site_id)
        .where(SourceLink.provider_live.is_(True))
    ).all()

    signals: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[tuple[str, str]]] = {}
    for name, hostname, source_type, domain_rank, semantic_location in rows:
        category, weight = source_focus_category(
            str(hostname or ""),
            source_type=str(source_type or ""),
            domain_rank=float(domain_rank or 0.0),
            semantic_location=str(semantic_location or ""),
        )
        if not category:
            continue
        row = signals.setdefault(
            str(name),
            {
                "best_category": _FOCUS_NONE,
                "best_weight": 0.0,
                "government": 0,
                "academic": 0,
                "editorial": 0,
                "community": 0,
            },
        )
        seen_for_name = seen.setdefault(str(name), set())
        identity = (category, _normalized_hostname(str(hostname or "")))
        if identity not in seen_for_name:
            row[category] = int(row.get(category, 0) or 0) + 1
            seen_for_name.add(identity)
        if weight > float(row["best_weight"]):
            row["best_category"] = category
            row["best_weight"] = weight
    return signals


def _cached_summary_signals(db: Session) -> dict[str, dict[str, float | int]]:
    rows = db.execute(
        select(
            Domain.name,
            BacklinkSummary.rank,
            BacklinkSummary.referring_pages,
            BacklinkSummary.referring_main_domains,
            BacklinkSummary.referring_domains,
            Opportunity.score,
        )
        .join(BacklinkSummary, BacklinkSummary.domain_id == Domain.id)
        .outerjoin(Opportunity, Opportunity.domain_id == Domain.id)
        .where(BacklinkSummary.provider == "dataforseo")
    ).all()
    return {
        str(name): {
            "rank": max(0.0, float(rank or 0.0)),
            "referring_pages": max(0, int(referring_pages or 0)),
            "referring_domains": max(
                0,
                int(referring_main_domains or referring_domains or 0),
            ),
            "opportunity_score": max(0.0, float(opportunity_score or 0.0)),
        }
        for (
            name,
            rank,
            referring_pages,
            referring_main_domains,
            referring_domains,
            opportunity_score,
        ) in rows
    }


def _summary_rescue_points(signal: dict[str, float | int]) -> float:
    if not signal:
        return 0.0
    rank = max(0.0, min(100.0, float(signal.get("rank", 0.0) or 0.0)))
    domains = max(0, int(signal.get("referring_domains", 0) or 0))
    pages = max(0, int(signal.get("referring_pages", 0) or 0))
    score = max(0.0, float(signal.get("opportunity_score", 0.0) or 0.0))
    return round(
        min(10.0, rank * 0.10)
        + min(8.0, 2.0 * math.log2(1 + domains))
        + min(4.0, 0.8 * math.log2(1 + pages))
        + min(3.0, score * 0.075),
        2,
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

        # Counts saturate quickly. A 6,000-page footprint should not beat a
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
        source_focus = max(0.0, float(signal.get("source_focus_bonus", 0.0) or 0.0))
        rescue_points = max(0.0, float(signal.get("summary_rescue_points", 0.0) or 0.0))

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

        # Preferred-source and cached-summary rescue signals are deliberately
        # capped. They decide which domains deserve deeper proof first, but they
        # cannot turn a no-traffic case into a money case.
        source_focus_points = min(8.0, source_focus)
        rescue_tiebreak = min(12.0, rescue_points * 0.5)

        # Keep only a tiny legacy-score influence as a tie-breaker. This stops
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
            + source_focus_points
            + rescue_tiebreak
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
            -float(free_signals.get(target, {}).get("summary_rescue_points", 0.0) or 0.0),
            -float(free_signals.get(target, {}).get("source_focus_bonus", 0.0) or 0.0),
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


def _install_production_web_focus() -> None:
    """Patch the already-loaded Link Hunter only when app.boot imports this module."""
    import app.link_hunter as link_hunter_module
    import app.link_hunter_preview as preview_module

    original_score = link_hunter_module._score_opportunity
    original_free_context = preview_module._free_rank_context
    original_rank_free = preview_module._rank_free_candidates

    def detailed_proof_checked_targets(db: Session) -> set[str]:
        # A cheap bulk summary no longer retires a candidate forever. Only a
        # completed detailed backlinks proof does. This makes the top summary-only
        # pool cycle back through the 25-name screen until five at a time receive
        # the expensive proof.
        return set(
            db.scalars(
                select(ProviderQuery.target).where(
                    ProviderQuery.provider == "dataforseo",
                    ProviderQuery.endpoint == "backlinks",
                    ProviderQuery.status == "complete",
                )
            ).all()
        )

    def focused_free_context(db: Session) -> dict[str, Any]:
        context = original_free_context(db)
        context["cached_summary"] = _cached_summary_signals(db)
        context["source_focus"] = _source_focus_signals(db)
        return context

    def focused_rank_free(candidates, context):
        ordered, scores, signals = original_rank_free(candidates, context)
        cached = context.get("cached_summary", {})
        focused = context.get("source_focus", {})
        original_position = {item.name: index for index, item in enumerate(ordered)}

        for candidate in candidates:
            name = candidate.name
            summary_signal = cached.get(name, {})
            focus_signal = focused.get(name, {})
            rescue_points = _summary_rescue_points(summary_signal)
            focus_weight = max(0.0, float(focus_signal.get("best_weight", 0.0) or 0.0))
            row = signals.setdefault(name, {})
            row["summary_rescue_points"] = rescue_points
            row["cached_summary_rank"] = float(summary_signal.get("rank", 0.0) or 0.0)
            row["cached_referring_pages"] = int(summary_signal.get("referring_pages", 0) or 0)
            row["cached_referring_domains"] = int(
                summary_signal.get("referring_domains", 0) or 0
            )
            row["source_focus_bonus"] = focus_weight
            row["source_focus_category"] = str(focus_signal.get("best_category", "") or "")
            row["source_focus_government"] = int(focus_signal.get("government", 0) or 0)
            row["source_focus_academic"] = int(focus_signal.get("academic", 0) or 0)
            row["source_focus_editorial"] = int(focus_signal.get("editorial", 0) or 0)
            row["source_focus_community"] = int(focus_signal.get("community", 0) or 0)
            scores[name] = round(
                float(scores.get(name, 0.0))
                + min(25.0, rescue_points)
                + min(8.0, focus_weight),
                2,
            )

        ordered = sorted(
            candidates,
            key=lambda candidate: (
                -float(scores.get(candidate.name, 0.0)),
                -float(signals.get(candidate.name, {}).get("summary_rescue_points", 0.0) or 0.0),
                -float(signals.get(candidate.name, {}).get("source_focus_bonus", 0.0) or 0.0),
                -int(signals.get(candidate.name, {}).get("verified_links", 0) or 0),
                -int(signals.get(candidate.name, {}).get("independent_sites", 0) or 0),
                original_position.get(candidate.name, len(original_position)),
            ),
        )
        return ordered, scores, signals

    def focused_score(
        opportunity,
        domain,
        saved_links,
        traffic,
        verified,
        *,
        db=None,
        clickability_score=0.0,
    ):
        original_score(
            opportunity,
            domain,
            saved_links,
            traffic,
            verified,
            db=db,
            clickability_score=clickability_score,
        )
        if db is None or not saved_links:
            return

        focus = _source_focus_for_links(db, list(saved_links))
        raw_weight = max(0.0, float(focus.get("best_weight", 0.0) or 0.0))
        if raw_weight <= 0.0:
            return

        # Authority is a tie-breaker only. A directly verified traffic case can
        # receive at most +4 buy-score points; unverified/zero-traffic evidence
        # gets only a token bump and remains behind the traffic-first gates.
        multiplier = 0.5 if verified and int(traffic or 0) > 0 else 0.125
        score_bonus = min(4.0, raw_weight * multiplier)
        economics = db.scalar(
            select(OpportunityEconomics).where(
                OpportunityEconomics.domain_id == opportunity.domain_id
            )
        )
        current = float(opportunity.score or 0.0)
        if economics is not None:
            current = max(current, float(economics.buy_score or 0.0))
        adjusted = round(min(100.0, current + score_bonus), 1)
        opportunity.score = adjusted
        if economics is not None:
            economics.buy_score = adjusted

        ordinary_available = domain.availability_status == "available" and not domain.premium
        if ordinary_available and verified and adjusted >= 80.0:
            opportunity.tier = "priority"
        elif ordinary_available and verified and adjusted >= 65.0:
            opportunity.tier = "qualified"
        elif adjusted >= 45.0:
            opportunity.tier = "watchlist"
        else:
            opportunity.tier = "pending"

    preview_module._dataforseo_checked_targets = detailed_proof_checked_targets
    preview_module._free_rank_context = focused_free_context
    preview_module._rank_free_candidates = focused_rank_free
    link_hunter_module._score_opportunity = focused_score


# Import-time patching is intentionally restricted to the production bootstrap.
# Unit tests can import this module's helpers without mutating the global crawler.
if "app.boot" in sys.modules:
    _install_production_web_focus()
