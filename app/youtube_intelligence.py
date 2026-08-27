from __future__ import annotations

import gc
import math
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.metrics import calculate_monthly_views, is_short_form_duration
from app.models import (
    BoughtDomain,
    Domain,
    DroppedDomain,
    DroppedDomainMatch,
    Video,
    VideoDomain,
    YouTubeChannel,
    YouTubeChannelIntelligence,
    YouTubeDomainSignal,
    YouTubeQuotaLedger,
)

_PACIFIC = ZoneInfo("America/Los_Angeles")
_BLOCKED_AVAILABILITY = {"registered", "premium", "aftermarket", "reserved"}
_SIGNAL_DOMAIN_CHUNK = 5
_SIGNAL_UNSCOPED_LIMIT = 25
_SIGNAL_MODEL_VERSION = 4


def _chunks(values: list[int], size: int = 5_000) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def youtube_quota_snapshot(db: Session, settings: Settings) -> dict[str, int | str]:
    quota_date = datetime.now(_PACIFIC).date()
    ledger = db.get(YouTubeQuotaLedger, quota_date)
    search_used = int(ledger.search_calls if ledger is not None else 0)
    data_used = int(ledger.data_units if ledger is not None else 0)
    fanout_used = int(ledger.fanout_data_units if ledger is not None else 0)
    stats_used = int(ledger.stats_units if ledger is not None else 0)
    return {
        "quota_date_pt": quota_date.isoformat(),
        "search_used": search_used,
        "search_remaining": max(0, settings.youtube_search_daily_limit - search_used),
        "data_used": data_used,
        "data_remaining": max(0, settings.youtube_data_daily_limit - data_used),
        "fanout_data_used": fanout_used,
        "fanout_data_remaining": max(0, settings.youtube_fanout_daily_data_limit - fanout_used),
        "stats_used": stats_used,
        "stats_remaining": max(0, settings.youtube_stats_daily_limit - stats_used),
    }


def consume_youtube_quota(
    db: Session,
    settings: Settings,
    *,
    search_calls: int = 0,
    data_units: int = 0,
    stats_units: int = 0,
    fanout: bool = False,
) -> bool:
    """Atomically consume conservative quota before a request is made."""
    search_calls = max(0, int(search_calls))
    data_units = max(0, int(data_units))
    stats_units = max(0, int(stats_units))
    quota_date = datetime.now(_PACIFIC).date()
    ledger = db.scalar(
        select(YouTubeQuotaLedger).where(YouTubeQuotaLedger.quota_date == quota_date).with_for_update()
    )
    if ledger is None:
        ledger = YouTubeQuotaLedger(quota_date=quota_date)
        db.add(ledger)
        db.flush()
    if ledger.search_calls + search_calls > settings.youtube_search_daily_limit:
        db.rollback()
        return False
    if ledger.data_units + data_units > settings.youtube_data_daily_limit:
        db.rollback()
        return False
    if fanout and (ledger.fanout_data_units + data_units > settings.youtube_fanout_daily_data_limit):
        db.rollback()
        return False
    if ledger.stats_units + stats_units > settings.youtube_stats_daily_limit:
        db.rollback()
        return False
    ledger.search_calls += search_calls
    ledger.data_units += data_units
    if fanout:
        ledger.fanout_data_units += data_units
    ledger.stats_units += stats_units
    ledger.updated_at = datetime.now(UTC)
    db.commit()
    return True


def ensure_channel_intelligence(
    db: Session,
    channel: YouTubeChannel,
) -> YouTubeChannelIntelligence:
    intelligence = db.get(YouTubeChannelIntelligence, channel.channel_id)
    if intelligence is None:
        starting_yield = max(0.0, float(channel.yield_score or 0.0) / 100.0)
        intelligence = YouTubeChannelIntelligence(
            channel_id=channel.channel_id,
            tier="warm" if starting_yield >= 0.03 else "unrated",
            ema_yield=starting_yield,
            marginal_yield=starting_yield,
            expected_links_per_page=starting_yield * 50.0,
            recommended_burst=4 if starting_yield >= 0.03 else 1,
            next_crawl_at=datetime.now(UTC),
        )
        db.add(intelligence)
    return intelligence


