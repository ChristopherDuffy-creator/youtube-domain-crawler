from __future__ import annotations

import pytest

from app.config import Settings
from app.dataforseo import DataForSEOClient
from app.link_hunter import _href_points_to_domain, _normalize_host, _score_opportunity
from app.models import Domain, Opportunity, SourceLink


def test_normalize_host_strips_scheme_and_www() -> None:
    assert _normalize_host("https://www.Example.COM/path") == "example.com"
    assert _normalize_host("example.com") == "example.com"


def test_href_match_accepts_target_and_subdomain_only() -> None:
    assert _href_points_to_domain("https://old-domain.com/offer", "old-domain.com")
    assert _href_points_to_domain("https://shop.old-domain.com/offer", "old-domain.com")
    assert not _href_points_to_domain("https://old-domain.com.evil.test/", "old-domain.com")
    assert not _href_points_to_domain("mailto:test@old-domain.com", "old-domain.com")


def test_dataforseo_bulk_guards() -> None:
    client = DataForSEOClient(Settings(dataforseo_login="login", dataforseo_password="password"))
    with pytest.raises(ValueError):
        client.bulk_backlink_summaries([])
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
