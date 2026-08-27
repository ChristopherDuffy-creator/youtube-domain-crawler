from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.domain_lifecycle import (
    domain_fingerprint,
    migrate_legacy_youtube_bought_decisions,
)
from app.jobs import ingest_dropped_text, process_video, refresh_candidates
from app.main import apply_youtube_domain_action
from app.models import (
    BoughtDomain,
    Candidate,
    DashboardDecision,
    DeletedDomainFingerprint,
    Domain,
    DroppedDomain,
    DroppedDomainMatch,
    PilotSiteEvent,
    ProviderQuery,
    Video,
    VideoDomain,
    VideoRefreshState,
    ViewSnapshot,
    YouTubeDomainSignal,
)
from app.youtube import YouTubeVideo


def _engine():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed_ranked_domain(db: Session, name: str = "actionable.example") -> tuple[Domain, Video]:
    now = datetime.now(UTC)
    video = Video(
        id="actionvideo1",
        title="Evergreen tutorial",
        description=f"Get the guide at https://{name}/offer",
        lifetime_views=250_000,
        first_seen_at=now - timedelta(days=7),
    )
    domain = Domain(
        name=name,
        availability_status="available",
        registrar_price_usd=11.25,
    )
    db.add_all([video, domain])
    db.flush()
    db.add_all(
        [
            VideoDomain(
                video_id=video.id,
                domain_id=domain.id,
                raw_url=f"https://{name}/offer",
                normalized_url=f"https://{name}/offer",
                active=True,
            ),
            ViewSnapshot(
                video_id=video.id,
                captured_at=now - timedelta(days=7),
                capture_date=(now - timedelta(days=7)).date(),
                view_count=200_000,
            ),
            Candidate(
                domain_id=domain.id,
                tier="qualified",
                monthly_views=75_000,
                start_monthly_views=60_000,
                day3_monthly_views=70_000,
                day7_monthly_views=75_000,
                evaluation_stage="day7",
                buy_ready=True,
                score=82.5,
                best_video_id=video.id,
            ),
            YouTubeDomainSignal(
                domain_id=domain.id,
                buy_score=88.0,
                monthly_revenue_low_usd=120.0,
                monthly_revenue_high_usd=480.0,
                max_purchase_price_usd=300.0,
                model_version=4,
            ),
            VideoRefreshState(
                video_id=video.id,
                next_refresh_at=now,
                last_view_count=video.lifetime_views,
            ),
        ]
    )
    db.commit()
    return domain, video


