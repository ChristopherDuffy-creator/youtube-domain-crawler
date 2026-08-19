from inspect import signature
from pathlib import Path

from app.main import dashboard


def test_dashboard_defaults_to_web_and_exposes_both_system_views() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert signature(dashboard).parameters["view"].default == "web"
    assert 'href="/?view=web"' in template
    assert 'href="/?view=youtube"' in template
    assert "Web-wide results and paid-run status" in template
    assert "Video opportunities and crawler totals" in template
    assert "{% if dashboard_view == 'youtube' %}" in template
    assert 'action="/logout"' in template


def test_dashboard_only_loads_the_selected_systems_large_table() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'if view == "youtube":' in source
    assert "candidate_rows = db.execute(" in source
    assert "web_evidence_rows = _load_web_evidence_rows(db, limit=100, tier=tier)" in source


def test_dashboard_headline_counts_link_to_each_result_tier() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    for view in ("web", "youtube"):
        for tier in ("priority", "qualified", "watchlist", "pending"):
            assert f'href="/?view={view}&amp;tier={tier}"' in template
    assert "Show all results" in template
    assert "result_tier" in template
