from pathlib import Path


def test_dashboard_removes_retired_web_proof_controls() -> None:
    text = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    for retired in (
        "Latest controlled proof:",
        "provider_cost_usd",
        "summary_screened",
        "deep_proof_target_count",
        "source_links_verified",
        "approved winner controller",
        "$2.16/UTC day",
    ):
        assert retired not in text
    assert "The active system is YouTube-only" in text
    assert "Web crawling is disabled" in text
