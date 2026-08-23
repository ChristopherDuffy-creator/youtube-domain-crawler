from __future__ import annotations

"""Memory-safe full-throughput production bootstrap for Railway.

The YouTube signal graph is hard-chunked to protect the 8 GB replica.  The web
Link Hunter is also upgraded here to prioritise verified, monetisable traffic
rather than backlink volume and to advance its cost-capped proof automatically.
"""

import gc
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# This production service is the automatic Web Hunter.  Force proof on even if
# Railway still has the legacy LINK_HUNTER_ENABLED=false variable from the old
# manual-proof setup. Spend remains independently bounded by the provider ledger.
os.environ["LINK_HUNTER_ENABLED"] = "true"

# Original YouTube throughput.  Web summary batches are intentionally narrower:
# 25 cheap summaries feeding 5 deep proofs gives far better proof coverage than
# 100 summaries feeding the same five detailed checks, without raising spend.
_BATCH_CAPS = {
    "YOUTUBE_CHANNEL_PAGES_PER_RUN": 100,
    "YOUTUBE_CHANNEL_PAGE_BURST": 12,
    "YOUTUBE_VIEW_REFRESH_BATCH_SIZE": 50_000,
    "YOUTUBE_INTELLIGENCE_BACKFILL_BATCH_SIZE": 5_000,
    "YOUTUBE_LOCAL_MATCH_BATCH_SIZE": 100_000,
    "AVAILABILITY_BATCH_SIZE": 500,
    "LINK_HUNTER_SUMMARY_BATCH_SIZE": 25,
    "LINK_HUNTER_LINK_REFRESH_BATCH_SIZE": 100,
    "LINK_HUNTER_FREE_SCREEN_BATCH_SIZE": 50_000,
}

# refresh_youtube_domain_signals historically eager-loaded Domain -> links ->
# Video -> snapshots for every affected domain in one ORM graph. A fan-out run
# can hand refresh_candidates thousands of affected domains and exhaust even an
# 8 GB Railway replica. This hard boundary stays tiny regardless of crawler
# throughput.
_SIGNAL_DOMAIN_CHUNK = 5
_SIGNAL_UNSCOPED_LIMIT = 25


def _cap_int_env(name: str, maximum: int) -> None:
    raw = os.getenv(name)
    if raw is None:
        os.environ[name] = str(maximum)
        return
    try:
        value = int(raw)
    except ValueError:
        return
    if value > maximum:
        logger.warning("Capping %s from %s to %s for Railway memory safety", name, value, maximum)
        os.environ[name] = str(maximum)


for _name, _maximum in _BATCH_CAPS.items():
    _cap_int_env(_name, _maximum)

# Import only after applying environment defaults/caps: app.main constructs and
# caches Settings during import.
import app.jobs as jobs_module  # noqa: E402
import app.link_hunter as link_hunter_module  # noqa: E402
import app.link_hunter_preview as link_hunter_preview_module  # noqa: E402
import app.main as main_module  # noqa: E402
import app.web_intelligence as web_intelligence_module  # noqa: E402
import app.youtube_intelligence as youtube_intelligence_module  # noqa: E402
from apscheduler.executors.pool import ThreadPoolExecutor  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402
from app.data_hygiene import (  # noqa: E402
    enforce_candidate_signal_consistency,
    purge_legacy_bare_youtube_links,
)
from app.database import SessionLocal  # noqa: E402
from app.web_hunter_upgrade import (  # noqa: E402
    enforce_money_tier,
    regrade_existing_web_opportunities,
    traffic_first_projection,
    traffic_first_rerank_summary_targets,
    traffic_first_web_row_key,
)

_original_build_scheduler = main_module.build_scheduler
_original_refresh_youtube_domain_signals = (
    youtube_intelligence_module.refresh_youtube_domain_signals
)
_original_lifespan_context = main_module.app.router.lifespan_context
_original_project_opportunity_economics = web_intelligence_module.project_opportunity_economics
_original_score_opportunity = link_hunter_module._score_opportunity
_original_save_summary_opportunity = link_hunter_module._save_summary_opportunity
_original_load_web_evidence_rows = main_module._load_web_evidence_rows


def _traffic_first_project_opportunity_economics(
    opportunity,
    domain,
    links,
    *,
    traffic,
    verified,
    evidence_score,
    clickability_score=0.0,
    screening_risk=0.0,
):
    return traffic_first_projection(
        _original_project_opportunity_economics,
        opportunity,
        domain,
        links,
        traffic=traffic,
        verified=verified,
        evidence_score=evidence_score,
        clickability_score=clickability_score,
        screening_risk=screening_risk,
    )


def _traffic_first_score_opportunity(
    opportunity,
    domain,
    saved_links,
    traffic,
    verified,
    *,
    db=None,
    clickability_score=0.0,
):
    _original_score_opportunity(
        opportunity,
        domain,
        saved_links,
        traffic,
        verified,
        db=db,
        clickability_score=clickability_score,
    )
    enforce_money_tier(
        db,
        opportunity,
        traffic=traffic,
        verified=verified,
    )


def _traffic_first_save_summary_opportunity(db, domain, summary, combined_score):
    opportunity = _original_save_summary_opportunity(
        db,
        domain,
        summary,
        combined_score,
    )
    if opportunity is not None and not opportunity.verified_live_link:
        # Summary-only backlink evidence is useful for the SEO-asset record, but
        # it is not enough to appear as a traffic Watchlist candidate.
        opportunity.tier = "pending"
        opportunity.score = min(float(opportunity.score or 0.0), 39.9)
    return opportunity


