from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import health
from app.models import RunLog


def test_health_exposes_only_stackexchange_aggregate_counts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        db.add(
            RunLog(
                job="stackexchange_prefilter",
                started_at=now,
                finished_at=now,
                status="complete",
                counters={
                    "queries": 15,
                    "questions_matched": 2,
                    "exact_links_saved": 3,
                    "domains_with_links": 1,
                    "quota_remaining": 9985,
                    "errors": 0,
                    "provider_cost_usd": 0.0,
                    "error_details": ["secret-target.example should not leak"],
                },
            )
        )
        db.commit()
        payload = health(db)

    expected_finished_at = now.replace(tzinfo=None).isoformat()
    assert payload["database"] == "ok"
    assert payload["stackexchange_prefilter"] == {
        "status": "complete",
        "queries": 15,
        "questions_matched": 2,
        "exact_links_saved": 3,
        "domains_with_links": 1,
        "quota_remaining": 9985,
        "errors": 0,
        "provider_cost_usd": 0.0,
        "finished_at": expected_finished_at,
    }
    assert "secret-target.example" not in str(payload)
