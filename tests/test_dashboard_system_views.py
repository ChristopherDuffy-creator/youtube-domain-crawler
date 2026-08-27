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
    assert "web_evidence_rows = _load_web_evidence_rows(" in source
    assert "new_since=new_since" in source


def test_dashboard_headline_counts_link_to_each_result_tier() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    for view in ("web", "youtube"):
        for tier in ("new", "priority", "qualified", "watchlist", "pending"):
            assert f'href="/?view={view}&amp;tier={tier}"' in template
    assert "Show all results" in template
    assert "result_tier" in template


def test_dashboard_exposes_status_strip_and_action_workflow() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'class="status-strip"' in template
    assert "Spent today" in template
    assert "Winner queue cadence" in template
    assert ">15 min<" in template
    assert "Latest Web success" in template
    assert "Latest YouTube success" in template
    assert "Prague time" in template
    assert 'class="system-details"' in template
    assert template.index('<div class="table-wrap">', template.index("WEB-WIDE")) < template.index(
        'class="system-details"'
    )
    assert 'action="/admin/dashboard-decision"' in template
    assert "Shortlist" in template
    assert "Bought" in template
    assert "Ignore" in template
    assert "Free-screened drops" in template
    assert "Permanent backlink summaries" in template
    assert "Acquisition money cases" in template
    assert "predicted outbound clicks" in template
    assert "The system never buys automatically" in template


def test_youtube_rankings_offer_only_permanent_delete_and_bought_actions() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    youtube_start = template.index('<p class="eyebrow">YOUTUBE</p>')
    web_start = template.index('<p class="eyebrow">WEB-WIDE</p>')
    youtube = template[youtube_start:web_start]
    web = template[web_start:]

    assert youtube.count('action="/admin/youtube-domain-action"') == 2
    assert 'name="domain_action" value="delete"' in youtube
    assert 'name="domain_action" value="bought"' in youtube
    assert ">Delete<" in youtube
    assert ">Bought<" in youtube
    assert "This cannot be undone" in youtube
    assert "Shortlist" not in youtube
    assert "Ignore" not in youtube
    assert 'action="/admin/dashboard-decision"' in web
