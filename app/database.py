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
    ("video_refresh_states", "last_view_count"),
    ("youtube_domain_signals", "lifetime_linked_video_views"),
    ("youtube_domain_signals", "monthly_linked_video_exposure"),
    ("youtube_domain_signals", "expected_clicks_monthly"),
)

_RUNTIME_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_dropped_domains_first_seen_at "
    "ON dropped_domains (first_seen_at)",
    "CREATE INDEX IF NOT EXISTS ix_provider_queries_provider_endpoint_status_target "
    "ON provider_queries (provider, endpoint, status, target)",
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
        for statement in _RUNTIME_INDEXES:
            connection.execute(text(statement))
        for table_name, column_name in _BIGINT_COLUMNS:
            data_type = connection.execute(
                type_query,
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one_or_none()
            if data_type == "integer":
                # Identifiers come only from the static allow-list above.
                connection.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ALTER COLUMN "{column_name}" TYPE BIGINT'
                    )
                )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
