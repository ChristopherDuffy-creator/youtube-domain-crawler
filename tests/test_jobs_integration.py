from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.jobs import _process_video_isolated, ingest_dropped_text, process_video, refresh_candidates
from app.models import Candidate, Domain, DroppedDomain, Video, ViewSnapshot
from app.youtube import YouTubeVideo


def test_video_to_qualified_candidate_and_dropped_match() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        process_video(
            db,
            YouTubeVideo(
                id="test1234567",
                title="Evergreen course tutorial",
                channel_id="channel-1",
                channel_title="Example channel",
                description="Visit https://example-course.com/start to download the course.",
                published_at=now - timedelta(days=365 * 6),
                view_count=125_000,
            ),
            discovery_query="course tutorial",
            discovery_route="youtube_first",
        )
        db.add(
            ViewSnapshot(
                video_id="test1234567",
                captured_at=now - timedelta(days=30),
                capture_date=(now - timedelta(days=30)).date(),
                view_count=100_000,
            )
        )
        domain = db.scalar(select(Domain).where(Domain.name == "example-course.com"))
        assert domain is not None
        domain.availability_status = "available"
        domain.availability_source = "porkbun"
        db.commit()

        assert refresh_candidates(db) == 1
        candidate = db.scalar(select(Candidate))
        assert candidate is not None
        assert candidate.tier == "qualified"
        assert candidate.monthly_views >= 24_000
        assert candidate.verified_30d is True

        counters = ingest_dropped_text(db, "example-course.com", "test feed")
        assert counters["matched_index"] == 1
        dropped = db.scalar(select(DroppedDomain))
        assert dropped is not None
        assert dropped.matched_existing_index is True

        repeated = ingest_dropped_text(
            db,
            "example-course.com, brand-new-example.net, brand-new-example.net",
            "second feed",
        )
        assert repeated == {"parsed": 2, "new": 1, "matched_index": 1}
        assert len(db.scalars(select(DroppedDomain)).all()) == 2


def test_malformed_video_is_isolated_and_external_text_is_sanitized() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    bad_video = YouTubeVideo(
        id="x" * 21,
        title="bad",
        channel_id="channel-1",
        channel_title="Example channel",
        description="",
        published_at=now,
        view_count=1,
    )
    good_video = YouTubeVideo(
        id="goodvideo01",
        title="Good\x00 title\x1f",
        channel_id="channel-1",
        channel_title="Example\x00 channel",
        description="Get this at https://clean-example.com/path\x00",
        published_at=now,
        view_count=10,
    )

    with Session(engine) as db:
        failed_result, error = _process_video_isolated(
            db, bad_video, "seed", "test"
        )
        saved_result, saved_error = _process_video_isolated(
            db, good_video, "seed", "test"
        )
        db.commit()

        assert failed_result is None
        assert error is not None and "Invalid YouTube video identifier" in error
        assert saved_error is None
        assert saved_result is not None
        stored = db.get(Video, "goodvideo01")
        assert stored is not None
        assert stored.title == "Good title"
        assert stored.channel_title == "Example channel"
        assert stored.description == "Get this at https://clean-example.com/path"
        assert db.scalar(select(Domain).where(Domain.name == "clean-example.com")) is not None
