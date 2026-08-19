from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.link_hunter_preview import build_provider_proof_preview
from app.models import DroppedDomain, ProviderQuery


def test_old_free_signal_candidate_is_not_lost_behind_recent_noise() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(link_hunter_proof_batch_size=1)

    with Session(engine) as db:
        db.add_all(
            [
                DroppedDomain(
                    name=f"noise-{index}.example",
                    source="test",
                    first_seen_at=now - timedelta(seconds=index),
                )
                for index in range(1001)
            ]
        )
        db.add(
            DroppedDomain(
                name="older-signal.example",
                source="test",
                first_seen_at=now - timedelta(days=10),
            )
        )
        db.add(
            ProviderQuery(
                provider="commoncrawl",
                endpoint="url_index",
                target="older-signal.example",
                status="complete",
                row_count=5,
            )
        )
        db.commit()

        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"][0] == "older-signal.example"
    assert preview["provisional_deep_targets"] == ["older-signal.example"]
    assert preview["target_free_rank_signals"]["older-signal.example"]["commoncrawl_hits"] == 5
