from datetime import UTC, datetime, timedelta

from app.jobs import _advance_availability_first_review
from app.metrics import ViewMetric
from app.models import Candidate, Domain


def _metric(monthly_views: int) -> ViewMetric:
    return ViewMetric(
        monthly_views=monthly_views,
        verified_30d=False,
        observation_days=10,
        delta_views=monthly_views // 3,
    )


def test_unknown_legacy_result_is_removed_from_the_active_review_clock() -> None:
    now = datetime.now(UTC)
    candidate = Candidate(
        domain_id=1,
        evaluation_started_at=now - timedelta(days=10),
        evaluation_stage="day7",
        start_monthly_views=40_000,
        day3_monthly_views=42_000,
        day7_monthly_views=45_000,
    )
    domain = Domain(name="unknown.example", availability_status="unknown")

    _advance_availability_first_review(
        candidate,
        domain,
        _metric(45_000),
        observed_at=now,
        short_form_only=False,
    )

    assert candidate.evaluation_stage == "awaiting"
    assert candidate.evaluation_started_at is None
    assert candidate.start_monthly_views == 0
    assert candidate.day3_monthly_views == 0
    assert candidate.day7_monthly_views == 0


def test_exact_porkbun_confirmation_starts_then_advances_the_review_clock() -> None:
    now = datetime.now(UTC)
    candidate = Candidate(domain_id=1)
    domain = Domain(
        name="available.example",
        availability_status="available",
        availability_source="porkbun",
        last_checked_at=now,
    )

    _advance_availability_first_review(
        candidate,
        domain,
        _metric(30_000),
        observed_at=now - timedelta(hours=1),
        short_form_only=False,
    )

    assert candidate.evaluation_stage == "day0"
    assert candidate.evaluation_started_at == now
    assert candidate.start_monthly_views == 30_000
    assert candidate.day3_monthly_views == 0

    _advance_availability_first_review(
        candidate,
        domain,
        _metric(36_000),
        observed_at=now + timedelta(days=3),
        short_form_only=False,
    )
    assert candidate.evaluation_stage == "day3"
    assert candidate.day3_monthly_views == 36_000

    _advance_availability_first_review(
        candidate,
        domain,
        _metric(42_000),
        observed_at=now + timedelta(days=7),
        short_form_only=False,
    )
    assert candidate.evaluation_stage == "day7"
    assert candidate.day3_monthly_views == 36_000
    assert candidate.day7_monthly_views == 42_000
    assert candidate.trend_percent == 40.0
