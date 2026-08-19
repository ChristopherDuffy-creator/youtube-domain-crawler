from __future__ import annotations

import pytest

from app.config import Settings
from app.dataforseo import DataForSEOClient
from app.link_hunter import (
    _href_matches_provider_target,
    _href_points_to_domain,
    _normalize_host,
    _score_opportunity,
    _validate_public_url,
)
from app.models import Domain, Opportunity, SourceLink


def test_normalize_host_strips_scheme_and_www() -> None:
    assert _normalize_host("https://www.Example.COM/path") == "example.com"
    assert _normalize_host("example.com") == "example.com"
    assert _normalize_host("//www.example.com/path") == "example.com"


def test_href_match_accepts_target_and_subdomain_only() -> None:
    assert _href_points_to_domain("https://old-domain.com/offer", "old-domain.com")
    assert _href_points_to_domain("https://shop.old-domain.com/offer", "old-domain.com")
    assert not _href_points_to_domain("https://old-domain.com.evil.test/", "old-domain.com")
    assert not _href_points_to_domain("mailto:test@old-domain.com", "old-domain.com")


def test_provider_target_match_requires_the_reported_path() -> None:
    target = "https://www.old-domain.com/products/offer/"
    assert _href_matches_provider_target(
        "http://old-domain.com/products/offer?utm_source=legacy#buy",
        target,
        "old-domain.com",
    )
    assert not _href_matches_provider_target(
        "https://old-domain.com/products/different",
        target,
        "old-domain.com",
    )
    assert not _href_matches_provider_target(
        "https://shop.old-domain.com/products/offer",
        target,
        "old-domain.com",
    )


def test_provider_root_target_allows_same_domain_paths() -> None:
    assert _href_matches_provider_target(
        "https://old-domain.com/legacy/page",
        "https://old-domain.com/",
        "old-domain.com",
    )
    assert _href_matches_provider_target(
        "https://shop.old-domain.com/legacy/page",
        "old-domain.com",
        "old-domain.com",
    )
    assert not _href_matches_provider_target(
        "https://old-domain.com.evil.test/legacy/page",
        "old-domain.com",
        "old-domain.com",
    )


def test_live_link_fetch_rejects_private_or_unsafe_urls_before_network() -> None:
    with pytest.raises(ValueError, match="non-public"):
        _validate_public_url("http://127.0.0.1/private")
    with pytest.raises(ValueError, match="credentials"):
        _validate_public_url("https://user:pass@example.com/")
    with pytest.raises(ValueError, match="HTTP"):
        _validate_public_url("ftp://example.com/file")


def test_dataforseo_bulk_guards() -> None:
    client = DataForSEOClient(Settings(dataforseo_login="login", dataforseo_password="password"))
    with pytest.raises(ValueError):
        client.bulk_backlink_summaries([])
    with pytest.raises(ValueError):
        client.bulk_backlink_summaries([f"example-{value}.com" for value in range(101)])
    with pytest.raises(ValueError):
        client.bulk_traffic_estimation([])


def test_web_opportunity_cannot_qualify_before_availability_confirmation() -> None:
    domain = Domain(name="example.com", availability_status="unknown", premium=False)
    opportunity = Opportunity(independent_site_count=25, referring_page_count=100, link_strength=80)
    links = [
        SourceLink(
            source_page_id=1,
            domain_id=1,
            target_url="https://example.com/offer",
            dofollow=True,
            provider_rank=80,
            spam_score=0,
            anchor_text="buy software",
            context_before="best deal",
            context_after="signup tool",
        )
    ]

    _score_opportunity(opportunity, domain, links, traffic=10_000, verified=True)

    assert opportunity.score >= 80
    assert opportunity.tier == "watchlist"


def test_web_opportunity_can_be_priority_after_all_gates() -> None:
    domain = Domain(name="example.com", availability_status="available", premium=False)
    opportunity = Opportunity(independent_site_count=25, referring_page_count=100, link_strength=80)
    links = [
        SourceLink(
            source_page_id=1,
            domain_id=1,
            target_url="https://example.com/offer",
            dofollow=True,
            provider_rank=80,
            spam_score=0,
            anchor_text="buy software",
            context_before="best deal",
            context_after="signup tool",
        )
    ]

    _score_opportunity(opportunity, domain, links, traffic=10_000, verified=True)

    assert opportunity.tier == "priority"
    assert opportunity.verified_live_link is True
    assert opportunity.source_page_traffic_estimate == 10_000
