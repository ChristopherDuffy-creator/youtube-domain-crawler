from __future__ import annotations

from app.pilot_sites import (
    PILOT_SITES,
    get_pilot_site,
    normalize_host,
    offer_url,
    safe_offer_id,
)


def test_normalize_host_handles_www_and_port() -> None:
    assert normalize_host("WWW.CraftsHeaven.Club:443") == "craftsheaven.club"


def test_known_hosts_resolve_and_unknown_host_does_not() -> None:
    assert get_pilot_site("craftsheaven.club") == PILOT_SITES["craftsheaven.club"]
    assert get_pilot_site("www.satvic.yoga") == PILOT_SITES["satvic.yoga"]
    assert get_pilot_site("youtube-domain-crawler-production.up.railway.app") is None


def test_team_gerardi_has_explicit_independence_disclosure() -> None:
    site = PILOT_SITES["teamgerardiperformance.com"]
    assert "Not affiliated" in site.disclosure
    assert "Gerardi Performance" in site.disclosure


def test_offer_url_requires_https(monkeypatch) -> None:
    site = PILOT_SITES["craftsheaven.club"]
    monkeypatch.setenv(site.offer_env, "http://example.com/offer")
    assert offer_url(site) == ""
    monkeypatch.setenv(site.offer_env, "javascript:alert(1)")
    assert offer_url(site) == ""
    monkeypatch.setenv(site.offer_env, "https://example.com/offer")
    assert offer_url(site) == "https://example.com/offer"


def test_offer_id_is_restricted() -> None:
    assert safe_offer_id("main<script>") == "mainscript"
    assert safe_offer_id("") == "main"
