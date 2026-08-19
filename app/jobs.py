from __future__ import annotations

import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.availability import AvailabilityResult, check_domain
from app.commoncrawl_prefilter import run_commoncrawl_prefilter as run_commoncrawl_prefilter_batch
from app.config import EVERGREEN_QUERIES, MANUAL_CHECKPOINTS, Settings, get_settings
from app.database import SessionLocal
from app.domain_tools import extract_domain_names, extract_links
from app.emailer import (
    DailyDigest,
    EmailCandidate,
    EmailError,
    EmailPendingCandidate,
    EmailRunIssue,
    EmailWebOpportunity,
    render_candidate_table,
    render_daily_digest,
    send_email,
)
from app.hackernews_prefilter import run_hackernews_prefilter as run_hackernews_prefilter_batch
from app.metrics import ViewMetric, calculate_monthly_views
from app.models import (
    AppCheckpoint,
    Candidate,
    Domain,
    DroppedDomain,
    FetchVerification,
    Opportunity,
    ProviderQuery,
    RunLog,
    SearchState,
    SourcePage,
    SourceSite,
    Video,
    VideoDomain,
    VideoRefreshState,
    ViewSnapshot,
    YouTubeChannel,
    utcnow,
)
from app.scoring import ScoreInputs, calculate_score, determine_tier
from app.stackexchange_prefilter import run_stackexchange_prefilter as run_stackexchange_prefilter_batch
from app.youtube import YouTubeClient, YouTubeVideo, exact_domain_in_description

logger = logging.getLogger(__name__)


def _start_run(db: Session, job: str) -> RunLog:
    run = RunLog(job=job, started_at=utcnow(), status="running", counters={})
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _finish_run(
    db: Session,
    run: RunLog,
    status: str,
    counters: dict[str, Any],
    error: str | None = None,
) -> None:
    run.status = status
    run.finished_at = utcnow()
    run.counters = counters
    run.error = error[:2000] if error else None
    db.commit()


def ensure_seed_data(db: Session) -> None:
    existing_queries = set(db.scalars(select(SearchState.query)).all())
    for query in EVERGREEN_QUERIES:
        if query not in existing_queries:
            db.add(SearchState(query=query))

    checkpoint = db.get(AppCheckpoint, "legacy_manual_test")
    if checkpoint is None:
        settings = get_settings()
        db.add(
            AppCheckpoint(
                key="legacy_manual_test",
                value={
                    "dropped_domains_checked": settings.legacy_dropped_checked,
                    "videos_checked": settings.legacy_videos_checked,
                    "external_domains_checked": settings.legacy_domains_checked,
                    "known_exact_hits": MANUAL_CHECKPOINTS,
                },
            )
        )
    db.commit()


def _upsert_snapshot(db: Session, video_id: str, view_count: int, captured_at: datetime) -> bool:
    capture_date = captured_at.date()
    snapshot = db.scalar(
        select(ViewSnapshot).where(
            ViewSnapshot.video_id == video_id,
            ViewSnapshot.capture_date == capture_date,
        )
    )
    if snapshot:
        snapshot.captured_at = captured_at
        snapshot.view_count = view_count
        return False
    db.add(
        ViewSnapshot(
            video_id=video_id,
            captured_at=captured_at,
            capture_date=capture_date,
            view_count=view_count,
        )
    )
    return True


def seed_youtube_channels(
    db: Session,
    videos: list[YouTubeVideo],
    *,
    count_as_search_seed: bool,
) -> int:
    """Persist channel seeds in batches so search results become crawl inventories."""
    by_id = {video.channel_id: video.channel_title for video in videos if video.channel_id}
    if not by_id:
        return 0

    existing: dict[str, YouTubeChannel] = {}
    channel_ids = list(by_id)
    for start in range(0, len(channel_ids), 500):
        rows = db.scalars(
            select(YouTubeChannel).where(YouTubeChannel.channel_id.in_(channel_ids[start : start + 500]))
        ).all()
        existing.update({row.channel_id: row for row in rows})

    now = utcnow()
    created = 0
    for channel_id, title in by_id.items():
        channel = existing.get(channel_id)
        if channel is None:
            channel = YouTubeChannel(
                channel_id=channel_id,
                title=title,
                seed_count=1 if count_as_search_seed else 0,
                last_seeded_at=now,
            )
            db.add(channel)
            existing[channel_id] = channel
            created += 1
        else:
            channel.title = title or channel.title
            channel.last_seeded_at = now
            if count_as_search_seed:
                channel.seed_count += 1
    return created


def _initial_refresh_interval_hours(view_count: int) -> int:
    if view_count >= 1_000_000:
        return 6
    if view_count >= 100_000:
        return 24
    if view_count >= 10_000:
        return 72
    return 168


def _ensure_video_refresh_state(
    db: Session,
    video: Video,
    external_link_count: int,
) -> None:
    if external_link_count <= 0:
        state = db.get(VideoRefreshState, video.id)
        if state is not None:
            db.delete(state)
        return
    state = db.get(VideoRefreshState, video.id)
    interval = _initial_refresh_interval_hours(video.lifetime_views)
    priority = round(
        10.0 * math.log10(1 + max(0, video.lifetime_views))
        + 5.0 * math.log2(1 + external_link_count),
        2,
    )
    if state is None:
        db.add(
            VideoRefreshState(
                video_id=video.id,
                next_refresh_at=utcnow() + timedelta(hours=interval),
                refresh_interval_hours=interval,
                priority_score=priority,
                last_view_count=video.lifetime_views,
            )
        )
        return
    state.priority_score = max(state.priority_score, priority)


