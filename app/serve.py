from __future__ import annotations

"""Production ASGI entrypoint with safe dashboard guards."""

import logging
import traceback

from fastapi import Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.boot import app
import app.main as main_module
from app.database import SessionLocal

logger = logging.getLogger(__name__)


@app.middleware("http")
async def _dashboard_exception_fallback(request: Request, call_next):
    """Keep the dashboard usable if the normal rendering pipeline throws.

    Authentication is still explicitly verified before the fallback route is
    called. Only GET / dashboard requests are eligible.
    """
    try:
        return await call_next(request)
    except Exception:
        if request.method != "GET" or request.url.path != "/":
            raise

        logger.exception("Normal dashboard pipeline failed; trying authenticated direct render")
        token = request.cookies.get(main_module.DASHBOARD_SESSION_COOKIE, "")
        if not main_module._dashboard_session_valid(token):
            return RedirectResponse(url="/login", status_code=303)

        view = request.query_params.get("view", "web")
        if view not in {"web", "youtube"}:
            view = "web"
        tier = request.query_params.get("tier", "all")
        if tier not in {"all", "new", "measured", "priority", "qualified", "watchlist", "pending"}:
            tier = "all"

        with SessionLocal() as db:
            return main_module.dashboard(
                request=request,
                view=view,
                tier=tier,
                _="admin",
                db=db,
            )


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
        },
    )


@app.get("/ops/dashboard-smoke")
def dashboard_smoke(
    view: str = Query(default="youtube", pattern="^(youtube|web)$"),
) -> JSONResponse:
    """Run the dashboard renderer directly against the live database."""
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
    try:
        signed = main_module._create_dashboard_session()
        auth_scope = dict(scope)
        auth_scope["headers"] = [
            (b"cookie", f"{main_module.DASHBOARD_SESSION_COOKIE}={signed}".encode())
        ]
        main_module.require_dashboard_auth(Request(auth_scope))

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
                "status_code": response.status_code,
                "body_bytes": len(body),
                "auth_validator": "passed",
            }
        )
    except Exception as exc:
        return _safe_failure(exc)
