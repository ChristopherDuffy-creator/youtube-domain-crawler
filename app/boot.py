"""Production bootstrap with conservative memory-safe batch ceilings.

The crawler's runtime behavior lives in its canonical modules. This entrypoint
only applies bounded environment defaults before :mod:`app.main` constructs its
settings, so importing production code cannot monkey-patch crawler functions.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

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


def _cap_int_env(name: str, maximum: int) -> None:
    raw = os.getenv(name)
    if raw is None:
        os.environ[name] = str(maximum)
        return
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer environment value for %s", name)
        return
    if value > maximum:
        logger.warning("Capping %s from %s to %s for memory safety", name, value, maximum)
        os.environ[name] = str(maximum)


for _name, _maximum in _BATCH_CAPS.items():
    _cap_int_env(_name, _maximum)

# Settings are cached during app.main import, so batch defaults must be applied
# first. Runtime policies are implemented directly in the modules they govern.
from app.main import app  # noqa: E402, F401