def process_video(
    db: Session,
    item: YouTubeVideo,
    discovery_query: str,
    discovery_route: str,
    affected_domain_ids: set[int] | None = None,
) -> dict[str, int]:
    now = utcnow()
    counters = {
        "new_videos": 0,
        "new_domains": 0,
        "new_links": 0,
        "snapshots": 0,
        "external_links": 0,
    }
    video = db.get(Video, item.id)
    if video is None:
        video = Video(id=item.id)
        db.add(video)
        counters["new_videos"] += 1
    video.title = item.title
    video.channel_id = item.channel_id
    video.channel_title = item.channel_title
    video.description = item.description
    video.published_at = item.published_at
    video.lifetime_views = item.view_count
    video.discovery_query = video.discovery_query or discovery_query
    video.discovery_route = video.discovery_route or discovery_route
    video.last_seen_at = now
    video.active = True
    db.flush()

    if _upsert_snapshot(db, video.id, item.view_count, now):
        counters["snapshots"] += 1

    existing_links = db.scalars(select(VideoDomain).where(VideoDomain.video_id == video.id)).all()
    for link in existing_links:
        if affected_domain_ids is not None:
            affected_domain_ids.add(link.domain_id)
        link.active = False

    extracted_links = extract_links(item.description)
    counters["external_links"] = len(extracted_links)
    for extracted in extracted_links:
        domain = db.scalar(select(Domain).where(Domain.name == extracted.domain))
        if domain is None:
            domain = Domain(name=extracted.domain, suffix=extracted.suffix)
            db.add(domain)
            db.flush()
            counters["new_domains"] += 1
        if affected_domain_ids is not None:
            affected_domain_ids.add(domain.id)

        link = db.scalar(
            select(VideoDomain).where(
                VideoDomain.video_id == video.id,
                VideoDomain.domain_id == domain.id,
                VideoDomain.raw_url == extracted.raw_url,
            )
        )
        if link is None:
            link = VideoDomain(
                video_id=video.id,
                domain_id=domain.id,
                raw_url=extracted.raw_url,
                normalized_url=extracted.normalized_url,
            )
            db.add(link)
            counters["new_links"] += 1
        link.description_position = extracted.position
        link.context = extracted.context
        link.has_cta = extracted.has_cta
        link.clickable = extracted.clickable
        link.active = True
        link.last_seen_at = now

    _ensure_video_refresh_state(db, video, len(extracted_links))
    return counters


def seed_manual_checkpoint() -> None:
    settings = get_settings()
    if not settings.youtube_api_key:
        return
    with SessionLocal() as db:
        ensure_seed_data(db)
        run = _start_run(db, "seed_checkpoint")
        counters = {
            "videos_requested": len(MANUAL_CHECKPOINTS),
            "videos_found": 0,
            "new_domains": 0,
        }
        try:
            client = YouTubeClient(settings.youtube_api_key)
            videos = client.fetch_videos(MANUAL_CHECKPOINTS.keys())
            seed_youtube_channels(db, videos, count_as_search_seed=False)
            counters["videos_found"] = len(videos)
            affected_domain_ids: set[int] = set()
            for video in videos:
                exact_domain = MANUAL_CHECKPOINTS.get(video.id, "")
                if exact_domain and not exact_domain_in_description(
                    exact_domain, video.description
                ):
                    logger.info("Manual checkpoint link no longer appears in %s", video.id)
                result = process_video(
                    db,
                    video,
                    "legacy manual checkpoint",
                    "manual_checkpoint",
                    affected_domain_ids,
                )
                counters["new_domains"] += result["new_domains"]
            db.commit()
            refresh_candidates(db, affected_domain_ids)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Manual checkpoint seed failed")


def _next_search_state(db: Session) -> SearchState | None:
    return db.scalar(
        select(SearchState)
        .order_by(
            case((SearchState.last_run_at.is_(None), 0), else_=1),
            SearchState.last_run_at.asc(),
            SearchState.id.asc(),
        )
        .limit(1)
    )


def run_discovery() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        ensure_seed_data(db)
        run = _start_run(db, "youtube_discovery")
        counters = {
            "search_calls": 0,
            "videos_returned": 0,
            "new_videos": 0,
            "new_domains": 0,
            "new_links": 0,
        }
        try:
            client = YouTubeClient(settings.youtube_api_key)
            affected_domain_ids: set[int] = set()
            published_before = datetime.now(UTC) - timedelta(
                days=365 * settings.published_before_years
            )
            for _ in range(settings.search_calls_per_run):
                state = _next_search_state(db)
                if state is None:
                    break
                page = client.search_videos(
                    state.query,
                    published_before=published_before,
                    page_token=state.page_token,
                )
                counters["search_calls"] += 1
                counters["videos_returned"] += len(page.video_ids)
                videos = client.fetch_videos(page.video_ids)
                seed_youtube_channels(db, videos, count_as_search_seed=True)
                for video in videos:
                    result = process_video(
                        db,
                        video,
                        state.query,
                        "youtube_first",
                        affected_domain_ids,
                    )
                    for key in ("new_videos", "new_domains", "new_links"):
                        counters[key] += result[key]
                state.page_token = page.next_page_token
                state.pages_scanned += 1
                state.last_run_at = utcnow()
                if not page.next_page_token:
                    state.pages_scanned = 0
                db.commit()

            refresh_candidates(db, affected_domain_ids)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("YouTube discovery failed")


def _resolve_channel_upload_playlists(
    db: Session,
    client: YouTubeClient,
    *,
    limit: int = 50,
) -> dict[str, int]:
    retry_before = utcnow() - timedelta(hours=24)
    channels = db.scalars(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.uploads_playlist_id == "",
            or_(
                YouTubeChannel.last_crawled_at.is_(None),
                YouTubeChannel.last_crawled_at <= retry_before,
            ),
        )
        .order_by(YouTubeChannel.seed_count.desc(), YouTubeChannel.last_seeded_at.desc())
        .limit(min(limit, 50))
    ).all()
    if not channels:
        return {"channel_calls": 0, "channels_resolved": 0, "channel_resolution_errors": 0}

    details = {item.id: item for item in client.fetch_channels([row.channel_id for row in channels])}
    now = utcnow()
    resolved = 0
    errors = 0
    for channel in channels:
        detail = details.get(channel.channel_id)
        channel.last_crawled_at = now
        if detail is None:
            channel.last_error = "YouTube channel or uploads playlist was not returned"
            errors += 1
            continue
        channel.title = detail.title or channel.title
        channel.uploads_playlist_id = detail.uploads_playlist_id
        channel.last_error = None
        resolved += 1
    db.commit()
    return {
        "channel_calls": 1,
        "channels_resolved": resolved,
        "channel_resolution_errors": errors,
    }


def _next_channel_to_crawl(
    db: Session,
    settings: Settings,
    excluded_channel_ids: set[str],
) -> YouTubeChannel | None:
    recrawl_before = utcnow() - timedelta(hours=settings.youtube_channel_recrawl_hours)
    statement = select(YouTubeChannel).where(
        YouTubeChannel.uploads_playlist_id != "",
        or_(
            YouTubeChannel.inventory_complete.is_(False),
            YouTubeChannel.next_page_token.is_not(None),
            YouTubeChannel.last_crawled_at.is_(None),
            YouTubeChannel.last_crawled_at <= recrawl_before,
        ),
    )
    if excluded_channel_ids:
        statement = statement.where(YouTubeChannel.channel_id.not_in(excluded_channel_ids))
    return db.scalar(
        statement.order_by(
            case((YouTubeChannel.next_page_token.is_not(None), 0), else_=1),
            case((YouTubeChannel.inventory_complete.is_(False), 0), else_=1),
            YouTubeChannel.yield_score.desc(),
            YouTubeChannel.seed_count.desc(),
            YouTubeChannel.last_crawled_at.asc(),
            YouTubeChannel.channel_id.asc(),
        ).limit(1)
    )


