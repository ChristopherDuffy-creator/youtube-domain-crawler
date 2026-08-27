from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.dataforseo import estimate_provider_proof_max_cost_usd
from app.link_hunter_preview import (
    build_provider_proof_preview,
    select_cached_deep_proof_targets_with_ranking,
)
from app.models import BacklinkSummary, Domain, ProviderQuery


def _summary(domain_id: int, *, pages: int, sites: int, rank: float) -> BacklinkSummary:
    return BacklinkSummary(
        domain_id=domain_id,
        provider="dataforseo",
        backlinks=pages * 2,
        referring_pages=pages,
        referring_domains=sites,
        referring_main_domains=sites,
        rank=rank,
        raw_summary={
            "url": "cached.example",
            "backlinks": pages * 2,
            "referring_pages": pages,
            "referring_domains": sites,
            "referring_main_domains": sites,
            "rank": rank,
        },
    )


def test_cached_only_deep_proof_has_no_new_summary_cost() -> None:
    assert estimate_provider_proof_max_cost_usd(0, 5, 25) == 0.1515


def test_cached_summary_stays_in_winner_queue_until_deep_proved() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(link_hunter_proof_batch_size=5)
    with Session(engine) as db:
        strong = Domain(name="strong.example")
        weaker = Domain(name="weaker.example")
        db.add_all([strong, weaker])
        db.flush()
        db.add_all([
            _summary(strong.id, pages=200, sites=60, rank=75),
            _summary(weaker.id, pages=20, sites=8, rank=30),
        ])
        db.commit()

        names, *_ = select_cached_deep_proof_targets_with_ranking(db, settings)
        assert names[:2] == ["strong.example", "weaker.example"]

        db.add(ProviderQuery(
            provider="dataforseo",
            endpoint="backlinks",
            target="strong.example",
            status="complete",
        ))
        db.commit()
        names, *_ = select_cached_deep_proof_targets_with_ranking(db, settings)
        assert "strong.example" not in names
        assert names[0] == "weaker.example"


def test_preview_can_run_cached_winner_queue_without_new_summary_targets() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        dataforseo_login="configured",
        dataforseo_password="configured",
        link_hunter_proof_batch_size=5,
        link_hunter_backlinks_per_domain=25,
        link_hunter_proof_max_cost_usd=0.18,
    )
    with Session(engine) as db:
        domain = Domain(name="winner.example")
        db.add(domain)
        db.flush()
        db.add(_summary(domain.id, pages=80, sites=30, rank=70))
        db.commit()
        preview = build_provider_proof_preview(db, settings)

    assert preview["summary_target_count"] == 0
    assert preview["cached_deep_target_count"] == 1
    assert preview["work_available_count"] == 1
    assert preview["deep_proof_target_count"] == 1
    assert preview["provisional_deep_targets"] == ["winner.example"]
    assert preview["estimated_max_cost_usd"] > 0
    assert preview["ready_for_controlled_proof"] is True


def test_retired_controller_and_youtube_dashboard_expose_new_behavior() -> None:
    scheduler = Path(".github/workflows/link-hunter-approved-scheduler.yml").read_text(encoding="utf-8")
    production = Path(".github/workflows/link-hunter-production-batch.yml").read_text(encoding="utf-8")
    dashboard = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "cron:" not in scheduler
    assert "no crawler or paid-provider work was started" in scheduler
    assert "/api/link-hunter/proof" in production
    assert "run_in_progress" in production
    assert "Web Link Hunter" not in dashboard
    assert "Buy Score" in dashboard
    assert "Potential value / month" in dashboard
    assert "10k–20k" in dashboard
