from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.link_hunter_preview import build_provider_proof_preview
from app.models import DroppedDomain, ProviderQuery


def test_preview_selects_next_unchecked_drops_without_paid_calls() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(
        dataforseo_login="configured-login",
        dataforseo_password="configured-password",
        link_hunter_enabled=False,
        link_hunter_summary_batch_size=3,
        link_hunter_proof_batch_size=3,
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=0.50,
    )

    with Session(engine) as db:
        db.add_all(
            [
                DroppedDomain(name="old.example", source="test", first_seen_at=now - timedelta(days=3)),
                DroppedDomain(name="one.example", source="test", first_seen_at=now - timedelta(hours=3)),
                DroppedDomain(name="two.example", source="test", first_seen_at=now - timedelta(hours=2)),
                DroppedDomain(name="three.example", source="test", first_seen_at=now - timedelta(hours=1)),
                DroppedDomain(name="checked.example", source="test", first_seen_at=now),
                ProviderQuery(
                    provider="dataforseo",
                    endpoint="bulk_backlink_summary",
                    target="checked.example",
                    status="complete",
                ),
            ]
        )
        db.commit()

        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"] == ["three.example", "two.example", "one.example"]
    assert preview["target_count"] == 3
    assert preview["summary_target_count"] == 3
    assert preview["deep_proof_target_count"] == 3
    assert preview["provisional_deep_targets"] == preview["targets"]
    assert preview["backlinks_per_domain"] == 25
    assert preview["max_source_pages"] == 75
    assert 0 < preview["estimated_max_cost_usd"] <= 0.50
    assert preview["configured_cost_cap_usd"] == 0.50
    assert preview["within_cost_cap"] is True
    assert preview["dataforseo_configured"] is True
    assert preview["link_hunter_enabled"] is False
    assert preview["paid_requests_made"] == 0
    assert preview["commoncrawl_signal_count"] == 0


def test_preview_is_zero_cost_even_without_credentials() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        dataforseo_login="",
        dataforseo_password="",
        link_hunter_proof_batch_size=1,
    )

    with Session(engine) as db:
        db.add(DroppedDomain(name="preview-only.example", source="test"))
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"] == ["preview-only.example"]
    assert preview["dataforseo_configured"] is False
    assert preview["paid_requests_made"] == 0


def test_preview_models_100_summary_targets_and_only_five_deep_proofs() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        dataforseo_login="configured-login",
        dataforseo_password="configured-password",
        link_hunter_summary_batch_size=100,
        link_hunter_proof_batch_size=5,
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=0.18,
    )

    with Session(engine) as db:
        db.add_all(
            [DroppedDomain(name=f"screen-{index:03}.example", source="test") for index in range(100)]
        )
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["summary_target_count"] == 100
    assert preview["deep_proof_target_count"] == 5
    assert len(preview["provisional_deep_targets"]) == 5
    assert preview["max_source_pages"] == 125
    assert preview["estimated_max_cost_usd"] == 0.1791
    assert preview["within_cost_cap"] is True
    assert preview["paid_requests_made"] == 0


def test_commoncrawl_positive_targets_move_ahead_of_newer_unknown_and_negative() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    settings = Settings(link_hunter_summary_batch_size=3, link_hunter_proof_batch_size=3)

    with Session(engine) as db:
        db.add_all(
            [
                DroppedDomain(name="newest.example", source="test", first_seen_at=now),
                DroppedDomain(
                    name="positive.example", source="test", first_seen_at=now - timedelta(minutes=1)
                ),
                DroppedDomain(
                    name="unknown.example", source="test", first_seen_at=now - timedelta(minutes=2)
                ),
                DroppedDomain(
                    name="negative.example", source="test", first_seen_at=now - timedelta(minutes=3)
                ),
                ProviderQuery(
                    provider="commoncrawl",
                    endpoint="url_index",
                    target="positive.example",
                    status="complete",
                    row_count=2,
                ),
                ProviderQuery(
                    provider="commoncrawl",
                    endpoint="url_index",
                    target="negative.example",
                    status="complete",
                    row_count=0,
                ),
            ]
        )
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["targets"] == ["positive.example", "newest.example", "unknown.example"]
    assert preview["commoncrawl_positive_targets"] == ["positive.example"]
    assert preview["target_commoncrawl_hits"] == {
        "positive.example": 2,
        "newest.example": None,
        "unknown.example": None,
    }
