from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.emailer import render_daily_digest
from app.jobs import _build_daily_digest_report, process_video, refresh_candidates
from app.models import Domain, RunLog, ViewSnapshot
from app.youtube import YouTubeVideo


def test_daily_digest_reports_real_work_pending_candidates_and_errors() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    settings = Settings(
        legacy_videos_checked=214,
        legacy_domains_checked=124,
        legacy_dropped_checked=120,
        dropped_domain_feed_urls="https://example.com/fresh-drops.csv",
    )

    with Session(engine) as db:
        process_video(
            db,
            YouTubeVideo(
                id="digest12345",
                title="Evergreen business tutorial",
                channel_id="channel-1",
                channel_title="Example channel",
                description="Download the guide at https://pending-example.com/start",
                published_at=now - timedelta(days=365 * 5),
                view_count=110_000,
            ),
            discovery_query="business tutorial",
            discovery_route="youtube_first",
        )
        db.add(
            ViewSnapshot(
                video_id="digest12345",
                captured_at=now - timedelta(days=7),
                capture_date=(now - timedelta(days=7)).date(),
                view_count=100_000,
            )
        )
        domain = db.scalar(select(Domain).where(Domain.name == "pending-example.com"))
        assert domain is not None
        domain.availability_status = "likely_available"
        domain.availability_source = "rdap_dns"
        db.add_all(
            [
                RunLog(
                    job="youtube_discovery",
                    started_at=now - timedelta(hours=2),
                    finished_at=now - timedelta(hours=2) + timedelta(minutes=1),
                    status="complete",
                    counters={
                        "search_calls": 3,
                        "videos_returned": 125,
                        "new_videos": 60,
                        "new_domains": 42,
                        "new_links": 88,
                    },
                ),
                RunLog(
                    job="dropped_feeds",
                    started_at=now - timedelta(hours=5),
                    finished_at=now - timedelta(hours=5) + timedelta(minutes=1),
                    status="complete",
                    counters={"feeds": 1, "parsed": 10_000, "new": 9_975, "errors": 0},
                ),
                RunLog(
                    job="availability_checks",
                    started_at=now - timedelta(hours=4),
                    finished_at=now - timedelta(hours=4) + timedelta(minutes=1),
                    status="complete",
                    counters={"checked": 100, "errors": 2},
                ),
            ]
        )
        db.commit()
        refresh_candidates(db)

        report = _build_daily_digest_report(db, settings, now=now)
        body = render_daily_digest(report)

    assert report.work["search_calls"] == 3
    assert report.work["new_videos"] == 60
    assert report.work["drops_loaded"] == 9_975
    assert report.work["availability_checked"] == 100
    assert report.work["availability_errors"] == 2
    assert report.watchlist_count == 1
    assert report.pending["verification"] == 1
    assert report.pending["registrar"] == 1
    assert report.pending_candidates[0].domain == "pending-example.com"
    assert report.issues[0].job == "availability_checks"
    assert "Work completed in the last 24 hours" in body
    assert "Fresh dropped names loaded" in body
    assert "9,975" in body
    assert "pending-example.com" in body
    assert "2 item-level error(s)" in body
