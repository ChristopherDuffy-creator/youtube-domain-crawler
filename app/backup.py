from __future__ import annotations

import base64
import gzip
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, func, select, text
from sqlalchemy.engine import Engine

BACKUP_FORMAT = "expandosaurus-logical-backup-v1"


def _encode_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__type__": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__type__": "decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {"__type__": "uuid", "value": str(value)}
    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }
    return value


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict) or "__type__" not in value:
        return value
    kind = value.get("__type__")
    raw = value.get("value")
    if kind == "datetime":
        return datetime.fromisoformat(str(raw))
    if kind == "date":
        return date.fromisoformat(str(raw))
    if kind == "decimal":
        return Decimal(str(raw))
    if kind == "uuid":
        return UUID(str(raw))
    if kind == "bytes":
        return base64.b64decode(str(raw))
    return value


def build_logical_snapshot(engine: Engine) -> bytes:
    """Return a gzip-compressed, portable snapshot of every current DB table."""
    metadata = MetaData()
    metadata.reflect(bind=engine)
    tables: dict[str, list[dict[str, Any]]] = {}
    row_counts: dict[str, int] = {}

    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            rows: list[dict[str, Any]] = []
            for row in connection.execute(select(table)).mappings():
                rows.append({key: _encode_value(value) for key, value in row.items()})
            tables[table.name] = rows
            row_counts[table.name] = len(rows)

    document = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "dialect": engine.dialect.name,
        "row_counts": row_counts,
        "tables": tables,
    }
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return gzip.compress(raw, compresslevel=9)


def read_logical_snapshot(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid or corrupt database backup") from exc
    if document.get("format") != BACKUP_FORMAT:
        raise ValueError("Unsupported database backup format")
    tables = document.get("tables")
    row_counts = document.get("row_counts")
    if not isinstance(tables, dict) or not isinstance(row_counts, dict):
        raise ValueError("Database backup is missing table metadata")
    for name, rows in tables.items():
        if not isinstance(rows, list) or int(row_counts.get(name, -1)) != len(rows):
            raise ValueError(f"Database backup row-count mismatch for {name}")
    return document


def restore_logical_snapshot(engine: Engine, payload: bytes, *, require_empty: bool = True) -> None:
    """Restore a snapshot into an already-created compatible schema.

    The production workflow only creates snapshots. This restore helper exists so
    every snapshot has a tested recovery path instead of being an unreadable dump.
    """
    document = read_logical_snapshot(payload)
    metadata = MetaData()
    metadata.reflect(bind=engine)
    backup_tables = document["tables"]
    target_names = set(metadata.tables)
    missing = set(backup_tables) - target_names
    if missing:
        raise ValueError(f"Target database is missing tables: {sorted(missing)}")

    with engine.begin() as connection:
        if require_empty:
            nonempty = [
                table.name
                for table in metadata.sorted_tables
                if table.name in backup_tables
                and (connection.scalar(select(func.count()).select_from(table)) or 0) > 0
            ]
            if nonempty:
                raise ValueError(f"Refusing to restore into non-empty tables: {nonempty}")

        for table in metadata.sorted_tables:
            rows = backup_tables.get(table.name, [])
            if not rows:
                continue
            decoded = [
                {key: _decode_value(value) for key, value in row.items()}
                for row in rows
            ]
            connection.execute(table.insert(), decoded)

        if engine.dialect.name == "postgresql":
            for table in metadata.sorted_tables:
                for column in table.primary_key.columns:
                    if not getattr(column, "autoincrement", False):
                        continue
                    sequence = connection.scalar(
                        text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                        {"table_name": table.name, "column_name": column.name},
                    )
                    if not sequence:
                        continue
                    maximum = connection.scalar(select(func.max(column)))
                    if maximum is not None:
                        connection.execute(
                            text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
                            {"sequence": sequence, "value": int(maximum)},
                        )