def update_channel_intelligence(
    db: Session,
    channel: YouTubeChannel,
    *,
    videos_seen: int,
    new_videos: int,
    linked_videos: int,
    external_links: int,
    completed: bool,
) -> YouTubeChannelIntelligence:
    intelligence = ensure_channel_intelligence(db, channel)
    page_yield = external_links / max(1, videos_seen)
    intelligence.marginal_yield = round(page_yield, 4)
    if intelligence.ema_yield <= 0:
        intelligence.ema_yield = page_yield
    else:
        intelligence.ema_yield = 0.7 * intelligence.ema_yield + 0.3 * page_yield
    intelligence.ema_yield = round(intelligence.ema_yield, 4)
    intelligence.expected_links_per_page = round(intelligence.ema_yield * 50.0, 2)
    intelligence.consecutive_empty_pages = (
        intelligence.consecutive_empty_pages + 1 if external_links == 0 else 0
    )
    intelligence.pages_without_new_video = intelligence.pages_without_new_video + 1 if new_videos == 0 else 0
    intelligence.failure_count = 0

    if intelligence.ema_yield >= 0.12 or linked_videos >= 3 or external_links >= 5:
        intelligence.tier = "hot"
        intelligence.recommended_burst = 12
        recrawl_hours = 6
    elif intelligence.consecutive_empty_pages >= 4:
        intelligence.tier = "dormant"
        intelligence.recommended_burst = 1
        recrawl_hours = 720
    elif intelligence.ema_yield >= 0.03 or linked_videos > 0 or channel.seed_count >= 3:
        intelligence.tier = "warm"
        intelligence.recommended_burst = 4
        recrawl_hours = 24
    else:
        intelligence.tier = "cold"
        intelligence.recommended_burst = 1
        recrawl_hours = 168

    now = datetime.now(UTC)
    intelligence.next_crawl_at = now + timedelta(hours=recrawl_hours) if completed else now
    intelligence.evaluated_at = now
    return intelligence


def record_channel_failure(
    db: Session,
    channel: YouTubeChannel,
) -> YouTubeChannelIntelligence:
    intelligence = ensure_channel_intelligence(db, channel)
    intelligence.failure_count += 1
    hours = min(168, 2 ** min(7, intelligence.failure_count))
    intelligence.next_crawl_at = datetime.now(UTC) + timedelta(hours=hours)
    intelligence.evaluated_at = datetime.now(UTC)
    return intelligence


def _monetization_route(domain: Domain, links: list[VideoDomain]) -> str:
    text = " ".join([domain.name, *(link.context or "" for link in links)]).lower()
    if any(term in text for term in ("insurance", "loan", "mortgage", "finance", "tax")):
        return "lead_generation"
    if any(term in text for term in ("software", "app", "tool", "shop", "deal", "travel")):
        return "affiliate_landing"
    if any(term in text for term in ("course", "learn", "training", "tutorial")):
        return "course_or_lead_page"
    return "content_restore"


