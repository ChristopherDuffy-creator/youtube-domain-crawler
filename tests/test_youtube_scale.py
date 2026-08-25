from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.jobs import (
    _domains_due_for_check,
    _dropped_domains_due_for_youtube_search,
    _initial_refresh_interval_hours,
    build_scheduler,
    ingest_dropped_text,
    process_video,
    run_channel_fanout_batch,
    run_view_snapshot_batch,
    seed_youtube_channels,
)
from app.models import (
    Domain,
    DroppedDomain,
    Video,
    VideoDomain,
    VideoRefreshState,
    ViewSnapshot,
    YouTubeChannel,
    YouTubeChannelIntelligence,
    YouTubeDomainSignal,
    YouTubeQuotaLedger,
)
from app.youtube import (
    ChannelDetails,
    PlaylistPage,
    VideoStatistics,
    YouTubeClient,
    YouTubeVideo,
    _StatisticsRequestPacer,
)


def _video(video_id: str, channel_id: str, description: str, views: int = 250_000) -> YouTubeVideo:
    return YouTubeVideo(
        id=video_id,
        title=f"Video {video_id}",
        channel_id=channel_id,
        channel_title="High-yield channel",
        description=description,
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
        view_count=views,
    )


def test_youtube_client_uses_quota_efficient_batched_inventory_endpoints(monkeypatch) -> None:
    client = YouTubeClient("test-key", statistics_requests_per_minute=1_000_000)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, params))
        if path == "channels":
            return {
                "items": [
                    {
                        "id": "channel-1",
                        "snippet": {"title": "Channel One"},
                        "contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}},
                    }
                ]
            }
        if path == "playlistItems":
            return {
                "items": [{"contentDetails": {"videoId": "video-1"}}],
                "nextPageToken": "next-page",
            }
        if path == "videos:batchGetStats":
            return {
                "items": [
                    {"id": "video-1", "statistics": {"viewCount": "4321"}}
                ]
            }
        if params["part"] == "statistics,status":
            return {
                "items": [
                    {
                        "id": "video-1",
                        "status": {"privacyStatus": "public"},
                        "statistics": {"viewCount": "1234"},
                    }
                ]
            }
        return {"items": []}

    monkeypatch.setattr(client, "_get", fake_get)

    channels = client.fetch_channels(["channel-1"])
    page = client.fetch_uploads_page("uploads-1", page_token="page-1")
    statistics = client.fetch_video_statistics(["video-1"])
    batch_statistics = client.fetch_video_statistics_batch(["video-1"])
    client.fetch_videos([f"video-{index}" for index in range(51)])

    assert channels == [ChannelDetails("channel-1", "Channel One", "uploads-1")]
    assert page == PlaylistPage(["video-1"], "next-page")
    assert statistics == [VideoStatistics("video-1", 1234)]
    assert batch_statistics == [VideoStatistics("video-1", 4321)]
    channel_call = next(params for path, params in calls if path == "channels")
    playlist_call = next(params for path, params in calls if path == "playlistItems")
    detail_calls = [
        params for path, params in calls if path == "videos" and params["part"].startswith("snippet")
    ]
    assert channel_call["part"] == "snippet,contentDetails"
    assert channel_call["maxResults"] == 50
    assert playlist_call == {
        "part": "contentDetails",
        "playlistId": "uploads-1",
        "maxResults": 50,
        "pageToken": "page-1",
    }
    assert len(detail_calls) == 2
    assert all("maxResults" not in params for params in detail_calls)
    assert all(len(params["id"].split(",")) <= 50 for params in detail_calls)
    batch_call = next(params for path, params in calls if path == "videos:batchGetStats")
    assert batch_call == {"part": "id,statistics", "id": "video-1"}


def test_granular_statistics_fetch_uses_a_bounded_parallel_worker_pool(monkeypatch) -> None:
    client = YouTubeClient(
        "test-key",
        statistics_workers=4,
        statistics_requests_per_minute=1_000_000,
    )
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, peak
        assert path == "videos:batchGetStats"
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {
            "items": [
                {"id": video_id, "statistics": {"viewCount": "42"}}
                for video_id in params["id"].split(",")
            ]
        }

    monkeypatch.setattr(client, "_get", fake_get)

    statistics = client.fetch_video_statistics_batch(
        [f"statistics-video-{index}" for index in range(200)]
    )

    assert peak == 4
    assert [item.id for item in statistics] == [
        f"statistics-video-{index}" for index in range(200)
    ]


