from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BacklinkSummary,
    Domain,
    DroppedDomain,
    Opportunity,
    OpportunityEconomics,
    SourceLink,
    WebScreening,
)

_BLOCKED_AVAILABILITY = {"registered", "aftermarket", "premium", "reserved"}
_HIGH_RISK_SUFFIXES = {"cam", "click", "icu", "live", "sbs", "shop", "site", "top", "xyz"}
_PROTECTED_BRANDS = {
    "adobe",
    "amazon",
    "apple",
    "envato",
    "facebook",
    "google",
    "instagram",
    "microsoft",
    "netflix",
    "nike",
    "squarespace",
    "tiktok",
    "youtube",
}
_COMMERCIAL_WORDS = {
    "app",
    "book",
    "buy",
    "course",
    "deal",
    "finance",
    "hire",
    "home",
    "insurance",
    "learn",
    "loan",
    "mortgage",
    "repair",
    "service",
    "shop",
    "software",
    "tool",
    "training",
    "travel",
}


@dataclass(frozen=True)
class ScreeningResult:
    status: str
    quality_score: float
    risk_score: float
    risk_reasons: list[str]
    monetization_hint: str
    signals: dict[str, int | float | str | bool]


@dataclass(frozen=True)
class EconomicProjection:
    buy_score: float
    expected_clicks_monthly: int
    monthly_revenue_low_usd: float
    monthly_revenue_high_usd: float
    max_purchase_price_usd: float
    estimated_payback_months: float | None
    confidence: float
    risk_score: float
    monetization_route: str
    rationale: list[str]
    safety_flags: list[str]


def _domain_parts(name: str) -> tuple[str, str]:
    cleaned = (name or "").strip().lower().strip(".")
    pieces = cleaned.split(".")
    if len(pieces) < 2:
        return cleaned, ""
    return pieces[-2], pieces[-1]


def _monetization_hint(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ("mortgage", "loan", "insurance", "finance", "tax")):
        return "lead_generation"
    if any(term in lowered for term in ("deal", "shop", "buy", "software", "tool", "travel")):
        return "affiliate_landing"
    if any(term in lowered for term in ("course", "learn", "training", "tutorial")):
        return "course_or_lead_page"
    return "content_restore"


def assess_dropped_domain(name: str, availability_status: str = "unknown") -> ScreeningResult:
    """Perform a deterministic, network-free first pass suitable for very large feeds."""
    label, suffix = _domain_parts(name)
    reasons: list[str] = []
    risk = 0.0

    if not suffix or not re.fullmatch(r"[a-z0-9.-]+", (name or "").lower()):
        reasons.append("invalid_or_internationalised_name")
        risk += 80
    if availability_status in _BLOCKED_AVAILABILITY:
        reasons.append("already_registered_or_nonstandard_purchase")
        risk = 100
    if label in _PROTECTED_BRANDS:
        reasons.append("obvious_protected_brand")
        risk = max(risk, 95)
    if label.startswith("xn--"):
        reasons.append("punycode_name")
        risk += 30
    if len(label) > 35:
        reasons.append("very_long_name")
        risk += 28
    elif len(label) > 24:
        reasons.append("long_name")
        risk += 14
    hyphens = label.count("-")
    digits = sum(character.isdigit() for character in label)
    if hyphens >= 2:
        reasons.append("multiple_hyphens")
        risk += 15
    if digits >= 4:
        reasons.append("many_digits")
        risk += 20
    letters = [character for character in label if character.isalpha()]
    vowels = sum(character in "aeiouy" for character in letters)
    if len(letters) >= 10 and vowels / max(len(letters), 1) < 0.18:
        reasons.append("low_readability")
        risk += 22
    if suffix in _HIGH_RISK_SUFFIXES:
        reasons.append("higher_spam_risk_suffix")
        risk += 12

    risk = round(min(100.0, risk), 1)
    tokens = set(filter(None, re.split(r"[-0-9]+", label)))
    commercial_terms = len(tokens & _COMMERCIAL_WORDS)
    quality = 55.0
    if suffix == "com":
        quality += 15
    elif suffix in {"org", "net", "co", "io"}:
        quality += 8
    if 5 <= len(label) <= 18:
        quality += 12
    if hyphens == 0 and digits == 0:
        quality += 8
    quality += min(10.0, commercial_terms * 5.0)
    quality = round(max(0.0, min(100.0, quality - risk * 0.65)), 1)

    if risk >= 80:
        status = "blocked"
    elif risk >= 50:
        status = "review"
    else:
        status = "eligible"
    return ScreeningResult(
        status=status,
        quality_score=quality,
        risk_score=risk,
        risk_reasons=reasons,
        monetization_hint=_monetization_hint(label),
        signals={
            "label_length": len(label),
            "suffix": suffix,
            "hyphens": hyphens,
            "digits": digits,
            "commercial_terms": commercial_terms,
            "availability": availability_status,
        },
    )