def _refresh_youtube_domain_signal_chunk(
    db: Session,
    settings: Settings,
    domain_ids: set[int] | None = None,
    *,
    limit: int | None = None,
) -> int:
    statement = select(Domain).where(
        Domain.video_links.any(VideoDomain.active.is_(True)),
        ~Domain.id.in_(select(BoughtDomain.domain_id)),
    )
    if domain_ids is not None:
        if not domain_ids:
            return 0
        statement = statement.where(Domain.id.in_(domain_ids))
    statement = statement.order_by(Domain.id.asc())
    if limit is not None:
        statement = statement.limit(limit)
    domains = db.scalars(
        statement.options(
            selectinload(Domain.candidate),
            selectinload(Domain.video_links).selectinload(VideoDomain.video).selectinload(Video.snapshots),
        )
    ).all()
    now = datetime.now(UTC)
    if domain_ids:
        db.execute(
            update(YouTubeDomainSignal)
            .where(YouTubeDomainSignal.domain_id.in_(domain_ids))
            .values(
                active_video_count=0,
                active_link_count=0,
                channel_count=0,
                observed_view_gain=0,
                monthly_linked_video_exposure=0,
                click_eligible_exposure=0,
                short_form_exposure=0,
                short_form_video_count=0,
                spike_video_count=0,
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
                model_version=_SIGNAL_MODEL_VERSION,
                updated_at=now,
            )
        )
    updated = 0
    for domain in domains:
        links = [
            link
            for link in domain.video_links
            if link.active and link.video is not None and link.video.active
        ]
        if not links:
            continue
        unique_videos = {link.video_id: link.video for link in links}
        metrics_by_video_id = {
            video_id: calculate_monthly_views(video.snapshots) for video_id, video in unique_videos.items()
        }
        metrics = list(metrics_by_video_id.values())
        observed_view_gain = sum(metric.delta_views for metric in metrics)
        monthly_exposure = sum(metric.monthly_views for metric in metrics)
        observation_days = max((metric.observation_days for metric in metrics), default=0.0)
        short_video_ids = {
            video_id
            for video_id, video in unique_videos.items()
            if is_short_form_duration(video.duration_seconds)
        }
        click_eligible_video_ids = {
            video_id
            for video_id, metric in metrics_by_video_id.items()
            if video_id not in short_video_ids and metric.evaluation_stage != "collecting"
        }
        click_eligible_exposure = sum(
            metrics_by_video_id[video_id].monthly_views for video_id in click_eligible_video_ids
        )
        short_form_exposure = sum(metrics_by_video_id[video_id].monthly_views for video_id in short_video_ids)
        measured = any(
            metrics_by_video_id[video_id].observation_days >= settings.youtube_measured_window_days
            for video_id in click_eligible_video_ids
        )
        verified = any(metrics_by_video_id[video_id].verified_30d for video_id in click_eligible_video_ids)
        if verified:
            confidence_label = "verified_30d"
            confidence_factor = 1.0
        elif measured:
            confidence_label = "measured_15d"
            confidence_factor = 0.92
        elif domain.candidate is not None and domain.candidate.evaluation_stage == "day7":
            confidence_label = "day7_evaluated"
            confidence_factor = 0.82
        elif domain.candidate is not None and domain.candidate.evaluation_stage == "day3":
            confidence_label = "day3_recheck"
            confidence_factor = 0.60
        elif domain.candidate is not None and domain.candidate.evaluation_stage == "day0":
            confidence_label = "day0_estimate"
            confidence_factor = 0.35
        else:
            confidence_label = "collecting"
            confidence_factor = 0.10

        click_eligible_links = [link for link in links if link.video_id in click_eligible_video_ids]
        if click_eligible_links:
            cta_rate = sum(int(link.has_cta) for link in click_eligible_links) / len(click_eligible_links)
            clickable_rate = sum(int(link.clickable) for link in click_eligible_links) / len(
                click_eligible_links
            )
            best_position = min(link.description_position for link in click_eligible_links)
            ctr = 0.0015 + 0.005 * cta_rate + 0.003 * clickable_rate
            ctr += max(0.0, 0.003 * (1.0 - min(1.0, best_position)))
            ctr = min(0.02, ctr)
            expected_clicks = int(round(click_eligible_exposure * ctr * confidence_factor))
        else:
            cta_rate = 0.0
            clickable_rate = 0.0
            expected_clicks = 0
        route = _monetization_route(domain, links)
        epc = {
            "lead_generation": (0.6, 2.0),
            "affiliate_landing": (0.18, 0.8),
            "course_or_lead_page": (0.25, 1.0),
            "content_restore": (0.05, 0.25),
        }[route]
        revenue_low = round(expected_clicks * epc[0], 2)
        revenue_high = round(expected_clicks * epc[1], 2)
        available = domain.availability_status == "available"
        max_purchase = round(
            min(500.0, revenue_low * 3.0 * confidence_factor)
            if available and domain.candidate is not None and domain.candidate.buy_ready
            else 0.0,
            2,
        )
        base_score = float(domain.candidate.score if domain.candidate is not None else 0.0)
        exposure_points = min(18.0, math.log10(click_eligible_exposure + 1) * 3.0)
        maturity_penalty = {
            "collecting": 20.0,
            "day0": 12.0,
            "day3": 6.0,
            "day7": 0.0,
        }.get(
            domain.candidate.evaluation_stage if domain.candidate is not None else "collecting",
            0.0,
        )
        buy_score = (
            max(
                0.0,
                min(100.0, base_score * 0.78 + exposure_points - maturity_penalty),
            )
            if click_eligible_exposure > 0
            else 0.0
        )
        if any(metric.spike_detected for metric in metrics):
            buy_score = max(0.0, buy_score - 8.0)
        if domain.availability_status in _BLOCKED_AVAILABILITY:
            buy_score = 0.0

        signal = db.get(YouTubeDomainSignal, domain.id)
        if signal is None:
            signal = YouTubeDomainSignal(domain_id=domain.id)
            db.add(signal)
        signal.active_video_count = len(unique_videos)
        signal.active_link_count = len(links)
        signal.channel_count = len({video.channel_id for video in unique_videos.values() if video.channel_id})
        signal.lifetime_linked_video_views = sum(
            max(0, video.lifetime_views) for video in unique_videos.values()
        )
        signal.observed_view_gain = observed_view_gain
        signal.monthly_linked_video_exposure = monthly_exposure
        signal.click_eligible_exposure = click_eligible_exposure
        signal.short_form_exposure = short_form_exposure
        signal.short_form_video_count = len(short_video_ids)
        signal.spike_video_count = sum(int(metric.spike_detected) for metric in metrics)
        signal.observation_days = observation_days
        signal.traffic_confidence = confidence_label
        signal.measured_15d = measured
        signal.verified_30d = verified
        signal.cta_rate = round(cta_rate, 3)
        signal.clickable_rate = round(clickable_rate, 3)
        signal.expected_clicks_monthly = expected_clicks
        signal.monthly_revenue_low_usd = revenue_low
        signal.monthly_revenue_high_usd = revenue_high
        signal.max_purchase_price_usd = max_purchase
        signal.buy_score = round(buy_score, 1)
        signal.monetization_route = route
        signal.model_version = _SIGNAL_MODEL_VERSION
        signal.updated_at = now
        updated += 1
    db.commit()
    return updated


