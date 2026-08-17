from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.jobs import ingest_dropped_text, process_video, refresh_candidates
from app.models import Candidate, Domain, DroppedDomain, ViewSnapshot
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
