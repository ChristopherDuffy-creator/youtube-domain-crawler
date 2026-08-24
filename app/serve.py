from __future__ import annotations

"""Production ASGI entrypoint with safe dashboard guards."""

import logging
import traceback

from fastapi import Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from app.boot import app
import app.link_hunter_preview as link_hunter_preview_module
import app.main as main_module
from app.database import SessionLocal
from app.models import DroppedDomain, WebScreening

logger = logging.getLogger(__name__)

_SQL_IN_CHUNK = 10_000


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


def _chunks(values: list[str], size: int = _SQL_IN_CHUNK):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _safe_select_provider_summary_targets_with_ranking(db, settings):
    """Select Web Hunter targets without ever exceeding PostgreSQL bind limits.

    The free-ranking pool can now exceed 65,535 names. Psycopg rejects a single
    ``WHERE name IN (...)`` with that many bound parameters, so both large name
    lookups are split into deterministic 10k chunks and merged in memory.
    """
    already_checked = link_hunter_preview_module._dataforseo_checked_targets(db)
    context = link_hunter_preview_module._free_rank_context(db)
    availability: dict[str, str] = context["availability"]

    recent_drops = db.scalars(
        select(DroppedDomain)
        .order_by(DroppedDomain.first_seen_at.desc())
        .limit(link_hunter_preview_module._RECENT_FALLBACK_POOL)
    ).all()

    priority_names = sorted(link_hunter_preview_module._priority_candidate_names(context))
    priority_drops: list[DroppedDomain] = []
    for chunk in _chunks(priority_names):
        priority_drops.extend(
            db.scalars(
                select(DroppedDomain)
                .where(DroppedDomain.name.in_(chunk))
                .order_by(DroppedDomain.first_seen_at.desc())
            ).all()
        )

    candidate_map = {drop.name: drop for drop in recent_drops}
    for drop in priority_drops:
        candidate_map.setdefault(drop.name, drop)
    pooled = list(candidate_map.values())
    unchecked = [drop for drop in pooled if drop.name not in already_checked]

    locally_blocked: set[str] = set()
    unchecked_names = [drop.name for drop in unchecked]
    for chunk in _chunks(unchecked_names):
        locally_blocked.update(
            db.scalars(
                select(WebScreening.domain_name).where(
                    WebScreening.status == "blocked",
                    WebScreening.domain_name.in_(chunk),
                )
            ).all()
        )

    blocked_names = {
        drop.name
        for drop in unchecked
        if availability.get(drop.name, "unknown")
        in link_hunter_preview_module._BLOCKED_AVAILABILITY
        or drop.name in locally_blocked
    }
    candidates = [drop for drop in unchecked if drop.name not in blocked_names]
    ordered, scores, signals = link_hunter_preview_module._rank_free_candidates(
        candidates,
        context,
    )
    targets = [
        drop.name
        for drop in ordered[: settings.link_hunter_summary_batch_size]
    ]
    return targets, scores, signals, len(blocked_names), context


# The same private helper backs the dashboard preview and the public selection
# helpers already imported by Link Hunter, so one patch protects both paths.
link_hunter_preview_module._select_provider_summary_targets_with_ranking = (
    _safe_select_provider_summary_targets_with_ranking
)
logger.info("Web Hunter large-name SQL filters capped at %s parameters per query", _SQL_IN_CHUNK)


@app.middleware("http")
async def _dashboard_exception_fallback(request: Request, call_next):
    """Keep the dashboard usable if an older middleware layer throws.

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
            "request_hygiene_removed": _removed,
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
                "request_hygiene_removed": _removed,
            }
        )
    except Exception as exc:
        return _safe_failure(exc)