def quarantine_stale_youtube_signals(db: Session, settings: Settings) -> int:
    """Fail closed before stale or under-observed economics reach the dashboard."""
    del settings
    stale = db.execute(
        update(YouTubeDomainSignal)
        .where(YouTubeDomainSignal.model_version < _SIGNAL_MODEL_VERSION)
        .values(
            traffic_confidence="recalculation_required",
            expected_clicks_monthly=0,
            monthly_revenue_low_usd=0.0,
            monthly_revenue_high_usd=0.0,
            max_purchase_price_usd=0.0,
            buy_score=0.0,
        )
    )
    db.commit()
    return int(stale.rowcount or 0)


def _release_signal_orm_memory(db: Session) -> None:
    try:
        db.expire_all()
    finally:
        gc.collect()


def refresh_youtube_domain_signals(
    db: Session,
    settings: Settings,
    domain_ids: set[int] | None = None,
    *,
    limit: int | None = None,
) -> int:
    """Refresh signal graphs in bounded chunks and enforce display consistency."""
    from app.data_hygiene import enforce_candidate_signal_consistency

    if domain_ids is not None:
        ids = sorted(int(value) for value in domain_ids)
        updated = 0
        for start in range(0, len(ids), _SIGNAL_DOMAIN_CHUNK):
            chunk = set(ids[start : start + _SIGNAL_DOMAIN_CHUNK])
            updated += _refresh_youtube_domain_signal_chunk(
                db,
                settings,
                chunk,
                limit=None,
            )
            enforce_candidate_signal_consistency(db, settings, chunk)
            _release_signal_orm_memory(db)
        return updated

    effective_limit = (
        _SIGNAL_UNSCOPED_LIMIT if limit is None else min(max(0, int(limit)), _SIGNAL_UNSCOPED_LIMIT)
    )
    updated = _refresh_youtube_domain_signal_chunk(
        db,
        settings,
        None,
        limit=effective_limit,
    )
    enforce_candidate_signal_consistency(db, settings)
    _release_signal_orm_memory(db)
    return updated