def _update_channel_yield(channel: YouTubeChannel) -> None:
    if channel.videos_indexed <= 0:
        channel.yield_score = 0.0
        return
    linked_video_rate = channel.videos_with_external_links / channel.videos_indexed
    links_per_video = channel.external_links_found / channel.videos_indexed
    seed_bonus = min(10.0, 2.5 * math.log2(1 + max(0, channel.seed_count)))
    channel.yield_score = round(
        min(100.0, 60.0 * linked_video_rate + min(30.0, 12.0 * links_per_video) + seed_bonus),
        2,
    )


def run_channel_fanout_batch(
    db: Session,
    settings: Settings,
    client: YouTubeClient,
) -> dict[str, Any]:
    """Turn search-seeded channels into a resumable, permanent outbound-link index."""
    counters: dict[str, Any] = {
        "channel_calls": 0,
        "channels_resolved": 0,
        "channel_resolution_errors": 0,
        "playlist_calls": 0,
        "video_detail_calls": 0,
        "videos_discovered": 0,
        "videos_fetched": 0,
        "new_videos": 0,
        "new_domains": 0,
        "new_links": 0,
        "channels_completed": 0,
        "refresh_stops_on_known_video": 0,
        "quota_units_estimate": 0,
        "errors": 0,
        "error_details": [],
    }
    try:
        resolution = _resolve_channel_upload_playlists(db, client)
        counters.update(resolution)
    except Exception as exc:
        db.rollback()
        counters["errors"] += 1
        counters["error_details"].append(f"channel resolution: {exc}"[:500])

    pages_by_channel: dict[str, int] = {}
    exhausted_for_run: set[str] = set()
    affected_domain_ids: set[int] = set()
    for _ in range(settings.youtube_channel_pages_per_run):
        channel = _next_channel_to_crawl(db, settings, exhausted_for_run)
        if channel is None:
            break
        try:
            page = client.fetch_uploads_page(
                channel.uploads_playlist_id,
                page_token=channel.next_page_token,
                max_results=50,
            )
            counters["playlist_calls"] += 1
            counters["videos_discovered"] += len(page.video_ids)

            known_ids = set(
                db.scalars(select(Video.id).where(Video.id.in_(page.video_ids))).all()
            )
            candidate_ids = page.video_ids
            stopped_on_known = False
            if channel.inventory_complete and known_ids:
                first_known = min(
                    index for index, video_id in enumerate(page.video_ids) if video_id in known_ids
                )
                candidate_ids = page.video_ids[:first_known]
                stopped_on_known = True
                counters["refresh_stops_on_known_video"] += 1

            new_ids = [video_id for video_id in candidate_ids if video_id not in known_ids]
            videos = client.fetch_videos(new_ids) if new_ids else []
            counters["video_detail_calls"] += (len(new_ids) + 49) // 50
            counters["videos_fetched"] += len(videos)
            seed_youtube_channels(db, videos, count_as_search_seed=False)

            linked_videos = 0
            external_links = 0
            for video in videos:
                result = process_video(
                    db,
                    video,
                    channel.title,
                    "channel_fanout",
                    affected_domain_ids,
                )
                for key in ("new_videos", "new_domains", "new_links"):
                    counters[key] += result[key]
                external_links += result["external_links"]
                linked_videos += int(result["external_links"] > 0)

            channel.pages_scanned += 1
            channel.videos_seen += len(page.video_ids)
            channel.videos_indexed += len(videos)
            channel.videos_with_external_links += linked_videos
            channel.external_links_found += external_links
            channel.last_crawled_at = utcnow()
            channel.last_error = None

            if stopped_on_known or not page.next_page_token:
                channel.next_page_token = None
                channel.inventory_complete = True
                counters["channels_completed"] += 1
            else:
                channel.next_page_token = page.next_page_token
            _update_channel_yield(channel)
            db.commit()

            pages_by_channel[channel.channel_id] = pages_by_channel.get(channel.channel_id, 0) + 1
            if pages_by_channel[channel.channel_id] >= settings.youtube_channel_page_burst:
                exhausted_for_run.add(channel.channel_id)
        except Exception as exc:
            db.rollback()
            current = db.get(YouTubeChannel, channel.channel_id)
            if current is not None:
                current.last_crawled_at = utcnow()
                current.last_error = str(exc)[:2000]
                db.commit()
            exhausted_for_run.add(channel.channel_id)
            counters["errors"] += 1
            if len(counters["error_details"]) < 20:
                counters["error_details"].append(f"{channel.channel_id}: {exc}"[:500])

    counters["quota_units_estimate"] = (
        counters["channel_calls"] + counters["playlist_calls"] + counters["video_detail_calls"]
    )
    counters["candidates_refreshed"] = refresh_candidates(db, affected_domain_ids)
    return counters


