from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.backup import build_logical_snapshot
from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_db
from app.jobs import JOB_FUNCTIONS, build_scheduler, ensure_seed_data, ingest_dropped_text
from app.models import Candidate, Domain, DroppedDomain, RunLog, Video, VideoDomain

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
security = HTTPBasic(auto_error=False)
scheduler = None


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


app = FastAPI(title=settings.app_name, version="0.2.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def require_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    supplied_user = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    valid_user = hmac.compare_digest(supplied_user.encode(), b"admin")
    valid_password = hmac.compare_digest(
        supplied_password.encode(), settings.dashboard_password.encode()
    )
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return supplied_user


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    supplied = (x_admin_token or "").encode()
    expected = settings.admin_token.encode()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        db.scalar(select(func.count()).select_from(RunLog))
        database = "ok"
    except Exception:
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "scheduler": bool(scheduler and scheduler.running),
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


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    candidate_rows = db.execute(
        select(Candidate, Domain, Video)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
        .where(Candidate.tier != "rejected")
        .order_by(
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
    latest_runs = db.scalars(select(RunLog).order_by(RunLog.started_at.desc()).limit(12)).all()

    qualified = (
        db.scalar(
            select(func.count())
            .select_from(Candidate)
            .where(Candidate.tier.in_(["qualified", "priority"]))
        )
        or 0
    )
    watchlist = (
        db.scalar(select(func.count()).select_from(Candidate).where(Candidate.tier == "watchlist"))
        or 0
    )
    crawler_videos = db.scalar(select(func.count()).select_from(Video)) or 0
    crawler_domains = db.scalar(select(func.count()).select_from(Domain)) or 0
    exact_links = (
        db.scalar(select(func.count()).select_from(VideoDomain).where(VideoDomain.active.is_(True)))
        or 0
    )
    dropped_ingested = db.scalar(select(func.count()).select_from(DroppedDomain)) or 0
    progress = min(100, round(qualified / settings.target_qualified_domains * 100, 1))

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "candidate_rows": candidate_rows,
            "latest_runs": latest_runs,
            "qualified": qualified,
            "watchlist": watchlist,
            "crawler_videos": crawler_videos,
            "crawler_domains": crawler_domains,
            "exact_links": exact_links,
            "dropped_ingested": dropped_ingested,
            "cumulative_videos": settings.legacy_videos_checked + crawler_videos,
            "cumulative_domains": settings.legacy_domains_checked + crawler_domains,
            "cumulative_dropped": settings.legacy_dropped_checked + dropped_ingested,
            "target": settings.target_qualified_domains,
            "progress": progress,
            "registrar_enabled": settings.registrar_enabled,
            "email_enabled": settings.email_enabled,
            "watch_threshold": settings.watchlist_monthly_views,
            "qualified_threshold": settings.qualified_monthly_views,
            "priority_threshold": settings.priority_monthly_views,
        },
    )


@app.get("/export/candidates.csv")
def export_candidates(
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> Response:
    rows = db.execute(
        select(Candidate, Domain, Video)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
        .where(Candidate.tier.in_(["priority", "qualified", "watchlist"]))
        .order_by(Candidate.score.desc())
    ).all()
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
