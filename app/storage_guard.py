"""Small, fail-closed guard for write-heavy PostgreSQL crawler work."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings

logger = logging.getLogger(__name__)

_BYTES_PER_GIB = 1024**3


@dataclass(frozen=True)
class DatabaseStorageStatus:
    """A safe-to-publish snapshot of the database storage circuit breaker."""

    backend: str
    enabled: bool
    limit_gb: float
    database_bytes: int | None
    database_gb: float | None
    write_allowed: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "limit_gb": self.limit_gb,
            "database_bytes": self.database_bytes,
            "database_gb": self.database_gb,
            "blocked": not self.write_allowed,
            "reason": self.reason,
        }


def database_storage_status(db: Session, settings: Settings) -> DatabaseStorageStatus:
    """Measure PostgreSQL storage and fail closed at the configured soft limit.

    SQLite is intentionally always allowed: it is used by local development and
    tests, and has no Railway-style PostgreSQL allocation to protect. A failed
    PostgreSQL size measurement is also treated as blocked so crawler jobs do
    not continue external work when the storage safety condition is unknown.
    """

    bind = db.get_bind()
    backend = bind.dialect.name
    limit_gb = float(settings.database_storage_soft_limit_gb)
    if backend != "postgresql":
        return DatabaseStorageStatus(
            backend=backend,
            enabled=limit_gb > 0,
            limit_gb=limit_gb,
            database_bytes=None,
            database_gb=None,
            write_allowed=True,
            reason="non_postgresql",
        )

    try:
        size_bytes = int(
            db.scalar(text("SELECT pg_database_size(current_database())")) or 0
        )
    except Exception:
        logger.exception("Could not determine PostgreSQL database storage size")
        return DatabaseStorageStatus(
            backend=backend,
            enabled=limit_gb > 0,
            limit_gb=limit_gb,
            database_bytes=None,
            database_gb=None,
            write_allowed=False,
            reason="size_check_failed",
        )

    size_gb = round(size_bytes / _BYTES_PER_GIB, 3)
    if limit_gb <= 0:
        return DatabaseStorageStatus(
            backend=backend,
            enabled=False,
            limit_gb=limit_gb,
            database_bytes=size_bytes,
            database_gb=size_gb,
            write_allowed=True,
            reason="disabled",
        )

    return DatabaseStorageStatus(
        backend=backend,
        enabled=True,
        limit_gb=limit_gb,
        database_bytes=size_bytes,
        database_gb=size_gb,
        write_allowed=size_bytes < int(limit_gb * _BYTES_PER_GIB),
        reason="soft_limit_reached"
        if size_bytes >= int(limit_gb * _BYTES_PER_GIB)
        else None,
    )


def storage_guard_allows_writes(db: Session, settings: Settings, job: str) -> bool:
    """Log and block a write-heavy job before it can call external services."""

    status = database_storage_status(db, settings)
    if status.write_allowed:
        return True
    logger.warning(
        "Database storage guard blocked %s before external calls: reason=%s "
        "database_gb=%s limit_gb=%s",
        job,
        status.reason,
        status.database_gb,
        status.limit_gb,
    )
    return False
