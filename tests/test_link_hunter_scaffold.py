from __future__ import annotations

import pytest

from app.config import Settings
from app.database import Base
from app.dataforseo import DataForSEOClient, DataForSEOError


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
    "app_checkpoints": {"key", "value", "updated_at"},
}

WEB_LINK_HUNTER_TABLES = {
    "dashboard_decisions",
    "source_sites",
    "source_pages",
    "source_links",
    "source_metric_snapshots",
    "provider_queries",
    "provider_daily_budgets",
    "fetch_verifications",
    "opportunities",
    "web_screenings",
    "backlink_summaries",
    "link_observations",
    "opportunity_economics",
}


def _ensure_models_registered() -> None:
    from app import models

    assert models.SourceSite.__tablename__ == "source_sites"


def _columns(table_name: str) -> set[str]:
    _ensure_models_registered()
    return set(Base.metadata.tables[table_name].columns.keys())


def test_link_hunter_is_safe_by_default() -> None:
    settings = Settings(dataforseo_login="login", dataforseo_password="password")
    assert settings.dataforseo_enabled is True
    assert settings.link_hunter_enabled is False
    assert settings.link_hunter_summary_batch_size == 100
    assert settings.link_hunter_proof_batch_size == 5
    assert settings.link_hunter_backlinks_per_domain == 25
    assert settings.link_hunter_proof_max_cost_usd == 0.18
    assert settings.link_hunter_daily_max_cost_usd == 2.16
    assert settings.link_hunter_free_screen_batch_size == 50_000
    assert settings.link_hunter_verification_cache_hours == 24
    assert settings.link_hunter_link_refresh_batch_size == 100
    assert settings.link_hunter_link_refresh_workers == 8


def test_dataforseo_client_requires_credentials() -> None:
    settings = Settings(dataforseo_login="", dataforseo_password="")
    with pytest.raises(DataForSEOError):
        DataForSEOClient(settings)


def test_generic_link_hunter_tables_are_registered() -> None:
    _ensure_models_registered()
    assert WEB_LINK_HUNTER_TABLES.issubset(set(Base.metadata.tables))


def test_link_hunter_does_not_mutate_production_core_schema() -> None:
    for table_name, expected_columns in PRODUCTION_CORE_COLUMNS.items():
        assert _columns(table_name) == expected_columns
