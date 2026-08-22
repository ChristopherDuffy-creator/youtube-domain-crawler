from __future__ import annotations

"""Memory-safe production bootstrap for Railway.

Full-throughput production mode. The expensive YouTube money-signal ORM graph
is independently hard-chunked, so the crawler can use its original production
batch sizes without materialising thousands of domains and snapshots at once.
"""

import gc
import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Original production throughput. The actual OOM path is guarded independently
# below, so these jobs no longer need emergency throttling.
_BATCH_CAPS = {
    "YOUTUBE_CHANNEL_PAGES_PER_RUN": 100,
    "YOUTUBE_CHANNEL_PAGE_BURST": 12,
    "YOUTUBE_VIEW_REFRESH_BATCH_SIZE": 50_000,
    "YOUTUBE_INTELLIGENCE_BACKFILL_BATCH_SIZE": 5_000,
    "YOUTUBE_LOCAL_MATCH_BATCH_SIZE": 100_000,
    "AVAILABILITY_BATCH_SIZE": 500,
    "LINK_HUNTER_SUMMARY_BATCH_SIZE": 100,
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

# Import only after applying environment caps: app.main constructs/caches the
# Settings object during import.
import app.jobs as jobs_module  # noqa: E402
import app.main as main_module  # noqa: E402
import app.youtube_intelligence as youtube_intelligence_module  # noqa: E402
from apscheduler.executors.pool import ThreadPoolExecutor  # noqa: E402
from app.data_hygiene import (  # noqa: E402
    enforce_candidate_signal_consistency,
    purge_legacy_bare_youtube_links,
)
from app.database import SessionLocal  # noqa: E402

_original_build_scheduler = main_module.build_scheduler
_original_refresh_youtube_domain_signals = (
    youtube_intelligence_module.refresh_youtube_domain_signals
)
_original_lifespan_context = main_module.app.router.lifespan_context


def _memory_safe_build_scheduler(settings):
    scheduler = _original_build_scheduler(settings)
    # Keep memory-heavy jobs serialized. Full per-job throughput is safe when
    # jobs do not overlap and the signal graph is hard-chunked below.
    scheduler.configure(executors={"default": ThreadPoolExecutor(max_workers=1)})
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
    # Schema setup/scheduler start happens in the original lifespan. The hygiene
    # pass is bounded and idempotent; it removes legacy false matches while
    # preserving genuine bare domains.
    async with _original_lifespan_context(app):
        with SessionLocal() as db:
            purge_legacy_bare_youtube_links(db, main_module.settings)
        yield


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