def test_bought_action_snapshots_value_and_removes_candidate_from_ranking() -> None:
    engine = _engine()
    with Session(engine) as db:
        domain, video = _seed_ranked_domain(db, "actionable.com")
        response = apply_youtube_domain_action(
            domain_id=domain.id,
            domain_action="bought",
            return_to="/?view=youtube&tier=qualified",
            _="admin",
            db=db,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/?view=youtube&tier=qualified"
        assert db.get(Domain, domain.id) is not None
        assert db.scalar(select(Candidate).where(Candidate.domain_id == domain.id)) is None
        bought = db.scalar(select(BoughtDomain).where(BoughtDomain.domain_id == domain.id))
        assert bought is not None
        assert bought.domain_name == "actionable.com"
        assert bought.original_tier == "qualified"
        assert bought.monthly_views == 75_000
        assert bought.buy_score == 88.0
        assert bought.monthly_revenue_high_usd == 480.0
        assert bought.best_video_id == video.id

        assert refresh_candidates(db, {domain.id}) == 0
        assert db.scalar(select(Candidate).where(Candidate.domain_id == domain.id)) is None
        assert db.get(Domain, domain.id).excluded_reason == "bought"
        assert ingest_dropped_text(db, domain.name, "repeat feed") == {
            "parsed": 0,
            "new": 0,
            "matched_index": 0,
        }

        repeated = process_video(
            db,
            YouTubeVideo(
                id=video.id,
                title=video.title,
                channel_id="channel-1",
                channel_title="Example",
                description="Still linked at https://actionable.com/offer",
                published_at=datetime.now(UTC) - timedelta(days=100),
                view_count=260_000,
            ),
            discovery_query="test",
            discovery_route="test",
        )
        db.commit()
        assert repeated["external_links"] == 0
        assert refresh_candidates(db, {domain.id}) == 0
        assert db.get(VideoRefreshState, video.id) is None


def test_legacy_youtube_bought_label_moves_into_bought_table() -> None:
    engine = _engine()
    with Session(engine) as db:
        domain, _ = _seed_ranked_domain(db)
        db.add(DashboardDecision(system="youtube", domain_id=domain.id, status="bought"))
        db.commit()

        assert migrate_legacy_youtube_bought_decisions(db) == 1
        assert db.scalar(select(BoughtDomain).where(BoughtDomain.domain_id == domain.id)) is not None
        assert db.scalar(select(Candidate).where(Candidate.domain_id == domain.id)) is None
        assert db.scalar(select(DashboardDecision)) is None
        assert migrate_legacy_youtube_bought_decisions(db) == 0


def test_delete_action_cascades_scrubs_and_blocks_rediscovery() -> None:
    engine = _engine()
    with Session(engine) as db:
        domain, video = _seed_ranked_domain(db, "delete-me.com")
        domain_id = domain.id
        domain_name = domain.name
        video_id = video.id
        dropped = DroppedDomain(name=domain.name, source="test")
        db.add(dropped)
        db.flush()
        db.add_all(
            [
                DroppedDomainMatch(dropped_domain_id=dropped.id, domain_id=domain.id),
                DashboardDecision(system="youtube", domain_id=domain.id, status="ignored"),
                ProviderQuery(provider="test", endpoint="summary", target=domain.name),
                PilotSiteEvent(
                    domain=domain.name,
                    event_type="pageview",
                    session_id="a" * 16,
                ),
            ]
        )
        db.commit()

        response = apply_youtube_domain_action(
            domain_id=domain.id,
            domain_action="delete",
            return_to="/?view=youtube&tier=watchlist",
            _="admin",
            db=db,
        )

        assert response.status_code == 303
        assert db.get(Domain, domain_id) is None
        assert db.scalar(select(Candidate).where(Candidate.domain_id == domain_id)) is None
        assert db.scalar(select(VideoDomain).where(VideoDomain.domain_id == domain_id)) is None
        assert db.get(YouTubeDomainSignal, domain_id) is None
        assert db.scalar(select(DroppedDomain).where(DroppedDomain.name == domain_name)) is None
        assert db.scalar(select(DashboardDecision)) is None
        assert db.scalar(select(ProviderQuery).where(ProviderQuery.target == domain_name)) is None
        assert db.scalar(select(PilotSiteEvent).where(PilotSiteEvent.domain == domain_name)) is None
        assert db.get(VideoRefreshState, video_id) is None

        stored_video = db.get(Video, video_id)
        assert stored_video is not None
        assert "delete-me.com" not in stored_video.description
        fingerprint = db.get(DeletedDomainFingerprint, domain_fingerprint(domain_name))
        assert fingerprint is not None
        assert fingerprint.domain_hash != domain_name

        repeated = process_video(
            db,
            YouTubeVideo(
                id=video_id,
                title=stored_video.title,
                channel_id="channel-1",
                channel_title="Example",
                description="This URL returned: https://delete-me.com/new",
                published_at=datetime.now(UTC) - timedelta(days=100),
                view_count=260_000,
            ),
            discovery_query="test",
            discovery_route="test",
        )
        db.commit()
        assert repeated["external_links"] == 0
        assert db.scalar(select(Domain).where(Domain.name == domain_name)) is None
        assert "delete-me.com" not in db.get(Video, video_id).description
        assert db.get(VideoRefreshState, video_id) is None

        assert ingest_dropped_text(db, "delete-me.com", "repeat feed") == {
            "parsed": 0,
            "new": 0,
            "matched_index": 0,
        }
        assert db.scalar(select(DroppedDomain).where(DroppedDomain.name == domain_name)) is None


def test_delete_action_rejects_a_domain_outside_the_youtube_queue() -> None:
    engine = _engine()
    with Session(engine) as db:
        domain = Domain(name="web-only.example")
        db.add(domain)
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            apply_youtube_domain_action(
                domain_id=domain.id,
                domain_action="delete",
                return_to="/?view=youtube",
                _="admin",
                db=db,
            )

        assert exc_info.value.status_code == 404
        assert db.get(Domain, domain.id) is not None
        assert db.scalar(select(DeletedDomainFingerprint)) is None
