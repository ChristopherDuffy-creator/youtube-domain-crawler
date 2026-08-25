from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.commoncrawl import CommonCrawlPresence
from app.commoncrawl_prefilter import _candidate_drops, run_commoncrawl_prefilter
from app.database import Base
from app.models import DroppedDomain, ProviderQuery


class FakeCommonCrawlClient:
    def __init__(self) -> None:
        self.domains: list[str] = []

    def latest_indexes(self, limit: int) -> list[str]:
        assert limit == 2
        return ["CC-MAIN-2026-30", "CC-MAIN-2026-25"]

    def domain_presence(self, domain: str, index_ids: list[str]) -> CommonCrawlPresence:
        self.domains.append(domain)
        hit_map = {
            "a.com": ("CC-MAIN-2026-30", "CC-MAIN-2026-25"),
            "longername.com": (),
            "b.net": ("CC-MAIN-2026-25",),
        }
        return CommonCrawlPresence(
            domain=domain,
            indexes_checked=tuple(index_ids),
            indexes_with_capture=hit_map[domain],
        )


def test_prefilter_skips_completed_targets_and_caches_zero_cost_signals() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)

    with Session(engine) as db:
        db.add_all(
            [
                DroppedDomain(name="junk.xyz", source="test", first_seen_at=now),
                DroppedDomain(
                    name="longername.com", source="test", first_seen_at=now - timedelta(minutes=1)
                ),
                DroppedDomain(name="a.com", source="test", first_seen_at=now - timedelta(minutes=2)),
                DroppedDomain(name="b.net", source="test", first_seen_at=now - timedelta(minutes=3)),
                DroppedDomain(
                    name="alreadycc.com", source="test", first_seen_at=now - timedelta(minutes=4)
                ),
                DroppedDomain(
                    name="alreadypaid.com", source="test", first_seen_at=now - timedelta(minutes=5)
                ),
                ProviderQuery(
                    provider="commoncrawl",
                    endpoint="url_index",
                    target="alreadycc.com",
                    status="complete",
                ),
                ProviderQuery(
                    provider="dataforseo",
                    endpoint="bulk_backlink_summary",
                    target="alreadypaid.com",
                    status="complete",
                ),
            ]
        )
        db.commit()

        fake = FakeCommonCrawlClient()
        counters = run_commoncrawl_prefilter(db, batch_size=3, index_count=2, client=fake)
        cached = db.scalars(
            select(ProviderQuery)
            .where(ProviderQuery.provider == "commoncrawl")
            .order_by(ProviderQuery.id.asc())
        ).all()

    assert fake.domains == ["a.com", "longername.com", "b.net"]
    assert counters == {
        "candidates": 3,
        "checked": 3,
        "with_capture": 2,
        "without_capture": 1,
        "index_requests": 6,
        "provider_cost_usd": 0.0,
        "errors": 0,
        "error_details": [],
    }
    new_rows = [row for row in cached if row.target != "alreadycc.com"]
    assert [(row.target, row.row_count, row.cost_usd, row.status) for row in new_rows] == [
        ("a.com", 2, 0.0, "complete"),
        ("longername.com", 0, 0.0, "complete"),
        ("b.net", 1, 0.0, "complete"),
    ]


def test_candidate_selection_reserves_a_slot_for_the_oldest_unchecked_drop() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

    with Session(engine) as db:
        db.add(DroppedDomain(name="backlog.com", source="test", first_seen_at=now - timedelta(days=7)))
        db.add_all(
            [
                DroppedDomain(
                    name=f"fresh-{index:03}.com",
                    source="test",
                    first_seen_at=now - timedelta(minutes=index),
                )
                for index in range(400)
            ]
        )
        db.commit()

        candidates = _candidate_drops(db, limit=3)

    assert len(candidates) == 3
    assert "backlog.com" in [candidate.name for candidate in candidates]
