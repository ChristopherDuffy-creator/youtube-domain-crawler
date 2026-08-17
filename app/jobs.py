from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.availability import AvailabilityResult, check_domain
from app.config import EVERGREEN_QUERIES, MANUAL_CHECKPOINTS, Settings, get_settings
from app.database import SessionLocal
from app.domain_tools import extract_domain_names, extract_links
from app.emailer import EmailCandidate, EmailError, render_candidate_table, send_email
from app.metrics import ViewMetric, calculate_monthly_views
from app.models import (
    AppCheckpoint,
    Candidate,
    Domain,
    DroppedDomain,
    RunLog,
    SearchState,
    Video,
    VideoDomain,
    ViewSnapshot,
    utcnow,
)
from app.scoring import ScoreInputs, calculate_score, determine_tier
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


def process_video(
    db: Session,
    item: YouTubeVideo,
    discovery_query: str,
    discovery_route: str,
) -> dict[str, int]:
    now = utcnow()
    counters = {"new_videos": 0, "new_domains": 0, "new_links": 0, "snapshots": 0}
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
        link.active = False

    for extracted in extract_links(item.description):
        domain = db.scalar(select(Domain).where(Domain.name == extracted.domain))
        if domain is None:
            domain = Domain(name=extracted.domain, suffix=extracted.suffix)
            db.add(domain)
            db.flush()
            counters["new_domains"] += 1

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
            counters["videos_found"] = len(videos)
            for video in videos:
                exact_domain = MANUAL_CHECKPOINTS.get(video.id, "")
                if exact_domain and not exact_domain_in_description(
                    exact_domain, video.description
                ):
                    logger.info("Manual checkpoint link no longer appears in %s", video.id)
                result = process_video(db, video, "legacy manual checkpoint", "manual_checkpoint")
                counters["new_domains"] += result["new_domains"]
            db.commit()
            refresh_candidates(db)
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
                for video in videos:
                    result = process_video(db, video, state.query, "youtube_first")
                    for key in ("new_videos", "new_domains", "new_links"):
                        counters[key] += result[key]
                state.page_token = page.next_page_token
                state.pages_scanned += 1
                state.last_run_at = utcnow()
                if not page.next_page_token:
                    state.pages_scanned = 0
                db.commit()

            refresh_candidates(db)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))
            logger.exception("YouTube discovery failed")


