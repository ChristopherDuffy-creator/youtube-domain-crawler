from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import jobs
from app.config import Settings
from app.database import Base
from app.models import RunLog, SearchState
from app.storage_guard import DatabaseStorageStatus, database_storage_status


class _FakePostgresSession:
    def __init__(self, database_bytes: int) -> None:
        self.database_bytes = database_bytes
        self.statements: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, statement):
        self.statements.append(str(statement))
        return self.database_bytes


def test_storage_guard_keeps_sqlite_allowed_and_honours_disabled_limit() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        sqlite_status = database_storage_status(
            db,
            Settings(database_storage_soft_limit_gb=16),
        )

    assert sqlite_status.write_allowed is True
    assert sqlite_status.reason == "non_postgresql"
    assert sqlite_status.database_bytes is None

    postgres = _FakePostgresSession(99 * 1024**3)
    disabled = database_storage_status(
        postgres,  # type: ignore[arg-type]
        Settings(database_storage_soft_limit_gb=0),
    )
    assert disabled.write_allowed is True
    assert disabled.enabled is False
    assert disabled.reason == "disabled"


def test_storage_guard_queries_postgres_and_blocks_at_the_soft_limit() -> None:
    size_bytes = 16 * 1024**3
    postgres = _FakePostgresSession(size_bytes)

    status = database_storage_status(
        postgres,  # type: ignore[arg-type]
        Settings(database_storage_soft_limit_gb=16),
    )

    assert postgres.statements == ["SELECT pg_database_size(current_database())"]
    assert status.database_bytes == size_bytes
    assert status.database_gb == 16.0
    assert status.write_allowed is False
    assert status.reason == "soft_limit_reached"


def test_storage_guard_blocks_discovery_before_client_or_run_log(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    calls = 0

    class NeverConstructClient:
        def __init__(self, *_args, **_kwargs) -> None:
            nonlocal calls
            calls += 1
            raise AssertionError("YouTube client must not be constructed when storage is blocked")

    monkeypatch.setattr(jobs, "SessionLocal", test_session)
    monkeypatch.setattr(jobs, "get_settings", lambda: Settings(youtube_api_key="test-key"))
    monkeypatch.setattr(jobs, "YouTubeClient", NeverConstructClient)
    monkeypatch.setattr(jobs, "storage_guard_allows_writes", lambda *_args: False)

    jobs.run_discovery()

    with Session(engine) as db:
        assert db.scalars(select(SearchState)).all() == []
        assert db.scalars(select(RunLog)).all() == []
    assert calls == 0


def test_paid_proof_is_blocked_before_its_client_is_called(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    calls = 0
    blocked = DatabaseStorageStatus(
        backend="postgresql",
        enabled=True,
        limit_gb=16,
        database_bytes=16 * 1024**3,
        database_gb=16.0,
        write_allowed=False,
        reason="soft_limit_reached",
    )

    def forbidden_proof(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Paid proof must not start when storage is blocked")

    from app import link_hunter

    monkeypatch.setattr(link_hunter, "SessionLocal", test_session)
    monkeypatch.setattr(
        link_hunter,
        "get_settings",
        lambda: Settings(link_hunter_enabled=True, dataforseo_login="user", dataforseo_password="pass"),
    )
    monkeypatch.setattr(link_hunter, "storage_guard_allows_writes", lambda *_args: False)
    monkeypatch.setattr(link_hunter, "database_storage_status", lambda *_args: blocked)
    monkeypatch.setattr(link_hunter, "run_provider_proof", forbidden_proof)

    counters = link_hunter.run_provider_proof_job()

    assert counters["storage_guard_blocked"] is True
    assert counters["provider_cost_usd"] == 0.0
    assert calls == 0


def test_storage_guard_blocks_digest_before_email_delivery(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    deliveries = 0

    def forbidden_delivery(*_args, **_kwargs) -> None:
        nonlocal deliveries
        deliveries += 1
        raise AssertionError("Email delivery must not start when storage is blocked")

    monkeypatch.setattr(jobs, "SessionLocal", test_session)
    monkeypatch.setattr(
        jobs,
        "get_settings",
        lambda: Settings(resend_api_key="test-key"),
    )
    monkeypatch.setattr(jobs, "storage_guard_allows_writes", lambda *_args: False)
    monkeypatch.setattr(jobs, "send_email", forbidden_delivery)

    jobs.run_daily_digest()

    with Session(engine) as db:
        assert db.scalars(select(RunLog).where(RunLog.job == "daily_digest")).all() == []
    assert deliveries == 0
