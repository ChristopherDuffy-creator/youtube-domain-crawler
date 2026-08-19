from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.main import health
from app.models import RunLog


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

    assert payload["database"] == "ok"
    assert payload["commoncrawl_prefilter"] == {
        "status": "complete",
        "checked": 10,
        "with_capture": 4,
        "without_capture": 6,
        "errors": 0,
        "provider_cost_usd": 0.0,
        "finished_at": now.isoformat(),
    }
    assert "target-domain.example" not in str(payload)
