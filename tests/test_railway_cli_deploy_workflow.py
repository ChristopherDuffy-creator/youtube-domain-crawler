from pathlib import Path


def test_deploy_health_check_allows_link_hunter_safe_off() -> None:
    text = Path(".github/workflows/railway-cli-deploy.yml").read_text(encoding="utf-8")

    assert 'p.get("status") == "ok"' in text
    assert 'p.get("database") == "ok"' in text
    assert 'p.get("link_hunter_enabled") is True' not in text
    assert "Link Hunter paid mode is intentionally optional at deploy time" in text
