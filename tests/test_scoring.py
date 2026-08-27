from datetime import UTC, datetime

from app.config import Settings
from app.scoring import ScoreInputs, calculate_score, determine_tier


def settings() -> Settings:
    return Settings(
        watchlist_monthly_views=10_000,
        qualified_monthly_views=50_000,
        priority_monthly_views=100_000,
    )


def test_tiers_use_10k_50k_100k_and_require_an_evaluation_start() -> None:
    config = settings()
    assert determine_tier(10_000, True, "available", config) == "watchlist"
    assert determine_tier(50_000, True, "available", config) == "qualified"
    assert determine_tier(100_000, True, "available", config) == "priority"
    assert determine_tier(100_000, False, "available", config) == "pending"
    assert determine_tier(100_000, True, "likely_available", config) == "watchlist"
    assert determine_tier(9_999, True, "available", config) == "pending"
    assert determine_tier(1_000_000, True, "registered", config) == "rejected"


def test_score_rewards_traffic_cta_prominence_and_repetition() -> None:
    strong = calculate_score(
        ScoreInputs(
            monthly_views=250_000,
            lifetime_views=20_000_000,
            link_position=0.05,
            has_cta=True,
            clickable=True,
            video_count=3,
            link_count=5,
            published_at=datetime(2018, 1, 1, tzinfo=UTC),
            availability_status="available",
        )
    )
    weak = calculate_score(
        ScoreInputs(
            monthly_views=5_000,
            lifetime_views=50_000,
            link_position=0.9,
            has_cta=False,
            clickable=False,
            video_count=1,
            link_count=1,
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
            availability_status="likely_available",
        )
    )
    assert strong > weak
    assert 0 <= weak <= 100
    assert 0 <= strong <= 100
