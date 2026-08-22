from __future__ import annotations

"""Memory-safe production bootstrap for Railway.

The crawler now has enough indexed data that the original per-run defaults can
materialise too many ORM rows at once. Cap the memory-heavy batches before the
application settings are imported, serialize background jobs so two large jobs
cannot spike RAM together, and run lightweight YouTube data-hygiene repairs.
"""

import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# These are throughput caps, not feature disables. Jobs are resumable and recur,
# so smaller batches continue advancing the permanent ledgers with a lower peak
# memory footprint.
_BATCH_CAPS = {
    "YOUTUBE_CHANNEL_PAGES_PER_RUN": 30,
    "YOUTUBE_VIEW_REFRESH_BATCH_SIZE": 2_000,
    "YOUTUBE_INTELLIGENCE_BACKFILL_BATCH_SIZE": 500,
    "YOUTUBE_LOCAL_MATCH_BATCH_SIZE": 20_000,
    "LINK_HUNTER_FREE_SCREEN_BATCH_SIZE": 5_000,
}


def _cap_int_env(name: str, maximum: int) -> None:
    raw = os.getenv(name)
    if raw is None:
        os.environ[name] = str(maximum)
        return
    try:
        value = int(raw)
    except ValueError:
        # Leave validation to Settings so a bad production variable remains
        # visible rather than being silently hidden by the bootstrap.
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
    # The old scheduler could run several memory-heavy jobs concurrently. One
    # worker keeps the web app responsive while crawler jobs execute serially.
    scheduler.configure(executors={"default": ThreadPoolExecutor(max_workers=1)})
    return scheduler


def _consistent_refresh_youtube_domain_signals(
    db,
    settings,
    domain_ids=None,
    *,
    limit=None,
):
    updated = _original_refresh_youtube_domain_signals(
        db,
        settings,
        domain_ids,
        limit=limit,
    )
    enforce_candidate_signal_consistency(db, settings, domain_ids)
    return updated


@asynccontextmanager
async def _production_lifespan(app):
    # Enter the original lifespan first so create_all/runtime migrations have
    # completed before the cleanup touches production tables.
    async with _original_lifespan_context(app):
        with SessionLocal() as db:
            purge_legacy_bare_youtube_links(db, main_module.settings)
        yield


main_module.build_scheduler = _memory_safe_build_scheduler
# jobs.py imported the function directly, while backfill code resolves it from
# youtube_intelligence.py. Patch both references so interrupted/stale signal
# refreshes can never leave a visible "Watchlist · 0 views" contradiction.
youtube_intelligence_module.refresh_youtube_domain_signals = (
    _consistent_refresh_youtube_domain_signals
)
jobs_module.refresh_youtube_domain_signals = _consistent_refresh_youtube_domain_signals
main_module.app.router.lifespan_context = _production_lifespan
app = main_module.app