def test_granular_statistics_pacer_spaces_requests_across_client_instances(monkeypatch) -> None:
    now = 1_000.0
    sleep_calls: list[float] = []
    request_times: list[float] = []

    def fake_monotonic() -> float:
        return now

    def fake_sleep(delay: float) -> None:
        nonlocal now
        sleep_calls.append(delay)
        now += delay

    pacer = _StatisticsRequestPacer(clock=fake_monotonic, sleeper=fake_sleep)

    def fake_get(_path: str, _params: dict[str, Any]) -> dict[str, Any]:
        request_times.append(now)
        return {"items": []}

    first = YouTubeClient(
        "test-key",
        statistics_requests_per_minute=120,
        statistics_request_pacer=pacer,
    )
    second = YouTubeClient(
        "test-key",
        statistics_requests_per_minute=120,
        statistics_request_pacer=pacer,
    )
    monkeypatch.setattr(first, "_get", fake_get)
    monkeypatch.setattr(second, "_get", fake_get)

    first.fetch_video_statistics_batch(["statistics-video-1"])
    second.fetch_video_statistics_batch(["statistics-video-2"])

    assert sleep_calls == [0.5]
    assert request_times == [1_000.0, 1_000.5]
class FakeFanoutClient:
    def __init__(self) -> None:
        self.playlist_tokens: list[str | None] = []

    def fetch_channels(self, channel_ids: list[str]) -> list[ChannelDetails]:
        assert channel_ids == ["channel-1"]
        return [ChannelDetails("channel-1", "High-yield channel", "uploads-1")]

    def fetch_uploads_page(
        self,
        uploads_playlist_id: str,
        page_token: str | None = None,
        max_results: int = 50,
    ) -> PlaylistPage:
        assert uploads_playlist_id == "uploads-1"
        assert max_results == 50
        self.playlist_tokens.append(page_token)
        if page_token is None:
            return PlaylistPage(["video000001", "video000002"], "page-2")
        assert page_token == "page-2"
        return PlaylistPage(["video000003"], None)

    def fetch_videos(self, video_ids: list[str]) -> list[YouTubeVideo]:
        descriptions = {
            "video000001": "Get the guide at https://resource-one.com/start",
            "video000002": "There is no external link in this description.",
            "video000003": "Download it from https://resource-two.net/file",
        }
        return [_video(video_id, "channel-1", descriptions[video_id]) for video_id in video_ids]


def test_channel_fanout_is_resumable_and_builds_the_permanent_local_index() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        youtube_channel_pages_per_run=1,
        youtube_channel_page_burst=1,
        youtube_channel_recrawl_hours=24,
    )
    client = FakeFanoutClient()

    with Session(engine) as db:
        seed_youtube_channels(
            db,
            [_video("seed0000001", "channel-1", "")],
            count_as_search_seed=True,
        )
        db.commit()

        first = run_channel_fanout_batch(db, settings, client)  # type: ignore[arg-type]
        channel = db.get(YouTubeChannel, "channel-1")
        assert channel is not None
        assert first["channel_calls"] == 1
        assert first["playlist_calls"] == 1
        assert first["videos_fetched"] == 2
        assert channel.next_page_token == "page-2"
        assert channel.inventory_complete is False
        assert channel.yield_score > 0
        assert db.get(VideoRefreshState, "video000001") is not None
        assert db.get(VideoRefreshState, "video000002") is None

        second = run_channel_fanout_batch(db, settings, client)  # type: ignore[arg-type]
        db.refresh(channel)
        assert second["channel_calls"] == 0
        assert second["playlist_calls"] == 1
        assert channel.next_page_token is None
        assert channel.inventory_complete is True
        assert db.get(VideoRefreshState, "video000003") is not None
        assert db.scalar(select(Domain).where(Domain.name == "resource-one.com")) is not None
        assert db.scalar(select(Domain).where(Domain.name == "resource-two.net")) is not None

        idle = run_channel_fanout_batch(db, settings, client)  # type: ignore[arg-type]
        assert idle["playlist_calls"] == 0
        assert client.playlist_tokens == [None, "page-2"]

        match = ingest_dropped_text(db, "resource-two.net", "fresh drop test")
        assert match["matched_index"] == 1
        dropped = db.scalar(select(DroppedDomain).where(DroppedDomain.name == "resource-two.net"))
        assert dropped is not None and dropped.matched_existing_index is True
        db.add(DroppedDomain(name="unmatched-new.com", source="fresh drop test"))
        db.commit()
        due_searches = _dropped_domains_due_for_youtube_search(db, 10)
        assert [item.name for item in due_searches] == ["unmatched-new.com"]


