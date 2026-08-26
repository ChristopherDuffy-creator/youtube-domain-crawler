from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Protocol

SHORT_FORM_MAX_SECONDS = 180


class SnapshotLike(Protocol):
    captured_at: datetime
    view_count: int


@dataclass(frozen=True)
class ViewMetric:
    monthly_views: int
    verified_30d: bool
    observation_days: float
    delta_views: int
    raw_monthly_views: int = 0
    spike_detected: bool = False
    sample_intervals: int = 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_short_form_duration(duration_seconds: int | None) -> bool:
    """Conservatively classify videos whose description links may be non-clickable."""
    return duration_seconds is not None and 0 < duration_seconds <= SHORT_FORM_MAX_SECONDS


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
    raw_projected = int(round(delta * 30 / days))

    # A single counter jump must never become a 30-day acquisition case.  Once
    # there are at least three usable intervals, use the median daily velocity
    # and cap it at the full-window pace.  This quarantines isolated viral
    # spikes and YouTube counter reconciliations without deleting the raw
    # snapshots needed for a later audit.
    daily_rates: list[float] = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        interval_days = (
            _aware(current.captured_at) - _aware(previous.captured_at)
        ).total_seconds() / 86400
        if interval_days < 0.5:
            continue
        interval_growth = max(0, int(current.view_count) - int(previous.view_count))
        daily_rates.append(interval_growth / interval_days)

    projected = raw_projected
    if len(daily_rates) >= 3:
        median_projected = int(round(median(daily_rates) * 30))
        projected = min(raw_projected, median_projected)

    spike_detected = raw_projected > max(projected * 3, projected + 100_000)
    verified = 27 <= days <= 35
    return ViewMetric(
        projected,
        verified,
        round(days, 2),
        delta,
        raw_monthly_views=raw_projected,
        spike_detected=spike_detected,
        sample_intervals=len(daily_rates),
    )