def run_channel_fanout() -> None:
    settings = get_settings()
    if not settings.youtube_channel_fanout_enabled:
        return
    with SessionLocal() as db:
        run = _start_run(db, "youtube_channel_fanout")
        counters: dict[str, Any] = {}
        try:
            counters = run_channel_fanout_batch(
                db,
                settings,
                YouTubeClient(settings.youtube_api_key),
            )
            status = "complete" if not counters.get("errors") else "partial"
            _finish_run(db, run, status, counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("YouTube channel fan-out failed")


def _adaptive_refresh_interval_hours(
    state: VideoRefreshState,
    current_view_count: int,
) -> tuple[int, int, float]:
    growth = max(0, current_view_count - state.last_view_count)
    low_growth_runs = state.consecutive_low_growth + 1 if growth < 10 else 0
    if growth >= 1_000 or current_view_count >= 5_000_000:
        interval = 6
    elif growth >= 100 or current_view_count >= 500_000:
        interval = 24
    elif growth > 0 or current_view_count >= 50_000:
        interval = 72
    else:
        interval = 168
    if low_growth_runs >= 3:
        interval = max(interval, 168 if current_view_count >= 100_000 else 720)
    priority = round(
        10.0 * math.log10(1 + current_view_count) + 8.0 * math.log10(1 + growth),
        2,
    )
    return interval, low_growth_runs, priority


def run_view_snapshot_batch(
    db: Session,
    settings: Settings,
    client: YouTubeClient,
) -> dict[str, int]:
    now = utcnow()
    ids = db.scalars(
        select(VideoRefreshState.video_id)
        .join(Video, Video.id == VideoRefreshState.video_id)
        .where(
            VideoRefreshState.next_refresh_at <= now,
            Video.active.is_(True),
        )
        .order_by(
            VideoRefreshState.priority_score.desc(),
            VideoRefreshState.next_refresh_at.asc(),
        )
        .limit(settings.youtube_view_refresh_batch_size)
    ).all()
    counters = {
        "videos_due": len(ids),
        "videos_requested": len(ids),
        "videos_updated": 0,
        "videos_inactive": 0,
        "snapshots": 0,
        "statistics_calls": 0,
        "quota_units_estimate": 0,
    }

    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        returned = {item.id: item for item in client.fetch_video_statistics(batch)}
        counters["statistics_calls"] += (len(batch) + 49) // 50
        captured_at = utcnow()
        for video_id in batch:
            video = db.get(Video, video_id)
            state = db.get(VideoRefreshState, video_id)
            if video is None or state is None:
                continue
            item = returned.get(video_id)
            state.last_refreshed_at = captured_at
            if item is None:
                video.active = False
                state.refresh_interval_hours = 720
                state.next_refresh_at = captured_at + timedelta(hours=720)
                counters["videos_inactive"] += 1
                continue

            interval, low_growth_runs, priority = _adaptive_refresh_interval_hours(
                state, item.view_count
            )
            video.lifetime_views = item.view_count
            video.last_seen_at = captured_at
            state.last_view_count = item.view_count
            state.refresh_interval_hours = interval
            state.next_refresh_at = captured_at + timedelta(hours=interval)
            state.consecutive_low_growth = low_growth_runs
            state.priority_score = priority
            if _upsert_snapshot(db, item.id, item.view_count, captured_at):
                counters["snapshots"] += 1
            counters["videos_updated"] += 1
        db.commit()

    counters["quota_units_estimate"] = counters["statistics_calls"]
    affected_domain_ids = set(
        db.scalars(
            select(VideoDomain.domain_id).where(
                VideoDomain.video_id.in_(ids),
                VideoDomain.active.is_(True),
            )
        ).all()
    )
    counters["candidates_refreshed"] = refresh_candidates(db, affected_domain_ids)
    return counters


def run_view_snapshots() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "view_snapshots")
        counters: dict[str, Any] = {}
        try:
            counters = run_view_snapshot_batch(
                db,
                settings,
                YouTubeClient(settings.youtube_api_key),
            )
            send_new_candidate_alerts(db)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("View snapshot job failed")


def _domains_due_for_check(db: Session, limit: int) -> list[Domain]:
    now = utcnow()
    one_day = now - timedelta(days=1)
    seven_days = now - timedelta(days=7)
    return db.scalars(
        select(Domain)
        .where(
            Domain.excluded_reason.is_(None),
            or_(
                Domain.last_checked_at.is_(None),
                and_(
                    Domain.availability_status.in_(["unknown", "likely_available", "conflicting"]),
                    Domain.last_checked_at < one_day,
                ),
                and_(Domain.availability_status == "available", Domain.last_checked_at < one_day),
                and_(
                    Domain.availability_status.in_(["registered", "premium"]),
                    Domain.last_checked_at < seven_days,
                ),
            ),
            Domain.video_links.any(VideoDomain.active.is_(True)),
        )
        .order_by(Domain.last_checked_at.asc(), Domain.first_seen_at.asc())
        .limit(limit)
    ).all()


def _apply_availability(domain: Domain, result: AvailabilityResult) -> None:
    domain.availability_status = result.status
    domain.availability_source = result.source
    domain.rdap_status = result.rdap_status
    domain.dns_status = result.dns_status
    domain.http_status = result.http_status
    domain.registrar_price_usd = result.price_usd
    domain.premium = result.premium
    domain.check_error = result.error
    domain.last_checked_at = utcnow()


def run_availability_checks() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "availability_checks")
        counters: dict[str, Any] = {
            "checked": 0,
            "available": 0,
            "likely_available": 0,
            "registered": 0,
            "errors": 0,
            "error_details": [],
        }
        try:
            domains = _domains_due_for_check(db, settings.availability_batch_size)
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(
                        check_domain,
                        domain.name,
                        settings,
                        bool(
                            domain.availability_status == "available"
                            or (
                                domain.candidate
                                and domain.candidate.monthly_views
                                >= settings.watchlist_monthly_views
                            )
                        ),
                    ): domain.id
                    for domain in domains
                }
                for future in as_completed(futures):
                    domain = db.get(Domain, futures[future])
                    if domain is None:
                        continue
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = AvailabilityResult(
                            status="unknown",
                            source="error",
                            rdap_status="error",
                            dns_status="unknown",
                            error=str(exc),
                        )
                    _apply_availability(domain, result)
                    counters["checked"] += 1
                    if result.status in counters:
                        counters[result.status] += 1
                    if result.error:
                        counters["errors"] += 1
                        if len(counters["error_details"]) < 20:
                            counters["error_details"].append(
                                f"{domain.name}: {result.error}"[:500]
                            )
            db.commit()
            refresh_candidates(db, {domain.id for domain in domains})
            send_new_candidate_alerts(db)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Availability job failed")


def _best_link_for_video(links: list[VideoDomain]) -> VideoDomain:
    return min(
        links,
        key=lambda link: (
            0 if link.has_cta else 1,
            0 if link.clickable else 1,
            link.description_position,
        ),
    )


