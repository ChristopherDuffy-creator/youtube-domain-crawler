from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import YOUTUBE_RESERVE_MINIMUM, Settings
from app.models import BoughtDomain, Candidate, Domain, YouTubeDomainSignal

YOUTUBE_VISIBLE_MAXIMUM = 1_000_000
YOUTUBE_REVIEW_STAGES = ("watchlist", "day3", "day7", "low")


def youtube_visible_conditions() -> tuple[object, ...]:
    """Eligibility rules shared by every acquisition-facing report."""
    return (
        Candidate.tier != "rejected",
        ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
        Domain.availability_status == "available",
        Domain.availability_source == "porkbun",
        Domain.premium.is_(False),
        Candidate.evaluation_started_at.is_not(None),
        YouTubeDomainSignal.model_version >= 4,
        YouTubeDomainSignal.click_eligible_exposure > 0,
        YouTubeDomainSignal.buy_score > 0,
        YouTubeDomainSignal.monthly_revenue_high_usd > 0,
        YouTubeDomainSignal.spike_video_count == 0,
    )


def youtube_stage_conditions(stage: str, settings: Settings) -> tuple[object, ...]:
    """Checkpoint-specific traffic rules shared by cards and all counts."""
    if stage == "day3":
        return (
            Candidate.evaluation_stage == "day3",
            Candidate.day3_monthly_views >= settings.watchlist_monthly_views,
            Candidate.day3_monthly_views <= YOUTUBE_VISIBLE_MAXIMUM,
        )
    if stage == "day7":
        return (
            Candidate.evaluation_stage == "day7",
            Candidate.day7_monthly_views >= settings.watchlist_monthly_views,
            Candidate.day7_monthly_views <= YOUTUBE_VISIBLE_MAXIMUM,
        )
    if stage == "low":
        return (
            Candidate.evaluation_stage == "day7",
            Candidate.day7_monthly_views >= YOUTUBE_RESERVE_MINIMUM,
            Candidate.day7_monthly_views < settings.watchlist_monthly_views,
        )
    return (
        Candidate.evaluation_stage == "day0",
        Candidate.start_monthly_views >= settings.watchlist_monthly_views,
        Candidate.start_monthly_views <= YOUTUBE_VISIBLE_MAXIMUM,
    )


def youtube_stage_count(db: Session, stage: str, settings: Settings) -> int:
    """Return the exact number the dashboard would put in one review tab."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Candidate)
            .join(Domain, Domain.id == Candidate.domain_id)
            .join(YouTubeDomainSignal, YouTubeDomainSignal.domain_id == Candidate.domain_id)
            .where(
                *youtube_visible_conditions(),
                *youtube_stage_conditions(stage, settings),
            )
        )
        or 0
    )


def youtube_stage_counts(db: Session, settings: Settings) -> dict[str, int]:
    return {
        stage: youtube_stage_count(db, stage, settings)
        for stage in YOUTUBE_REVIEW_STAGES
    }
