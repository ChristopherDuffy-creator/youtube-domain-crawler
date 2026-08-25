from pathlib import Path


def test_dashboard_reports_latest_controlled_proof_and_safe_paid_state() -> None:
    text = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert "Latest controlled proof:" in text
    assert "provider_cost_usd" in text
    assert "summary_screened" in text
    assert "deep_proof_target_count" in text
    assert "source_links_verified" in text
    assert "safe/off between guarded batches" in text
    assert "approved winner controller checks every 15 minutes" in text
    assert "$2.16/UTC day" in text
    assert "until the first controlled proof is deliberately enabled" not in text
