from __future__ import annotations

"""Production ASGI entrypoint.

Import the full production bootstrap, then keep data-maintenance work out of the
HTTP request path. Ranked YouTube hygiene is already enforced by the crawler's
candidate/signal refresh wrappers; running the same write-capable consistency
pass on every dashboard GET can collide with long refresh transactions and turn
an otherwise healthy service into a 500 for the user.
"""

import logging

from app.boot import app

logger = logging.getLogger(__name__)


def _is_ranked_youtube_request_hygiene(middleware) -> bool:
    kwargs = getattr(middleware, "kwargs", {}) or {}
    dispatch = kwargs.get("dispatch")
    return getattr(dispatch, "__name__", "") == "_repair_ranked_youtube_rows_before_render"


_before = len(app.user_middleware)
app.user_middleware = [
    middleware
    for middleware in app.user_middleware
    if not _is_ranked_youtube_request_hygiene(middleware)
]
_removed = _before - len(app.user_middleware)

# Middleware is built lazily by Starlette. Reset the cached stack in case the
# imported app constructed it during import-time setup.
app.middleware_stack = None
logger.info("Production serving wrapper removed %s request-time hygiene middleware(s)", _removed)
