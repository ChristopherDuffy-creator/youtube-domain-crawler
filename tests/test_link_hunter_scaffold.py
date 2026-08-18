from __future__ import annotations

import pytest

import app.models  # noqa: F401 - importing registers all model tables
from app.config import Settings
from app.database import Base
from app.dataforseo import DataForSEOClient, DataForSEOError


def test_link_hunter_is_safe_by_default() -> None:
    settings = Settings(dataforseo_login="login", dataforseo_password="password")
    assert settings.dataforseo_enabled is True
    assert settings.link_hunter_enabled is False
    assert settings.link_hunter_proof_batch_size == 5
    assert settings.link_hunter_backlinks_per_domain == 25


def test_dataforseo_client_requires_credentials() -> None:
    settings = Settings(dataforseo_login="", dataforseo_password="")
    with pytest.raises(DataForSEOError):
        DataForSEOClient(settings)


def test_generic_link_hunter_tables_are_registered() -> None:
    expected = {
        "source_sites",
        "source_pages",
        "source_links",
        "source_metric_snapshots",
        "provider_queries",
        "fetch_verifications",
        "opportunities",
    }
    assert expected.issubset(set(Base.metadata.tables))