def run_view_snapshots() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "view_snapshots")
        counters = {"videos_requested": 0, "videos_updated": 0, "snapshots": 0}
        try:
            ids = db.scalars(
                select(distinct(VideoDomain.video_id)).where(VideoDomain.active.is_(True))
            ).all()
            counters["videos_requested"] = len(ids)
            client = YouTubeClient(settings.youtube_api_key)
            for start in range(0, len(ids), 500):
                videos = client.fetch_videos(ids[start : start + 500])
                for item in videos:
                    video = db.get(Video, item.id)
                    if video is None:
                        continue
                    video.lifetime_views = item.view_count
                    video.last_seen_at = utcnow()
                    if _upsert_snapshot(db, item.id, item.view_count, utcnow()):
                        counters["snapshots"] += 1
                    counters["videos_updated"] += 1
                db.commit()
            refresh_candidates(db)
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
        counters = {
            "checked": 0,
            "available": 0,
            "likely_available": 0,
            "registered": 0,
            "errors": 0,
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
            db.commit()
            refresh_candidates(db)
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


def refresh_candidates(db: Session) -> int:
    settings = get_settings()
    domains = db.scalars(
        select(Domain)
        .where(Domain.video_links.any(VideoDomain.active.is_(True)))
        .options(
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
    for name in domains:
        dropped = db.scalar(select(DroppedDomain).where(DroppedDomain.name == name))
        if dropped is None:
            dropped = DroppedDomain(name=name, source=source)
            db.add(dropped)
            counters["new"] += 1
        linked_domain = db.scalar(
            select(Domain).where(
                Domain.name == name,
                Domain.video_links.any(VideoDomain.active.is_(True)),
            )
        )
        if linked_domain is not None:
            dropped.matched_existing_index = True
            linked_domain.last_checked_at = None
            counters["matched_index"] += 1
    db.commit()
    return counters


def run_dropped_feeds() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "dropped_feeds")
        counters = {"feeds": 0, "parsed": 0, "new": 0, "matched_index": 0, "errors": 0}
        try:
            for url in settings.dropped_domain_feed_urls:
                try:
                    response = httpx.get(url, follow_redirects=True, timeout=45.0)
                    response.raise_for_status()
                    result = ingest_dropped_text(db, response.text, url)
                    counters["feeds"] += 1
                    for key in ("parsed", "new", "matched_index"):
                        counters[key] += result[key]
                except Exception:
                    counters["errors"] += 1
                    logger.exception("Dropped-domain feed failed: %s", url)
            _finish_run(db, run, "complete", counters)
        except Exception as exc:
            db.rollback()
            run = db.get(RunLog, run.id)
            _finish_run(db, run, "failed", counters, str(exc))


def run_dropped_youtube_search(max_searches: int = 10) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "dropped_youtube_search")
        counters = {"search_calls": 0, "drops_checked": 0, "videos_returned": 0, "exact_matches": 0}
        try:
            candidates = db.scalars(
                select(DroppedDomain)
                .where(DroppedDomain.youtube_searched_at.is_(None))
                .order_by(
                    case((DroppedDomain.name.like("%.com"), 0), else_=1),
                    func.length(DroppedDomain.name).asc(),
                    DroppedDomain.first_seen_at.asc(),
                )
                .limit(max_searches)
            ).all()
            client = YouTubeClient(settings.youtube_api_key)
            published_before = datetime.now(UTC) - timedelta(
                days=365 * settings.published_before_years
            )
            for dropped in candidates:
                page = client.search_videos(dropped.name, published_before, max_results=50)
                counters["search_calls"] += 1
                counters["drops_checked"] += 1
                counters["videos_returned"] += len(page.video_ids)
                for video in client.fetch_videos(page.video_ids):
                    if exact_domain_in_description(dropped.name, video.description):
                        process_video(db, video, dropped.name, "dropped_first")
                        counters["exact_matches"] += 1
                dropped.youtube_searched_at = utcnow()
                db.commit()
            refresh_candidates(db)
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
        .order_by(Candidate.tier.desc(), Candidate.score.desc())
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


def run_daily_digest() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = _start_run(db, "daily_digest")
        counters = {"emailed": 0, "qualified": 0, "watchlist": 0}
        try:
            qualified_rows = _email_candidates(db)
            counters["qualified"] = len(qualified_rows)
            watchlist_count = (
                db.scalar(
                    select(func.count()).select_from(Candidate).where(Candidate.tier == "watchlist")
                )
                or 0
            )
            counters["watchlist"] = watchlist_count
            items = _to_email_candidates(qualified_rows[:25])
            body = (
                "<h2>Daily YouTube domain crawler report</h2>"
                f"<p><strong>{len(qualified_rows)}</strong> qualified/priority domains; "
                f"<strong>{watchlist_count}</strong> watchlist domains; "
                f"target: <strong>{settings.target_qualified_domains}</strong>.</p>"
                + render_candidate_table(items)
            )
            if settings.email_enabled:
                send_email(settings, "Daily YouTube domain crawler report", body)
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


JOB_FUNCTIONS: dict[str, Callable[[], None]] = {
    "discovery": run_discovery,
    "snapshots": run_view_snapshots,
    "availability": run_availability_checks,
    "dropped_feeds": run_dropped_feeds,
    "dropped_search": run_dropped_youtube_search,
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
    scheduler.add_job(
        run_availability_checks,
        IntervalTrigger(hours=6, start_date=start + timedelta(minutes=3)),
        id="availability_checks",
        replace_existing=True,
    )
    scheduler.add_job(
        run_view_snapshots,
        CronTrigger(hour=2, minute=15, timezone="UTC"),
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
        run_daily_digest,
        CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="daily_digest",
        replace_existing=True,
    )
    return scheduler
