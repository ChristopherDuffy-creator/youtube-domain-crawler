from inspect import signature
from pathlib import Path

from app.main import _youtube_result_status, app, dashboard
from app.models import Candidate, Domain, YouTubeDomainSignal


def _status(
    *,
    stage: str = "day7",
    availability: str = "available",
    day3: int = 80_000,
    day7: int = 90_000,
    buy_score: float = 80.0,
) -> dict[str, str]:
    return _youtube_result_status(
        Candidate(
            evaluation_stage=stage,
            day3_monthly_views=day3,
            day7_monthly_views=day7,
        ),
        Domain(name="example.com", availability_status=availability),
        YouTubeDomainSignal(buy_score=buy_score),
        stage if stage in {"watchlist", "day3", "low"} else "day7",
    )


def test_dashboard_defaults_to_the_youtube_watchlist_and_removes_web_view() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    login = Path("app/templates/login.html").read_text(encoding="utf-8")

    assert signature(dashboard).parameters["tier"].default == "watchlist"
    assert signature(dashboard).parameters["view"].default is None
    assert "YouTube Domain Opportunities" in template
    assert "Web Link Hunter" not in template
    assert "Web CSV" not in template
    assert "YouTube Domain Opportunities" in login
    assert "Web Link Hunter" not in login
    assert 'action="/logout"' in template


def test_dashboard_has_only_the_four_review_tabs() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "['watchlist', 'day3', 'day7', 'low']" in template
    assert 'href="/?tier={{ stage }}"' in template
    assert '"label": "Watchlist"' in source
    assert '"label": "3 Day Results"' in source
    assert '"label": "7+ Day Results"' in source
    assert '"label": "10k–20k"' in source
    for removed in (
        "All YouTube results",
        "New since your last visit",
        "Pending — click",
        "Priority — click",
    ):
        assert removed not in template


def test_dashboard_removes_yellow_notes_and_links_to_bought_database() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    bought_template = Path("app/templates/bought.html").read_text(encoding="utf-8")

    assert 'class="ranking-note"' not in template
    assert 'class="compact-notice"' not in template
    assert "Availability-first pipeline:" not in template
    assert "Final rankings:" not in template
    assert 'href="/bought"' in template
    assert "Bought ({{ bought_domain_count }})" in template
    assert any(route.path == "/bought" for route in app.routes)
    assert "Bought database" in bought_template
    assert "Potential value / month" in bought_template


def test_dashboard_uses_checkpoint_filters_and_quarantines_noisy_rows() -> None:
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert 'Candidate.evaluation_stage == "day0"' in source
    assert 'Candidate.evaluation_stage == "day3"' in source
    assert 'Candidate.evaluation_stage == "day7"' in source
    assert "Candidate.start_monthly_views >= settings.watchlist_monthly_views" in source
    assert "Candidate.day7_monthly_views >= _YOUTUBE_RESERVE_MINIMUM" in source
    assert "Candidate.day7_monthly_views < settings.watchlist_monthly_views" in source
    assert "YouTubeDomainSignal.spike_video_count == 0" in source
    assert "_YOUTUBE_VISIBLE_MAXIMUM = 1_000_000" in source
    assert 'Domain.availability_status == "available"' in source
    assert 'Domain.availability_source == "porkbun"' in source
    assert "Candidate.evaluation_started_at.is_not(None)" in source


def test_technical_counts_are_collapsed_instead_of_prominent_cards() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'class="crawler-health' in template
    assert "<summary>Crawler details</summary>" in template
    assert "YouTube crawler running" in template
    for removed in (
        "Seeded channels",
        "Permanent YouTube evidence records",
        "YouTube searches remaining",
        "Granular video-stat units remaining",
        "Videos checked cumulative",
    ):
        assert removed not in template


def test_final_rankings_exist_only_inside_completed_day7_results() -> None:
    assert _status(stage="watchlist") == {"label": "Waiting for Day 3", "class": "review"}
    assert _status(stage="day3") == {"label": "Day 3 checked", "class": "review"}
    assert _status(stage="low", day7=15_000) == {
        "label": "10k–20k value play",
        "class": "value",
    }
    assert _status(day7=120_000, buy_score=78.0) == {"label": "Priority", "class": "priority"}
    assert _status(day7=75_000, buy_score=70.0) == {"label": "Qualified", "class": "qualified"}
    assert _status(availability="unknown") == {
        "label": "Availability pending",
        "class": "pending",
    }
    assert _status(day3=20_000, day7=90_000) == {
        "label": "Hold — unstable",
        "class": "hold",
    }


def test_youtube_results_keep_score_value_and_only_delete_bought_actions() -> None:
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert "Buy Score" in template
    assert "Potential value / month" in template
    assert template.count('action="/admin/youtube-domain-action"') == 2
    assert 'name="domain_action" value="delete"' in template
    assert 'name="domain_action" value="bought"' in template
    assert ">Delete<" in template
    assert ">Bought<" in template
    assert "This cannot be undone" in template
    assert "Shortlist" not in template
    assert "Ignore" not in template