def refresh_local_dropped_matches(
    db: Session,
    *,
    names: list[str] | None = None,
    domain_ids: set[int] | None = None,
    limit: int = 100_000,
) -> dict[str, int]:
    statement = (
        select(
            DroppedDomain.id,
            Domain.id,
            func.count(func.distinct(VideoDomain.video_id)),
            func.count(VideoDomain.id),
        )
        .join(Domain, Domain.name == DroppedDomain.name)
        .join(
            VideoDomain,
            (VideoDomain.domain_id == Domain.id) & VideoDomain.active.is_(True),
        )
        .join(
            Video,
            (Video.id == VideoDomain.video_id) & Video.active.is_(True),
        )
        .where(
            Domain.excluded_reason.is_(None),
            ~Domain.id.in_(select(BoughtDomain.domain_id)),
        )
    )
    if names is not None:
        if not names:
            return {"matched": 0, "new_matches": 0, "refreshed_matches": 0}
        statement = statement.where(DroppedDomain.name.in_(names))
    if domain_ids is not None:
        if not domain_ids:
            return {"matched": 0, "new_matches": 0, "refreshed_matches": 0}
        statement = statement.where(Domain.id.in_(domain_ids))
    now = datetime.now(UTC)
    reset_statement = None
    if domain_ids:
        reset_statement = update(DroppedDomainMatch).where(DroppedDomainMatch.domain_id.in_(domain_ids))
    elif names:
        relevant_dropped_ids = db.scalars(select(DroppedDomain.id).where(DroppedDomain.name.in_(names))).all()
        if relevant_dropped_ids:
            reset_statement = update(DroppedDomainMatch).where(
                DroppedDomainMatch.dropped_domain_id.in_(relevant_dropped_ids)
            )
    if reset_statement is not None:
        db.execute(
            reset_statement.values(
                active_video_count=0,
                active_link_count=0,
                refreshed_at=now,
            )
        )

    rows = db.execute(
        statement.group_by(DroppedDomain.id, Domain.id).order_by(DroppedDomain.id.asc()).limit(limit)
    ).all()
    if not rows:
        db.commit()
        return {"matched": 0, "new_matches": 0, "refreshed_matches": 0}

    dropped_ids = [int(row[0]) for row in rows]
    existing: dict[tuple[int, int], DroppedDomainMatch] = {}
    for chunk in _chunks(dropped_ids):
        matches = db.scalars(
            select(DroppedDomainMatch).where(DroppedDomainMatch.dropped_domain_id.in_(chunk))
        ).all()
        existing.update({(match.dropped_domain_id, match.domain_id): match for match in matches})

    new_matches = 0
    refreshed = 0
    domain_id_values: list[int] = []
    for dropped_id, domain_id, video_count, link_count in rows:
        key = (int(dropped_id), int(domain_id))
        match = existing.get(key)
        if match is None:
            match = DroppedDomainMatch(
                dropped_domain_id=key[0],
                domain_id=key[1],
                matched_at=now,
            )
            db.add(match)
            new_matches += 1
        else:
            refreshed += 1
        match.active_video_count = int(video_count or 0)
        match.active_link_count = int(link_count or 0)
        match.refreshed_at = now
        domain_id_values.append(key[1])
    for chunk in _chunks(dropped_ids):
        db.execute(
            update(DroppedDomain).where(DroppedDomain.id.in_(chunk)).values(matched_existing_index=True)
        )
    for chunk in _chunks(domain_id_values):
        db.execute(update(Domain).where(Domain.id.in_(chunk)).values(last_checked_at=None))
    db.commit()
    return {
        "matched": len(rows),
        "new_matches": new_matches,
        "refreshed_matches": refreshed,
    }


def backfill_youtube_intelligence(
    db: Session,
    settings: Settings,
) -> dict[str, int]:
    channels = db.scalars(
        select(YouTubeChannel)
        .outerjoin(
            YouTubeChannelIntelligence,
            YouTubeChannelIntelligence.channel_id == YouTubeChannel.channel_id,
        )
        .where(YouTubeChannelIntelligence.channel_id.is_(None))
        .order_by(YouTubeChannel.channel_id.asc())
        .limit(settings.youtube_intelligence_backfill_batch_size)
    ).all()
    for channel in channels:
        ensure_channel_intelligence(db, channel)
    db.commit()

    domain_ids = set(
        db.scalars(
            select(Domain.id)
            .outerjoin(YouTubeDomainSignal, YouTubeDomainSignal.domain_id == Domain.id)
            .where(
                YouTubeDomainSignal.domain_id.is_(None),
                Domain.video_links.any(VideoDomain.active.is_(True)),
            )
            .order_by(Domain.id.asc())
            .limit(settings.youtube_intelligence_backfill_batch_size)
        ).all()
    )
    signals = refresh_youtube_domain_signals(db, settings, domain_ids)
    matches = refresh_local_dropped_matches(
        db,
        limit=settings.youtube_local_match_batch_size,
    )
    return {
        "channels_backfilled": len(channels),
        "domain_signals_backfilled": signals,
        **matches,
    }
