from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings


@dataclass(frozen=True)
class ScoreInputs:
    monthly_views: int
    lifetime_views: int
    link_position: float
    has_cta: bool
    clickable: bool
    video_count: int
    link_count: int
    published_at: datetime | None
    availability_status: str


def determine_tier(
    monthly_views: int,
    verified_30d: bool,
    availability_status: str,
    settings: Settings,
) -> str:
    exact_available = availability_status == "available"
    plausible_available = availability_status in {"available", "likely_available"}
    if availability_status in {"registered", "premium", "aftermarket", "reserved"}:
        return "rejected"
    if exact_available and verified_30d and monthly_views >= settings.priority_monthly_views:
        return "priority"
    if exact_available and verified_30d and monthly_views >= settings.qualified_monthly_views:
        return "qualified"
    if plausible_available and monthly_views >= settings.watchlist_monthly_views:
        return "watchlist"
    return "pending"


def calculate_score(inputs: ScoreInputs) -> float:
    # Recent click opportunity dominates the score.
    recent = min(40.0, max(0.0, math.log10(max(inputs.monthly_views, 1)) - 2.5) * 18)
    lifetime = min(10.0, max(0.0, math.log10(max(inputs.lifetime_views, 1)) - 3) * 2.5)
    prominence = max(0.0, 10.0 * (1 - min(max(inputs.link_position, 0), 1)))
    cta = 8.0 if inputs.has_cta else 0.0
    clickability = 4.0 if inputs.clickable else 0.0
    repetition = min(
        15.0, (max(inputs.video_count, 1) - 1) * 5 + (max(inputs.link_count, 1) - 1) * 1.5
    )
    availability = {
        "available": 8.0,
        "likely_available": 4.0,
        "unknown": 0.0,
    }.get(inputs.availability_status, -15.0)

    evergreen = 0.0
    if inputs.published_at:
        published = inputs.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_years = max(0.0, (datetime.now(UTC) - published).days / 365.25)
        evergreen = min(5.0, age_years)

    return round(
        max(
            0.0,
            min(
                100.0,
                recent
                + lifetime
                + prominence
                + cta
                + clickability
                + repetition
                + availability
                + evergreen,
            ),
        ),
        1,
    )
