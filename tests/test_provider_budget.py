from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import link_hunter
from app.config import Settings
from app.database import Base
from app.models import ProviderDailyBudget, RunLog
from app.provider_budget import (
    finalize_provider_daily_budget,
    provider_daily_budget_snapshot,
    reserve_provider_daily_budget,
)


def _settings() -> Settings:
    return Settings(
        dataforseo_login="login",
        dataforseo_password="password",
        link_hunter_enabled=True,
        link_hunter_proof_max_cost_usd=0.18,
        link_hunter_daily_max_cost_usd=2.16,
    )


def test_twelve_reservations_fit_but_thirteenth_is_refused() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings()

    with Session(engine) as db:
        for _ in range(12):
            reservation = reserve_provider_daily_budget(db, settings)
            assert reservation is not None
            finalize_provider_daily_budget(
                db,
                reservation,
                0.1791,
                release_unused=True,
            )

        assert reserve_provider_daily_budget(db, settings) is None
        snapshot = provider_daily_budget_snapshot(db, settings)
        assert snapshot == {
            "date_utc": snapshot["date_utc"],
            "limit_usd": 2.16,
            "spent_usd": 2.1492,
            "reserved_usd": 0.0,
            "remaining_usd": 0.0108,
            "completed_runs": 12,
        }


def test_partial_run_keeps_the_unused_envelope_reserved() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = _settings()

    with Session(engine) as db:
        reservation = reserve_provider_daily_budget(db, settings)
        assert reservation is not None
        finalize_provider_daily_budget(
            db,
            reservation,
            0.0276,
            release_unused=False,
        )
        snapshot = provider_daily_budget_snapshot(db, settings)

    assert snapshot["spent_usd"] == 0.0276
    assert snapshot["reserved_usd"] == 0.1524
    assert snapshot["remaining_usd"] == 1.98
    assert snapshot["completed_runs"] == 0


def test_job_wrapper_skips_thirteenth_run_without_calling_provider(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    settings = _settings()
    provider_calls = 0

    def fake_proof(_db: Session, _settings: Settings) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        return {"provider_cost_usd": 0.1791, "errors": 0, "summary_screened": 100}

    monkeypatch.setattr(link_hunter, "SessionLocal", test_session)
    monkeypatch.setattr(link_hunter, "get_settings", lambda: settings)
    monkeypatch.setattr(link_hunter, "run_provider_proof", fake_proof)

    for _ in range(12):
        counters = link_hunter.run_provider_proof_job()
        assert counters["daily_budget_skipped"] is False

    skipped = link_hunter.run_provider_proof_job()
    assert skipped["daily_budget_skipped"] is True
    assert skipped["provider_cost_usd"] == 0.0
    assert provider_calls == 12

    with Session(engine) as db:
        budget = db.scalar(select(ProviderDailyBudget))
        runs = db.scalars(select(RunLog).order_by(RunLog.id)).all()
        assert budget is not None and budget.completed_runs == 12
        assert [run.status for run in runs] == ["complete"] * 12 + ["skipped"]
