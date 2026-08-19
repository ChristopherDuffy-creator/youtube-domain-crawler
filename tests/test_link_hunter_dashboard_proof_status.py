from pathlib import Path


def test_dashboard_reports_latest_controlled_proof_and_safe_paid_state() -> None:
    text = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert "Latest controlled proof:" in text
    assert "provider_cost_usd" in text
    assert "domains_with_live_backlinks" in text
    assert "source_links_verified" in text
    assert "automatic spending disabled" in text
    assert "routine paid Link Hunter runs remain off" in text
    assert "until the first controlled proof is deliberately enabled" not in text
