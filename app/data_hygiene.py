from __future__ import annotations

import logging

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Candidate, VideoDomain, VideoRefreshState, YouTubeDomainSignal, utcnow

logger = logging.getLogger(__name__)


def _is_explicit_url_clause():
    return (
        VideoDomain.raw_url.ilike("http://%")
        | VideoDomain.raw_url.ilike("https://%")
        | VideoDomain.raw_url.ilike("www.%")
    )


def purge_legacy_bare_youtube_links(db: Session, settings: Settings) -> dict[str, int]:
    """Remove old prose/file-name matches that were incorrectly indexed as links.

    This is database-side and idempotent so it is safe at every deployment. It
    deliberately keeps dropped-domain list parsing broad; only YouTube outbound
    links are required to have an explicit URL form.
    """
    bare_active = VideoDomain.active.is_(True) & ~_is_explicit_url_clause()
    removed = int(
        db.scalar(
            select(VideoDomain.id)
            .where(bare_active)
            .limit(1)
        )
        is not None
    )
    db.execute(update(VideoDomain).where(bare_active).values(active=False))

    active_link_for_candidate = exists(
        select(VideoDomain.id).where(
            VideoDomain.domain_id == Candidate.domain_id,
            VideoDomain.active.is_(True),
        )
    )
    db.execute(
        update(Candidate)
        .where(~active_link_for_candidate)
        .values(
            tier="rejected",
            monthly_views=0,
            verified_30d=False,
            observation_days=0.0,
            score=0.0,
            video_count=0,
            link_count=0,
            best_video_id=None,
            updated_at=utcnow(),
        )
    )

    active_link_for_signal = exists(
        select(VideoDomain.id).where(
            VideoDomain.domain_id == YouTubeDomainSignal.domain_id,
            VideoDomain.active.is_(True),
        )
    )
    db.execute(
        update(YouTubeDomainSignal)
        .where(~active_link_for_signal)
        .values(
            active_video_count=0,
            active_link_count=0,
            channel_count=0,
            lifetime_linked_video_views=0,
            monthly_linked_video_exposure=0,
            observation_days=0.0,
            traffic_confidence="no_active_links",
            measured_15d=False,
            verified_30d=False,
            cta_rate=0.0,
            clickable_rate=0.0,
            expected_clicks_monthly=0,
            monthly_revenue_low_usd=0.0,
            monthly_revenue_high_usd=0.0,
            max_purchase_price_usd=0.0,
            buy_score=0.0,
            updated_at=utcnow(),
        )
    )

    active_link_for_refresh = exists(
        select(VideoDomain.id).where(
            VideoDomain.video_id == VideoRefreshState.video_id,
            VideoDomain.active.is_(True),
        )
    )
    db.execute(delete(VideoRefreshState).where(~active_link_for_refresh))
    db.commit()

    # A Watchlist/Qualified/Priority candidate cannot legitimately have a lower
    # aggregate exposure than the watch threshold. If an interrupted refresh
    # leaves a stale Candidate beside a newer zero/low signal, fail closed to
    # Pending instead of displaying an impossible "Watchlist · 0 views" row.
    enforce_candidate_signal_consistency(db, settings)
    return {"legacy_bare_links_found": removed}


def enforce_candidate_signal_consistency(
    db: Session,
    settings: Settings,
    domain_ids: set[int] | None = None,
) -> int:
    weak_signal_domains = select(YouTubeDomainSignal.domain_id).where(
        YouTubeDomainSignal.monthly_linked_video_exposure < settings.watchlist_monthly_views
    )
    if domain_ids is not None:
        if not domain_ids:
            return 0
        weak_signal_domains = weak_signal_domains.where(
            YouTubeDomainSignal.domain_id.in_(domain_ids)
        )

    result = db.execute(
        update(Candidate)
        .where(
            Candidate.domain_id.in_(weak_signal_domains),
            Candidate.tier.in_(("watchlist", "qualified", "priority")),
        )
        .values(tier="pending", updated_at=utcnow())
    )
    db.commit()
    return int(result.rowcount or 0)
