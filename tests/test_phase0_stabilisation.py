from __future__ import annotations

import importlib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import data_hygiene, jobs, link_hunter, link_hunter_preview, youtube_intelligence
from app.config import Settings
from app.database import Base
from app.models import Domain


def test_production_bootstrap_does_not_patch_canonical_crawler_functions(monkeypatch) -> None:
    batch_environment = (
        "YOUTUBE_CHANNEL_PAGES_PER_RUN",
        "YOUTUBE_CHANNEL_PAGE_BURST",
        "YOUTUBE_VIEW_REFRESH_BATCH_SIZE",
        "YOUTUBE_INTELLIGENCE_BACKFILL_BATCH_SIZE",
        "YOUTUBE_LOCAL_MATCH_BATCH_SIZE",
        "AVAILABILITY_BATCH_SIZE",
        "LINK_HUNTER_SUMMARY_BATCH_SIZE",
        "LINK_HUNTER_LINK_REFRESH_BATCH_SIZE",
        "LINK_HUNTER_LINK_REFRESH_WORKERS",
        "LINK_HUNTER_FREE_SCREEN_BATCH_SIZE",
    )
    for name in batch_environment:
        monkeypatch.delenv(name, raising=False)

    score_function = link_hunter._score_opportunity
    candidate_refresh = jobs.refresh_candidates
    signal_refresh = youtube_intelligence.refresh_youtube_domain_signals

    importlib.import_module("app.boot")

    assert link_hunter._score_opportunity is score_function
    assert jobs.refresh_candidates is candidate_refresh
    assert youtube_intelligence.refresh_youtube_domain_signals is signal_refresh
    assert (
        link_hunter.rerank_summary_screen_targets
        is link_hunter_preview.rerank_summary_screen_targets
    )


def test_production_bootstrap_preserves_the_full_cheap_screen_and_bounded_refresh_pool() -> None:
    boot = importlib.import_module("app.boot")

    assert boot._BATCH_CAPS["LINK_HUNTER_SUMMARY_BATCH_SIZE"] == 100
    assert boot._BATCH_CAPS["LINK_HUNTER_LINK_REFRESH_WORKERS"] == 8


def test_summary_only_evidence_cannot_enter_a_ranked_web_tier() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        domain = Domain(name="summary-only.example", availability_status="available")
        db.add(domain)
        db.flush()

        opportunity = link_hunter._save_summary_opportunity(
            db,
            domain,
            {"referring_pages": 5_000, "referring_domains": 900, "rank": 99},
            combined_score=99,
        )

        assert opportunity is not None
        assert opportunity.score == 39.9
        assert opportunity.tier == "pending"


def test_scheduler_isolates_work_lanes_and_never_runs_paid_proof_in_process() -> None:
    scheduler = jobs.build_scheduler(Settings())
    scheduled = {job.id: job for job in scheduler.get_jobs()}

    assert "link_hunter_proof" not in scheduled
    assert scheduled["youtube_discovery"].executor == "youtube"
    assert scheduled["youtube_channel_fanout"].executor == "youtube"
    assert scheduled["view_snapshots"].executor == "youtube"
    assert scheduled["availability_checks"].executor == "availability"
    assert scheduled["commoncrawl_prefilter"].executor == "sources"
    assert scheduled["stackexchange_prefilter"].executor == "sources"
    assert scheduled["hackernews_prefilter"].executor == "sources"
    assert scheduled["web_free_screening"].executor == "web"
    assert scheduled["web_link_refresh"].executor == "web"
    assert scheduled["youtube_intelligence"].executor == "maintenance"
    assert scheduled["daily_digest"].executor == "email"
    assert scheduled["daily_digest_catchup"].executor == "email"


def test_candidate_refresh_chunks_large_scopes(monkeypatch) -> None:
    chunks: list[set[int]] = []

    def fake_refresh(_db, domain_ids: set[int]) -> int:
        chunks.append(domain_ids)
        return len(domain_ids)

    monkeypatch.setattr(jobs, "_refresh_candidate_chunk", fake_refresh)
    monkeypatch.setattr(jobs, "_release_orm_memory", lambda _db: None)

    assert jobs.refresh_candidates(object(), set(range(62))) == 62
    assert [len(chunk) for chunk in chunks] == [25, 25, 12]


def test_youtube_signal_refresh_chunks_and_revalidates_each_scope(monkeypatch) -> None:
    chunks: list[set[int]] = []
    consistency_scopes: list[set[int] | None] = []

    def fake_refresh(_db, _settings, domain_ids, *, limit=None) -> int:
        assert limit is None
        chunks.append(domain_ids)
        return len(domain_ids)

    monkeypatch.setattr(
        youtube_intelligence,
        "_refresh_youtube_domain_signal_chunk",
        fake_refresh,
    )
    monkeypatch.setattr(
        youtube_intelligence,
        "_release_signal_orm_memory",
        lambda _db: None,
    )
    monkeypatch.setattr(
        data_hygiene,
        "enforce_candidate_signal_consistency",
        lambda _db, _settings, scope=None: consistency_scopes.append(scope),
    )

    updated = youtube_intelligence.refresh_youtube_domain_signals(
        object(),
        Settings(),
        set(range(12)),
    )

    assert updated == 12
    assert [len(chunk) for chunk in chunks] == [5, 5, 2]
    assert consistency_scopes == chunks
