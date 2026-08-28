from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.jobs import _build_daily_digest_report
from app.main import health
from app.models import Candidate, Domain, YouTubeDomainSignal
from app.youtube_review import youtube_stage_counts


def _add_watchlist_candidate(
    db: Session,
    name: str,
    *,
    click_eligible_exposure: int,
) -> None:
    domain = Domain(
        name=name,
        availability_status="available",
        availability_source="porkbun",
        premium=False,
    )
    db.add(domain)
    db.flush()
    db.add_all(
        [
            Candidate(
                domain_id=domain.id,
                tier="watchlist",
                monthly_views=30_000,
                start_monthly_views=30_000,
                evaluation_stage="day0",
                evaluation_started_at=datetime.now(UTC),
            ),
            YouTubeDomainSignal(
                domain_id=domain.id,
                model_version=4,
                click_eligible_exposure=click_eligible_exposure,
                buy_score=70.0,
                monthly_revenue_high_usd=50.0,
                spike_video_count=0,
            ),
        ]
    )


def test_dashboard_health_and_email_use_the_same_visible_watchlist_count() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(dropped_domain_feed_urls="")

    with Session(engine) as db:
        _add_watchlist_candidate(db, "visible.example", click_eligible_exposure=30_000)
        _add_watchlist_candidate(db, "hidden.example", click_eligible_exposure=0)
        db.commit()

        raw_watchlist = int(
            db.scalar(
                select(func.count())
                .select_from(Candidate)
                .where(Candidate.tier == "watchlist")
            )
            or 0
        )
        stage_counts = youtube_stage_counts(db, settings)
        health_payload = health(db)
        digest = _build_daily_digest_report(db, settings)

    assert raw_watchlist == 2
    assert stage_counts == {"watchlist": 1, "day3": 0, "day7": 0, "low": 0}
    assert health_payload["youtube"]["stages"] == stage_counts
    assert health_payload["youtube"]["tiers"]["watchlist"] == 1
    assert health_payload["youtube"]["raw_tiers"]["watchlist"] == 2
    assert digest.watchlist_count == 1
