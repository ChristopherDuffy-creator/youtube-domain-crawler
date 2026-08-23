from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import case, delete, exists, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain_tools import is_plausible_youtube_link
from app.models import Candidate, VideoDomain, VideoRefreshState, YouTubeDomainSignal, utcnow

logger = logging.getLogger(__name__)


def _is_explicit_url_clause():
    return (
        VideoDomain.raw_url.ilike("http://%")
        | VideoDomain.raw_url.ilike("https://%")
        | VideoDomain.raw_url.ilike("www.%")
    )


def _chunks(values: list[int], size: int = 50):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def purge_legacy_bare_youtube_links(db: Session, settings: Settings) -> dict[str, int]:
    """Remove ambiguous bare-text matches without deleting real bare domains.

    Genuine YouTube descriptions often contain ``example.com`` without http or
    www, so bare links remain first-class evidence. The parser's plausibility
    rules reject obvious prose/file collisions such as B.Tech, 3.how, m.ch and
    manage.py. This deployment repair also restores plausible bare links for
    candidates that the short-lived clickable-only hotfix may have demoted.
    """
    recent_cutoff = utcnow() - timedelta(hours=6)
    ranked = ("watchlist", "qualified", "priority")

    candidate_rows = db.execute(
        select(Candidate.domain_id, Candidate.tier, Candidate.updated_at)
        .where(
            or_(
                Candidate.tier.in_(ranked),
                Candidate.updated_at >= recent_cutoff,
            )
        )
        .order_by(
            case((Candidate.tier.in_(ranked), 0), else_=1),
            Candidate.updated_at.desc(),
            Candidate.domain_id.asc(),
        )
        .limit(750)
    ).all()
    candidate_state = {
        int(domain_id): (str(tier), updated_at)
        for domain_id, tier, updated_at in candidate_rows
    }

    removed = 0
    restored = 0
    affected: set[int] = set()
    for chunk in _chunks(list(candidate_state), 50):
        bare_links = db.scalars(
            select(VideoDomain).where(
                VideoDomain.domain_id.in_(chunk),
                ~_is_explicit_url_clause(),
            )
        ).all()
        for link in bare_links:
            plausible = is_plausible_youtube_link(link.raw_url)
            state = candidate_state.get(link.domain_id)
            recent_rejected = bool(
                state
                and state[0] == "rejected"
                and state[1] is not None
                and state[1] >= recent_cutoff
            )
            if link.active and not plausible:
                link.active = False
                affected.add(link.domain_id)
                removed += 1
            elif not link.active and plausible and recent_rejected:
                link.active = True
                affected.add(link.domain_id)
                restored += 1
        db.commit()

    if affected:
        from app.jobs import refresh_candidates

        for chunk in _chunks(sorted(affected), 25):
            refresh_candidates(db, set(chunk))

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

    enforce_candidate_signal_consistency(db, settings)
    logger.info(
        "YouTube bare-link hygiene removed=%s restored=%s affected_domains=%s",
        removed,
        restored,
        len(affected),
    )
    return {
        "legacy_bare_links_removed": removed,
        "plausible_bare_links_restored": restored,
        "affected_domains": len(affected),
    }


def enforce_candidate_signal_consistency(
    db: Session,
    settings: Settings,
    domain_ids: set[int] | None = None,
) -> int:
    """Fail closed when ranked YouTube evidence is stale or implausible.

    Ranked candidates are revalidated against the same raw-link plausibility
    rules used by new ingestion. This closes the legacy-ledger hole where an old
    active false-positive link could be cleaned once, remain elsewhere in the
    permanent ledger, then regain enough exposure to climb back into Watchlist.

    Any ranked domain with newly-invalid active evidence is immediately demoted
    and its displayed money signal is zeroed. A later normal video refresh can
    restore the candidate if genuine active links remain.
    """
    if domain_ids is not None and not domain_ids:
        return 0

    ranked = ("watchlist", "qualified", "priority")
    ranked_query = select(Candidate.domain_id).where(Candidate.tier.in_(ranked))
    if domain_ids is not None:
        ranked_query = ranked_query.where(Candidate.domain_id.in_(domain_ids))
    ranked_ids = [int(value) for value in db.scalars(ranked_query).all()]

    invalid_domains: set[int] = set()
    for chunk in _chunks(ranked_ids, 50):
        links = db.scalars(
            select(VideoDomain).where(
                VideoDomain.domain_id.in_(chunk),
                VideoDomain.active.is_(True),
            )
        ).all()
        for link in links:
            if not is_plausible_youtube_link(link.raw_url):
                link.active = False
                invalid_domains.add(int(link.domain_id))

    changed = 0
    if invalid_domains:
        result = db.execute(
            update(Candidate)
            .where(
                Candidate.domain_id.in_(invalid_domains),
                Candidate.tier.in_(ranked),
            )
            .values(tier="pending", updated_at=utcnow())
        )
        changed += int(result.rowcount or 0)
        db.execute(
            update(YouTubeDomainSignal)
            .where(YouTubeDomainSignal.domain_id.in_(invalid_domains))
            .values(
                active_video_count=0,
                active_link_count=0,
                channel_count=0,
                lifetime_linked_video_views=0,
                monthly_linked_video_exposure=0,
                observation_days=0.0,
                traffic_confidence="revalidating_links",
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
        logger.info(
            "Continuous ranked YouTube hygiene demoted %s domains with stale invalid links",
            len(invalid_domains),
        )

    scope = []
    if domain_ids is not None:
        scope.append(Candidate.domain_id.in_(domain_ids))

    thresholds = (
        ("watchlist", settings.watchlist_monthly_views),
        ("qualified", settings.qualified_monthly_views),
        ("priority", settings.priority_monthly_views),
    )
    for tier, threshold in thresholds:
        sufficient_signal = exists(
            select(YouTubeDomainSignal.domain_id).where(
                YouTubeDomainSignal.domain_id == Candidate.domain_id,
                YouTubeDomainSignal.monthly_linked_video_exposure >= threshold,
            )
        )
        result = db.execute(
            update(Candidate)
            .where(
                Candidate.tier == tier,
                ~sufficient_signal,
                *scope,
            )
            .values(tier="pending", updated_at=utcnow())
        )
        changed += int(result.rowcount or 0)

    db.commit()
    return changed
