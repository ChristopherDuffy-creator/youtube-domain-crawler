from pathlib import Path


def test_activation_workflow_is_path_gated_and_changes_only_the_feature_flag() -> None:
    text = Path(".github/workflows/activate-link-hunter-approved.yml").read_text(encoding="utf-8")

    assert "activate-link-hunter-approved.yml" in text
    assert "APPROVE_LINK_HUNTER_ACTIVATION_20260825" in text
    assert "variable set LINK_HUNTER_ENABLED=true" in text
    assert "--project 31fbe878-aa83-4b74-9820-e9f0d13b984c" in text
    assert "--environment production" in text
    assert "--service youtube-domain-crawler" in text
    assert 'p.get("link_hunter_enabled") is True' in text
    assert "/api/link-hunter/proof" not in text
    assert "APPROVE_MAX_0.18_USD" not in text
