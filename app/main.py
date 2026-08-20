from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.backup import build_logical_snapshot
from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_db
from app.jobs import JOB_FUNCTIONS, build_scheduler, ensure_seed_data, ingest_dropped_text
from app.link_hunter import run_provider_proof_job
from app.link_hunter_preview import build_provider_proof_preview
from app.models import (
    Candidate,
    DashboardDecision,
    Domain,
    DroppedDomain,
    FetchVerification,
    Opportunity,
    RunLog,
    SourceLink,
    SourcePage,
    SourceSite,
    Video,
    VideoDomain,
    VideoRefreshState,
    YouTubeChannel,
)
from app.provider_budget import provider_daily_budget_snapshot

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO. YouTube uses an API key in the query
# string, so keep transport logging at WARNING while retaining app/job INFO logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
scheduler = None
DASHBOARD_SESSION_COOKIE = "expandosaurus_session"
DASHBOARD_SESSION_SECONDS = 7 * 24 * 60 * 60
DASHBOARD_VISIT_BASELINE_COOKIE = "expandosaurus_visit_baseline"
DASHBOARD_LAST_ACTIVITY_COOKIE = "expandosaurus_last_activity"
DASHBOARD_VISIT_GAP_SECONDS = 2 * 60 * 60
DASHBOARD_VISIT_COOKIE_SECONDS = 365 * 24 * 60 * 60
DASHBOARD_TIMEZONE = ZoneInfo("Europe/Prague")
ResultTier = Literal["all", "new", "priority", "qualified", "watchlist", "pending"]
DashboardSystem = Literal["web", "youtube"]
DecisionStatus = Literal["shortlisted", "bought", "ignored"]

WebEvidenceRow = tuple[
    Opportunity,
    Domain,
    SourcePage | None,
    SourceSite | None,
    SourceLink | None,
    FetchVerification | None,
]


