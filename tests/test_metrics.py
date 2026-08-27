from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.metrics import calculate_monthly_views


@dataclass
class Snapshot:
    captured_at: datetime
    view_count: int


def test_verifies_thirty_day_delta() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    metric = calculate_monthly_views(
        [
            Snapshot(now - timedelta(days=30), 100_000),
            Snapshot(now, 125_000),
        ]
    )
    assert metric.monthly_views == 25_000
    assert metric.verified_30d is True
    assert metric.observation_days == 30


def test_projects_short_window_without_calling_it_verified() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    metric = calculate_monthly_views(
        [
            Snapshot(now - timedelta(days=7), 10_000),
            Snapshot(now, 10_700),
        ]
    )
    assert metric.monthly_views == 3_000
    assert metric.verified_30d is False
    assert metric.observation_days == 7


def test_never_reports_negative_view_growth() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    metric = calculate_monthly_views(
        [
            Snapshot(now - timedelta(days=30), 20_000),
            Snapshot(now, 19_000),
        ]
    )
    assert metric.monthly_views == 0
    assert metric.delta_views == 0


def test_quarantines_a_single_counter_spike_without_deleting_raw_growth() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    snapshots = [Snapshot(now - timedelta(days=5 - index), 10_000 + index * 100) for index in range(5)]
    snapshots.append(Snapshot(now, 110_400))

    metric = calculate_monthly_views(snapshots)

    assert metric.raw_monthly_views > 500_000
    assert metric.monthly_views == 3_000
    assert metric.spike_detected is True
    assert metric.sample_intervals == 5


def test_records_start_day3_and_day7_monthly_run_rates() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    daily_counts = [100_000, 101_000, 102_000, 103_000, 105_000, 107_000, 109_000, 111_000]

    metric = calculate_monthly_views(
        [Snapshot(now - timedelta(days=7 - index), count) for index, count in enumerate(daily_counts)]
    )

    assert metric.evaluation_stage == "day7"
    assert metric.start_monthly_views == 30_000
    assert metric.day3_monthly_views == 30_000
    assert metric.day7_monthly_views == 47_143
    assert metric.trend_percent == 57.1


def test_two_interval_counter_jump_uses_the_lower_repeatable_pace() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    metric = calculate_monthly_views(
        [
            Snapshot(now - timedelta(days=2), 10_000),
            Snapshot(now - timedelta(days=1), 10_100),
            Snapshot(now, 110_100),
        ]
    )

    assert metric.raw_monthly_views > 1_000_000
    assert metric.monthly_views == 3_000
    assert metric.spike_detected is True


def test_sparse_history_does_not_fake_three_checkpoint_rechecks() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    metric = calculate_monthly_views(
        [
            Snapshot(now - timedelta(days=15), 100_000),
            Snapshot(now, 160_000),
        ]
    )

    assert metric.evaluation_stage == "day0"
    assert metric.start_monthly_views == 120_000
    assert metric.day3_monthly_views == 0
    assert metric.day7_monthly_views == 0