class FakeStatisticsClient:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def fetch_video_statistics(self, video_ids: list[str]) -> list[VideoStatistics]:
        self.requested.extend(video_ids)
        return [VideoStatistics(video_id, 252_500) for video_id in video_ids]


def test_adaptive_refresh_requests_only_due_linked_videos() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(youtube_view_refresh_batch_size=50)
    now = datetime.now(UTC)

    with Session(engine) as db:
        due_video = Video(id="duevideo001", lifetime_views=250_000, active=True)
        later_video = Video(id="latervideo01", lifetime_views=250_000, active=True)
        db.add_all([due_video, later_video])
        db.flush()
        db.add_all(
            [
                VideoRefreshState(
                    video_id=due_video.id,
                    next_refresh_at=now - timedelta(minutes=1),
                    last_view_count=250_000,
                ),
                VideoRefreshState(
                    video_id=later_video.id,
                    next_refresh_at=now + timedelta(days=1),
                    last_view_count=250_000,
                ),
            ]
        )
        db.commit()

        client = FakeStatisticsClient()
        counters = run_view_snapshot_batch(db, settings, client)  # type: ignore[arg-type]

        assert client.requested == ["duevideo001"]
        assert counters["videos_due"] == 1
        assert counters["videos_updated"] == 1
        assert counters["statistics_calls"] == 1
        assert counters["quota_units_estimate"] == 1
        state = db.get(VideoRefreshState, "duevideo001")
        assert state is not None
        assert state.refresh_interval_hours == 6
        assert state.last_view_count == 252_500
        untouched = db.get(VideoRefreshState, "latervideo01")
        assert untouched is not None and untouched.last_view_count == 250_000


def test_snapshot_refresh_bulk_inserts_only_missing_daily_snapshots() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        youtube_view_refresh_batch_size=500,
        youtube_data_daily_limit=100,
        youtube_fanout_daily_data_limit=100,
        youtube_stats_daily_limit=100,
    )
    now = datetime.now(UTC)
    video_ids = ["snapshot0001", "snapshot0002", "snapshot0003"]

    with Session(engine) as db:
        db.add_all(
            [Video(id=video_id, lifetime_views=200_000, active=True) for video_id in video_ids]
        )
        db.add_all(
            [
                VideoRefreshState(
                    video_id=video_id,
                    next_refresh_at=now - timedelta(minutes=1),
                    last_view_count=200_000,
                )
                for video_id in video_ids
            ]
        )
        db.add(
            ViewSnapshot(
                video_id="snapshot0001",
                captured_at=now - timedelta(hours=1),
                capture_date=now.date(),
                view_count=200_000,
            )
        )
        db.commit()

        client = FakeStatisticsClient()
        counters = run_view_snapshot_batch(db, settings, client)  # type: ignore[arg-type]
        snapshots = db.scalars(
            select(ViewSnapshot)
            .where(ViewSnapshot.video_id.in_(video_ids))
            .order_by(ViewSnapshot.video_id)
        ).all()

        assert client.requested == video_ids
        assert counters["videos_due"] == 3
        assert counters["videos_updated"] == 3
        assert counters["snapshots"] == 2
        assert counters["statistics_calls"] == 1
        assert len(snapshots) == 3
        assert {snapshot.video_id for snapshot in snapshots} == set(video_ids)
        assert all(snapshot.capture_date == now.date() for snapshot in snapshots)
        assert all(snapshot.view_count == 252_500 for snapshot in snapshots)


def test_every_linked_video_gets_first_follow_up_within_one_day() -> None:
    assert _initial_refresh_interval_hours(9_999) == 24
    assert _initial_refresh_interval_hours(99_999) == 24
    assert _initial_refresh_interval_hours(999_999) == 24
    assert _initial_refresh_interval_hours(1_000_000) == 6


