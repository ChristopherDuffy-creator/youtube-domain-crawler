from __future__ import annotations

"""Production ASGI entrypoint with safe dashboard guards and pilot site routing."""

import logging
import secrets
import traceback
from datetime import UTC, datetime
from threading import Lock

from fastapi import Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.boot import app
import app.main as main_module
from app.database import SessionLocal, engine
from app.pilot_sites import (
    PILOT_SESSION_COOKIE,
    PILOT_SESSION_SECONDS,
    ensure_pilot_schema,
    get_pilot_site,
    offer_url,
    pilot_sites_enabled,
    record_pilot_event,
    safe_offer_id,
)

logger = logging.getLogger(__name__)
_pilot_schema_lock = Lock()
_pilot_schema_ready = False


def _ensure_pilot_schema() -> None:
    global _pilot_schema_ready
    if _pilot_schema_ready:
        return
    with _pilot_schema_lock:
        if _pilot_schema_ready:
            return
        ensure_pilot_schema(engine)
        _pilot_schema_ready = True


def _pilot_session(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(PILOT_SESSION_COOKIE, "").strip()
    if existing and len(existing) <= 80:
        return existing, False
    return secrets.token_urlsafe(24), True


def _set_pilot_cookie(response, session_id: str, is_new: bool) -> None:
    if not is_new:
        return
    response.set_cookie(
        key=PILOT_SESSION_COOKIE,
        value=session_id,
        max_age=PILOT_SESSION_SECONDS,
        httponly=True,
        secure=main_module.settings.environment == "production",
        samesite="lax",
        path="/",
    )


@app.middleware("http")
async def _pilot_site_router(request: Request, call_next):
    """Serve many lightweight sites from one app, selected solely by Host.

    This middleware runs before dashboard authentication. It also prevents the
    crawler's admin/login/ops endpoints from being exposed through pilot domains:
    any non-special path on a pilot host resolves to that site's landing page.
    """
    if not pilot_sites_enabled():
        return await call_next(request)

    site = get_pilot_site(request.headers.get("host", ""))
    if site is None:
        return await call_next(request)

    try:
        _ensure_pilot_schema()
    except Exception:
        logger.exception("Pilot event schema setup failed")
        return JSONResponse({"status": "temporarily unavailable"}, status_code=503)

    session_id, is_new_session = _pilot_session(request)
    path = request.url.path or "/"
    landing_path = path
    if request.url.query:
        landing_path += f"?{request.url.query}"
    referrer = request.headers.get("referer", "")[:2000]

    if path == "/privacy":
        response = main_module.templates.TemplateResponse(
            request=request,
            name="pilot_message.html",
            context={
                "site": site,
                "title": "Privacy",
                "message": (
                    "This site uses a first-party anonymous session cookie so we can count "
                    "visits and understand which pages and recommendations are useful."
                ),
                "detail": (
                    "We do not store your IP address in the pilot analytics table. We record "
                    "the site, landing path, an anonymous random session identifier, referrer "
                    "when your browser provides one, and clicks on recommendation links."
                ),
            },
            headers={"Cache-Control": "public, max-age=300"},
        )
        _set_pilot_cookie(response, session_id, is_new_session)
        return response

    if path.startswith("/go/"):
        offer_id = safe_offer_id(path.rsplit("/", 1)[-1])
        target = offer_url(site)
        try:
            with SessionLocal() as db:
                record_pilot_event(
                    db,
                    site=site,
                    event_type="outbound_click" if target else "interest_click",
                    path=landing_path,
                    session_id=session_id,
                    referrer=referrer,
                    offer_id=offer_id,
                )
        except Exception:
            logger.exception("Pilot click event write failed for %s", site.domain)

        if target:
            response = RedirectResponse(url=target, status_code=302)
        else:
            response = main_module.templates.TemplateResponse(
                request=request,
                name="pilot_message.html",
                context={
                    "site": site,
                    "title": "Recommendations are being finalised",
                    "message": (
                        "Thanks — your click has been counted. We are currently reviewing the "
                        "best matching options for this guide."
                    ),
                    "detail": "The full recommendation link will be switched on shortly.",
                },
                headers={"Cache-Control": "no-store"},
            )
        _set_pilot_cookie(response, session_id, is_new_session)
        return response

    try:
        with SessionLocal() as db:
            record_pilot_event(
                db,
                site=site,
                event_type="pageview",
                path=landing_path,
                session_id=session_id,
                referrer=referrer,
            )
    except Exception:
        logger.exception("Pilot pageview write failed for %s", site.domain)

    response = main_module.templates.TemplateResponse(
        request=request,
        name="pilot_site.html",
        context={"site": site, "year": datetime.now(UTC).year},
        headers={"Cache-Control": "public, max-age=60"},
    )
    _set_pilot_cookie(response, session_id, is_new_session)
    return response


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
