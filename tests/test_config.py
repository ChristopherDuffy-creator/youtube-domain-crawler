from app.config import DEFAULT_DROPPED_DOMAIN_FEED_URLS, Settings
from app.jobs import build_scheduler


def test_public_daily_dropped_feed_is_connected_by_default() -> None:
    settings = Settings()

    assert settings.dropped_domain_feed_urls == DEFAULT_DROPPED_DOMAIN_FEED_URLS
    assert settings.dropped_domain_feed_urls[0].endswith(
        "/0-latest-free-dropped-domains.csv"
    )


def test_dropped_feed_can_still_be_overridden() -> None:
    settings = Settings(dropped_domain_feed_urls="https://example.com/drops.csv")

    assert settings.dropped_domain_feed_urls == ["https://example.com/drops.csv"]


def test_fresh_feed_runs_after_deploy_and_daily() -> None:
    scheduler = build_scheduler(Settings())
    job_ids = {job.id for job in scheduler.get_jobs()}

    assert "initial_dropped_feeds" in job_ids
    assert "initial_dropped_youtube_search" in job_ids
    assert "dropped_feeds" in job_ids
    assert "dropped_youtube_search" in job_ids
