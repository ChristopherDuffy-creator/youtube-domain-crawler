from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.jobs import (
    ingest_dropped_text,
    process_video,
    refresh_candidates,
    run_view_snapshot_batch,
)
from app.models import (
    Domain,
    DroppedDomain,
    DroppedDomainMatch,
    VideoDomain,
    VideoRefreshState,
    ViewSnapshot,
    YouTubeChannel,
    YouTubeDomainSignal,
)
from app.youtube import VideoStatistics, YouTubeVideo
from app.youtube_intelligence import (
    consume_youtube_quota,
    refresh_local_dropped_matches,
    update_channel_intelligence,
    youtube_quota_snapshot,
)


def _linked_video(
    video_id: str,
    domain: str,
    views: int = 160_000,
    duration_seconds: int | None = None,
) -> YouTubeVideo:
    return YouTubeVideo(
        id=video_id,
        title="High intent evergreen tutorial",
        channel_id="high-yield-channel",
        channel_title="High yield channel",
        description=f"Download the full guide now at https://{domain}/offer",
        published_at=datetime.now(UTC) - timedelta(days=1_000),
        view_count=views,
        duration_seconds=duration_seconds,
    )


def test_database_quota_ledger_enforces_all_three_protected_buckets() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        youtube_search_daily_limit=1,
        youtube_data_daily_limit=100,
        youtube_fanout_daily_data_limit=100,
        youtube_stats_daily_limit=100,
    )

    with Session(engine) as db:
        assert consume_youtube_quota(db, settings, search_calls=1) is True
        assert consume_youtube_quota(db, settings, search_calls=1) is False
        assert consume_youtube_quota(db, settings, data_units=100, fanout=True) is True
        assert consume_youtube_quota(db, settings, data_units=1, fanout=True) is False
        assert consume_youtube_quota(db, settings, stats_units=100) is True
        assert consume_youtube_quota(db, settings, stats_units=1) is False

        snapshot = youtube_quota_snapshot(db, settings)

    assert snapshot["search_remaining"] == 0
    assert snapshot["data_remaining"] == 0
    assert snapshot["fanout_data_remaining"] == 0
    assert snapshot["stats_remaining"] == 0


def test_channel_allocator_promotes_yield_and_backs_off_empty_inventory() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        channel = YouTubeChannel(
            channel_id="high-yield-channel",
            uploads_playlist_id="uploads",
            seed_count=1,
        )
        db.add(channel)
        db.flush()

        intelligence = update_channel_intelligence(
            db,
            channel,
            videos_seen=50,
            new_videos=50,
            linked_videos=5,
            external_links=8,
            completed=False,
        )
        assert intelligence.tier == "hot"
        assert intelligence.recommended_burst == 12

        for _ in range(4):
            intelligence = update_channel_intelligence(
                db,
                channel,
                videos_seen=50,
                new_videos=0,
                linked_videos=0,
                external_links=0,
                completed=True,
            )

        assert intelligence.tier == "dormant"
        assert intelligence.recommended_burst == 1
        assert intelligence.next_crawl_at > datetime.now(UTC) + timedelta(days=29)


def test_15_day_signal_is_measured_not_verified_and_builds_a_money_case() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        process_video(db, _linked_video("measured0001", "measured-opportunity.com"), "seed", "test")
        domain = db.scalar(
            select(Domain).where(Domain.name == "measured-opportunity.com")
        )
        assert domain is not None
        domain.availability_status = "available"
        db.add(
            ViewSnapshot(
                video_id="measured0001",
                captured_at=now - timedelta(days=15),
                capture_date=(now - timedelta(days=15)).date(),
                view_count=120_000,
            )
        )
        db.commit()

        assert refresh_candidates(db, {domain.id}) == 1
        signal = db.get(YouTubeDomainSignal, domain.id)

        assert signal is not None
        assert signal.measured_15d is True
        assert signal.verified_30d is False
        assert signal.traffic_confidence == "measured_15d"
        assert signal.monthly_linked_video_exposure >= 70_000
        assert signal.expected_clicks_monthly > 0
        assert signal.monthly_revenue_high_usd > signal.monthly_revenue_low_usd
        assert signal.max_purchase_price_usd == 0
        assert signal.buy_score > 0

        links = db.scalars(
            select(VideoDomain).where(VideoDomain.domain_id == domain.id)
        ).all()
        for link in links:
            link.active = False
        db.commit()
        assert refresh_candidates(db, {domain.id}) == 0
        db.refresh(signal)
        assert signal.active_link_count == 0
        assert signal.expected_clicks_monthly == 0
        assert signal.buy_score == 0


