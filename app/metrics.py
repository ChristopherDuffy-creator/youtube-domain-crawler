from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class SnapshotLike(Protocol):
    captured_at: datetime
    view_count: int


@dataclass(frozen=True)
class ViewMetric:
    monthly_views: int
    verified_30d: bool
    observation_days: float
    delta_views: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def calculate_monthly_views(snapshots: Iterable[SnapshotLike]) -> ViewMetric:
    ordered = sorted(snapshots, key=lambda item: _aware(item.captured_at))
    if len(ordered) < 2:
        return ViewMetric(0, False, 0.0, 0)

    latest = ordered[-1]
    latest_at = _aware(latest.captured_at)
    target_seconds = 30 * 86400
    eligible = [
        item for item in ordered[:-1] if (latest_at - _aware(item.captured_at)).total_seconds() > 0
    ]
    if not eligible:
        return ViewMetric(0, False, 0.0, 0)

    # Prefer the snapshot closest to 30 days ago; otherwise use the oldest observation.
    baseline = min(
        eligible,
        key=lambda item: abs(
            (latest_at - _aware(item.captured_at)).total_seconds() - target_seconds
        ),
    )
    days = (latest_at - _aware(baseline.captured_at)).total_seconds() / 86400
    if days < 1:
        return ViewMetric(0, False, round(days, 2), 0)
    delta = max(0, int(latest.view_count) - int(baseline.view_count))
    projected = int(round(delta * 30 / days))
    verified = 27 <= days <= 35
    return ViewMetric(projected, verified, round(days, 2), delta)
