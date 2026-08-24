from __future__ import annotations

"""Production ASGI entrypoint.

Import the full production bootstrap, then keep data-maintenance work out of the
HTTP request path. Ranked YouTube hygiene is already enforced by the crawler's
candidate/signal refresh wrappers; running the same write-capable consistency
pass on every dashboard GET can collide with long refresh transactions and turn
an otherwise healthy service into a 500 for the user.
"""

import logging
import traceback

from fastapi import Request
from fastapi.responses import JSONResponse

from app.boot import app
import app.main as main_module
from app.database import SessionLocal

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


@app.get("/ops/youtube-dashboard-smoke")
def youtube_dashboard_smoke() -> JSONResponse:
    """Run the real authenticated dashboard renderer without exposing its data.

    Temporary production diagnostic: it bypasses only the auth dependency by
    calling the route function directly, uses the live database, and returns a
    compact exception trace if the render fails. No row/domain values are emitted.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"view=youtube",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("diagnostic", 443),
        "root_path": "",
    }
    request = Request(scope)
    try:
        with SessionLocal() as db:
            response = main_module.dashboard(
                request=request,
                view="youtube",
                tier="all",
                _="admin",
                db=db,
            )
        body = getattr(response, "body", b"") or b""
        return JSONResponse({"ok": True, "status_code": response.status_code, "body_bytes": len(body)})
    except Exception as exc:  # diagnostic boundary intentionally broad
        logger.exception("YouTube dashboard smoke diagnostic failed")
        frames = traceback.format_exc().splitlines()
        # File/line/function/error text only. Do not expose request headers, SQL
        # parameters, environment variables, database rows or domain values.
        safe_trace = [line[:300] for line in frames[-12:]]
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "trace": safe_trace,
            },
        )
