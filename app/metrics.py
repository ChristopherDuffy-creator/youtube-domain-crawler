from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median_low
from typing import Protocol

SHORT_FORM_MAX_SECONDS = 180
START_CHECKPOINT_DAYS = 1
DAY3_CHECKPOINT_DAYS = 3
DAY7_CHECKPOINT_DAYS = 7


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
    start_monthly_views: int = 0
    day3_monthly_views: int = 0
    day7_monthly_views: int = 0
    evaluation_stage: str = "collecting"
    trend_percent: float = 0.0


@dataclass(frozen=True)
class _Projection:
    monthly_views: int
    raw_monthly_views: int
    observation_days: float
    delta_views: int
    spike_detected: bool
    sample_intervals: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_short_form_duration(duration_seconds: int | None) -> bool:
    """Conservatively classify videos whose description links may be non-clickable."""
    return duration_seconds is not None and 0 < duration_seconds <= SHORT_FORM_MAX_SECONDS


def _projection_between(
    ordered: list[SnapshotLike],
    baseline_index: int,
    endpoint_index: int,
) -> _Projection:
    baseline = ordered[baseline_index]
    latest = ordered[endpoint_index]
    days = (_aware(latest.captured_at) - _aware(baseline.captured_at)).total_seconds() / 86400
    if days < 1:
        return _Projection(0, 0, round(max(0.0, days), 2), 0, False, 0)

    delta = max(0, int(latest.view_count) - int(baseline.view_count))
    raw_projected = int(round(delta * 30 / days))
    daily_rates: list[float] = []
    window = ordered[baseline_index : endpoint_index + 1]
    for previous, current in zip(window, window[1:], strict=False):
        interval_days = (_aware(current.captured_at) - _aware(previous.captured_at)).total_seconds() / 86400
        if interval_days < 0.5:
            continue
        interval_growth = max(0, int(current.view_count) - int(previous.view_count))
        daily_rates.append(interval_growth / interval_days)

    # Two or more independent intervals are enough to reject a one-off counter
    # jump. median_low deliberately chooses the lower rate when there are only
    # two samples; genuine acceleration can still prove itself at Day 7.
    projected = raw_projected
    if len(daily_rates) >= 2:
        robust_projected = int(round(median_low(daily_rates) * 30))
        projected = min(raw_projected, robust_projected)

    spike_detected = raw_projected > max(projected * 3, projected + 100_000)
    return _Projection(
        monthly_views=projected,
        raw_monthly_views=raw_projected,
        observation_days=round(days, 2),
        delta_views=delta,
        spike_detected=spike_detected,
        sample_intervals=len(daily_rates),
    )


def _checkpoint_projection(
    ordered: list[SnapshotLike],
    checkpoint_days: int,
    *,
    after_index: int,
) -> tuple[_Projection, int] | None:
    baseline_at = _aware(ordered[0].captured_at)
    for endpoint_index, snapshot in enumerate(ordered[after_index + 1 :], start=after_index + 1):
        elapsed = (_aware(snapshot.captured_at) - baseline_at).total_seconds() / 86400
        if elapsed >= checkpoint_days:
            return _projection_between(ordered, 0, endpoint_index), endpoint_index
    return None


def calculate_monthly_views(snapshots: Iterable[SnapshotLike]) -> ViewMetric:
    ordered = sorted(snapshots, key=lambda item: _aware(item.captured_at))
    if len(ordered) < 2:
        return ViewMetric(0, False, 0.0, 0)

    latest = ordered[-1]
    latest_at = _aware(latest.captured_at)
    target_seconds = 30 * 86400
    eligible = [item for item in ordered[:-1] if (latest_at - _aware(item.captured_at)).total_seconds() > 0]
    if not eligible:
        return ViewMetric(0, False, 0.0, 0)

    # Prefer the snapshot closest to 30 days ago; otherwise use the oldest observation.
    baseline = min(
        eligible,
        key=lambda item: abs((latest_at - _aware(item.captured_at)).total_seconds() - target_seconds),
    )
    baseline_index = ordered.index(baseline)
    current = _projection_between(ordered, baseline_index, len(ordered) - 1)
    if current.observation_days < 1:
        return ViewMetric(0, False, current.observation_days, 0)

    start_checkpoint = _checkpoint_projection(ordered, START_CHECKPOINT_DAYS, after_index=0)
    start, start_index = start_checkpoint if start_checkpoint is not None else (None, 0)
    day3_checkpoint = (
        _checkpoint_projection(ordered, DAY3_CHECKPOINT_DAYS, after_index=start_index)
        if start is not None
        else None
    )
    day3, day3_index = day3_checkpoint if day3_checkpoint is not None else (None, start_index)
    day7_checkpoint = (
        _checkpoint_projection(ordered, DAY7_CHECKPOINT_DAYS, after_index=day3_index)
        if day3 is not None
        else None
    )
    day7 = day7_checkpoint[0] if day7_checkpoint is not None else None
    if day7 is not None:
        evaluation_stage = "day7"
        comparison = day7.monthly_views
    elif day3 is not None:
        evaluation_stage = "day3"
        comparison = day3.monthly_views
    elif start is not None:
        evaluation_stage = "day0"
        comparison = start.monthly_views
    else:
        evaluation_stage = "collecting"
        comparison = 0

    start_views = start.monthly_views if start is not None else 0
    trend_percent = round((comparison - start_views) / start_views * 100, 1) if start_views > 0 else 0.0
    spike_detected = current.spike_detected or any(
        checkpoint is not None and checkpoint.spike_detected for checkpoint in (start, day3, day7)
    )
    verified = 27 <= current.observation_days <= 35
    return ViewMetric(
        current.monthly_views,
        verified,
        current.observation_days,
        current.delta_views,
        raw_monthly_views=current.raw_monthly_views,
        spike_detected=spike_detected,
        sample_intervals=current.sample_intervals,
        start_monthly_views=start_views,
        day3_monthly_views=day3.monthly_views if day3 is not None else 0,
        day7_monthly_views=day7.monthly_views if day7 is not None else 0,
        evaluation_stage=evaluation_stage,
        trend_percent=trend_percent,
    )