def _dashboard_time(value: datetime | None) -> datetime | None:
    """Present stored UTC timestamps in the dashboard owner's Prague timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DASHBOARD_TIMEZONE)


templates.env.filters["dashboard_time"] = _dashboard_time


@asynccontextmanager
async def lifespan(_: FastAPI):
    global scheduler
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_seed_data(db)
    if settings.scheduler_enabled:
        scheduler = build_scheduler(settings)
        scheduler.start()
        logger.info("Background crawler scheduler started")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="0.4.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _valid_dashboard_credentials(username: str, password: str) -> bool:
    supplied_user = username.encode()
    supplied_password = password.encode()
    valid_user = hmac.compare_digest(supplied_user, b"admin")
    valid_password = hmac.compare_digest(
        supplied_password, settings.dashboard_password.encode()
    )
    return valid_user and valid_password


def _dashboard_session_secret() -> bytes:
    material = (
        f"expandosaurus-dashboard:{settings.admin_token}:{settings.dashboard_password}"
    ).encode()
    return hashlib.sha256(material).digest()


def _create_dashboard_session(*, expires_at: int | None = None) -> str:
    if expires_at is None:
        expires_at = int(datetime.now(UTC).timestamp()) + DASHBOARD_SESSION_SECONDS
    payload = f"admin:{expires_at}".encode()
    signature = hmac.new(_dashboard_session_secret(), payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_payload}.{encoded_signature}"


def _dashboard_session_valid(token: str, *, now: int | None = None) -> bool:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload_padding = "=" * (-len(encoded_payload) % 4)
        signature_padding = "=" * (-len(encoded_signature) % 4)
        payload = base64.urlsafe_b64decode(encoded_payload + payload_padding)
        supplied_signature = base64.urlsafe_b64decode(
            encoded_signature + signature_padding
        )
        expected_signature = hmac.new(
            _dashboard_session_secret(), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False
        username, expires_text = payload.decode().split(":", 1)
        current_time = now if now is not None else int(datetime.now(UTC).timestamp())
        return username == "admin" and int(expires_text) > current_time
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False


def _safe_next_path(next_path: str) -> str:
    if not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def _cookie_timestamp(value: str, *, now: int) -> int | None:
    try:
        timestamp = int(value)
    except ValueError:
        return None
    oldest = now - DASHBOARD_VISIT_COOKIE_SECONDS
    if timestamp < oldest or timestamp > now + 300:
        return None
    return timestamp


def _dashboard_visit_window(
    request: Request,
    *,
    now: datetime | None = None,
) -> tuple[datetime, int, int]:
    current = now or datetime.now(UTC)
    current_timestamp = int(current.timestamp())
    last_activity = _cookie_timestamp(
        request.cookies.get(DASHBOARD_LAST_ACTIVITY_COOKIE, ""),
        now=current_timestamp,
    )
    baseline = _cookie_timestamp(
        request.cookies.get(DASHBOARD_VISIT_BASELINE_COOKIE, ""),
        now=current_timestamp,
    )
    if last_activity is None:
        baseline = current_timestamp - 24 * 60 * 60
    elif (
        current_timestamp - last_activity > DASHBOARD_VISIT_GAP_SECONDS
        or baseline is None
    ):
        baseline = last_activity
    return datetime.fromtimestamp(baseline, UTC), baseline, current_timestamp


def _set_dashboard_visit_cookies(
    response: Response,
    *,
    baseline: int,
    current_timestamp: int,
) -> None:
    cookie_options = {
        "max_age": DASHBOARD_VISIT_COOKIE_SECONDS,
        "secure": settings.environment == "production",
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        key=DASHBOARD_VISIT_BASELINE_COOKIE,
        value=str(baseline),
        **cookie_options,
    )
    response.set_cookie(
        key=DASHBOARD_LAST_ACTIVITY_COOKIE,
        value=str(current_timestamp),
        **cookie_options,
    )


def _next_link_hunter_slot(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    candidate = current.replace(minute=43, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(hours=1)
    while candidate.hour % 2:
        candidate += timedelta(hours=1)
    return candidate


def require_dashboard_auth(request: Request) -> str:
    token = request.cookies.get(DASHBOARD_SESSION_COOKIE, "")
    if _dashboard_session_valid(token):
        return "admin"
    next_path = request.url.path
    if request.url.query:
        next_path += f"?{request.url.query}"
    login_url = f"/login?next={quote(_safe_next_path(next_path), safe='')}"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Authentication required",
        headers={"Location": login_url, "Cache-Control": "no-store"},
    )


@app.get("/login", response_class=HTMLResponse)
def dashboard_login_page(request: Request, next: str = "/") -> Response:
    if _dashboard_session_valid(request.cookies.get(DASHBOARD_SESSION_COOKIE, "")):
        return RedirectResponse(url=_safe_next_path(next), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "", "next_path": _safe_next_path(next)},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/login", response_class=HTMLResponse)
def dashboard_login(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default="/"),
) -> Response:
    next_path = _safe_next_path(next)
    if not _valid_dashboard_credentials(username, password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Incorrect username or password.", "next_path": next_path},
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"Cache-Control": "no-store"},
        )
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        key=DASHBOARD_SESSION_COOKIE,
        value=_create_dashboard_session(),
        max_age=DASHBOARD_SESSION_SECONDS,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/logout")
def dashboard_logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        key=DASHBOARD_SESSION_COOKIE,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    supplied = (x_admin_token or "").encode()
    expected = settings.admin_token.encode()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _load_web_evidence_rows(
    db: Session,
    *,
    limit: int | None = 100,
    tier: ResultTier = "all",
    new_since: datetime | None = None,
) -> list[WebEvidenceRow]:
    statement = (
        select(Opportunity, Domain, SourcePage)
        .join(Domain, Domain.id == Opportunity.domain_id)
        .outerjoin(SourcePage, SourcePage.id == Opportunity.best_source_page_id)
    )
    if tier == "new" and new_since is not None:
        statement = statement.where(Opportunity.updated_at >= new_since)
    elif tier != "all":
        statement = statement.where(Opportunity.tier == tier)
    statement = statement.order_by(
        case(
            (Opportunity.tier == "priority", 0),
            (Opportunity.tier == "qualified", 1),
            (Opportunity.tier == "watchlist", 2),
            else_=3,
        ),
        Opportunity.score.desc(),
        Opportunity.link_strength.desc(),
    )
    if limit is not None:
        statement = statement.limit(limit)

    rows: list[WebEvidenceRow] = []
    for opportunity, domain, page in db.execute(statement).all():
        site = db.get(SourceSite, page.site_id) if page is not None else None
        link = None
        verification = None
        if page is not None:
            link = db.scalar(
                select(SourceLink)
                .where(
                    SourceLink.source_page_id == page.id,
                    SourceLink.domain_id == domain.id,
                    SourceLink.provider_live.is_(True),
                )
                .order_by(SourceLink.provider_rank.desc().nullslast(), SourceLink.id.asc())
                .limit(1)
            )
            if link is not None:
                verification = db.scalar(
                    select(FetchVerification).where(FetchVerification.source_link_id == link.id)
                )
        rows.append((opportunity, domain, page, site, link, verification))
    return rows


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, object]:
    commoncrawl_summary: dict[str, object] | None = None
    stackexchange_summary: dict[str, object] | None = None
    hackernews_summary: dict[str, object] | None = None
    youtube_summary: dict[str, object] | None = None
    email_summary: dict[str, object] = {
        "configured": settings.email_enabled,
        "latest_digest": None,
    }
    try:
        db.scalar(select(func.count()).select_from(RunLog))
        database = "ok"

        youtube_job_keys = {
            "youtube_discovery": (
                "search_calls",
                "videos_returned",
                "new_videos",
                "new_domains",
                "new_links",
            ),
            "youtube_channel_fanout": (
                "playlist_calls",
                "videos_discovered",
                "videos_fetched",
                "new_videos",
                "new_domains",
                "new_links",
                "channels_completed",
                "candidates_refreshed",
                "errors",
            ),
            "view_snapshots": (
                "videos_due",
                "videos_updated",
                "snapshots",
                "candidates_refreshed",
            ),
            "availability_checks": (
                "checked",
                "available",
                "likely_available",
                "registered",
                "rdap_rate_limited",
                "rdap_errors",
                "dns_unknown",
                "errors",
            ),
            "dropped_youtube_search": (
                "drops_checked",
                "videos_returned",
                "exact_matches",
                "new_videos",
                "new_domains",
                "new_links",
            ),
        }
        latest_youtube_runs: dict[str, object] = {}
        for job, counter_keys in youtube_job_keys.items():
            latest = db.scalar(
                select(RunLog)
                .where(RunLog.job == job)
                .order_by(RunLog.started_at.desc())
                .limit(1)
            )
            if latest is None:
                latest_youtube_runs[job] = None
                continue
            raw_counters = latest.counters if isinstance(latest.counters, dict) else {}
            latest_youtube_runs[job] = {
                "status": latest.status,
                "counters": {
                    key: int(raw_counters.get(key) or 0) for key in counter_keys
                },
                "finished_at": (
                    latest.finished_at.isoformat()
                    if latest.finished_at is not None
                    else None
                ),
            }

        tier_counts = {
            tier: int(count)
            for tier, count in db.execute(
                select(Candidate.tier, func.count()).group_by(Candidate.tier)
            ).all()
        }
        now = datetime.now(UTC)
        youtube_summary = {
            "totals": {
                "videos": int(db.scalar(select(func.count()).select_from(Video)) or 0),
                "domains": int(db.scalar(select(func.count()).select_from(Domain)) or 0),
                "exact_links": int(
                    db.scalar(
                        select(func.count())
                        .select_from(VideoDomain)
                        .where(VideoDomain.active.is_(True))
                    )
                    or 0
                ),
                "channels": int(
                    db.scalar(select(func.count()).select_from(YouTubeChannel)) or 0
                ),
                "channels_complete": int(
                    db.scalar(
                        select(func.count())
                        .select_from(YouTubeChannel)
                        .where(YouTubeChannel.inventory_complete.is_(True))
                    )
                    or 0
                ),
                "linked_videos_due_refresh": int(
                    db.scalar(
                        select(func.count())
                        .select_from(VideoRefreshState)
                        .where(VideoRefreshState.next_refresh_at <= now)
                    )
                    or 0
                ),
                "never_checked_domains": int(
                    db.scalar(
                        select(func.count())
                        .select_from(Domain)
                        .where(
                            Domain.last_checked_at.is_(None),
                            Domain.video_links.any(VideoDomain.active.is_(True)),
                        )
                    )
                    or 0
                ),
            },
            "tiers": tier_counts,
            "latest_runs": latest_youtube_runs,
        }

        latest_digest = db.scalar(
            select(RunLog)
            .where(RunLog.job == "daily_digest")
            .order_by(RunLog.started_at.desc())
            .limit(1)
        )
        if latest_digest is not None:
            digest_counters = (
                latest_digest.counters if isinstance(latest_digest.counters, dict) else {}
            )
            email_summary["latest_digest"] = {
                "status": latest_digest.status,
                "emailed": int(digest_counters.get("emailed") or 0),
                "finished_at": (
                    latest_digest.finished_at.isoformat()
                    if latest_digest.finished_at is not None
                    else None
                ),
            }
        last_commoncrawl = db.scalar(
            select(RunLog)
            .where(RunLog.job == "commoncrawl_prefilter")
            .order_by(RunLog.started_at.desc())
            .limit(1)
        )
        if last_commoncrawl is not None:
            counters = last_commoncrawl.counters or {}
            commoncrawl_summary = {
                "status": last_commoncrawl.status,
                "checked": int(counters.get("checked") or 0),
                "with_capture": int(counters.get("with_capture") or 0),
                "without_capture": int(counters.get("without_capture") or 0),
                "errors": int(counters.get("errors") or 0),
                "provider_cost_usd": float(counters.get("provider_cost_usd") or 0.0),
                "finished_at": (
                    last_commoncrawl.finished_at.isoformat()
                    if last_commoncrawl.finished_at is not None
                    else None
                ),
            }
        last_stackexchange = db.scalar(
            select(RunLog)
            .where(RunLog.job == "stackexchange_prefilter")
            .order_by(RunLog.started_at.desc())
            .limit(1)
        )
        if last_stackexchange is not None:
            counters = last_stackexchange.counters or {}
            stackexchange_summary = {
                "status": last_stackexchange.status,
                "queries": int(counters.get("queries") or 0),
                "questions_matched": int(counters.get("questions_matched") or 0),
                "exact_links_saved": int(counters.get("exact_links_saved") or 0),
                "domains_with_links": int(counters.get("domains_with_links") or 0),
                "quota_remaining": counters.get("quota_remaining"),
                "errors": int(counters.get("errors") or 0),
                "provider_cost_usd": float(counters.get("provider_cost_usd") or 0.0),
                "finished_at": (
                    last_stackexchange.finished_at.isoformat()
                    if last_stackexchange.finished_at is not None
                    else None
                ),
            }
        last_hackernews = db.scalar(
            select(RunLog)
            .where(RunLog.job == "hackernews_prefilter")
            .order_by(RunLog.started_at.desc())
            .limit(1)
        )
        if last_hackernews is not None:
            counters = last_hackernews.counters or {}
            hackernews_summary = {
                "status": last_hackernews.status,
                "queries": int(counters.get("queries") or 0),
                "search_hits": int(counters.get("search_hits") or 0),
                "items_with_exact_links": int(counters.get("items_with_exact_links") or 0),
                "exact_links_saved": int(counters.get("exact_links_saved") or 0),
                "domains_with_links": int(counters.get("domains_with_links") or 0),
                "errors": int(counters.get("errors") or 0),
                "provider_cost_usd": float(counters.get("provider_cost_usd") or 0.0),
                "finished_at": (
                    last_hackernews.finished_at.isoformat()
                    if last_hackernews.finished_at is not None
                    else None
                ),
            }
    except Exception:
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "scheduler": bool(scheduler and scheduler.running),
        "email": email_summary,
        "link_hunter_enabled": settings.link_hunter_enabled,
        "dataforseo_configured": settings.dataforseo_enabled,
        "youtube": youtube_summary,
        "commoncrawl_prefilter": commoncrawl_summary,
        "stackexchange_prefilter": stackexchange_summary,
        "hackernews_prefilter": hackernews_summary,
        "time": datetime.now(UTC).isoformat(),
    }


@app.get("/admin/database-backup")
def download_database_backup(_: str = Depends(require_dashboard_auth)) -> Response:
    payload = build_logical_snapshot(engine)
    digest = hashlib.sha256(payload).hexdigest()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"expandosaurus-postgres-{stamp}.json.gz"
    return Response(
        content=payload,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Backup-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )


@app.get("/admin/link-hunter/proof-preview")
def link_hunter_proof_preview(
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return {
        "status": "preview",
        "provider_calls_made": 0,
        "preview": build_provider_proof_preview(db, settings),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    view: DashboardSystem = "web",
    tier: ResultTier = "all",
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    new_since, visit_baseline, current_timestamp = _dashboard_visit_window(request)
    candidate_rows = []
    web_evidence_rows = []
    proof_preview: dict[str, object] = {}
    if view == "youtube":
        candidate_statement = (
            select(Candidate, Domain, Video)
            .join(Domain, Domain.id == Candidate.domain_id)
            .join(Video, Video.id == Candidate.best_video_id)
            .where(Candidate.tier != "rejected")
        )
        if tier == "new":
            candidate_statement = candidate_statement.where(Candidate.updated_at >= new_since)
        elif tier != "all":
            candidate_statement = candidate_statement.where(Candidate.tier == tier)
        candidate_rows = db.execute(
            candidate_statement.order_by(
                case(
                    (Candidate.tier == "priority", 0),
                    (Candidate.tier == "qualified", 1),
                    (Candidate.tier == "watchlist", 2),
                    else_=3,
                ),
                Candidate.score.desc(),
                Candidate.monthly_views.desc(),
            )
            .limit(100)
        ).all()
    else:
        web_evidence_rows = _load_web_evidence_rows(
            db,
            limit=100,
            tier=tier,
            new_since=new_since,
        )
        proof_preview = build_provider_proof_preview(db, settings)
    latest_runs = db.scalars(select(RunLog).order_by(RunLog.started_at.desc()).limit(16)).all()
    last_web_success = db.scalar(
        select(RunLog)
        .where(
            RunLog.job == "link_hunter_proof",
            RunLog.status.in_(["complete", "partial"]),
        )
        .order_by(RunLog.finished_at.desc())
        .limit(1)
    )
    last_youtube_success = db.scalar(
        select(RunLog)
        .where(
            RunLog.job.in_(
                [
                    "youtube_discovery",
                    "youtube_channel_fanout",
                    "view_snapshots",
                    "dropped_youtube_search",
                ]
            ),
            RunLog.status.in_(["complete", "partial"]),
        )
        .order_by(RunLog.finished_at.desc())
        .limit(1)
    )
    daily_budget = provider_daily_budget_snapshot(db, settings)

    youtube_tier_counts = {
        tier_name: int(count)
        for tier_name, count in db.execute(
            select(Candidate.tier, func.count()).group_by(Candidate.tier)
        ).all()
    }
    web_tier_counts = {
        tier_name: int(count)
        for tier_name, count in db.execute(
            select(Opportunity.tier, func.count()).group_by(Opportunity.tier)
        ).all()
    }
    youtube_new_count = (
        db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.tier != "rejected", Candidate.updated_at >= new_since)
        )
        or 0
    )
    web_new_count = (
        db.scalar(
            select(func.count())
            .select_from(Opportunity)
            .where(Opportunity.updated_at >= new_since)
        )
        or 0
    )
    qualified = youtube_tier_counts.get("qualified", 0) + youtube_tier_counts.get(
        "priority", 0
    )
    youtube_results = sum(
        youtube_tier_counts.get(tier_name, 0)
        for tier_name in ("priority", "qualified", "watchlist", "pending")
    )
    crawler_videos = db.scalar(select(func.count()).select_from(Video)) or 0
    crawler_domains = db.scalar(select(func.count()).select_from(Domain)) or 0
    exact_links = (
        db.scalar(select(func.count()).select_from(VideoDomain).where(VideoDomain.active.is_(True)))
        or 0
    )
    youtube_channels = db.scalar(select(func.count()).select_from(YouTubeChannel)) or 0
    youtube_channels_complete = (
        db.scalar(
            select(func.count())
            .select_from(YouTubeChannel)
            .where(YouTubeChannel.inventory_complete.is_(True))
        )
        or 0
    )
    adaptive_refresh_due = (
        db.scalar(
            select(func.count())
            .select_from(VideoRefreshState)
            .where(VideoRefreshState.next_refresh_at <= datetime.now(UTC))
        )
        or 0
    )
    dropped_ingested = db.scalar(select(func.count()).select_from(DroppedDomain)) or 0

    web_opportunities = sum(web_tier_counts.values())
    web_source_sites = db.scalar(select(func.count()).select_from(SourceSite)) or 0
    web_source_pages = db.scalar(select(func.count()).select_from(SourcePage)) or 0
    web_source_links = (
        db.scalar(select(func.count()).select_from(SourceLink).where(SourceLink.provider_live.is_(True)))
        or 0
    )

    displayed_rows = candidate_rows if view == "youtube" else web_evidence_rows
    displayed_domain_ids = [row[1].id for row in displayed_rows]
    decisions = []
    if displayed_domain_ids:
        decisions = db.scalars(
            select(DashboardDecision).where(
                DashboardDecision.system == view,
                DashboardDecision.domain_id.in_(displayed_domain_ids),
            )
        ).all()
    decision_by_domain = {decision.domain_id: decision.status for decision in decisions}
    decision_counts = {
        decision_status: int(count)
        for decision_status, count in db.execute(
            select(DashboardDecision.status, func.count())
            .where(DashboardDecision.system == view)
            .group_by(DashboardDecision.status)
        ).all()
    }

    progress = min(100, round(qualified / settings.target_qualified_domains * 100, 1))
    return_to = request.url.path
    if request.url.query:
        return_to += f"?{request.url.query}"

    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "dashboard_view": view,
            "result_tier": tier,
            "candidate_rows": candidate_rows,
            "web_evidence_rows": web_evidence_rows,
            "proof_preview": proof_preview,
            "latest_runs": latest_runs,
            "qualified": qualified,
            "youtube_results": youtube_results,
            "youtube_tier_counts": youtube_tier_counts,
            "web_tier_counts": web_tier_counts,
            "youtube_new_count": youtube_new_count,
            "web_new_count": web_new_count,
            "new_since": new_since,
            "decision_by_domain": decision_by_domain,
            "decision_counts": decision_counts,
            "return_to": return_to,
            "daily_budget": daily_budget,
            "next_web_run": _next_link_hunter_slot(),
            "last_web_success": last_web_success,
            "last_youtube_success": last_youtube_success,
            "crawler_videos": crawler_videos,
            "crawler_domains": crawler_domains,
            "exact_links": exact_links,
            "youtube_channels": youtube_channels,
            "youtube_channels_complete": youtube_channels_complete,
            "adaptive_refresh_due": adaptive_refresh_due,
            "dropped_ingested": dropped_ingested,
            "cumulative_videos": settings.legacy_videos_checked + crawler_videos,
            "cumulative_domains": settings.legacy_domains_checked + crawler_domains,
            "cumulative_dropped": settings.legacy_dropped_checked + dropped_ingested,
            "web_opportunities": web_opportunities,
            "web_source_sites": web_source_sites,
            "web_source_pages": web_source_pages,
            "web_source_links": web_source_links,
            "target": settings.target_qualified_domains,
            "progress": progress,
            "registrar_enabled": settings.registrar_enabled,
            "email_enabled": settings.email_enabled,
            "dataforseo_enabled": settings.dataforseo_enabled,
            "link_hunter_enabled": settings.link_hunter_enabled,
            "watch_threshold": settings.watchlist_monthly_views,
            "qualified_threshold": settings.qualified_monthly_views,
            "priority_threshold": settings.priority_monthly_views,
        },
    )
    _set_dashboard_visit_cookies(
        response,
        baseline=visit_baseline,
        current_timestamp=current_timestamp,
    )
    return response


@app.get("/export/candidates.csv")
def export_candidates(
    request: Request,
    tier: ResultTier = "all",
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> Response:
    statement = (
        select(Candidate, Domain, Video)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
    )
    if tier == "all":
        statement = statement.where(Candidate.tier.in_(["priority", "qualified", "watchlist"]))
    elif tier == "new":
        new_since, _, _ = _dashboard_visit_window(request)
        statement = statement.where(
            Candidate.tier != "rejected",
            Candidate.updated_at >= new_since,
        )
    else:
        statement = statement.where(Candidate.tier == tier)
    rows = db.execute(statement.order_by(Candidate.score.desc())).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "domain",
            "tier",
            "verified_30_day_views",
            "verified",
            "observation_days",
            "score",
            "availability",
            "registration_price_usd",
            "linked_videos",
            "exact_links",
            "best_video_title",
            "best_video_url",
        ]
    )
    for candidate, domain, video in rows:
        writer.writerow(
            [
                domain.name,
                candidate.tier,
                candidate.monthly_views,
                candidate.verified_30d,
                candidate.observation_days,
                candidate.score,
                domain.availability_status,
                domain.registrar_price_usd or "",
                candidate.video_count,
                candidate.link_count,
                video.title,
                f"https://www.youtube.com/watch?v={video.id}",
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=youtube-domain-candidates.csv"},
    )


@app.get("/export/link-hunter.csv")
def export_link_hunter(
    request: Request,
    tier: ResultTier = "all",
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> Response:
    new_since, _, _ = _dashboard_visit_window(request)
    rows = _load_web_evidence_rows(db, limit=None, tier=tier, new_since=new_since)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "domain",
            "tier",
            "score",
            "niche",
            "source_page_traffic_estimate",
            "referring_pages",
            "independent_sites",
            "link_strength",
            "live_link_verified",
            "availability",
            "registration_price_usd",
            "source_site",
            "best_source_page",
            "best_source_title",
            "anchor_text",
            "context_before",
            "context_after",
            "dofollow",
            "provider_rank",
            "provider_spam_score",
            "fetch_http_status",
            "fetch_final_url",
        ]
    )
    for opportunity, domain, page, site, link, verification in rows:
        writer.writerow(
            [
                domain.name,
                opportunity.tier,
                opportunity.score,
                opportunity.niche,
                opportunity.source_page_traffic_estimate,
                opportunity.referring_page_count,
                opportunity.independent_site_count,
                opportunity.link_strength,
                opportunity.verified_live_link,
                domain.availability_status,
                domain.registrar_price_usd or "",
                site.hostname if site else "",
                page.url if page else "",
                page.title if page else "",
                link.anchor_text if link else "",
                link.context_before if link else "",
                link.context_after if link else "",
                link.dofollow if link else "",
                link.provider_rank if link else "",
                link.spam_score if link else "",
                verification.http_status if verification else "",
                verification.final_url if verification else "",
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=link-hunter-opportunities.csv"},
    )


@app.post("/admin/dropped-domains")
async def add_dropped_domains(
    domains: str = Form(default=""),
    file: UploadFile | None = File(default=None),
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    text = domains
    source = "dashboard paste"
    if file and file.filename:
        raw = await file.read()
        text += "\n" + raw.decode("utf-8", errors="ignore")
        source = f"dashboard upload: {file.filename}"
    if text.strip():
        ingest_dropped_text(db, text, source)
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/dashboard-decision")
def set_dashboard_decision(
    system: DashboardSystem = Form(),
    domain_id: int = Form(),
    decision_status: DecisionStatus = Form(),
    return_to: str = Form(default="/"),
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if db.get(Domain, domain_id) is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    if system == "youtube":
        result_exists = db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.domain_id == domain_id)
        )
    else:
        result_exists = db.scalar(
            select(func.count())
            .select_from(Opportunity)
            .where(Opportunity.domain_id == domain_id)
        )
    if not result_exists:
        raise HTTPException(status_code=404, detail="Dashboard result not found")

    decision = db.scalar(
        select(DashboardDecision).where(
            DashboardDecision.system == system,
            DashboardDecision.domain_id == domain_id,
        )
    )
    if decision is not None and decision.status == decision_status:
        db.delete(decision)
    elif decision is None:
        db.add(
            DashboardDecision(
                system=system,
                domain_id=domain_id,
                status=decision_status,
            )
        )
    else:
        decision.status = decision_status
        decision.updated_at = datetime.now(UTC)
    db.commit()
    return RedirectResponse(url=_safe_next_path(return_to), status_code=303)


@app.post("/api/link-hunter/proof")
def trigger_link_hunter_proof(_: None = Depends(require_admin_token)) -> dict[str, object]:
    if not settings.link_hunter_enabled:
        raise HTTPException(status_code=503, detail="Link Hunter is disabled")
    if not settings.dataforseo_enabled:
        raise HTTPException(status_code=503, detail="DataForSEO credentials are not configured")
    try:
        counters = run_provider_proof_job()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "complete", "job": "link_hunter_proof", "counters": counters}


@app.post("/api/run/{job_name}")
def trigger_job(job_name: str, _: None = Depends(require_admin_token)) -> dict[str, str]:
    function = JOB_FUNCTIONS.get(job_name)
    if function is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    if scheduler and scheduler.running:
        scheduler.add_job(function, id=f"manual_{job_name}", replace_existing=True)
    else:
        raise HTTPException(status_code=503, detail="Scheduler is not running")
    return {"status": "queued", "job": job_name}
