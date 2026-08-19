from __future__ import annotations

from app.database import Base
from app.models import Domain


PRODUCTION_CORE_COLUMNS = {
    "domains": {
        "id",
        "name",
        "suffix",
        "excluded_reason",
        "availability_status",
        "availability_source",
        "rdap_status",
        "dns_status",
        "http_status",
        "registrar_price_usd",
        "premium",
        "first_seen_at",
        "last_checked_at",
        "check_error",
    },
    "dropped_domains": {
        "id",
        "name",
        "source",
        "first_seen_at",
        "youtube_searched_at",
        "matched_existing_index",
    },
    "run_logs": {
        "id",
        "job",
        "started_at",
        "finished_at",
        "status",
        "counters",
        "error",
    },
    "app_checkpoints": {
        "key",
        "value",
        "updated_at",
    },
}

WEB_LINK_HUNTER_TABLES = {
    "source_sites",
    "source_pages",
    "source_links",
    "source_metric_snapshots",
    "provider_queries",
    "fetch_verifications",
    "opportunities",
}


def _columns(table_name: str) -> set[str]:
    return set(Base.metadata.tables[table_name].columns.keys())


def test_link_hunter_does_not_mutate_production_core_schema() -> None:
    assert Domain.__tablename__ == "domains"
    for table_name, expected_columns in PRODUCTION_CORE_COLUMNS.items():
        assert _columns(table_name) == expected_columns


def test_link_hunter_schema_is_additive_and_separate() -> None:
    table_names = set(Base.metadata.tables)
    assert table_names >= WEB_LINK_HUNTER_TABLES
    assert WEB_LINK_HUNTER_TABLES.isdisjoint(PRODUCTION_CORE_COLUMNS)
