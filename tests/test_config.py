from datetime import timedelta

from app.config import DEFAULT_DROPPED_DOMAIN_FEED_URLS, Settings
from app.jobs import build_scheduler


def test_public_daily_dropped_feed_is_connected_by_default() -> None:
    settings = Settings()

    assert settings.dropped_domain_feed_urls == DEFAULT_DROPPED_DOMAIN_FEED_URLS
    assert settings.dropped_domain_feed_urls[0].endswith("/0-latest-free-dropped-domains.csv")


def test_stale_environment_values_cannot_lower_approved_youtube_bands() -> None:
    settings = Settings(
        watchlist_monthly_views=5_000,
        qualified_monthly_views=20_000,
        priority_monthly_views=75_000,
    )

    assert settings.watchlist_monthly_views == 10_000
    assert settings.qualified_monthly_views == 50_000
    assert settings.priority_monthly_views == 100_000


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


def test_default_discovery_schedule_increases_seed_coverage_within_search_quota() -> None:
    settings = Settings()
    scheduler = build_scheduler(settings)
    discovery = scheduler.get_job("youtube_discovery")

    assert settings.discovery_interval_minutes == 60
    assert settings.search_calls_per_run == 3
    assert discovery is not None
    assert discovery.trigger.interval == timedelta(minutes=60)
    assert (24 * 60 // settings.discovery_interval_minutes) * settings.search_calls_per_run == 72
    assert settings.youtube_search_daily_limit > 72 + 10
