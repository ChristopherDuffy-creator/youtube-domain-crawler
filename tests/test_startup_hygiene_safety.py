from pathlib import Path


def test_expensive_legacy_hygiene_is_not_run_during_app_startup() -> None:
    text = Path("app/main.py").read_text(encoding="utf-8")

    assert "purge_legacy_bare_youtube_links" not in text


def test_hygiene_cleanup_scopes_candidate_and_refresh_state_mutations() -> None:
    text = Path("app/data_hygiene.py").read_text(encoding="utf-8")

    assert "Candidate.domain_id.in_(candidate_scope)" in text
    assert "YouTubeDomainSignal.domain_id.in_(candidate_scope)" in text
    assert "VideoRefreshState.video_id.in_(chunk)" in text
    assert "delete(VideoRefreshState).where(~active_link_for_refresh)" not in text
