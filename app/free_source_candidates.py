"""Bounded, fair candidate selection for free web-evidence sources."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import DroppedDomain, ProviderQuery

type CandidateRank = Callable[[DroppedDomain], tuple[object, ...]]


def completed_target_exists(*, provider: str, endpoint: str) -> ColumnElement[bool]:
    """Correlated completion check that lets PostgreSQL use the query index."""
    return select(ProviderQuery.id).where(
        ProviderQuery.provider == provider,
        ProviderQuery.endpoint == endpoint,
        ProviderQuery.status == "complete",
        ProviderQuery.target == DroppedDomain.name,
    ).exists()


def load_candidate_lanes(
    db: Session,
    *,
    eligibility: Sequence[ColumnElement[bool]],
    limit: int,
) -> tuple[list[DroppedDomain], list[DroppedDomain]]:
    """Load small newest and oldest eligible pools without materialising history."""
    pool_size = max(64, limit * 8)
    statement = select(DroppedDomain).where(*eligibility)
    newest = db.scalars(
        statement.order_by(DroppedDomain.first_seen_at.desc(), DroppedDomain.id.desc()).limit(pool_size)
    ).all()
    oldest = db.scalars(
        statement.order_by(DroppedDomain.first_seen_at.asc(), DroppedDomain.id.asc()).limit(pool_size)
    ).all()
    return newest, oldest


def select_fair_candidates(
    newest: Sequence[DroppedDomain],
    oldest: Sequence[DroppedDomain],
    *,
    limit: int,
    rank_key: CandidateRank,
) -> list[DroppedDomain]:
    """Reserve one third of each batch for the oldest unprocessed candidates."""
    if limit < 1:
        return []

    fresh_slots = max(1, (limit * 2 + 2) // 3)
    backlog_slots = limit - fresh_slots
    selected: list[DroppedDomain] = []
    selected_names: set[str] = set()

    def timestamp(item: DroppedDomain) -> float:
        seen_at = item.first_seen_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        return seen_at.timestamp()

    def take(items: Sequence[DroppedDomain], count: int, *, newest_first: bool) -> None:
        ordered = sorted(
            items,
            key=lambda item: (
                *rank_key(item),
                -timestamp(item) if newest_first else timestamp(item),
                item.name,
            ),
        )
        for item in ordered:
            if len(selected) >= limit or count <= 0:
                break
            if item.name in selected_names:
                continue
            selected.append(item)
            selected_names.add(item.name)
            count -= 1
            if count == 0:
                break

    take(newest, fresh_slots, newest_first=True)
    take(oldest, backlog_slots, newest_first=False)
    take(newest, limit - len(selected), newest_first=True)
    take(oldest, limit - len(selected), newest_first=False)
    return selected
