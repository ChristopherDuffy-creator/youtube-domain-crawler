from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.link_hunter_preview import (
    _blocked_screening_for_names,
    _dropped_domains_for_names,
    _select_provider_summary_targets_with_ranking,
    select_cached_deep_proof_targets_with_ranking,
)
from app.models import BacklinkSummary, Domain


def _parameter_counts(engine):
    counts: list[int] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if " IN " in statement.upper() and isinstance(parameters, dict | tuple | list):
            counts.append(len(parameters))

    return counts


def test_large_priority_name_pool_is_split_before_sql_execution(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    counts = _parameter_counts(engine)
    names = {f"candidate-{index}.example" for index in range(70_000)}

    context = {
        "commoncrawl": {},
        "exact_links": {name: 1 for name in names},
        "independent_sites": {},
        "verified_links": {},
        "observations": {},
        "youtube": {},
        "availability": {},
        "screening": {},
    }
    monkeypatch.setattr(
        "app.link_hunter_preview._free_rank_context",
        lambda _db: context,
    )

    with Session(engine) as db:
        targets, *_ = _select_provider_summary_targets_with_ranking(
            db,
            Settings(link_hunter_summary_batch_size=5),
        )

    assert targets == []
    assert counts
    assert max(counts) <= 10_000


def test_large_blocked_pool_is_split_before_sql_execution():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    counts = _parameter_counts(engine)
    names = {f"blocked-{index}.example" for index in range(70_000)}

    with Session(engine) as db:
        blocked = _blocked_screening_for_names(db, names)

    assert blocked == set()
    assert counts
    assert max(counts) <= 10_000


def test_cached_winner_queue_uses_correlated_exclusions_and_keeps_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "backlink_summaries" in statement:
            statements.append(statement)

    with Session(engine) as db:
        strong = Domain(name="strong.example")
        weaker = Domain(name="weaker.example")
        db.add_all([strong, weaker])
        db.flush()
        db.add_all(
            [
                BacklinkSummary(
                    domain_id=strong.id,
                    provider="dataforseo",
                    referring_pages=200,
                    referring_domains=60,
                    referring_main_domains=60,
                    rank=75,
                ),
                BacklinkSummary(
                    domain_id=weaker.id,
                    provider="dataforseo",
                    referring_pages=20,
                    referring_domains=8,
                    referring_main_domains=8,
                    rank=30,
                ),
            ]
        )
        db.commit()

        names, *_ = select_cached_deep_proof_targets_with_ranking(
            db,
            Settings(),
        )

    assert names == ["strong.example", "weaker.example"]
    assert statements
    assert "EXISTS (" in statements[-1].upper()
    statement = statements[-1].upper()
    assert "DOMAIN_NAME IN (" not in statement
    assert "TARGET IN (" not in statement


def test_large_dropped_name_helper_bounds_each_in_clause():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    counts = _parameter_counts(engine)

    with Session(engine) as db:
        rows = _dropped_domains_for_names(
            db,
            {f"dropped-{index}.example" for index in range(70_000)},
        )

    assert rows == []
    assert counts
    assert max(counts) <= 10_000