def test_never_checked_domains_are_first_in_capped_availability_queue() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        old_checked = Domain(
            name="old-checked.example",
            last_checked_at=now - timedelta(days=8),
            availability_status="registered",
        )
        never_checked = Domain(name="never-checked.example")
        db.add_all([old_checked, never_checked])
        db.flush()
        for index, domain in enumerate((old_checked, never_checked), start=1):
            video = Video(id=f"queuevideo{index:02d}", active=True)
            db.add(video)
            db.flush()
            db.add(
                VideoDomain(
                    video_id=video.id,
                    domain_id=domain.id,
                    raw_url=f"https://{domain.name}",
                    normalized_url=f"https://{domain.name}/",
                    active=True,
                )
            )
        db.commit()

        due = _domains_due_for_check(db, 1)

        assert [domain.name for domain in due] == ["never-checked.example"]


def test_rate_limited_availability_is_negative_cached_for_three_days() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        domain = Domain(
            name="rate-limited.example",
            availability_status="unknown",
            rdap_status="rate_limited",
            last_checked_at=now - timedelta(days=2),
        )
        video = Video(id="limitedvideo1", active=True)
        db.add_all([domain, video])
        db.flush()
        db.add(
            VideoDomain(
                video_id=video.id,
                domain_id=domain.id,
                raw_url="https://rate-limited.example",
                normalized_url="https://rate-limited.example/",
                active=True,
            )
        )
        db.commit()

        assert _domains_due_for_check(db, 10) == []
        domain.last_checked_at = now - timedelta(days=4)
        db.commit()
        assert [item.name for item in _domains_due_for_check(db, 10)] == [
            "rate-limited.example"
        ]


def test_removed_links_are_included_in_targeted_candidate_refresh() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        affected_domain_ids: set[int] = set()
        linked = _video("mutable00001", "channel-1", "Visit https://old-resource.com/start")
        process_video(db, linked, "seed", "test", affected_domain_ids)
        old_domain = db.scalar(select(Domain).where(Domain.name == "old-resource.com"))
        assert old_domain is not None and old_domain.id in affected_domain_ids
        db.commit()

        affected_domain_ids.clear()
        unlinked = _video("mutable00001", "channel-1", "The link has been removed.")
        process_video(db, unlinked, "seed", "test", affected_domain_ids)

        assert old_domain.id in affected_domain_ids
        assert db.get(VideoRefreshState, "mutable00001") is None


def test_scheduler_runs_channel_fanout_and_adaptive_refresh() -> None:
    scheduler = build_scheduler(Settings())
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert "youtube_channel_fanout" in jobs
    assert "view_snapshots" in jobs
    assert "youtube_intelligence" in jobs
    assert jobs["youtube_channel_fanout"].trigger.interval == timedelta(minutes=30)
    assert jobs["view_snapshots"].trigger.interval == timedelta(hours=6)


def test_scale_tables_are_registered_without_mutating_existing_video_schema() -> None:
    assert "youtube_channels" in Base.metadata.tables
    assert "video_refresh_states" in Base.metadata.tables
    assert YouTubeQuotaLedger.__tablename__ in Base.metadata.tables
    assert YouTubeChannelIntelligence.__tablename__ in Base.metadata.tables
    assert YouTubeDomainSignal.__tablename__ in Base.metadata.tables
    assert "dropped_domain_matches" in Base.metadata.tables
    assert "next_refresh_at" in Base.metadata.tables["video_refresh_states"].columns


def test_view_and_money_aggregates_use_64_bit_integers() -> None:
    bigint_columns = (
        ("videos", "lifetime_views"),
        ("view_snapshots", "view_count"),
        ("candidates", "monthly_views"),
        ("video_refresh_states", "last_view_count"),
        ("youtube_domain_signals", "lifetime_linked_video_views"),
        ("youtube_domain_signals", "monthly_linked_video_exposure"),
        ("youtube_domain_signals", "expected_clicks_monthly"),
    )
    for table_name, column_name in bigint_columns:
        column = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(column.type, BigInteger)
    assert "channel_id" in Base.metadata.tables["youtube_channels"].columns
    assert "next_refresh_at" not in Base.metadata.tables["videos"].columns
