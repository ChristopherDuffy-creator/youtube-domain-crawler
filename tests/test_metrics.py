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
    snapshots = [
        Snapshot(now - timedelta(days=5 - index), 10_000 + index * 100)
        for index in range(5)
    ]
    snapshots.append(Snapshot(now, 110_400))

    metric = calculate_monthly_views(snapshots)

    assert metric.raw_monthly_views > 500_000
    assert metric.monthly_views == 3_000
    assert metric.spike_detected is True
    assert metric.sample_intervals == 5
