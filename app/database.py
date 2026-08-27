from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _normalise_database_url(url: str) -> str:
    # Railway supplies postgresql://; SQLAlchemy needs the selected psycopg driver.
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


settings = get_settings()
database_url = _normalise_database_url(settings.database_url)
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}

engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


# YouTube and aggregate view counts can legitimately exceed PostgreSQL's
# signed 32-bit INTEGER ceiling.  create_all() does not widen existing columns,
# so keep this small, idempotent runtime migration beside schema creation.
_BIGINT_COLUMNS = (
    ("videos", "lifetime_views"),
    ("view_snapshots", "view_count"),
    ("candidates", "monthly_views"),
    ("candidates", "start_monthly_views"),
    ("candidates", "day3_monthly_views"),
    ("candidates", "day7_monthly_views"),
    ("video_refresh_states", "last_view_count"),
    ("youtube_domain_signals", "lifetime_linked_video_views"),
    ("youtube_domain_signals", "observed_view_gain"),
    ("youtube_domain_signals", "monthly_linked_video_exposure"),
    ("youtube_domain_signals", "click_eligible_exposure"),
    ("youtube_domain_signals", "short_form_exposure"),
    ("youtube_domain_signals", "expected_clicks_monthly"),
)

_ADDITIVE_COLUMNS = (
    ("videos", "duration_seconds", "INTEGER"),
    (
        "candidates",
        "start_monthly_views",
        "BIGINT NOT NULL DEFAULT 0",
    ),
    (
        "candidates",
        "day3_monthly_views",
        "BIGINT NOT NULL DEFAULT 0",
    ),
    (
        "candidates",
        "day7_monthly_views",
        "BIGINT NOT NULL DEFAULT 0",
    ),
    (
        "candidates",
        "evaluation_stage",
        "VARCHAR(16) NOT NULL DEFAULT 'collecting'",
    ),
    (
        "candidates",
        "trend_percent",
        "DOUBLE PRECISION NOT NULL DEFAULT 0",
    ),
    (
        "candidates",
        "buy_ready",
        "BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "youtube_domain_signals",
        "observed_view_gain",
        "BIGINT NOT NULL DEFAULT 0",
    ),
    (
        "youtube_domain_signals",
        "click_eligible_exposure",
        "BIGINT NOT NULL DEFAULT 0",
    ),
    (
        "youtube_domain_signals",
        "short_form_exposure",
        "BIGINT NOT NULL DEFAULT 0",
    ),
    (
        "youtube_domain_signals",
        "short_form_video_count",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "youtube_domain_signals",
        "spike_video_count",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "youtube_domain_signals",
        "model_version",
        "INTEGER NOT NULL DEFAULT 1",
    ),
)


def ensure_runtime_schema(bind: Engine = engine) -> None:
    """Apply safe additive/widening migrations missed by ``create_all``."""
    if bind.dialect.name != "postgresql":
        return
    type_query = text(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = :table_name
          AND column_name = :column_name
        """
    )
    with bind.begin() as connection:
        for table_name, column_name, column_type in _ADDITIVE_COLUMNS:
            # Every identifier and SQL type comes from the static allow-list.
            connection.execute(
                text(f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{column_name}" {column_type}')
            )
        for table_name, column_name in _BIGINT_COLUMNS:
            data_type = connection.execute(
                type_query,
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one_or_none()
            if data_type == "integer":
                # Identifiers come only from the static allow-list above.
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" TYPE BIGINT')
                )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_youtube_domain_signals_model_version "
                "ON youtube_domain_signals (model_version)"
            )
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_candidates_evaluation_stage ON candidates (evaluation_stage)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_candidates_buy_ready ON candidates (buy_ready)")
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pilot_site_events_domain_time "
                "ON pilot_site_events (domain, created_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pilot_site_events_session_time "
                "ON pilot_site_events (session_id, created_at)"
            )
        )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
