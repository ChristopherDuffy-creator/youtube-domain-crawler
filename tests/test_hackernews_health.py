from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import health
from app.models import RunLog


def test_health_exposes_only_hackernews_aggregate_counts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as db:
        db.add(
            RunLog(
                job="hackernews_prefilter",
                started_at=now,
                finished_at=now,
                status="complete",
                counters={
                    "queries": 10,
                    "search_hits": 44,
                    "items_with_exact_links": 3,
                    "exact_links_saved": 4,
                    "domains_with_links": 2,
                    "errors": 0,
                    "provider_cost_usd": 0.0,
                    "error_details": ["private-target.example should not leak"],
                },
            )
        )
        db.commit()
        payload = health(db)

    expected_finished_at = now.replace(tzinfo=None).isoformat()
    assert payload["database"] == "ok"
    assert payload["hackernews_prefilter"] == {
        "status": "complete",
        "queries": 10,
        "search_hits": 44,
        "items_with_exact_links": 3,
        "exact_links_saved": 4,
        "domains_with_links": 2,
        "errors": 0,
        "provider_cost_usd": 0.0,
        "finished_at": expected_finished_at,
    }
    assert "private-target.example" not in str(payload)