def refresh_candidates(db: Session, domain_ids: set[int] | None = None) -> int:
    settings = get_settings()
    if domain_ids is not None and not domain_ids:
        return 0
    statement = select(Domain).where(Domain.video_links.any(VideoDomain.active.is_(True)))
    if domain_ids is not None:
        statement = statement.where(Domain.id.in_(domain_ids))
    domains = db.scalars(
        statement.options(
            selectinload(Domain.video_links)
            .selectinload(VideoDomain.video)
            .selectinload(Video.snapshots)
        )
    ).all()
    updated = 0
    for domain in domains:
        active_links = [
            link for link in domain.video_links if link.active and link.video and link.video.active
        ]
        if not active_links:
            continue
        by_video: dict[str, list[VideoDomain]] = {}
        for link in active_links:
            by_video.setdefault(link.video_id, []).append(link)

        best_video: Video | None = None
        best_link: VideoDomain | None = None
        best_metric = ViewMetric(0, False, 0.0, 0)
        for video_links in by_video.values():
            video = video_links[0].video
            metric = calculate_monthly_views(video.snapshots)
            if (
                best_video is None
                or metric.monthly_views > best_metric.monthly_views
                or (
                    metric.monthly_views == best_metric.monthly_views
                    and video.lifetime_views > best_video.lifetime_views
                )
            ):
                best_video = video
                best_link = _best_link_for_video(video_links)
                best_metric = metric

        if best_video is None or best_link is None:
            continue
        tier = determine_tier(
            best_metric.monthly_views,
            best_metric.verified_30d,
            domain.availability_status,
            settings,
        )
        score = calculate_score(
            ScoreInputs(
                monthly_views=best_metric.monthly_views,
                lifetime_views=best_video.lifetime_views,
                link_position=best_link.description_position,
                has_cta=best_link.has_cta,
                clickable=best_link.clickable,
                video_count=len(by_video),
                link_count=len(active_links),
                published_at=best_video.published_at,
                availability_status=domain.availability_status,
            )
        )
        candidate = domain.candidate
        if candidate is None:
            candidate = Candidate(domain_id=domain.id)
            db.add(candidate)
        candidate.tier = tier
        candidate.monthly_views = best_metric.monthly_views
        candidate.verified_30d = best_metric.verified_30d
        candidate.observation_days = best_metric.observation_days
        candidate.score = score
        candidate.video_count = len(by_video)
        candidate.link_count = len(active_links)
        candidate.best_video_id = best_video.id
        candidate.updated_at = utcnow()
        updated += 1
    db.commit()
    return updated


def ingest_dropped_text(db: Session, text: str, source: str) -> dict[str, int]:
    domains = extract_domain_names(text)
    counters = {"parsed": len(domains), "new": 0, "matched_index": 0}
    if not domains:
        return counters

    # Feed files contain thousands of names. Batch the lookups so Railway does
    # dozens of database round trips instead of two round trips per domain.
    dropped_by_name: dict[str, DroppedDomain] = {}
    for start in range(0, len(domains), 500):
        batch = domains[start : start + 500]
        existing = db.scalars(
            select(DroppedDomain).where(DroppedDomain.name.in_(batch))
        ).all()
        dropped_by_name.update({item.name: item for item in existing})

    for name in domains:
        if name in dropped_by_name:
            continue
        dropped = DroppedDomain(name=name, source=source)
        db.add(dropped)
        dropped_by_name[name] = dropped
        counters["new"] += 1

    matched_domains: dict[str, Domain] = {}
    for start in range(0, len(domains), 500):
        batch = domains[start : start + 500]
        linked = db.scalars(
            select(Domain).where(
                Domain.name.in_(batch),
                Domain.video_links.any(VideoDomain.active.is_(True)),
            )
        ).all()
        matched_domains.update({item.name: item for item in linked})

    for name, linked_domain in matched_domains.items():
        dropped_by_name[name].matched_existing_index = True
        linked_domain.last_checked_at = None
        counters["matched_index"] += 1
    db.commit()
    return counters


def run_dropped_feeds() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "dropped_feeds")
        counters: dict[str, Any] = {
            "configured": len(settings.dropped_domain_feed_urls),
            "feeds": 0,
            "parsed": 0,
            "new": 0,
            "matched_index": 0,
            "errors": 0,
            "error_details": [],
        }
        try:
            if not settings.dropped_domain_feed_urls:
                _finish_run(
                    db,
                    run,
                    "failed",
                    counters,
                    "No dropped-domain feed URLs are configured",
                )
                return
            for url in settings.dropped_domain_feed_urls:
                try:
                    response = httpx.get(
                        url,
                        headers={"User-Agent": "YouTubeDomainCrawler/0.2"},
                        follow_redirects=True,
                        timeout=45.0,
                    )
                    response.raise_for_status()
                    result = ingest_dropped_text(db, response.text, url)
                    counters["feeds"] += 1
                    for key in ("parsed", "new", "matched_index"):
                        counters[key] += result[key]
                except Exception as exc:
                    counters["errors"] += 1
                    counters["error_details"].append(f"{url}: {exc}"[:500])
                    logger.exception("Dropped-domain feed failed: %s", url)
            if counters["feeds"] == 0:
                details = "; ".join(counters["error_details"]) or "Every feed failed"
                _finish_run(db, run, "failed", counters, details)
            else:
                _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))


def _dropped_domains_due_for_youtube_search(
    db: Session,
    max_searches: int,
) -> list[DroppedDomain]:
    return db.scalars(
        select(DroppedDomain)
        .where(
            DroppedDomain.youtube_searched_at.is_(None),
            DroppedDomain.matched_existing_index.is_(False),
        )
        .order_by(
            case((DroppedDomain.name.like("%.com"), 0), else_=1),
            func.length(DroppedDomain.name).asc(),
            DroppedDomain.first_seen_at.asc(),
        )
        .limit(max_searches)
    ).all()


def run_dropped_youtube_search(max_searches: int = 10) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "dropped_youtube_search")
        counters = {
            "search_calls": 0,
            "drops_checked": 0,
            "videos_returned": 0,
            "exact_matches": 0,
            "new_videos": 0,
            "new_domains": 0,
            "new_links": 0,
        }
        try:
            affected_domain_ids: set[int] = set()
            candidates = _dropped_domains_due_for_youtube_search(db, max_searches)
            client = YouTubeClient(settings.youtube_api_key)
            published_before = datetime.now(UTC) - timedelta(
                days=365 * settings.published_before_years
            )
            for dropped in candidates:
                page = client.search_videos(dropped.name, published_before, max_results=50)
                counters["search_calls"] += 1
                counters["drops_checked"] += 1
                counters["videos_returned"] += len(page.video_ids)
                matched_videos = [
                    video
                    for video in client.fetch_videos(page.video_ids)
                    if exact_domain_in_description(dropped.name, video.description)
                ]
                seed_youtube_channels(db, matched_videos, count_as_search_seed=True)
                for video in matched_videos:
                    result = process_video(
                        db,
                        video,
                        dropped.name,
                        "dropped_first",
                        affected_domain_ids,
                    )
                    for key in ("new_videos", "new_domains", "new_links"):
                        counters[key] += result[key]
                    counters["exact_matches"] += 1
                dropped.youtube_searched_at = utcnow()
                db.commit()
            refresh_candidates(db, affected_domain_ids)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Dropped-domain YouTube search failed")


def _email_candidates(
    db: Session, only_unnotified: bool = False
) -> list[tuple[Candidate, Domain, Video]]:
    statement = (
        select(Candidate, Domain, Video)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
        .where(Candidate.tier.in_(["priority", "qualified"]))
        .order_by(
            case((Candidate.tier == "priority", 0), else_=1),
            Candidate.score.desc(),
        )
    )
    rows = db.execute(statement).all()
    if only_unnotified:
        rows = [row for row in rows if row[0].notified_tier != row[0].tier]
    return rows


