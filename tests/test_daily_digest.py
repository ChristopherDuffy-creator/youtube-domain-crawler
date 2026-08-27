from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.emailer import render_daily_digest
from app.jobs import _build_daily_digest_report, process_video, refresh_candidates
from app.models import (
    Domain,
    FetchVerification,
    Opportunity,
    ProviderQuery,
    RunLog,
    SourceLink,
    SourcePage,
    SourceSite,
    ViewSnapshot,
)
from app.youtube import YouTubeVideo


def test_daily_digest_reports_real_work_pending_candidates_errors_and_web_hits() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
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
        db.add_all(
            [
                ViewSnapshot(
                    video_id="digest12345",
                    captured_at=now - timedelta(days=6),
                    capture_date=(now - timedelta(days=6)).date(),
                    view_count=101_000,
                ),
                ViewSnapshot(
                    video_id="digest12345",
                    captured_at=now - timedelta(days=4),
                    capture_date=(now - timedelta(days=4)).date(),
                    view_count=104_000,
                ),
            ]
        )
        domain = db.scalar(select(Domain).where(Domain.name == "pending-example.com"))
        assert domain is not None
        domain.availability_status = "likely_available"
        domain.availability_source = "rdap_dns"

        web_domain = Domain(
            name="web-example.com",
            suffix="com",
            availability_status="available",
            availability_source="porkbun",
            registrar_price_usd=12.0,
            premium=False,
        )
        db.add(web_domain)
        db.flush()
        source_site = SourceSite(hostname="publisher.example", source_type="web")
        db.add(source_site)
        db.flush()
        source_page = SourcePage(
            site_id=source_site.id,
            url="https://publisher.example/buyers-guide",
            title="Independent buyer guide",
            language="en",
            http_status=200,
            page_rank=72,
            domain_rank=76,
        )
        db.add(source_page)
        db.flush()
        source_link = SourceLink(
            source_page_id=source_page.id,
            domain_id=web_domain.id,
            target_url="https://web-example.com/offer",
            anchor_text="buy the software",
            context_before="best deal",
            context_after="for small business",
            dofollow=True,
            provider_live=True,
            provider_rank=80,
            spam_score=0,
        )
        db.add(source_link)
        db.flush()
        db.add_all(
            [
                Opportunity(
                    domain_id=web_domain.id,
                    tier="qualified",
                    score=73.5,
                    best_source_page_id=source_page.id,
                    source_page_traffic_estimate=10_000,
                    referring_page_count=18,
                    independent_site_count=7,
                    link_strength=80,
                    commercial_intent=0.75,
                    verified_live_link=True,
                    niche="software",
                    updated_at=now - timedelta(minutes=45),
                ),
                FetchVerification(
                    source_link_id=source_link.id,
                    fetched_at=now - timedelta(minutes=40),
                    http_status=200,
                    final_url=source_page.url,
                    link_present=True,
                    content_hash="a" * 64,
                ),
                ProviderQuery(
                    provider="dataforseo",
                    endpoint="bulk_backlink_summary",
                    target="web-example.com",
                    provider_task_id="summary-task",
                    status="complete",
                    requested_at=now - timedelta(hours=1),
                    completed_at=now - timedelta(minutes=59),
                    cost_usd=0.02,
                    row_count=18,
                ),
                ProviderQuery(
                    provider="dataforseo",
                    endpoint="backlinks",
                    target="web-example.com",
                    provider_task_id="links-task",
                    status="complete",
                    requested_at=now - timedelta(minutes=58),
                    completed_at=now - timedelta(minutes=57),
                    cost_usd=0.03,
                    row_count=7,
                ),
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
    assert report.watchlist_count == 0
    assert report.pending_count == 1
    assert report.pending["verification"] == 0
    assert report.pending["day7"] == 0
    assert report.pending["registrar"] == 1
    assert report.pending_candidates[0].domain == "pending-example.com"
    assert report.issues[0].job == "availability_checks"

    assert report.web_priority_count == 0
    assert report.web_qualified_count == 1
    assert report.web_watchlist_count == 0
    assert report.web_pending_count == 0
    assert report.web_domains_checked_24h == 1
    assert report.web_links_verified_24h == 1
    assert report.web_provider_cost_usd_24h == pytest.approx(0.05)
    assert report.web_opportunities[0].domain == "web-example.com"
    assert report.web_opportunities[0].source_page_traffic == 10_000
    assert report.web_opportunities[0].source_site == "publisher.example"

    assert "Web Link Hunter" not in body
    assert "Independent buyer guide" not in body
    assert "web-example.com" not in body
    assert "20,000" in body
    assert "10,000–20,000" in body
    assert "$0.0500" not in body
    assert "Work completed in the last 24 hours" in body
    assert "Fresh dropped names loaded" in body
    assert "9,975" in body
    assert "pending-example.com" in body
    assert "2 item-level error(s)" in body