def test_early_signal_has_no_click_revenue_or_decision_case() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        process_video(db, _linked_video("early0000001", "early-only.com"), "seed", "test")
        domain = db.scalar(select(Domain).where(Domain.name == "early-only.com"))
        assert domain is not None
        domain.availability_status = "available"
        db.add(
            ViewSnapshot(
                video_id="early0000001",
                captured_at=now - timedelta(days=6),
                capture_date=(now - timedelta(days=6)).date(),
                view_count=10_000,
            )
        )
        db.commit()

        refresh_candidates(db, {domain.id})
        signal = db.get(YouTubeDomainSignal, domain.id)

        assert signal is not None
        assert signal.measured_15d is False
        assert signal.observed_view_gain == 150_000
        assert signal.monthly_linked_video_exposure > 0
        assert signal.expected_clicks_monthly == 0
        assert signal.monthly_revenue_low_usd == 0
        assert signal.monthly_revenue_high_usd == 0
        assert signal.max_purchase_price_usd == 0
        assert signal.buy_score == 0


def test_short_form_exposure_never_becomes_assumed_clickable_traffic() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        process_video(
            db,
            _linked_video(
                "short0000001",
                "short-description.com",
                duration_seconds=20,
            ),
            "seed",
            "test",
        )
        domain = db.scalar(
            select(Domain).where(Domain.name == "short-description.com")
        )
        assert domain is not None
        domain.availability_status = "available"
        db.add(
            ViewSnapshot(
                video_id="short0000001",
                captured_at=now - timedelta(days=15),
                capture_date=(now - timedelta(days=15)).date(),
                view_count=10_000,
            )
        )
        db.commit()

        refresh_candidates(db, {domain.id})
        signal = db.get(YouTubeDomainSignal, domain.id)

        assert signal is not None
        assert signal.measured_15d is True
        assert signal.short_form_video_count == 1
        assert signal.short_form_exposure > 0
        assert signal.click_eligible_exposure == 0
        assert signal.expected_clicks_monthly == 0
        assert signal.monthly_revenue_high_usd == 0
        assert signal.buy_score == 0


def test_dropped_domain_matching_works_in_both_arrival_orders_and_is_permanent() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(DroppedDomain(name="drop-first.com", source="test"))
        db.commit()
        affected: set[int] = set()
        process_video(db, _linked_video("dropfirst001", "drop-first.com"), "seed", "test", affected)
        db.commit()

        first = refresh_local_dropped_matches(db, domain_ids=affected)
        assert first["new_matches"] == 1

        process_video(db, _linked_video("indexfirst01", "index-first.net"), "seed", "test")
        db.commit()
        second = ingest_dropped_text(db, "index-first.net", "test")

        assert second["matched_index"] == 1
        matches = db.scalars(select(DroppedDomainMatch)).all()
        assert len(matches) == 2
        assert all(match.active_video_count == 1 for match in matches)
        assert all(match.active_link_count >= 1 for match in matches)
        linked_names = set(
            db.scalars(
                select(Domain.name)
                .join(VideoDomain, VideoDomain.domain_id == Domain.id)
                .where(VideoDomain.active.is_(True))
            ).all()
        )
        assert {"drop-first.com", "index-first.net"}.issubset(linked_names)

        drop_first = db.scalar(select(Domain).where(Domain.name == "drop-first.com"))
        assert drop_first is not None
        for link in db.scalars(
            select(VideoDomain).where(VideoDomain.domain_id == drop_first.id)
        ).all():
            link.active = False
        db.commit()
        refresh_local_dropped_matches(db, domain_ids={drop_first.id})
        retained = db.scalar(
            select(DroppedDomainMatch).where(
                DroppedDomainMatch.domain_id == drop_first.id
            )
        )
        assert retained is not None
        assert retained.active_video_count == 0
        assert retained.active_link_count == 0


class _GranularStatsClient:
    def fetch_video_statistics_batch(
        self, video_ids: list[str]
    ) -> list[VideoStatistics]:
        return [VideoStatistics(video_id, 200_000) for video_id in video_ids]


def test_adaptive_refresh_uses_the_separate_granular_statistics_bucket() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        youtube_view_refresh_batch_size=50,
        youtube_data_daily_limit=100,
        youtube_fanout_daily_data_limit=100,
        youtube_stats_daily_limit=100,
    )

    with Session(engine) as db:
        process_video(db, _linked_video("granular0001", "granular-stats.com"), "seed", "test")
        state = db.get(VideoRefreshState, "granular0001")
        assert state is not None
        state.next_refresh_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        counters = run_view_snapshot_batch(
            db,
            settings,
            _GranularStatsClient(),  # type: ignore[arg-type]
        )
        quota = youtube_quota_snapshot(db, settings)

    assert counters["statistics_endpoint"] == "videos.batchGetStats"
    assert counters["statistics_calls"] == 1
    assert quota["stats_used"] == 1
    assert quota["data_used"] == 0