def _to_email_candidates(rows: list[tuple[Candidate, Domain, Video]]) -> list[EmailCandidate]:
    return [
        EmailCandidate(
            domain=domain.name,
            tier=candidate.tier,
            monthly_views=candidate.monthly_views,
            score=candidate.score,
            video_title=video.title,
            video_id=video.id,
            price_usd=domain.registrar_price_usd,
        )
        for candidate, domain, video in rows
    ]


def send_new_candidate_alerts(db: Session) -> int:
    settings = get_settings()
    if not settings.email_enabled:
        return 0
    rows = _email_candidates(db, only_unnotified=True)
    if not rows:
        return 0
    items = _to_email_candidates(rows)
    subject = f"Domain crawler: {len(items)} new qualified hit{'s' if len(items) != 1 else ''}"
    body = (
        "<h2>New YouTube expired-domain hit</h2>"
        "<p>These domains passed ordinary-registration availability and the "
        "verified 30-day view threshold.</p>" + render_candidate_table(items)
    )
    send_email(settings, subject, body)
    now = utcnow()
    for candidate, _, _ in rows:
        candidate.notified_tier = candidate.tier
        candidate.notified_at = now
    db.commit()
    return len(rows)


def _counter_total(runs: list[RunLog], key: str, job: str | None = None) -> int:
    total = 0
    for item in runs:
        if job is not None and item.job != job:
            continue
        counters = item.counters if isinstance(item.counters, dict) else {}
        value = counters.get(key, 0)
        if isinstance(value, int | float):
            total += int(value)
    return total


def _pending_reason(candidate: Candidate, domain: Domain, settings: Settings) -> str:
    if candidate.observation_days < 1:
        return "A second daily view snapshot"
    if (
        candidate.monthly_views >= settings.watchlist_monthly_views
        and domain.availability_status in {"unknown", "likely_available", "conflicting"}
    ):
        return "Exact registrar confirmation"
    if not candidate.verified_30d and candidate.monthly_views >= settings.watchlist_monthly_views:
        return "A full 27-day traffic window"
    if candidate.monthly_views < settings.watchlist_monthly_views:
        return f"Traffic to reach {settings.watchlist_monthly_views:,}/month"
    return "Final traffic and availability verification"


