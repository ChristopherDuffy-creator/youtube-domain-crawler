from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import jobs
from app.config import Settings
from app.database import Base
from app.models import RunLog, SearchState, Video, YouTubeQuotaLedger
from app.youtube import SearchPage, YouTubeError, YouTubeVideo


def _video(video_id: str) -> YouTubeVideo:
    return YouTubeVideo(
        id=video_id,
        title=f"Video {video_id}",
        channel_id="channel-one",
        channel_title="Channel One",
        description=f"Visit https://{video_id}.example/path",
        published_at=datetime(2020, 1, 1, tzinfo=UTC),
        view_count=25_000,
    )


class FakeDiscoveryClient:
    def __init__(self, page: SearchPage) -> None:
        self.page = page
        self.search_calls: list[tuple[str, str | None]] = []
        self.detail_calls: list[list[str]] = []

    def search_videos(
        self,
        query: str,
        *,
        published_before: datetime,
        page_token: str | None = None,
    ) -> SearchPage:
        del published_before
        self.search_calls.append((query, page_token))
        return self.page

    def fetch_videos(self, video_ids: list[str]) -> list[YouTubeVideo]:
        self.detail_calls.append(video_ids)
        return [_video(video_id) for video_id in video_ids]


def _run_discovery_with(
    monkeypatch,
    *,
    page: SearchPage,
    known_ids: list[str],
) -> tuple[Session, FakeDiscoveryClient]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    client = FakeDiscoveryClient(page)
    with Session(engine) as db:
        db.add(SearchState(query="seed query", page_token="saved-cursor"))
        db.add_all([Video(id=video_id) for video_id in known_ids])
        db.commit()

    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(jobs, "EVERGREEN_QUERIES", ["seed query"])
    monkeypatch.setattr(jobs, "get_settings", lambda: Settings(search_calls_per_run=1))
    monkeypatch.setattr(jobs, "YouTubeClient", lambda _api_key: client)
    jobs.run_discovery()
    return Session(engine), client


def test_discovery_fetches_only_unknown_video_ids_and_advances_cursor(monkeypatch) -> None:
    db, client = _run_discovery_with(
        monkeypatch,
        page=SearchPage(["known001", "new00001", "known002", "new00002"], "next-cursor"),
        known_ids=["known001", "known002"],
    )
    try:
        run = db.scalar(select(RunLog).where(RunLog.job == "youtube_discovery"))
        state = db.scalar(select(SearchState).where(SearchState.query == "seed query"))
        ledger = db.scalar(select(YouTubeQuotaLedger))
        assert run is not None
        assert state is not None
        assert ledger is not None
        assert client.search_calls == [("seed query", "saved-cursor")]
        assert client.detail_calls == [["new00001", "new00002"]]
        assert state.page_token == "next-cursor"
        assert run.counters["videos_returned"] == 4
        assert run.counters["known_videos_skipped"] == 2
        assert run.counters["video_detail_calls"] == 1
        assert run.counters["new_videos"] == 2
        assert ledger.search_calls == 1
        assert ledger.data_units == 2
    finally:
        db.close()


def test_discovery_skips_detail_request_for_an_already_indexed_page(monkeypatch) -> None:
    db, client = _run_discovery_with(
        monkeypatch,
        page=SearchPage(["known001", "known002"], None),
        known_ids=["known001", "known002"],
    )
    try:
        run = db.scalar(select(RunLog).where(RunLog.job == "youtube_discovery"))
        state = db.scalar(select(SearchState).where(SearchState.query == "seed query"))
        ledger = db.scalar(select(YouTubeQuotaLedger))
        assert run is not None
        assert state is not None
        assert ledger is not None
        assert client.detail_calls == []
        assert state.page_token is None
        assert state.pages_scanned == 0
        assert run.counters["known_videos_skipped"] == 2
        assert run.counters["video_detail_calls"] == 0
        assert run.counters["new_videos"] == 0
        assert ledger.search_calls == 1
        assert ledger.data_units == 1
    finally:
        db.close()


def test_discovery_continues_after_one_youtube_search_error(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    class PartiallyFailingClient:
        def __init__(self) -> None:
            self.search_calls: list[str] = []
            self.detail_calls: list[list[str]] = []

        def search_videos(
            self,
            query: str,
            *,
            published_before: datetime,
            page_token: str | None = None,
        ) -> SearchPage:
            del published_before, page_token
            self.search_calls.append(query)
            if query == "bad query":
                raise YouTubeError("expired page token")
            return SearchPage(["new00001"], None)

        def fetch_videos(self, video_ids: list[str]) -> list[YouTubeVideo]:
            self.detail_calls.append(video_ids)
            return [_video(video_id) for video_id in video_ids]

    with Session(engine) as db:
        db.add_all(
            [
                SearchState(query="bad query", page_token="stale-cursor"),
                SearchState(query="good query"),
            ]
        )
        db.commit()

    client = PartiallyFailingClient()
    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(jobs, "EVERGREEN_QUERIES", ["bad query", "good query"])
    monkeypatch.setattr(jobs, "get_settings", lambda: Settings(search_calls_per_run=2))
    monkeypatch.setattr(jobs, "YouTubeClient", lambda _api_key: client)

    jobs.run_discovery()

    with Session(engine) as db:
        run = db.scalar(select(RunLog).where(RunLog.job == "youtube_discovery"))
        bad_state = db.scalar(select(SearchState).where(SearchState.query == "bad query"))
        good_state = db.scalar(select(SearchState).where(SearchState.query == "good query"))
        assert run is not None
        assert bad_state is not None
        assert good_state is not None
        assert run.status == "partial"
        assert run.counters["search_calls"] == 2
        assert run.counters["api_errors"] == 1
        assert run.counters["new_videos"] == 1
        assert client.search_calls == ["bad query", "good query"]
        assert client.detail_calls == [["new00001"]]
        assert bad_state.page_token is None
        assert bad_state.pages_scanned == 0
        assert bad_state.last_run_at is not None
        assert good_state.last_run_at is not None
        assert run.counters["cursor_resets"] == 1
