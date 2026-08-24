from __future__ import annotations

"""Production ASGI entrypoint with safe dashboard diagnostics."""

import logging
import traceback

import httpx
from fastapi import Query, Request
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
app.middleware_stack = None
logger.info("Production serving wrapper removed %s request-time hygiene middleware(s)", _removed)


def _safe_failure(exc: Exception) -> JSONResponse:
    logger.exception("Dashboard smoke diagnostic failed")
    frames = traceback.format_exc().splitlines()
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:300],
            "trace": [line[:300] for line in frames[-12:]],
            "request_hygiene_removed": _removed,
        },
    )


@app.get("/ops/dashboard-smoke")
async def dashboard_smoke(
    view: str = Query(default="youtube", pattern="^(youtube|web)$"),
    pipeline: str = Query(default="asgi", pattern="^(asgi|direct)$"),
) -> JSONResponse:
    """Exercise the live dashboard without exposing dashboard/domain data."""
    try:
        if pipeline == "direct":
            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/",
                "raw_path": b"/",
                "query_string": f"view={view}".encode(),
                "headers": [],
                "client": ("127.0.0.1", 0),
                "server": ("diagnostic", 443),
                "root_path": "",
            }
            request = Request(scope)
            with SessionLocal() as db:
                response = main_module.dashboard(
                    request=request,
                    view=view,
                    tier="all",
                    _="admin",
                    db=db,
                )
            body = getattr(response, "body", b"") or b""
            return JSONResponse(
                {
                    "ok": True,
                    "view": view,
                    "pipeline": pipeline,
                    "status_code": response.status_code,
                    "body_bytes": len(body),
                    "request_hygiene_removed": _removed,
                }
            )

        # Full ASGI path: signed login cookie, dependency/auth, middleware,
        # dashboard query construction, template render and response middleware.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        cookie = main_module._create_dashboard_session()
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://diagnostic.local",
            follow_redirects=False,
            timeout=60.0,
        ) as client:
            response = await client.get(
                f"/?view={view}",
                cookies={main_module.DASHBOARD_SESSION_COOKIE: cookie},
            )
        return JSONResponse(
            {
                "ok": response.status_code == 200 and len(response.content) > 0,
                "view": view,
                "pipeline": pipeline,
                "status_code": response.status_code,
                "body_bytes": len(response.content),
                "request_hygiene_removed": _removed,
            },
            status_code=200 if response.status_code == 200 else 500,
        )
    except Exception as exc:  # diagnostic boundary intentionally broad
        return _safe_failure(exc)