def _build_daily_digest_report(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
    current_run_id: int | None = None,
) -> DailyDigest:
    report_time = now or utcnow()
    recent_cutoff = report_time - timedelta(hours=24)
    recent_statement = select(RunLog).where(RunLog.started_at >= recent_cutoff)
    if current_run_id is not None:
        recent_statement = recent_statement.where(RunLog.id != current_run_id)
    recent_runs = db.scalars(recent_statement.order_by(RunLog.started_at.desc())).all()

    qualified_rows = _email_candidates(db)
    priority_count = sum(1 for candidate, _, _ in qualified_rows if candidate.tier == "priority")
    qualified_count = sum(
        1 for candidate, _, _ in qualified_rows if candidate.tier == "qualified"
    )
    watchlist_count = (
        db.scalar(
            select(func.count()).select_from(Candidate).where(Candidate.tier == "watchlist")
        )
        or 0
    )
    pending_count = (
        db.scalar(select(func.count()).select_from(Candidate).where(Candidate.tier == "pending"))
        or 0
    )

    pipeline_rows = db.execute(
        select(Candidate, Domain)
        .join(Domain, Domain.id == Candidate.domain_id)
        .where(Candidate.tier.in_(["pending", "watchlist"]))
    ).all()
    pending_summary = {
        "total": len(pipeline_rows),
        "initial": sum(1 for candidate, _ in pipeline_rows if candidate.observation_days < 1),
        "projected": sum(
            1 for candidate, _ in pipeline_rows if 1 <= candidate.observation_days < 27
        ),
        "verification": sum(
            1
            for candidate, _ in pipeline_rows
            if not candidate.verified_30d
            and candidate.monthly_views >= settings.qualified_monthly_views
        ),
        "registrar": sum(
            1
            for candidate, domain in pipeline_rows
            if candidate.monthly_views >= settings.watchlist_monthly_views
            and domain.availability_status in {"unknown", "likely_available", "conflicting"}
        ),
    }

    pending_rows = db.execute(
        select(Candidate, Domain, Video)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
        .where(Candidate.tier.in_(["watchlist", "pending"]))
        .order_by(
            case((Candidate.tier == "watchlist", 0), else_=1),
            Candidate.monthly_views.desc(),
            Candidate.score.desc(),
            Video.lifetime_views.desc(),
        )
        .limit(10)
    ).all()
    pending_candidates = [
        EmailPendingCandidate(
            domain=domain.name,
            tier=candidate.tier,
            monthly_views=candidate.monthly_views,
            observation_days=candidate.observation_days,
            availability=domain.availability_status,
            score=candidate.score,
            video_title=video.title,
            video_id=video.id,
            reason=_pending_reason(candidate, domain, settings),
        )
        for candidate, domain, video in pending_rows
    ]

    web_tier_counts = dict(
        db.execute(select(Opportunity.tier, func.count()).group_by(Opportunity.tier)).all()
    )
    web_rows = db.execute(
        select(Opportunity, Domain, SourcePage, SourceSite)
        .join(Domain, Domain.id == Opportunity.domain_id)
        .outerjoin(SourcePage, SourcePage.id == Opportunity.best_source_page_id)
        .outerjoin(SourceSite, SourceSite.id == SourcePage.site_id)
        .where(Opportunity.tier.in_(["priority", "qualified", "watchlist", "pending"]))
        .order_by(
            case(
                (Opportunity.tier == "priority", 0),
                (Opportunity.tier == "qualified", 1),
                (Opportunity.tier == "watchlist", 2),
                else_=3,
            ),
            Opportunity.score.desc(),
            Opportunity.source_page_traffic_estimate.desc(),
        )
        .limit(15)
    ).all()
    web_opportunities = [
        EmailWebOpportunity(
            domain=domain.name,
            tier=opportunity.tier,
            score=opportunity.score,
            source_page_traffic=opportunity.source_page_traffic_estimate,
            referring_pages=opportunity.referring_page_count,
            independent_sites=opportunity.independent_site_count,
            niche=opportunity.niche,
            verified_live_link=opportunity.verified_live_link,
            availability=domain.availability_status,
            price_usd=domain.registrar_price_usd,
            source_site=site.hostname if site is not None else "",
            source_title=page.title if page is not None else "",
            source_url=page.url if page is not None else "",
        )
        for opportunity, domain, page, site in web_rows
    ]
    web_domains_checked_24h = (
        db.scalar(
            select(func.count())
            .select_from(ProviderQuery)
            .where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
                ProviderQuery.completed_at >= recent_cutoff,
            )
        )
        or 0
    )
    web_links_verified_24h = (
        db.scalar(
            select(func.count())
            .select_from(FetchVerification)
            .where(
                FetchVerification.link_present.is_(True),
                FetchVerification.fetched_at >= recent_cutoff,
            )
        )
        or 0
    )
    web_provider_cost_usd_24h = (
        db.scalar(
            select(func.sum(ProviderQuery.cost_usd)).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.status == "complete",
                ProviderQuery.completed_at >= recent_cutoff,
            )
        )
        or 0.0
    )

    availability_counts = dict(
        db.execute(
            select(Domain.availability_status, func.count())
            .where(Domain.excluded_reason.is_(None))
            .group_by(Domain.availability_status)
        ).all()
    )
    availability_summary = {
        "available": int(availability_counts.get("available", 0)),
        "likely_available": int(availability_counts.get("likely_available", 0)),
        "registered": int(availability_counts.get("registered", 0)),
        "premium_or_aftermarket": int(availability_counts.get("premium", 0))
        + int(availability_counts.get("aftermarket", 0)),
        "unknown_or_conflicting": int(availability_counts.get("unknown", 0))
        + int(availability_counts.get("conflicting", 0)),
    }

    work = {
        "successful_runs": sum(1 for item in recent_runs if item.status == "complete"),
        "failed_runs": sum(1 for item in recent_runs if item.status == "failed"),
        "search_calls": _counter_total(recent_runs, "search_calls"),
        "videos_returned": _counter_total(recent_runs, "videos_returned"),
        "new_videos": _counter_total(recent_runs, "new_videos"),
        "new_domains": _counter_total(recent_runs, "new_domains"),
        "new_links": _counter_total(recent_runs, "new_links"),
        "videos_updated": _counter_total(recent_runs, "videos_updated"),
        "availability_checked": _counter_total(recent_runs, "checked", "availability_checks"),
        "availability_errors": _counter_total(recent_runs, "errors", "availability_checks"),
        "drops_loaded": _counter_total(recent_runs, "new", "dropped_feeds"),
        "drops_searched": _counter_total(
            recent_runs, "drops_checked", "dropped_youtube_search"
        ),
        "dropped_matches": _counter_total(
            recent_runs, "exact_matches", "dropped_youtube_search"
        ),
    }

    issues: list[EmailRunIssue] = []
    for item in recent_runs:
        counters = item.counters if isinstance(item.counters, dict) else {}
        occurred_at = item.started_at.strftime("%d %b %H:%M UTC")
        if item.status == "failed":
            issues.append(
                EmailRunIssue(
                    job=item.job,
                    occurred_at=occurred_at,
                    message=item.error or "Job failed without an error message",
                )
            )
            continue
        error_count = counters.get("errors", 0)
        if isinstance(error_count, int | float) and error_count:
            details = counters.get("error_details", [])
            detail_text = "; ".join(str(value) for value in details[:3]) if details else ""
            message = f"{int(error_count)} item-level error(s)"
            if detail_text:
                message += f": {detail_text}"
            issues.append(
                EmailRunIssue(job=item.job, occurred_at=occurred_at, message=message)
            )

    crawler_videos = db.scalar(select(func.count()).select_from(Video)) or 0
    crawler_domains = db.scalar(select(func.count()).select_from(Domain)) or 0
    dropped_ingested = db.scalar(select(func.count()).select_from(DroppedDomain)) or 0
    exact_links = (
        db.scalar(select(func.count()).select_from(VideoDomain).where(VideoDomain.active.is_(True)))
        or 0
    )
    longest_observation = db.scalar(select(func.max(Candidate.observation_days))) or 0.0

    return DailyDigest(
        priority_count=priority_count,
        qualified_count=qualified_count,
        watchlist_count=int(watchlist_count),
        pending_count=int(pending_count),
        target=settings.target_qualified_domains,
        cumulative_videos=settings.legacy_videos_checked + int(crawler_videos),
        cumulative_domains=settings.legacy_domains_checked + int(crawler_domains),
        cumulative_dropped=settings.legacy_dropped_checked + int(dropped_ingested),
        exact_links=int(exact_links),
        longest_observation_days=float(longest_observation),
        feed_count=len(settings.dropped_domain_feed_urls),
        work=work,
        pending=pending_summary,
        availability=availability_summary,
        qualified_candidates=_to_email_candidates(qualified_rows[:25]),
        pending_candidates=pending_candidates,
        issues=issues,
        web_priority_count=int(web_tier_counts.get("priority", 0)),
        web_qualified_count=int(web_tier_counts.get("qualified", 0)),
        web_watchlist_count=int(web_tier_counts.get("watchlist", 0)),
        web_pending_count=int(web_tier_counts.get("pending", 0)),
        web_domains_checked_24h=int(web_domains_checked_24h),
        web_links_verified_24h=int(web_links_verified_24h),
        web_provider_cost_usd_24h=float(web_provider_cost_usd_24h),
        web_opportunities=web_opportunities,
    )


def run_daily_digest() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "daily_digest")
        counters = {
            "emailed": 0,
            "priority": 0,
            "qualified": 0,
            "watchlist": 0,
            "pending": 0,
            "web_priority": 0,
            "web_qualified": 0,
            "web_watchlist": 0,
            "web_pending": 0,
            "issues": 0,
        }
        try:
            report = _build_daily_digest_report(
                db,
                settings,
                current_run_id=run.id,
            )
            counters.update(
                {
                    "priority": report.priority_count,
                    "qualified": report.qualified_count,
                    "watchlist": report.watchlist_count,
                    "pending": report.pending_count,
                    "web_priority": report.web_priority_count,
                    "web_qualified": report.web_qualified_count,
                    "web_watchlist": report.web_watchlist_count,
                    "web_pending": report.web_pending_count,
                    "issues": len(report.issues),
                }
            )
            body = render_daily_digest(report)
            if settings.email_enabled:
                subject = (
                    "Daily crawler: "
                    f"{report.priority_count + report.qualified_count} YouTube qualified, "
                    f"{report.web_priority_count + report.web_qualified_count} web qualified, "
                    f"{report.work.get('drops_loaded', 0)} fresh drops"
                )
                send_email(settings, subject, body)
                counters["emailed"] = 1
            _finish_run(db, run, "complete", counters)
        except EmailError as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Daily digest failed")
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Daily digest failed")


