from __future__ import annotations

"""Memory-safe production bootstrap for Railway.

Production safe mode keeps the crawler advancing in small resumable batches.
The database is the permanent ledger, so lowering batch sizes reduces peak RAM
without losing work or changing opportunity rules.
"""

import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Conservative production caps after a second Railway OOM. These are temporary
# throughput caps, not feature disables. The recurring jobs resume from the
# permanent database ledger on every run.
_BATCH_CAPS = {
    "YOUTUBE_CHANNEL_PAGES_PER_RUN": 10,
    "YOUTUBE_CHANNEL_PAGE_BURST": 3,
    "YOUTUBE_VIEW_REFRESH_BATCH_SIZE": 500,
    "YOUTUBE_INTELLIGENCE_BACKFILL_BATCH_SIZE": 100,
    "YOUTUBE_LOCAL_MATCH_BATCH_SIZE": 5_000,
    "AVAILABILITY_BATCH_SIZE": 100,
    "LINK_HUNTER_SUMMARY_BATCH_SIZE": 50,
    "LINK_HUNTER_LINK_REFRESH_BATCH_SIZE": 50,
    "LINK_HUNTER_FREE_SCREEN_BATCH_SIZE": 1_000,
}


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
    # One worker means memory-heavy crawler jobs cannot overlap. If several are
    # due together they queue and run sequentially rather than multiplying RAM.
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
