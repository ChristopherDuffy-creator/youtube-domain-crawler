from pathlib import Path


def test_guarded_resume_workflow_is_path_gated_and_changes_only_scheduler_state() -> None:
    text = Path(
        ".github/workflows/resume-crawler-scheduler-guarded.yml"
    ).read_text(encoding="utf-8")

    assert "resume-crawler-scheduler-guarded.yml" in text
    assert "RESUME_GUARDED_CRAWLER_20260825" in text
    assert "variable set SCHEDULER_ENABLED=true" in text
    assert "--project 31fbe878-aa83-4b74-9820-e9f0d13b984c" in text
    assert "--environment production" in text
    assert "--service youtube-domain-crawler" in text
    assert 'p.get("scheduler") is True' in text
    assert 'storage.get("enabled") is True' in text
    assert 'storage.get("blocked") is False' in text
    assert 'p.get("link_hunter_enabled") is True' in text
    assert "LINK_HUNTER_ENABLED=" not in text
    assert "DATABASE_STORAGE_SOFT_LIMIT_GB=" not in text
    assert "/api/link-hunter/proof" not in text