def _chunks(values: list[str], size: int = 5_000) -> Iterator[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def screen_dropped_domains(db: Session, batch_size: int) -> dict[str, int]:
    """Persist one free screening per drop; repeated jobs continue where the ledger stopped."""
    drops = db.scalars(
        select(DroppedDomain)
        .outerjoin(WebScreening, WebScreening.dropped_domain_id == DroppedDomain.id)
        .where(WebScreening.id.is_(None))
        .order_by(DroppedDomain.id.asc())
        .limit(batch_size)
    ).all()
    names = [drop.name for drop in drops]
    availability: dict[str, str] = {}
    for chunk in _chunks(names):
        availability.update(
            {
                name: str(status or "unknown")
                for name, status in db.execute(
                    select(Domain.name, Domain.availability_status).where(Domain.name.in_(chunk))
                ).all()
            }
        )

    counters = {"screened": 0, "eligible": 0, "review": 0, "blocked": 0}
    now = datetime.now(UTC)
    for drop in drops:
        result = assess_dropped_domain(drop.name, availability.get(drop.name, "unknown"))
        db.add(
            WebScreening(
                dropped_domain_id=drop.id,
                domain_name=drop.name,
                status=result.status,
                quality_score=result.quality_score,
                risk_score=result.risk_score,
                risk_reasons=result.risk_reasons,
                monetization_hint=result.monetization_hint,
                signals=result.signals,
                screened_at=now,
            )
        )
        counters["screened"] += 1
        counters[result.status] += 1
    db.commit()
    return counters


def project_opportunity_economics(
    opportunity: Opportunity,
    domain: Domain,
    links: list[SourceLink],
    *,
    traffic: int,
    verified: bool,
    evidence_score: float,
    clickability_score: float = 0.0,
    screening_risk: float = 0.0,
) -> EconomicProjection:
    """Estimate a conservative money case from evidence; it never purchases anything."""
    niche = opportunity.niche or "general"
    route = {
        "finance": "lead_generation",
        "home": "lead_generation",
        "automotive": "lead_generation",
        "software": "affiliate_landing",
        "ecommerce": "affiliate_landing",
        "travel": "affiliate_landing",
        "education": "course_or_lead_page",
    }.get(niche, "content_restore")
    epc_ranges = {
        "finance": (0.8, 2.5),
        "software": (0.35, 1.2),
        "home": (0.45, 1.5),
        "automotive": (0.30, 1.0),
        "travel": (0.20, 0.8),
        "education": (0.20, 0.75),
        "ecommerce": (0.12, 0.50),
    }
    low_epc, high_epc = epc_ranges.get(niche, (0.08, 0.35))

    ctr = 0.003
    ctr += opportunity.commercial_intent * 0.009
    if any(link.semantic_location in {"article", "content", "main"} for link in links):
        ctr += 0.004
    if any(link.anchor_text and not link.anchor_text.lower().startswith("http") for link in links):
        ctr += 0.002
    if verified:
        ctr *= 1.25
    if clickability_score:
        ctr *= 0.75 + min(100.0, clickability_score) / 200.0
    ctr = max(0.001, min(0.03, ctr))
    clicks = int(round(max(0, traffic) * ctr))

    confidence = 0.1
    if verified:
        confidence += 0.3
    if traffic > 0:
        confidence += 0.2
    if domain.availability_status == "available":
        confidence += 0.2
    elif domain.availability_status == "likely_available":
        confidence += 0.08
    confidence += min(0.15, opportunity.independent_site_count * 0.025)
    if clickability_score >= 60:
        confidence += 0.05
    confidence = round(min(1.0, confidence), 2)

    spam_values = [float(link.spam_score) for link in links if link.spam_score is not None]
    average_spam = sum(spam_values) / len(spam_values) if spam_values else 0.0
    risk = max(screening_risk, min(100.0, average_spam * 1.5))
    safety_flags: list[str] = []
    if domain.availability_status in _BLOCKED_AVAILABILITY:
        risk = 100.0
        safety_flags.append("not_an_ordinary_registration")
    if not verified:
        safety_flags.append("best_link_not_directly_verified")
    if average_spam >= 30:
        safety_flags.append("high_backlink_spam")
    if confidence < 0.5:
        safety_flags.append("low_confidence")
    risk = round(min(100.0, risk), 1)

    revenue_low = round(clicks * low_epc * confidence, 2)
    revenue_high = round(clicks * high_epc * min(1.0, confidence + 0.15), 2)
    max_purchase = round(min(500.0, revenue_low * 3.0 * max(0.25, confidence)), 2)
    price = float(domain.registrar_price_usd or 0.0)
    payback = round(price / revenue_low, 1) if price > 0 and revenue_low > 0 else None
    revenue_points = min(20.0, math.log10(revenue_high + 1) * 7.0)
    buy_score = round(
        max(0.0, min(100.0, evidence_score * 0.75 + revenue_points + confidence * 8 - risk * 0.12)),
        1,
    )

    rationale = [
        f"{clicks:,} modelled outbound clicks/month from {traffic:,} source-page visits",
        f"{opportunity.independent_site_count:,} independent referring sites",
        f"{int(round(confidence * 100))}% evidence confidence",
    ]
    if opportunity.commercial_intent >= 0.5:
        rationale.append("strong commercial call-to-action context")
    return EconomicProjection(
        buy_score=buy_score,
        expected_clicks_monthly=clicks,
        monthly_revenue_low_usd=revenue_low,
        monthly_revenue_high_usd=revenue_high,
        max_purchase_price_usd=max_purchase,
        estimated_payback_months=payback,
        confidence=confidence,
        risk_score=risk,
        monetization_route=route,
        rationale=rationale,
        safety_flags=safety_flags,
    )


def save_opportunity_economics(
    db: Session,
    domain: Domain,
    projection: EconomicProjection,
) -> OpportunityEconomics:
    economics = db.scalar(
        select(OpportunityEconomics).where(OpportunityEconomics.domain_id == domain.id)
    )
    if economics is None:
        economics = OpportunityEconomics(domain_id=domain.id)
        db.add(economics)
    economics.buy_score = projection.buy_score
    economics.expected_clicks_monthly = projection.expected_clicks_monthly
    economics.monthly_revenue_low_usd = projection.monthly_revenue_low_usd
    economics.monthly_revenue_high_usd = projection.monthly_revenue_high_usd
    economics.max_purchase_price_usd = projection.max_purchase_price_usd
    economics.estimated_payback_months = projection.estimated_payback_months
    economics.confidence = projection.confidence
    economics.risk_score = projection.risk_score
    economics.monetization_route = projection.monetization_route
    economics.rationale = projection.rationale
    economics.safety_flags = projection.safety_flags
    economics.updated_at = datetime.now(UTC)
    return economics


def backfill_existing_web_intelligence(
    db: Session,
    batch_size: int = 5_000,
) -> dict[str, int]:
    """Upgrade historical proof rows locally without making provider or HTTP calls."""
    rows = db.execute(
        select(Opportunity, Domain, BacklinkSummary, OpportunityEconomics)
        .join(Domain, Domain.id == Opportunity.domain_id)
        .outerjoin(BacklinkSummary, BacklinkSummary.domain_id == Domain.id)
        .outerjoin(OpportunityEconomics, OpportunityEconomics.domain_id == Domain.id)
        .where(
            (BacklinkSummary.id.is_(None)) | (OpportunityEconomics.id.is_(None))
        )
        .order_by(Opportunity.id.asc())
        .limit(batch_size)
    ).all()
    if not rows:
        return {"summaries_backfilled": 0, "money_cases_backfilled": 0}

    domain_ids = [domain.id for _, domain, _, _ in rows]
    links_by_domain: dict[int, list[SourceLink]] = {domain_id: [] for domain_id in domain_ids}
    for chunk in _chunks([str(domain_id) for domain_id in domain_ids]):
        numeric_ids = [int(value) for value in chunk]
        for link in db.scalars(
            select(SourceLink).where(SourceLink.domain_id.in_(numeric_ids))
        ).all():
            links_by_domain.setdefault(link.domain_id, []).append(link)

    screening_risks = {
        name: float(risk or 0.0)
        for name, risk in db.execute(
            select(WebScreening.domain_name, WebScreening.risk_score).where(
                WebScreening.domain_name.in_([domain.name for _, domain, _, _ in rows])
            )
        ).all()
    }
    counters = {"summaries_backfilled": 0, "money_cases_backfilled": 0}
    now = datetime.now(UTC)
    for opportunity, domain, summary, economics in rows:
        if summary is None:
            db.add(
                BacklinkSummary(
                    domain_id=domain.id,
                    provider="historical_ledger",
                    referring_pages=max(0, int(opportunity.referring_page_count or 0)),
                    referring_domains=max(0, int(opportunity.independent_site_count or 0)),
                    referring_main_domains=max(
                        0, int(opportunity.independent_site_count or 0)
                    ),
                    rank=max(0.0, float(opportunity.link_strength or 0.0)),
                    raw_summary={"backfilled_from": "opportunity"},
                    first_seen_at=now,
                    last_refreshed_at=now,
                )
            )
            counters["summaries_backfilled"] += 1
        if economics is None:
            links = links_by_domain.get(domain.id, [])
            projection = project_opportunity_economics(
                opportunity,
                domain,
                links,
                traffic=max(0, int(opportunity.source_page_traffic_estimate or 0)),
                verified=bool(opportunity.verified_live_link),
                evidence_score=float(opportunity.score or 0.0),
                screening_risk=screening_risks.get(domain.name, 0.0),
            )
            save_opportunity_economics(db, domain, projection)
            counters["money_cases_backfilled"] += 1
    db.commit()
    return counters
