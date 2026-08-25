from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppCheckpoint, ProviderDailyBudget, utcnow

MICRO_USD = Decimal("1000000")
PROVIDER = "dataforseo"
PROOF_LEASE_KEY = "link_hunter_proof_lease"
PROOF_LEASE_SECONDS = 15 * 60
# The deployment configuration may only tighten these limits.  These are the
# user-approved ceilings and remain effective even if a Railway variable is
# accidentally raised later.
HARD_RUN_CAP_USD = Decimal("0.18")
HARD_DAILY_CAP_USD = Decimal("2.16")


class ProviderBudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderBudgetReservation:
    provider: str
    spend_date: date
    reserved_microusd: int


@dataclass(frozen=True)
class ProviderProofLease:
    """A durable single-flight lease for the paid proof job.

    The daily budget prevents overspend but does not, by itself, stop two
    schedulers from spending two run envelopes at the same time.  Keeping this
    lease in the existing checkpoint table makes the guard shared by every app
    process and recoverable after a crash.
    """

    token: str
    expires_at: datetime


def _to_microusd(value: float) -> int:
    if value < 0:
        raise ValueError("USD value cannot be negative")
    return int((Decimal(str(value)) * MICRO_USD).to_integral_value(rounding=ROUND_CEILING))


def _effective_run_limit_microusd(settings: Settings) -> int:
    configured = _to_microusd(settings.link_hunter_proof_max_cost_usd)
    return min(configured, int(HARD_RUN_CAP_USD * MICRO_USD))


def _effective_daily_limit_microusd(settings: Settings) -> int:
    configured = _to_microusd(settings.link_hunter_daily_max_cost_usd)
    return min(configured, int(HARD_DAILY_CAP_USD * MICRO_USD))


def effective_provider_run_limit_usd(settings: Settings) -> float:
    """Return the configured cap tightened by the immutable approved ceiling."""
    return _effective_run_limit_microusd(settings) / 1_000_000


def effective_provider_daily_limit_usd(settings: Settings) -> float:
    """Return the configured daily cap tightened by the immutable ceiling."""
    return _effective_daily_limit_microusd(settings) / 1_000_000


def _coerce_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def acquire_provider_proof_lease(
    db: Session,
    *,
    now: datetime | None = None,
    lease_seconds: int = PROOF_LEASE_SECONDS,
) -> ProviderProofLease | None:
    """Acquire the one active paid-proof lease, or return ``None`` if busy."""
    if lease_seconds <= 0:
        raise ValueError("Proof lease duration must be positive")

    current = (now or utcnow()).astimezone(UTC)
    expires_at = current + timedelta(seconds=lease_seconds)
    for _ in range(3):
        checkpoint = db.scalar(
            select(AppCheckpoint)
            .where(AppCheckpoint.key == PROOF_LEASE_KEY)
            .with_for_update()
        )
        if checkpoint is None:
            checkpoint = AppCheckpoint(key=PROOF_LEASE_KEY, value={})
            db.add(checkpoint)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                continue

        previous_expiry = _coerce_utc((checkpoint.value or {}).get("expires_at"))
        if previous_expiry is not None and previous_expiry > current:
            db.rollback()
            return None

        token = uuid4().hex
        checkpoint.value = {
            "token": token,
            "acquired_at": current.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        checkpoint.updated_at = current
        db.commit()
        return ProviderProofLease(token=token, expires_at=expires_at)
    raise ProviderBudgetError("Could not initialize the provider proof lease")


def release_provider_proof_lease(db: Session, lease: ProviderProofLease) -> None:
    """Release only the caller's lease; never clear a newer owner's lease."""
    checkpoint = db.scalar(
        select(AppCheckpoint)
        .where(AppCheckpoint.key == PROOF_LEASE_KEY)
        .with_for_update()
    )
    if checkpoint is None or (checkpoint.value or {}).get("token") != lease.token:
        db.rollback()
        return
    checkpoint.value = {}
    checkpoint.updated_at = utcnow()
    db.commit()


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
    limit = _effective_daily_limit_microusd(settings)
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
    run_limit = _effective_run_limit_microusd(settings)
    daily_limit = _effective_daily_limit_microusd(settings)
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
