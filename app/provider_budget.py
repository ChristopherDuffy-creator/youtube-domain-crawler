from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ProviderDailyBudget, utcnow

MICRO_USD = Decimal("1000000")
PROVIDER = "dataforseo"


class ProviderBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderBudgetReservation:
    provider: str
    spend_date: date
    reserved_microusd: int


def _to_microusd(value: float) -> int:
    if value < 0:
        raise ValueError("USD value cannot be negative")
    return int((Decimal(str(value)) * MICRO_USD).to_integral_value(rounding=ROUND_CEILING))


def _budget_row_for_update(
    db: Session,
    provider: str,
    spend_date: date,
) -> ProviderDailyBudget:
    for _ in range(3):
        row = db.scalar(
            select(ProviderDailyBudget)
            .where(
                ProviderDailyBudget.provider == provider,
                ProviderDailyBudget.spend_date == spend_date,
            )
            .with_for_update()
        )
        if row is not None:
            return row
        db.add(ProviderDailyBudget(provider=provider, spend_date=spend_date))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    row = db.scalar(
        select(ProviderDailyBudget)
        .where(
            ProviderDailyBudget.provider == provider,
            ProviderDailyBudget.spend_date == spend_date,
        )
        .with_for_update()
    )
    if row is None:
        raise ProviderBudgetError("Could not initialize the provider daily budget ledger")
    return row


def provider_daily_budget_snapshot(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, float | int | str]:
    spend_date = (now or utcnow()).date()
    row = db.get(ProviderDailyBudget, (PROVIDER, spend_date))
    spent = int(row.spent_microusd if row is not None else 0)
    reserved = int(row.reserved_microusd if row is not None else 0)
    limit = _to_microusd(settings.link_hunter_daily_max_cost_usd)
    return {
        "date_utc": spend_date.isoformat(),
        "limit_usd": round(limit / 1_000_000, 6),
        "spent_usd": round(spent / 1_000_000, 6),
        "reserved_usd": round(reserved / 1_000_000, 6),
        "remaining_usd": round(max(0, limit - spent - reserved) / 1_000_000, 6),
        "completed_runs": int(row.completed_runs if row is not None else 0),
    }


def reserve_provider_daily_budget(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> ProviderBudgetReservation | None:
    run_limit = _to_microusd(settings.link_hunter_proof_max_cost_usd)
    daily_limit = _to_microusd(settings.link_hunter_daily_max_cost_usd)
    if run_limit > daily_limit:
        raise ProviderBudgetError("The per-run provider cap exceeds the daily provider cap")

    spend_date = (now or utcnow()).date()
    row = _budget_row_for_update(db, PROVIDER, spend_date)
    if row.spent_microusd + row.reserved_microusd + run_limit > daily_limit:
        db.rollback()
        return None
    row.reserved_microusd += run_limit
    row.updated_at = utcnow()
    db.commit()
    return ProviderBudgetReservation(PROVIDER, spend_date, run_limit)


def finalize_provider_daily_budget(
    db: Session,
    reservation: ProviderBudgetReservation,
    actual_cost_usd: float,
    *,
    release_unused: bool,
) -> dict[str, float | int | str]:
    actual = _to_microusd(actual_cost_usd)
    row = _budget_row_for_update(db, reservation.provider, reservation.spend_date)
    held = min(row.reserved_microusd, reservation.reserved_microusd)
    row.spent_microusd += actual
    if release_unused:
        row.reserved_microusd -= held
        row.completed_runs += 1
    else:
        # Preserve the unused part of the reservation after a partial/error run.
        # Together with recorded spend this keeps the full per-run envelope held.
        row.reserved_microusd -= min(held, actual)
    row.updated_at = utcnow()
    db.commit()
    return {
        "date_utc": reservation.spend_date.isoformat(),
        "spent_usd": round(row.spent_microusd / 1_000_000, 6),
        "reserved_usd": round(row.reserved_microusd / 1_000_000, 6),
        "completed_runs": row.completed_runs,
    }
