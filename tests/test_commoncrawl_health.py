from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import health
from app.models import Candidate, Domain, RunLog, Video, VideoDomain, YouTubeChannel


def test_health_exposes_only_sanitized_commoncrawl_counts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        db.add(
            RunLog(
                job="commoncrawl_prefilter",
                started_at=now,
                finished_at=now,
                status="complete",
                counters={
                    "checked": 10,
                    "with_capture": 4,
                    "without_capture": 6,
                    "errors": 0,
                    "provider_cost_usd": 0.0,
                    "error_details": ["target-domain.example should not leak"],
                },
            )
        )
        db.commit()
        payload = health(db)

    # SQLite round-trips DateTime values without tzinfo even when the source
    # datetime is UTC-aware. The production PostgreSQL path is unaffected.
    expected_finished_at = now.replace(tzinfo=None).isoformat()
    assert payload["database"] == "ok"
    assert payload["database_storage"] is None
    assert payload["commoncrawl_prefilter"] == {
        "status": "complete",
        "checked": 10,
        "with_capture": 4,
        "without_capture": 6,
        "errors": 0,
        "provider_cost_usd": 0.0,
        "finished_at": expected_finished_at,
    }
    assert "target-domain.example" not in str(payload)


def test_health_exposes_sanitized_youtube_and_email_operations() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        domain = Domain(name="private-candidate.example")
        video = Video(id="healthvideo1", active=True)
        db.add_all([domain, video])
        db.flush()
        db.add_all(
            [
                VideoDomain(
                    video_id=video.id,
                    domain_id=domain.id,
                    raw_url="https://private-candidate.example",
                    normalized_url="https://private-candidate.example/",
                    active=True,
                ),
                Candidate(
                    domain_id=domain.id,
                    tier="pending",
                    best_video_id=video.id,
                ),
                YouTubeChannel(channel_id="private-channel", inventory_complete=True),
                RunLog(
                    job="youtube_channel_fanout",
                    started_at=now,
                    finished_at=now,
                    status="partial",
                    counters={
                        "playlist_calls": 12,
                        "videos_fetched": 420,
                        "new_links": 15,
                        "errors": 1,
                        "error_details": ["private-candidate.example must stay private"],
                    },
                ),
                RunLog(
                    job="daily_digest",
                    started_at=now,
                    finished_at=now,
                    status="complete",
                    counters={"emailed": 0},
                ),
            ]
        )
        db.commit()

        payload = health(db)

    assert payload["email"]["configured"] is False
    assert payload["email"]["latest_digest"]["emailed"] == 0
    assert payload["youtube"]["totals"]["videos"] == 1
    assert payload["youtube"]["totals"]["channels_complete"] == 1
    assert payload["youtube"]["totals"]["never_checked_domains"] == 1
    assert payload["youtube"]["tiers"]["pending"] == 1
    assert payload["youtube"]["latest_runs"]["youtube_channel_fanout"] == {
        "status": "partial",
        "counters": {
            "playlist_calls": 12,
            "videos_discovered": 0,
            "videos_fetched": 420,
            "new_videos": 0,
            "new_domains": 0,
            "new_links": 15,
            "channels_completed": 0,
            "candidates_refreshed": 0,
            "hot_pages": 0,
            "warm_pages": 0,
            "cold_or_unrated_pages": 0,
            "quota_exhausted": 0,
            "errors": 1,
        },
        "finished_at": now.replace(tzinfo=None).isoformat(),
        "failure_stage": None,
        "error_summary": None,
    }
    assert "private-candidate.example" not in str(payload)


def test_health_exposes_only_aggregate_paid_proof_cost_and_result_counts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        db.add(
            RunLog(
                job="link_hunter_proof",
                started_at=now,
                finished_at=now,
                status="complete",
                counters={
                    "summary_screened": 100,
                    "deep_proof_target_count": 5,
                    "source_links_verified": 3,
                    "errors": 0,
                    "provider_cost_usd": 0.1791,
                    "daily_budget": {
                        "limit_usd": 2.16,
                        "spent_usd": 0.1791,
                        "reserved_usd": 0.0,
                    },
                    "deep_proof_targets": ["private-target.example"],
                    "error_details": ["private-provider-error.example"],
                },
            )
        )
        db.commit()
        payload = health(db)

    assert payload["web_intelligence"]["latest_proof"] == {
        "status": "complete",
        "summary_screened": 100,
        "deep_proof_target_count": 5,
        "source_links_verified": 3,
        "errors": 0,
        "provider_cost_usd": 0.1791,
        "daily_budget_limit_usd": 2.16,
        "daily_budget_committed_usd": 0.1791,
        "finished_at": now.replace(tzinfo=None).isoformat(),
    }
    assert "private-target.example" not in str(payload)
    assert "private-provider-error.example" not in str(payload)