def run_commoncrawl_prefilter_job() -> None:
    """Cache free historical-domain signals inside Railway's private network."""
    with SessionLocal() as db:
        run = _start_run(db, "commoncrawl_prefilter")
        counters: dict[str, Any] = {
            "candidates": 0,
            "checked": 0,
            "with_capture": 0,
            "without_capture": 0,
            "index_requests": 0,
            "provider_cost_usd": 0.0,
            "errors": 0,
            "error_details": [],
        }
        try:
            counters = run_commoncrawl_prefilter_batch(db, batch_size=10, index_count=2)
            status = "complete" if not counters.get("errors") else "partial"
            _finish_run(db, run, status, counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Common Crawl prefilter failed")


def run_stackexchange_prefilter_job() -> None:
    """Cache free exact-link evidence from high-view Stack Exchange questions."""
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "stackexchange_prefilter")
        counters: dict[str, Any] = {
            "candidates": 0,
            "queries": 0,
            "questions_matched": 0,
            "exact_links_saved": 0,
            "new_links": 0,
            "domains_with_links": 0,
            "quota_remaining": None,
            "backoff_events": 0,
            "provider_cost_usd": 0.0,
            "errors": 0,
            "error_details": [],
        }
        try:
            counters = run_stackexchange_prefilter_batch(
                db,
                batch_size=settings.stackexchange_prefilter_batch_size,
                sites=tuple(settings.stackexchange_sites),
                min_views=settings.stackexchange_min_views,
            )
            status = "complete" if not counters.get("errors") else "partial"
            _finish_run(db, run, status, counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Stack Exchange prefilter failed")


def run_hackernews_prefilter_job() -> None:
    """Cache free exact-link evidence from Hacker News stories/comments."""
    with SessionLocal() as db:
        run = _start_run(db, "hackernews_prefilter")
        counters: dict[str, Any] = {
            "candidates": 0,
            "queries": 0,
            "search_hits": 0,
            "items_with_exact_links": 0,
            "exact_links_saved": 0,
            "new_links": 0,
            "domains_with_links": 0,
            "provider_cost_usd": 0.0,
            "errors": 0,
            "error_details": [],
        }
        try:
            counters = run_hackernews_prefilter_batch(db, batch_size=10, hits_per_page=50)
            status = "complete" if not counters.get("errors") else "partial"
            _finish_run(db, run, status, counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("Hacker News prefilter failed")


JOB_FUNCTIONS: dict[str, Callable[[], None]] = {
    "discovery": run_discovery,
    "channel_fanout": run_channel_fanout,
    "snapshots": run_view_snapshots,
    "availability": run_availability_checks,
    "dropped_feeds": run_dropped_feeds,
    "dropped_search": run_dropped_youtube_search,
    "commoncrawl_prefilter": run_commoncrawl_prefilter_job,
    "stackexchange_prefilter": run_stackexchange_prefilter_job,
    "hackernews_prefilter": run_hackernews_prefilter_job,
    "digest": run_daily_digest,
}


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1}
    )
    start = datetime.now(UTC)
    scheduler.add_job(
        seed_manual_checkpoint,
        DateTrigger(run_date=start + timedelta(seconds=15)),
        id="seed_checkpoint",
        replace_existing=True,
    )
    scheduler.add_job(
        run_discovery,
        IntervalTrigger(
            minutes=settings.discovery_interval_minutes, start_date=start + timedelta(seconds=45)
        ),
        id="youtube_discovery",
        replace_existing=True,
    )
    if settings.youtube_channel_fanout_enabled:
        scheduler.add_job(
            run_channel_fanout,
            IntervalTrigger(
                minutes=settings.youtube_channel_fanout_interval_minutes,
                start_date=start + timedelta(seconds=75),
            ),
            id="youtube_channel_fanout",
            replace_existing=True,
        )
    scheduler.add_job(
        run_availability_checks,
        IntervalTrigger(hours=6, start_date=start + timedelta(minutes=3)),
        id="availability_checks",
        replace_existing=True,
    )
    scheduler.add_job(
        run_dropped_feeds,
        DateTrigger(run_date=start + timedelta(minutes=2)),
        id="initial_dropped_feeds",
        replace_existing=True,
    )
    scheduler.add_job(
        run_dropped_youtube_search,
        DateTrigger(run_date=start + timedelta(minutes=4)),
        id="initial_dropped_youtube_search",
        replace_existing=True,
    )
    scheduler.add_job(
        run_commoncrawl_prefilter_job,
        DateTrigger(run_date=start + timedelta(minutes=6)),
        id="initial_commoncrawl_prefilter",
        replace_existing=True,
    )
    scheduler.add_job(
        run_stackexchange_prefilter_job,
        DateTrigger(run_date=start + timedelta(minutes=9)),
        id="initial_stackexchange_prefilter",
        replace_existing=True,
    )
    scheduler.add_job(
        run_hackernews_prefilter_job,
        DateTrigger(run_date=start + timedelta(minutes=12)),
        id="initial_hackernews_prefilter",
        replace_existing=True,
    )
    scheduler.add_job(
        run_view_snapshots,
        IntervalTrigger(
            hours=settings.youtube_view_refresh_interval_hours,
            start_date=start + timedelta(minutes=5),
        ),
        id="view_snapshots",
        replace_existing=True,
    )
    scheduler.add_job(
        run_dropped_feeds,
        CronTrigger(hour=3, minute=5, timezone="UTC"),
        id="dropped_feeds",
        replace_existing=True,
    )
    scheduler.add_job(
        run_dropped_youtube_search,
        CronTrigger(hour=4, minute=10, timezone="UTC"),
        id="dropped_youtube_search",
        replace_existing=True,
    )
    scheduler.add_job(
        run_commoncrawl_prefilter_job,
        CronTrigger(hour="1,13", minute=17, timezone="UTC"),
        id="commoncrawl_prefilter",
        replace_existing=True,
    )
    scheduler.add_job(
        run_stackexchange_prefilter_job,
        CronTrigger(hour="2,14", minute=27, timezone="UTC"),
        id="stackexchange_prefilter",
        replace_existing=True,
    )
    scheduler.add_job(
        run_hackernews_prefilter_job,
        CronTrigger(hour="3,15", minute=37, timezone="UTC"),
        id="hackernews_prefilter",
        replace_existing=True,
    )
    scheduler.add_job(
        run_daily_digest,
        CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="daily_digest",
        replace_existing=True,
    )
    return scheduler
