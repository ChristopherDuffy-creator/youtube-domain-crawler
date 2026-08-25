from pathlib import Path


def test_emergency_pause_workflow_is_path_gated_and_changes_only_scheduler_state() -> None:
    text = Path(
        ".github/workflows/pause-crawler-scheduler-emergency.yml"
    ).read_text(encoding="utf-8")

    assert "pause-crawler-scheduler-emergency.yml" in text
    assert "EMERGENCY_PAUSE_SCHEDULER_20260825" in text
    assert "variable set SCHEDULER_ENABLED=false" in text
    assert "--project 31fbe878-aa83-4b74-9820-e9f0d13b984c" in text
    assert "--environment production" in text
    assert "--service youtube-domain-crawler" in text
    assert 'p.get("scheduler") is False' in text
    assert 'p.get("link_hunter_enabled") is True' in text
    assert "LINK_HUNTER_ENABLED=" not in text