def _traffic_first_load_web_evidence_rows(
    db,
    *,
    limit=100,
    tier="all",
    new_since=None,
):
    # Pull a somewhat wider working set, then present money/traffic proof first.
    # This avoids one large unbounded dashboard query during normal browsing.
    working_limit = None if limit is None else max(int(limit), 250)
    rows = _original_load_web_evidence_rows(
        db,
        limit=working_limit,
        tier=tier,
        new_since=new_since,
    )
    rows.sort(key=traffic_first_web_row_key)
    return rows if limit is None else rows[: int(limit)]


def _memory_safe_build_scheduler(settings):
    scheduler = _original_build_scheduler(settings)
    # Keep memory-heavy jobs serialized. Full YouTube throughput stays safe when
    # jobs do not overlap and the signal graph is hard-chunked below.
    scheduler.configure(executors={"default": ThreadPoolExecutor(max_workers=1)})

    # The provider budget ledger is the kill switch: at the existing defaults a
    # proof reserves at most $0.18 and the day stops at $2.16.  Every two hours
    # therefore advances up to five deep domains, or roughly 60/day at the cap.
    if settings.link_hunter_enabled and settings.dataforseo_enabled:
        scheduler.add_job(
            link_hunter_module.run_provider_proof_job,
            IntervalTrigger(
                hours=2,
                start_date=datetime.now(UTC) + timedelta(minutes=5),
            ),
            id="link_hunter_proof",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    return scheduler


def _release_orm_memory(db) -> None:
    """Drop loaded relationship state between signal chunks."""
    try:
        db.expire_all()
    finally:
        gc.collect()


def _consistent_refresh_youtube_domain_signals(
    db,
    settings,
    domain_ids=None,
    *,
    limit=None,
):
    # Scoped refreshes are the dangerous path: refresh_candidates may pass a
    # very large set of domain IDs after channel fan-out. Process only a handful
    # at a time so links/videos/snapshots from one group can be freed first.
    if domain_ids is not None:
        ids = sorted(int(value) for value in domain_ids)
        if not ids:
            return 0
        updated = 0
        for start in range(0, len(ids), _SIGNAL_DOMAIN_CHUNK):
            chunk = set(ids[start : start + _SIGNAL_DOMAIN_CHUNK])
            updated += _original_refresh_youtube_domain_signals(
                db,
                settings,
                chunk,
                limit=None,
            )
            enforce_candidate_signal_consistency(db, settings, chunk)
            _release_orm_memory(db)
        return updated

    # Never allow an unscoped production call to materialise every active
    # domain. Even if a caller forgets a limit, retain a hard production guard.
    effective_limit = _SIGNAL_UNSCOPED_LIMIT if limit is None else min(
        int(limit), _SIGNAL_UNSCOPED_LIMIT
    )
    updated = _original_refresh_youtube_domain_signals(
        db,
        settings,
        None,
        limit=effective_limit,
    )
    enforce_candidate_signal_consistency(db, settings, None)
    _release_orm_memory(db)
    return updated


@asynccontextmanager
async def _production_lifespan(app):
    # Schema setup/scheduler start happens in the original lifespan.  Repair old
    # false YouTube matches and re-grade existing web rows under the new money-
    # first model so stale backlink-heavy scores disappear after deployment.
    async with _original_lifespan_context(app):
        with SessionLocal() as db:
            purge_legacy_bare_youtube_links(db, main_module.settings)
            regraded = regrade_existing_web_opportunities(
                db,
                _traffic_first_score_opportunity,
                limit=250,
            )
            logger.info("Traffic-first Web Hunter regraded %s existing opportunities", regraded)
        yield


# Patch every imported reference used by the running app.  link_hunter imported
# the projection/reranker by name, so both the source module and its local global
# must be replaced.
web_intelligence_module.project_opportunity_economics = (
    _traffic_first_project_opportunity_economics
)
link_hunter_module.project_opportunity_economics = _traffic_first_project_opportunity_economics
link_hunter_preview_module.rerank_summary_screen_targets = traffic_first_rerank_summary_targets
link_hunter_module.rerank_summary_screen_targets = traffic_first_rerank_summary_targets
link_hunter_module._score_opportunity = _traffic_first_score_opportunity
link_hunter_module._save_summary_opportunity = _traffic_first_save_summary_opportunity
main_module._load_web_evidence_rows = _traffic_first_load_web_evidence_rows
main_module.build_scheduler = _memory_safe_build_scheduler
youtube_intelligence_module.refresh_youtube_domain_signals = (
    _consistent_refresh_youtube_domain_signals
)
jobs_module.refresh_youtube_domain_signals = _consistent_refresh_youtube_domain_signals
main_module.app.router.lifespan_context = _production_lifespan
app = main_module.app


@app.middleware("http")
async def _repair_ranked_youtube_rows_before_render(request, call_next):
    """Never render a ranked YouTube row whose current signal cannot support it."""
    if (
        request.method == "GET"
        and request.url.path == "/"
        and request.query_params.get("view", "web") == "youtube"
    ):
        with SessionLocal() as db:
            enforce_candidate_signal_consistency(db, main_module.settings)
    return await call_next(request)
