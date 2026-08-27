from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import html
import io
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Literal
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.affiliate_links import AFFILIATE_LINKS, PublicSite, public_site_for_host
from app.backup import build_logical_snapshot
from app.config import (
    YOUTUBE_PRIORITY_BUY_SCORE,
    YOUTUBE_QUALIFIED_BUY_SCORE,
    get_settings,
)
from app.config import (
    YOUTUBE_RESERVE_MINIMUM as _YOUTUBE_RESERVE_MINIMUM,
)
from app.database import Base, SessionLocal, engine, ensure_runtime_schema, get_db
from app.domain_lifecycle import (
    hard_delete_domain,
    migrate_legacy_youtube_bought_decisions,
    move_youtube_domain_to_bought,
)
from app.emailer import EmailError, send_email
from app.jobs import (
    JOB_FUNCTIONS,
    build_scheduler,
    ensure_seed_data,
    ingest_dropped_text,
    refresh_candidates,
)
from app.link_hunter_preview import build_provider_proof_preview
from app.models import (
    BacklinkSummary,
    BoughtDomain,
    Candidate,
    ContactMessage,
    DashboardDecision,
    Domain,
    DroppedDomain,
    DroppedDomainMatch,
    EmailSubscriber,
    FetchVerification,
    LinkObservation,
    Opportunity,
    OpportunityEconomics,
    PilotSiteEvent,
    RunLog,
    SourceLink,
    SourcePage,
    SourceSite,
    Video,
    VideoDomain,
    VideoRefreshState,
    WebScreening,
    YouTubeChannel,
    YouTubeChannelIntelligence,
    YouTubeDomainSignal,
)
from app.provider_budget import provider_daily_budget_snapshot
from app.storage_guard import database_storage_status, storage_guard_allows_writes
from app.youtube_intelligence import (
    quarantine_stale_youtube_signals,
    youtube_quota_snapshot,
)

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
ResultTier = Literal[
    "all",
    "new",
    "measured",
    "day3",
    "day7",
    "low",
    "priority",
    "qualified",
    "watchlist",
    "pending",
]
DashboardSystem = Literal["web", "youtube"]
DecisionStatus = Literal["shortlisted", "bought", "ignored"]
YouTubeDomainAction = Literal["delete", "bought"]

WebEvidenceRow = tuple[
    Opportunity,
    Domain,
    SourcePage | None,
    SourceSite | None,
    SourceLink | None,
    FetchVerification | None,
    OpportunityEconomics | None,
    LinkObservation | None,
]

_HIDDEN_WEB_AVAILABILITY = {"registered", "aftermarket", "premium", "reserved"}
_HIDDEN_YOUTUBE_AVAILABILITY = _HIDDEN_WEB_AVAILABILITY
_YOUTUBE_VISIBLE_MAXIMUM = 1_000_000


def _sanitized_job_error(value: str | None) -> str | None:
    """Expose enough failure context for operations without leaking targets or secrets."""
    if not value:
        return None
    cleaned = re.sub(r"https?://\S+", "[url]", value)
    cleaned = re.sub(
        r"\b[A-Za-z0-9.-]+\.(?:com|net|org|io|co|dev|app|xyz|info|biz)\b",
        "[domain]",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bAIza[A-Za-z0-9_-]+\b", "[secret]", cleaned)
    return cleaned[:500]


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
    scheduler = None
    runtime_stopping = Event()
    scheduler_lock = Lock()

    def prepare_runtime() -> None:
        """Keep idempotent database work out of the HTTP health-check path."""
        global scheduler
        try:
            Base.metadata.create_all(bind=engine)
            ensure_runtime_schema(engine)
            with SessionLocal() as db:
                if storage_guard_allows_writes(db, settings, "startup_maintenance"):
                    ensure_seed_data(db)
                    migrated_bought = migrate_legacy_youtube_bought_decisions(db)
                    quarantined = quarantine_stale_youtube_signals(db, settings)
                    retained_youtube_ids = set(
                        db.scalars(
                            select(Candidate.domain_id).where(
                                or_(
                                    Candidate.tier.in_({"priority", "qualified", "watchlist"}),
                                    Candidate.notified_tier.in_({"priority", "qualified", "watchlist"}),
                                    Candidate.domain_id.in_(
                                        select(DashboardDecision.domain_id).where(
                                            DashboardDecision.system == "youtube",
                                            DashboardDecision.status.in_({"shortlisted", "bought"}),
                                        )
                                    ),
                                )
                            )
                        ).all()
                    )
                    refreshed_youtube = refresh_candidates(db, retained_youtube_ids)
                    logger.info(
                        "YouTube purchase migration moved %s legacy records; projection "
                        "safety gate quarantined %s and recalculated %s retained candidates",
                        migrated_bought,
                        quarantined,
                        refreshed_youtube,
                    )
            if settings.scheduler_enabled:
                with scheduler_lock:
                    if runtime_stopping.is_set():
                        return
                    scheduler = build_scheduler(settings)
                    scheduler.start()
                    logger.info("Background crawler scheduler started")
        except Exception:
            logger.exception("Background crawler startup maintenance failed")

    startup_thread = Thread(
        target=prepare_runtime,
        name="crawler-startup-maintenance",
        daemon=True,
    )
    startup_thread.start()
    yield
    runtime_stopping.set()
    startup_thread.join(timeout=5)
    with scheduler_lock:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="0.4.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


_SATVIC_HOSTS = {"satvic.yoga", "www.satvic.yoga"}
_CRAFTS_HOSTS = {"craftsheaven.club", "www.craftsheaven.club"}
_GERARDI_HOSTS = {
    "teamgerardiperformance.com",
    "www.teamgerardiperformance.com",
}
_PUBLIC_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,63}$")
_PUBLIC_CONSENT_VERSION = "2026-08-26"
_PUBLIC_TRACKING_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_PUBLIC_TRACKING_OFFER_PATTERN = re.compile(r"^[a-z0-9-]{1,120}$")
_PUBLIC_TRACKING_BOT_PATTERN = re.compile(
    r"bot|crawler|spider|slurp|headless|lighthouse|uptime|monitor",
    flags=re.IGNORECASE,
)
_PUBLIC_FORM_MIN_AGE_SECONDS = 3
_PUBLIC_FORM_MAX_AGE_SECONDS = 7_200
_PUBLIC_FORM_RATE_LIMITS = {"contact": (4, 3_600), "subscribe": (6, 3_600)}
_PUBLIC_FORM_RATE_LOCK = Lock()
_PUBLIC_FORM_RATE_BUCKETS: dict[tuple[str, str, str], list[float]] = {}
_PUBLIC_CONTACT_SPAM_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"\bseo\b",
        r"\baeo\b",
        r"\bgeo\b",
        r"rank(?:ing)? higher",
        r"search engine optimi[sz]ation",
        r"ai-powered search",
        r"\bbacklinks?\b",
        r"\bguest posts?\b",
        r"domain authority",
        r"first page of google",
        r"quote\s*(?:&|and)\s*price list",
        r"\bweb design services?\b",
    )
)


def _normalise_public_email(value: str) -> str | None:
    email = value.strip().lower()
    if len(email) > 320 or not _PUBLIC_EMAIL_PATTERN.fullmatch(email):
        return None
    return email


def _public_form_secret() -> bytes:
    material = (
        "expandosaurus-public-forms:"
        f"{settings.admin_token}:{settings.dashboard_password}"
    ).encode()
    return hashlib.sha256(material).digest()


def _issue_public_form_token(
    site_key: str,
    form_kind: Literal["contact", "subscribe"],
    *,
    issued_at: int | None = None,
) -> str:
    issued = int(time.time()) if issued_at is None else int(issued_at)
    nonce = secrets.token_urlsafe(12)
    payload = f"{issued}:{site_key}:{form_kind}:{nonce}".encode()
    signature = hmac.new(_public_form_secret(), payload, hashlib.sha256).digest()
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_payload}.{encoded_signature}"


def _public_form_token_valid(
    token: str,
    site_key: str,
    form_kind: Literal["contact", "subscribe"],
    *,
    now: int | None = None,
) -> bool:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload_padding = "=" * (-len(encoded_payload) % 4)
        signature_padding = "=" * (-len(encoded_signature) % 4)
        payload = base64.urlsafe_b64decode(encoded_payload + payload_padding)
        supplied_signature = base64.urlsafe_b64decode(
            encoded_signature + signature_padding
        )
        expected_signature = hmac.new(
            _public_form_secret(), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return False
        issued_text, token_site, token_kind, nonce = payload.decode().split(":", 3)
        issued = int(issued_text)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    if token_site != site_key or token_kind != form_kind or len(nonce) < 12:
        return False
    age = (int(time.time()) if now is None else int(now)) - issued
    return _PUBLIC_FORM_MIN_AGE_SECONDS <= age <= _PUBLIC_FORM_MAX_AGE_SECONDS


def _public_form_client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded[:64]
    if request.client is not None and request.client.host:
        return request.client.host[:64]
    return "unknown"


def _public_form_rate_limited(
    request: Request,
    site_key: str,
    form_kind: Literal["contact", "subscribe"],
) -> bool:
    limit, window_seconds = _PUBLIC_FORM_RATE_LIMITS[form_kind]
    now = time.monotonic()
    cutoff = now - window_seconds
    key = (site_key, form_kind, _public_form_client_key(request))
    with _PUBLIC_FORM_RATE_LOCK:
        if len(_PUBLIC_FORM_RATE_BUCKETS) > 5_000:
            stale_keys = [
                bucket_key
                for bucket_key, timestamps in _PUBLIC_FORM_RATE_BUCKETS.items()
                if not timestamps or timestamps[-1] < cutoff
            ]
            for bucket_key in stale_keys:
                _PUBLIC_FORM_RATE_BUCKETS.pop(bucket_key, None)
        timestamps = [
            timestamp
            for timestamp in _PUBLIC_FORM_RATE_BUCKETS.get(key, [])
            if timestamp >= cutoff
        ]
        blocked = len(timestamps) >= limit
        if not blocked:
            timestamps.append(now)
        _PUBLIC_FORM_RATE_BUCKETS[key] = timestamps
    return blocked


def _public_form_blocked(
    request: Request,
    site_key: str,
    form_kind: Literal["contact", "subscribe"],
    *,
    form_token: str,
    form_guard: str,
    honeypots: tuple[str, ...],
) -> bool:
    if any(value.strip() for value in honeypots):
        return True
    if form_guard != "ready":
        return True
    if not _public_form_token_valid(form_token, site_key, form_kind):
        return True
    return _public_form_rate_limited(request, site_key, form_kind)


def _public_contact_looks_like_spam(message: str) -> bool:
    url_count = len(re.findall(r"(?:https?://|www\.)", message, flags=re.IGNORECASE))
    if url_count >= 2:
        return True
    signal_count = sum(
        bool(pattern.search(message)) for pattern in _PUBLIC_CONTACT_SPAM_PATTERNS
    )
    return signal_count >= 3


_PUBLIC_PAGE_CONTENT: dict[str, dict[str, tuple[str, list[tuple[str, list[str]]]]]] = {
    "about": {
        "crafts": (
            "About Crafts Heaven",
            [
                (
                    "A practical place to begin",
                    [
                        "Crafts Heaven is an independent guide for people who want "
                        "to make useful things without needing a large workshop or "
                        "a wall of power tools.",
                        "We focus on approachable hand tools, sensible starter kits "
                        "and projects that help beginners build skill one careful "
                        "cut at a time.",
                    ],
                ),
                (
                    "How recommendations are chosen",
                    [
                        "We favour versatile tools, beginner-friendly features and "
                        "equipment that can earn its place in a small workspace. "
                        "Our guides explain what to compare so you can make your "
                        "own decision.",
                    ],
                ),
            ],
        ),
        "satvic": (
            "About Satvic Yoga",
            [
                (
                    "A calmer home practice",
                    [
                        "Satvic Yoga is an independent guide to creating a simple, "
                        "comfortable practice at home. It is for ordinary people "
                        "who want to move, breathe and rest with less complication.",
                        "We curate practical props, books and quiet-space essentials "
                        "while encouraging readers to begin with what they already "
                        "have.",
                    ],
                ),
                (
                    "How recommendations are chosen",
                    [
                        "We look for useful, versatile items that support comfort "
                        "and consistency. Product pages on Amazon contain the current "
                        "specifications, price and availability.",
                    ],
                ),
            ],
        ),
        "gerardi": (
            "About Team Gerardi Performance",
            [
                (
                    "Straightforward home training",
                    [
                        "Team Gerardi Performance helps beginners build a sensible "
                        "home-training setup and a routine they can repeat. The emphasis "
                        "is on good basics, progressive habits and equipment that fits "
                        "real homes.",
                        "Our buying guides make it easier to compare common training "
                        "tools without promising shortcuts or instant results.",
                    ],
                ),
                (
                    "A sensible starting point",
                    [
                        "Training needs vary. Begin conservatively, learn sound "
                        "technique and seek qualified medical advice before starting "
                        "if you have an injury, health condition or concern.",
                    ],
                ),
            ],
        ),
    },
    "privacy": {
        "crafts": ("Privacy", []),
        "satvic": ("Privacy", []),
        "gerardi": ("Privacy", []),
    },
    "affiliate-disclosure": {
        "crafts": ("Affiliate disclosure", []),
        "satvic": ("Affiliate disclosure", []),
        "gerardi": ("Affiliate disclosure", []),
    },
}


@app.middleware("http")
async def serve_satvic_site(request: Request, call_next):
    """Serve the public Satvic guide before the authenticated crawler dashboard.

    Railway sends all custom domains to the same service.  Keeping this narrow
    host/path check preserves the dashboard and crawler routes while allowing
    satvic.yoga to be a standalone public site.
    """
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    if host in _SATVIC_HOSTS and request.url.path == "/":
        return templates.TemplateResponse(
            request=request,
            name="satvic.html",
            context={
                "site": public_site_for_host(host),
                "form_token": _issue_public_form_token("satvic", "subscribe"),
            },
        )
    if host in _CRAFTS_HOSTS and request.url.path == "/":
        return templates.TemplateResponse(
            request=request,
            name="crafts.html",
            context={
                "site": public_site_for_host(host),
                "form_token": _issue_public_form_token("crafts", "subscribe"),
            },
        )
    if host in _GERARDI_HOSTS and request.url.path == "/":
        return templates.TemplateResponse(
            request=request,
            name="gerardi.html",
            context={
                "site": public_site_for_host(host),
                "form_token": _issue_public_form_token("gerardi", "subscribe"),
            },
        )
    return await call_next(request)


def _require_public_site(request: Request) -> PublicSite:
    site = public_site_for_host(request.headers.get("host", ""))
    if site is None:
        raise HTTPException(status_code=404, detail="Public site not found")
    return site


def _public_page_sections(
    site: PublicSite,
    page_name: str,
) -> tuple[str, list[tuple[str, list[str]]]]:
    title, sections = _PUBLIC_PAGE_CONTENT[page_name][site.key]
    if page_name == "privacy":
        sections = [
            (
                "What this site records",
                [
                    f"{site.name} does not require a reader account and does not use "
                    "behavioural advertising cookies. Google Analytics loads with "
                    "the site so we can measure visits and improve useful content.",
                    "If you join the email list, we store your email address, the site "
                    "you joined from, your consent status and the date of consent. We "
                    "use that information only for the updates you requested and do "
                    "not sell the list.",
                    "If you use the contact form, we store your name, email address and "
                    "message so we can reply and keep a record of the enquiry.",
                    "Like most websites, the hosting service may retain short-lived "
                    "technical logs needed for security and reliability. We record "
                    "anonymous page views and the name of an affiliate link when it "
                    "is selected so we can understand which guides are useful. Each "
                    "page uses a fresh random identifier that is not stored in your "
                    "browser; these events do not contain your name, email address or "
                    "full referring URL.",
                    "Google Analytics records pages viewed, "
                    "approximate location, referral source and technical details such "
                    "as browser and device type. Google may set analytics cookies for "
                    "this purpose. Advertising signals and personalisation remain "
                    "disabled.",
                ],
            ),
            (
                "Links to other websites",
                [
                    "When you follow a link to Amazon or another external service, "
                    "that service applies its own privacy and cookie policies. Review "
                    "those policies before providing personal information.",
                ],
            ),
            (
                "Contact",
                [
                    "For a privacy question, email info@expandosaurus.com and identify "
                    "the site you are asking about. You can use the same address to "
                    "unsubscribe or ask for your stored email data to be removed.",
                ],
            ),
        ]
    elif page_name == "affiliate-disclosure":
        sections = [
            (
                "How affiliate links work",
                [
                    f"{site.name} participates in the Amazon Associates Programme. "
                    "As an Amazon Associate we earn from qualifying purchases.",
                    "We may also use clearly identified links from approved partners "
                    "including advertisers joined through Awin and Udemy through "
                    "its affiliate platform.",
                    "If you follow a marked Amazon link and make a qualifying purchase, "
                    "we may receive a commission. This does not add a separate charge "
                    "to your order.",
                ],
            ),
            (
                "Our approach",
                [
                    "Affiliate relationships help fund the site, but they do not change "
                    "the practical criteria explained in our guides. We do not copy "
                    "live Amazon prices, ratings or availability; check the Amazon "
                    "product page for current details before buying.",
                ],
            ),
        ]
    return title, sections


def _public_page_response(request: Request, page_name: str) -> Response:
    site = _require_public_site(request)
    page_title, sections = _public_page_sections(site, page_name)
    return templates.TemplateResponse(
        request=request,
        name="site_page.html",
        context={
            "site": site,
            "page_title": page_title,
            "sections": sections,
            "canonical_url": f"{site.canonical_url}/{page_name}",
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/about", response_class=HTMLResponse, include_in_schema=False)
def public_about(request: Request) -> Response:
    return _public_page_response(request, "about")


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def public_privacy(request: Request) -> Response:
    return _public_page_response(request, "privacy")


@app.get("/affiliate-disclosure", response_class=HTMLResponse, include_in_schema=False)
def public_affiliate_disclosure(request: Request) -> Response:
    return _public_page_response(request, "affiliate-disclosure")


def _contact_page_response(
    request: Request,
    *,
    sent: bool = False,
    error: str = "",
    form_values: dict[str, str] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    site = _require_public_site(request)
    return templates.TemplateResponse(
        request=request,
        name="contact.html",
        context={
            "site": site,
            "sent": sent,
            "error": error,
            "form_values": form_values or {},
            "canonical_url": f"{site.canonical_url}/contact",
            "form_token": _issue_public_form_token(site.key, "contact"),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/contact", response_class=HTMLResponse, include_in_schema=False)
def public_contact(request: Request, sent: bool = False) -> Response:
    return _contact_page_response(request, sent=sent)


@app.post("/contact", response_class=HTMLResponse, include_in_schema=False)
def public_contact_submit(
    request: Request,
    name: str = Form(default=""),
    email: str = Form(default=""),
    message: str = Form(default=""),
    website: str = Form(default=""),
    fax_number: str = Form(default=""),
    form_token: str = Form(default=""),
    form_guard: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    site = _require_public_site(request)
    if _public_form_blocked(
        request,
        site.key,
        "contact",
        form_token=form_token,
        form_guard=form_guard,
        honeypots=(website, fax_number),
    ):
        logger.info("Discarded automated public form site=%s kind=contact", site.key)
        return RedirectResponse(url="/contact?sent=true", status_code=303)

    clean_name = " ".join(name.strip().split())
    clean_email = _normalise_public_email(email)
    clean_message = message.strip()
    form_values = {"name": clean_name, "email": email.strip(), "message": clean_message}
    if not clean_name or len(clean_name) > 120:
        return _contact_page_response(
            request,
            error="Please enter your name.",
            form_values=form_values,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if clean_email is None:
        return _contact_page_response(
            request,
            error="Please enter a valid email address.",
            form_values=form_values,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(clean_message) < 10 or len(clean_message) > 5_000:
        return _contact_page_response(
            request,
            error="Please enter a message between 10 and 5,000 characters.",
            form_values=form_values,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if _public_contact_looks_like_spam(clean_message):
        logger.info("Discarded promotional public form site=%s kind=contact", site.key)
        return RedirectResponse(url="/contact?sent=true", status_code=303)

    duplicate = db.scalar(
        select(ContactMessage.id).where(
            ContactMessage.site_key == site.key,
            ContactMessage.email == clean_email,
            ContactMessage.message == clean_message,
            ContactMessage.created_at >= datetime.now(UTC) - timedelta(hours=24),
        )
    )
    if duplicate is not None:
        logger.info("Discarded duplicate public form site=%s kind=contact", site.key)
        return RedirectResponse(url="/contact?sent=true", status_code=303)

    db.add(
        ContactMessage(
            site_key=site.key,
            name=clean_name,
            email=clean_email,
            message=clean_message,
        )
    )
    db.commit()

    if settings.email_enabled:
        subject = f"{site.name} website enquiry"
        body = (
            f"<h2>{html.escape(site.name)} website enquiry</h2>"
            f"<p><strong>Name:</strong> {html.escape(clean_name)}</p>"
            f"<p><strong>Email:</strong> {html.escape(clean_email)}</p>"
            f"<p><strong>Message:</strong><br>{html.escape(clean_message).replace(chr(10), '<br>')}</p>"
        )
        try:
            send_email(
                settings,
                subject,
                body,
                to_email="info@expandosaurus.com",
            )
        except EmailError:
            logger.exception("Contact notification delivery failed for site=%s", site.key)

    return RedirectResponse(url="/contact?sent=true", status_code=303)


@app.post("/subscribe", include_in_schema=False)
def public_subscribe(
    request: Request,
    email: str = Form(default=""),
    consent: str = Form(default=""),
    website: str = Form(default=""),
    fax_number: str = Form(default=""),
    form_token: str = Form(default=""),
    form_guard: str = Form(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    site = _require_public_site(request)
    if _public_form_blocked(
        request,
        site.key,
        "subscribe",
        form_token=form_token,
        form_guard=form_guard,
        honeypots=(website, fax_number),
    ):
        logger.info("Discarded automated public form site=%s kind=subscribe", site.key)
        return RedirectResponse(url="/?subscribed=1#newsletter", status_code=303)

    clean_email = _normalise_public_email(email)
    if clean_email is None or consent != "yes":
        return RedirectResponse(url="/?subscribe=invalid#newsletter", status_code=303)

    now = datetime.now(UTC)
    subscriber = db.scalar(
        select(EmailSubscriber).where(
            EmailSubscriber.site_key == site.key,
            EmailSubscriber.email == clean_email,
        )
    )
    if subscriber is None:
        subscriber = EmailSubscriber(
            site_key=site.key,
            email=clean_email,
            status="active",
            source="homepage",
            consent_version=_PUBLIC_CONSENT_VERSION,
            consented_at=now,
            updated_at=now,
        )
        db.add(subscriber)
    else:
        subscriber.status = "active"
        subscriber.source = "homepage"
        subscriber.consent_version = _PUBLIC_CONSENT_VERSION
        subscriber.consented_at = now
        subscriber.updated_at = now

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(EmailSubscriber).where(
                EmailSubscriber.site_key == site.key,
                EmailSubscriber.email == clean_email,
            )
        )
        if existing is None:
            raise
        existing.status = "active"
        existing.consent_version = _PUBLIC_CONSENT_VERSION
        existing.consented_at = now
        existing.updated_at = now
        db.commit()

    return RedirectResponse(url="/?subscribed=1#newsletter", status_code=303)


@app.get("/go/{slug}", include_in_schema=False)
def public_affiliate_redirect(request: Request, slug: str) -> RedirectResponse:
    site = _require_public_site(request)
    target = AFFILIATE_LINKS.get(site.key, {}).get(slug)
    if target is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    logger.info("affiliate_click site=%s recommendation=%s", site.key, slug)
    return RedirectResponse(
        url=target,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


@app.post("/track/site-event", include_in_schema=False, status_code=204)
async def public_site_event(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Record bounded, anonymous first-party evidence and quietly reject noise."""
    site = _require_public_site(request)
    if request.headers.get("x-expandosaurus-verification") == "1":
        return Response(status_code=204)
    user_agent = request.headers.get("user-agent", "")[:300]
    if not user_agent or _PUBLIC_TRACKING_BOT_PATTERN.search(user_agent):
        return Response(status_code=204)
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=204)
    if not isinstance(payload, dict):
        return Response(status_code=204)

    event_type = str(payload.get("event_type") or "")
    session_id = str(payload.get("session_id") or "")
    if event_type not in {"pageview", "interest_click"}:
        return Response(status_code=204)
    if not _PUBLIC_TRACKING_SESSION_PATTERN.fullmatch(session_id):
        return Response(status_code=204)
    path = str(payload.get("path") or "/").split("?", 1)[0][:300]
    if not path.startswith("/"):
        path = "/"
    offer_id = str(payload.get("offer_id") or "") or None
    if offer_id and not _PUBLIC_TRACKING_OFFER_PATTERN.fullmatch(offer_id):
        offer_id = None
    if event_type == "interest_click" and offer_id is None:
        return Response(status_code=204)

    now = datetime.now(UTC)
    recent_cutoff = now - timedelta(seconds=30)
    duplicate = db.scalar(
        select(PilotSiteEvent.id)
        .where(
            PilotSiteEvent.domain == urlparse(site.canonical_url).netloc,
            PilotSiteEvent.session_id == session_id,
            PilotSiteEvent.event_type == event_type,
            PilotSiteEvent.path == path,
            PilotSiteEvent.offer_id == offer_id,
            PilotSiteEvent.created_at >= recent_cutoff,
        )
        .limit(1)
    )
    if duplicate is not None:
        return Response(status_code=204)
    recent_events = db.scalar(
        select(func.count(PilotSiteEvent.id)).where(
            PilotSiteEvent.session_id == session_id,
            PilotSiteEvent.created_at >= now - timedelta(hours=1),
        )
    )
    if int(recent_events or 0) >= 40:
        return Response(status_code=204)

    raw_referrer = str(payload.get("referrer") or "")[:1_000]
    referrer_host = urlparse(raw_referrer).hostname or ""
    db.add(
        PilotSiteEvent(
            domain=urlparse(site.canonical_url).netloc,
            event_type=event_type,
            path=path,
            session_id=session_id,
            referrer=referrer_host[:255],
            offer_id=offer_id,
            created_at=now,
        )
    )
    db.commit()
    return Response(status_code=204)


@app.get("/robots.txt", include_in_schema=False)
def public_robots(request: Request) -> Response:
    site = _require_public_site(request)
    content = f"User-agent: *\nAllow: /\nDisallow: /go/\nSitemap: {site.canonical_url}/sitemap.xml\n"
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def public_sitemap(request: Request) -> Response:
    site = _require_public_site(request)
    urls = ["", "/about", "/contact", "/privacy", "/affiliate-disclosure"]
    entries = "".join(f"<url><loc>{site.canonical_url}{path}</loc></url>" for path in urls)
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )
    return Response(content=content, media_type="application/xml")


def _valid_dashboard_credentials(username: str, password: str) -> bool:
    supplied_user = username.encode()
    supplied_password = password.encode()
    valid_user = hmac.compare_digest(supplied_user, b"admin")
    valid_password = hmac.compare_digest(supplied_password, settings.dashboard_password.encode())
    return valid_user and valid_password


def _dashboard_session_secret() -> bytes:
    material = (f"expandosaurus-dashboard:{settings.admin_token}:{settings.dashboard_password}").encode()
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
        supplied_signature = base64.urlsafe_b64decode(encoded_signature + signature_padding)
        expected_signature = hmac.new(_dashboard_session_secret(), payload, hashlib.sha256).digest()
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
    elif current_timestamp - last_activity > DASHBOARD_VISIT_GAP_SECONDS or baseline is None:
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
        select(Opportunity, Domain, SourcePage, OpportunityEconomics)
        .join(Domain, Domain.id == Opportunity.domain_id)
        .outerjoin(SourcePage, SourcePage.id == Opportunity.best_source_page_id)
        .outerjoin(OpportunityEconomics, OpportunityEconomics.domain_id == Domain.id)
        .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
        .where(
            ~Domain.id.in_(select(BoughtDomain.domain_id)),
            Domain.availability_status.notin_(_HIDDEN_WEB_AVAILABILITY),
            or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
        )
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
        Opportunity.verified_live_link.desc(),
        OpportunityEconomics.monthly_revenue_high_usd.desc().nullslast(),
        OpportunityEconomics.expected_clicks_monthly.desc().nullslast(),
        Opportunity.source_page_traffic_estimate.desc(),
        Opportunity.score.desc(),
        Opportunity.independent_site_count.desc(),
    )
    if limit is not None:
        statement = statement.limit(limit)

    rows: list[WebEvidenceRow] = []
    for opportunity, domain, page, economics in db.execute(statement).all():
        site = db.get(SourceSite, page.site_id) if page is not None else None
        link = None
        verification = None
        observation = None
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
                observation = db.scalar(
                    select(LinkObservation)
                    .where(LinkObservation.source_link_id == link.id)
                    .order_by(LinkObservation.observed_at.desc())
                    .limit(1)
                )
        rows.append((opportunity, domain, page, site, link, verification, economics, observation))
    return rows


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, object]:
    commoncrawl_summary: dict[str, object] | None = None
    stackexchange_summary: dict[str, object] | None = None
    hackernews_summary: dict[str, object] | None = None
    youtube_summary: dict[str, object] | None = None
    web_intelligence_summary: dict[str, object] | None = None
    database_storage: dict[str, object] | None = None
    email_summary: dict[str, object] = {
        "configured": settings.email_enabled,
        "latest_digest": None,
    }
    try:
        db.scalar(select(func.count()).select_from(RunLog))
        database = "ok"
        storage_status = database_storage_status(db, settings)
        database_storage = storage_status.as_dict()
        if (
            db.bind is not None
            and db.bind.dialect.name == "postgresql"
            and storage_status.database_bytes is not None
        ):
            storage_rows = db.execute(
                text(
                    """
                    SELECT relname, n_live_tup, pg_total_relation_size(relid)
                    FROM pg_stat_user_tables
                    ORDER BY pg_total_relation_size(relid) DESC
                    LIMIT 10
                    """
                )
            ).all()
            database_storage["largest_relations"] = [
                {
                    "name": str(name),
                    "estimated_rows": int(row_count or 0),
                    "total_bytes": int(total_bytes or 0),
                }
                for name, row_count, total_bytes in storage_rows
            ]

        latest_screening = db.scalar(
            select(RunLog)
            .where(RunLog.job == "web_free_screening")
            .order_by(RunLog.started_at.desc())
            .limit(1)
        )
        screening_counters = (
            latest_screening.counters
            if latest_screening is not None and isinstance(latest_screening.counters, dict)
            else {}
        )
        latest_proof = db.scalar(
            select(RunLog)
            .where(RunLog.job == "link_hunter_proof")
            .order_by(RunLog.started_at.desc())
            .limit(1)
        )
        proof_counters = (
            latest_proof.counters
            if latest_proof is not None and isinstance(latest_proof.counters, dict)
            else {}
        )
        proof_budget = proof_counters.get("daily_budget")
        proof_budget = proof_budget if isinstance(proof_budget, dict) else {}
        proof_failure_label = None
        if latest_proof is not None and latest_proof.error:
            proof_failure_label = str(latest_proof.error).split(":", 1)[0].splitlines()[0][:120]
        web_intelligence_summary = {
            "screened": int(db.scalar(select(func.count()).select_from(WebScreening)) or 0),
            "blocked_free": int(
                db.scalar(
                    select(func.count()).select_from(WebScreening).where(WebScreening.status == "blocked")
                )
                or 0
            ),
            "permanent_summaries": int(db.scalar(select(func.count()).select_from(BacklinkSummary)) or 0),
            "link_observations": int(db.scalar(select(func.count()).select_from(LinkObservation)) or 0),
            "money_cases": int(db.scalar(select(func.count()).select_from(OpportunityEconomics)) or 0),
            "latest_screening": {
                "status": latest_screening.status,
                "screened": int(screening_counters.get("screened") or 0),
                "blocked": int(screening_counters.get("blocked") or 0),
                "provider_cost_usd": float(screening_counters.get("provider_cost_usd") or 0.0),
            }
            if latest_screening is not None
            else None,
            # Keep paid-work observability public but aggregate-only: this is
            # enough to prove the hard budget guards held without exposing
            # candidate targets, source URLs, or provider errors.
            "latest_proof": {
                "status": latest_proof.status,
                # A safe error label keeps failed proof attempts diagnosable
                # without exposing provider response bodies, targets, or credentials.
                "failure_label": proof_failure_label,
                "summary_screened": int(proof_counters.get("summary_screened") or 0),
                "deep_proof_target_count": int(proof_counters.get("deep_proof_target_count") or 0),
                "source_links_verified": int(proof_counters.get("source_links_verified") or 0),
                "errors": int(proof_counters.get("errors") or 0),
                "provider_cost_usd": float(proof_counters.get("provider_cost_usd") or 0.0),
                "daily_budget_limit_usd": float(proof_budget.get("limit_usd") or 0.0),
                "daily_budget_committed_usd": round(
                    float(proof_budget.get("spent_usd") or 0.0)
                    + float(proof_budget.get("reserved_usd") or 0.0),
                    6,
                ),
                "finished_at": (
                    latest_proof.finished_at.isoformat() if latest_proof.finished_at is not None else None
                ),
            }
            if latest_proof is not None
            else None,
        }

        youtube_job_keys = {
            "youtube_discovery": (
                "search_calls",
                "videos_returned",
                "known_videos_skipped",
                "video_detail_calls",
                "quota_exhausted",
                "api_errors",
                "cursor_resets",
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
                "hot_pages",
                "warm_pages",
                "cold_or_unrated_pages",
                "quota_exhausted",
                "errors",
            ),
            "view_snapshots": (
                "videos_due",
                "videos_updated",
                "snapshots",
                "candidates_refreshed",
                "quota_exhausted",
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
                "quota_exhausted",
            ),
            "youtube_intelligence": (
                "channels_backfilled",
                "domain_signals_backfilled",
                "matched",
                "new_matches",
            ),
        }
        latest_youtube_runs: dict[str, object] = {}
        for job, counter_keys in youtube_job_keys.items():
            latest = db.scalar(
                select(RunLog).where(RunLog.job == job).order_by(RunLog.started_at.desc()).limit(1)
            )
            if latest is None:
                latest_youtube_runs[job] = None
                continue
            raw_counters = latest.counters if isinstance(latest.counters, dict) else {}
            latest_youtube_runs[job] = {
                "status": latest.status,
                "counters": {key: int(raw_counters.get(key) or 0) for key in counter_keys},
                "finished_at": (latest.finished_at.isoformat() if latest.finished_at is not None else None),
                "failure_stage": raw_counters.get("failure_stage"),
                "error_summary": _sanitized_job_error(latest.error),
            }

        tier_counts = {
            tier: int(count)
            for tier, count in db.execute(select(Candidate.tier, func.count()).group_by(Candidate.tier)).all()
        }
        now = datetime.now(UTC)
        youtube_summary = {
            "totals": {
                "videos": int(db.scalar(select(func.count()).select_from(Video)) or 0),
                "domains": int(db.scalar(select(func.count()).select_from(Domain)) or 0),
                "exact_links": int(
                    db.scalar(
                        select(func.count()).select_from(VideoDomain).where(VideoDomain.active.is_(True))
                    )
                    or 0
                ),
                "channels": int(db.scalar(select(func.count()).select_from(YouTubeChannel)) or 0),
                "channels_complete": int(
                    db.scalar(
                        select(func.count())
                        .select_from(YouTubeChannel)
                        .where(YouTubeChannel.inventory_complete.is_(True))
                    )
                    or 0
                ),
                "hot_channels": int(
                    db.scalar(
                        select(func.count())
                        .select_from(YouTubeChannelIntelligence)
                        .where(YouTubeChannelIntelligence.tier == "hot")
                    )
                    or 0
                ),
                "warm_channels": int(
                    db.scalar(
                        select(func.count())
                        .select_from(YouTubeChannelIntelligence)
                        .where(YouTubeChannelIntelligence.tier == "warm")
                    )
                    or 0
                ),
                "dormant_channels": int(
                    db.scalar(
                        select(func.count())
                        .select_from(YouTubeChannelIntelligence)
                        .where(YouTubeChannelIntelligence.tier == "dormant")
                    )
                    or 0
                ),
                "domain_signals": int(db.scalar(select(func.count()).select_from(YouTubeDomainSignal)) or 0),
                "measured_15d": int(
                    db.scalar(
                        select(func.count())
                        .select_from(YouTubeDomainSignal)
                        .where(
                            YouTubeDomainSignal.measured_15d.is_(True),
                            YouTubeDomainSignal.verified_30d.is_(False),
                        )
                    )
                    or 0
                ),
                "local_dropped_matches": int(
                    db.scalar(select(func.count()).select_from(DroppedDomainMatch)) or 0
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
            "quota": youtube_quota_snapshot(db, settings),
            "latest_runs": latest_youtube_runs,
        }

        latest_digest = db.scalar(
            select(RunLog).where(RunLog.job == "daily_digest").order_by(RunLog.started_at.desc()).limit(1)
        )
        if latest_digest is not None:
            digest_counters = latest_digest.counters if isinstance(latest_digest.counters, dict) else {}
            email_summary["latest_digest"] = {
                "status": latest_digest.status,
                "emailed": int(digest_counters.get("emailed") or 0),
                "finished_at": (
                    latest_digest.finished_at.isoformat() if latest_digest.finished_at is not None else None
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
        "database_storage": database_storage,
        "scheduler": bool(scheduler and scheduler.running),
        "email": email_summary,
        "link_hunter_enabled": settings.link_hunter_enabled,
        "dataforseo_configured": settings.dataforseo_enabled,
        "youtube": youtube_summary,
        "web_intelligence": web_intelligence_summary,
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
) -> dict[str, object]:
    raise HTTPException(status_code=410, detail="Web Link Hunter has been retired")


def _youtube_stage_conditions(tier: str) -> tuple[object, ...]:
    """Return the exact, checkpoint-specific rules used by cards and counts."""
    if tier == "day3":
        return (
            Candidate.evaluation_stage == "day3",
            Candidate.day3_monthly_views >= settings.watchlist_monthly_views,
            Candidate.day3_monthly_views <= _YOUTUBE_VISIBLE_MAXIMUM,
        )
    if tier == "day7":
        return (
            Candidate.evaluation_stage == "day7",
            Candidate.day7_monthly_views >= settings.watchlist_monthly_views,
            Candidate.day7_monthly_views <= _YOUTUBE_VISIBLE_MAXIMUM,
        )
    if tier == "low":
        return (
            Candidate.evaluation_stage == "day7",
            Candidate.day7_monthly_views >= _YOUTUBE_RESERVE_MINIMUM,
            Candidate.day7_monthly_views < settings.watchlist_monthly_views,
        )
    return (
        Candidate.evaluation_stage == "day0",
        Candidate.start_monthly_views >= settings.watchlist_monthly_views,
        Candidate.start_monthly_views <= _YOUTUBE_VISIBLE_MAXIMUM,
    )


def _youtube_result_status(
    candidate: Candidate,
    domain: Domain,
    signal: YouTubeDomainSignal,
    tier: str,
) -> dict[str, str]:
    if tier == "watchlist":
        return {"label": "Waiting for Day 3", "class": "review"}
    if tier == "day3":
        return {"label": "Day 3 checked", "class": "review"}
    if tier == "low":
        return {"label": "10k–20k value play", "class": "value"}

    day7_stable = bool(
        candidate.day3_monthly_views > 0
        and candidate.day7_monthly_views >= round(candidate.day3_monthly_views * 0.5)
        and candidate.day7_monthly_views <= round(candidate.day3_monthly_views * 2.0)
    )
    if domain.availability_status != "available":
        return {"label": "Availability pending", "class": "pending"}
    if not day7_stable:
        return {"label": "Hold — unstable", "class": "hold"}
    if (
        candidate.day7_monthly_views >= settings.priority_monthly_views
        and signal.buy_score >= YOUTUBE_PRIORITY_BUY_SCORE
    ):
        return {"label": "Priority", "class": "priority"}
    if (
        candidate.day7_monthly_views >= settings.qualified_monthly_views
        and signal.buy_score >= YOUTUBE_QUALIFIED_BUY_SCORE
    ):
        return {"label": "Qualified", "class": "qualified"}
    return {"label": "Reviewed", "class": "reviewed"}


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    tier: ResultTier = "watchlist",
    view: str | None = None,
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    del view  # Old bookmarked URLs now land on the YouTube-only dashboard.
    selected_tier = tier if tier in {"watchlist", "day3", "day7", "low"} else "watchlist"
    _, visit_baseline, current_timestamp = _dashboard_visit_window(request)

    common_conditions = (
        Candidate.tier != "rejected",
        ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
        Domain.availability_status == "available",
        Domain.availability_source == "porkbun",
        Domain.premium.is_(False),
        Candidate.evaluation_started_at.is_not(None),
        YouTubeDomainSignal.model_version >= 4,
        YouTubeDomainSignal.click_eligible_exposure > 0,
        YouTubeDomainSignal.buy_score > 0,
        YouTubeDomainSignal.monthly_revenue_high_usd > 0,
        YouTubeDomainSignal.spike_video_count == 0,
    )
    candidate_statement = (
        select(Candidate, Domain, Video, YouTubeDomainSignal)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
        .join(YouTubeDomainSignal, YouTubeDomainSignal.domain_id == Candidate.domain_id)
        .where(*common_conditions, *_youtube_stage_conditions(selected_tier))
        .order_by(
            case(
                (Domain.availability_status == "available", 0),
                (Domain.availability_status == "likely_available", 1),
                else_=2,
            ),
            YouTubeDomainSignal.buy_score.desc(),
            YouTubeDomainSignal.monthly_revenue_high_usd.desc(),
            Candidate.monthly_views.desc(),
        )
        .limit(100)
    )
    candidate_rows = db.execute(candidate_statement).all()

    stage_counts: dict[str, int] = {}
    for stage in ("watchlist", "day3", "day7", "low"):
        stage_counts[stage] = int(
            db.scalar(
                select(func.count())
                .select_from(Candidate)
                .join(Domain, Domain.id == Candidate.domain_id)
                .join(YouTubeDomainSignal, YouTubeDomainSignal.domain_id == Candidate.domain_id)
                .where(*common_conditions, *_youtube_stage_conditions(stage))
            )
            or 0
        )

    youtube_jobs = (
        "youtube_discovery",
        "youtube_channel_fanout",
        "availability_checks",
        "view_snapshots",
        "dropped_feeds",
        "dropped_youtube_search",
        "youtube_intelligence",
    )
    latest_runs = db.scalars(
        select(RunLog)
        .where(RunLog.job.in_(youtube_jobs))
        .order_by(RunLog.started_at.desc())
        .limit(8)
    ).all()
    last_youtube_success = db.scalar(
        select(RunLog)
        .where(
            RunLog.job.in_(youtube_jobs),
            RunLog.status.in_(("complete", "partial")),
        )
        .order_by(RunLog.finished_at.desc())
        .limit(1)
    )
    crawler_running = bool(settings.scheduler_enabled)
    if last_youtube_success is not None and last_youtube_success.finished_at is not None:
        finished_at = last_youtube_success.finished_at
        if finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)
        crawler_running = crawler_running and datetime.now(UTC) - finished_at < timedelta(hours=12)

    result_status_by_domain = {
        domain.id: _youtube_result_status(candidate, domain, signal, selected_tier)
        for candidate, domain, _, signal in candidate_rows
    }
    bought_domain_count = int(
        db.scalar(
            select(func.count()).select_from(BoughtDomain).where(BoughtDomain.source_system == "youtube")
        )
        or 0
    )
    stage_copy = {
        "watchlist": {
            "label": "Watchlist",
            "short": "Before Day 3",
            "heading": "Watchlist",
            "description": "Registrar-confirmed 20k+ domains waiting for their Day 3 comparison.",
        },
        "day3": {
            "label": "3 Day Results",
            "short": "First comparison",
            "heading": "3 Day Results",
            "description": "Available 20k+ domains after the first traffic recheck.",
        },
        "day7": {
            "label": "7+ Day Results",
            "short": "Full review",
            "heading": "7+ Day Results",
            "description": "Available domains with a completed week review. Final rankings are decided here.",
        },
        "low": {
            "label": "10k–20k",
            "short": "7+ day value plays",
            "heading": "10k–20k Value Plays",
            "description": "Only available lower-band domains that completed the full 7+ day review.",
        },
    }

    return_to = request.url.path
    if request.url.query:
        return_to += f"?{request.url.query}"
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "result_tier": selected_tier,
            "candidate_rows": candidate_rows,
            "stage_counts": stage_counts,
            "stage_copy": stage_copy,
            "selected_stage": stage_copy[selected_tier],
            "result_status_by_domain": result_status_by_domain,
            "latest_runs": latest_runs,
            "last_youtube_success": last_youtube_success,
            "crawler_running": crawler_running,
            "bought_domain_count": bought_domain_count,
            "return_to": return_to,
            "registrar_enabled": settings.registrar_enabled,
            "watch_threshold": settings.watchlist_monthly_views,
            "qualified_threshold": settings.qualified_monthly_views,
            "priority_threshold": settings.priority_monthly_views,
            "reserve_threshold": _YOUTUBE_RESERVE_MINIMUM,
            "visible_maximum": _YOUTUBE_VISIBLE_MAXIMUM,
        },
    )
    _set_dashboard_visit_cookies(
        response,
        baseline=visit_baseline,
        current_timestamp=current_timestamp,
    )
    return response


@app.get("/bought", response_class=HTMLResponse)
def bought_domains_dashboard(
    request: Request,
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = db.scalars(
        select(BoughtDomain)
        .where(BoughtDomain.source_system == "youtube")
        .order_by(BoughtDomain.purchased_at.desc(), BoughtDomain.id.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="bought.html",
        context={"bought_domains": rows},
    )


def _retired_dashboard(
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
            .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
            .where(
                Candidate.tier != "rejected",
                ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
                Domain.availability_status.notin_(_HIDDEN_YOUTUBE_AVAILABILITY),
                or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
            )
        )
        if tier == "new":
            candidate_statement = candidate_statement.where(Candidate.updated_at >= new_since)
        elif tier in {"measured", "day7"}:
            candidate_statement = candidate_statement.where(Candidate.evaluation_stage == "day7")
        elif tier == "day3":
            candidate_statement = candidate_statement.where(Candidate.evaluation_stage == "day3")
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
            ).limit(100)
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
            select(Candidate.tier, func.count())
            .join(Domain, Domain.id == Candidate.domain_id)
            .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
            .where(
                ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
                Domain.availability_status.notin_(_HIDDEN_YOUTUBE_AVAILABILITY),
                or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
            )
            .group_by(Candidate.tier)
        ).all()
    }
    web_tier_counts = {
        tier_name: int(count)
        for tier_name, count in db.execute(
            select(Opportunity.tier, func.count())
            .join(Domain, Domain.id == Opportunity.domain_id)
            .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
            .where(
                ~Domain.id.in_(select(BoughtDomain.domain_id)),
                Domain.availability_status.notin_(_HIDDEN_WEB_AVAILABILITY),
                or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
            )
            .group_by(Opportunity.tier)
        ).all()
    }
    youtube_new_count = (
        db.scalar(
            select(func.count())
            .select_from(Candidate)
            .join(Domain, Domain.id == Candidate.domain_id)
            .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
            .where(
                Candidate.tier != "rejected",
                ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
                Candidate.updated_at >= new_since,
                Domain.availability_status.notin_(_HIDDEN_YOUTUBE_AVAILABILITY),
                or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
            )
        )
        or 0
    )

    def youtube_stage_count(stage: str) -> int:
        return int(
            db.scalar(
                select(func.count())
                .select_from(Candidate)
                .join(Domain, Domain.id == Candidate.domain_id)
                .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
                .where(
                    Candidate.tier != "rejected",
                    ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
                    Candidate.evaluation_stage == stage,
                    Domain.availability_status.notin_(_HIDDEN_YOUTUBE_AVAILABILITY),
                    or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
                )
            )
            or 0
        )

    youtube_day3_count = youtube_stage_count("day3")
    youtube_day7_count = youtube_stage_count("day7")
    web_new_count = (
        db.scalar(
            select(func.count())
            .select_from(Opportunity)
            .join(Domain, Domain.id == Opportunity.domain_id)
            .outerjoin(WebScreening, WebScreening.domain_name == Domain.name)
            .where(
                Opportunity.updated_at >= new_since,
                ~Domain.id.in_(select(BoughtDomain.domain_id)),
                Domain.availability_status.notin_(_HIDDEN_WEB_AVAILABILITY),
                or_(WebScreening.id.is_(None), WebScreening.status != "blocked"),
            )
        )
        or 0
    )
    qualified = youtube_tier_counts.get("qualified", 0) + youtube_tier_counts.get("priority", 0)
    youtube_results = sum(
        youtube_tier_counts.get(tier_name, 0)
        for tier_name in ("priority", "qualified", "watchlist", "pending")
    )
    crawler_videos = db.scalar(select(func.count()).select_from(Video)) or 0
    crawler_domains = db.scalar(select(func.count()).select_from(Domain)) or 0
    exact_links = (
        db.scalar(select(func.count()).select_from(VideoDomain).where(VideoDomain.active.is_(True))) or 0
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
    youtube_hot_channels = (
        db.scalar(
            select(func.count())
            .select_from(YouTubeChannelIntelligence)
            .where(YouTubeChannelIntelligence.tier == "hot")
        )
        or 0
    )
    youtube_warm_channels = (
        db.scalar(
            select(func.count())
            .select_from(YouTubeChannelIntelligence)
            .where(YouTubeChannelIntelligence.tier == "warm")
        )
        or 0
    )
    youtube_domain_signals = db.scalar(select(func.count()).select_from(YouTubeDomainSignal)) or 0
    youtube_local_matches = db.scalar(select(func.count()).select_from(DroppedDomainMatch)) or 0
    youtube_quota = youtube_quota_snapshot(db, settings)
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
        db.scalar(select(func.count()).select_from(SourceLink).where(SourceLink.provider_live.is_(True))) or 0
    )
    web_screened = db.scalar(select(func.count()).select_from(WebScreening)) or 0
    web_screened_blocked = (
        db.scalar(select(func.count()).select_from(WebScreening).where(WebScreening.status == "blocked")) or 0
    )
    web_summary_indexed = db.scalar(select(func.count()).select_from(BacklinkSummary)) or 0
    web_money_cases = db.scalar(select(func.count()).select_from(OpportunityEconomics)) or 0

    displayed_rows = candidate_rows if view == "youtube" else web_evidence_rows
    displayed_domain_ids = [row[1].id for row in displayed_rows]
    decisions = []
    if displayed_domain_ids and view == "web":
        decisions = db.scalars(
            select(DashboardDecision).where(
                DashboardDecision.system == view,
                DashboardDecision.domain_id.in_(displayed_domain_ids),
            )
        ).all()
    decision_by_domain = {decision.domain_id: decision.status for decision in decisions}
    youtube_signal_by_domain: dict[int, YouTubeDomainSignal] = {}
    if view == "youtube" and displayed_domain_ids:
        youtube_signals = db.scalars(
            select(YouTubeDomainSignal).where(YouTubeDomainSignal.domain_id.in_(displayed_domain_ids))
        ).all()
        youtube_signal_by_domain = {signal.domain_id: signal for signal in youtube_signals}
    decision_counts = (
        {
            decision_status: int(count)
            for decision_status, count in db.execute(
                select(DashboardDecision.status, func.count())
                .where(DashboardDecision.system == "web")
                .group_by(DashboardDecision.status)
            ).all()
        }
        if view == "web"
        else {}
    )
    bought_domain_count = int(
        db.scalar(
            select(func.count()).select_from(BoughtDomain).where(BoughtDomain.source_system == "youtube")
        )
        or 0
    )

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
            "youtube_day3_count": youtube_day3_count,
            "youtube_day7_count": youtube_day7_count,
            "web_new_count": web_new_count,
            "new_since": new_since,
            "decision_by_domain": decision_by_domain,
            "youtube_signal_by_domain": youtube_signal_by_domain,
            "decision_counts": decision_counts,
            "bought_domain_count": bought_domain_count,
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
            "youtube_hot_channels": youtube_hot_channels,
            "youtube_warm_channels": youtube_warm_channels,
            "youtube_domain_signals": youtube_domain_signals,
            "youtube_local_matches": youtube_local_matches,
            "youtube_quota": youtube_quota,
            "adaptive_refresh_due": adaptive_refresh_due,
            "dropped_ingested": dropped_ingested,
            "cumulative_videos": settings.legacy_videos_checked + crawler_videos,
            "cumulative_domains": settings.legacy_domains_checked + crawler_domains,
            "cumulative_dropped": settings.legacy_dropped_checked + dropped_ingested,
            "web_opportunities": web_opportunities,
            "web_source_sites": web_source_sites,
            "web_source_pages": web_source_pages,
            "web_source_links": web_source_links,
            "web_screened": web_screened,
            "web_screened_blocked": web_screened_blocked,
            "web_summary_indexed": web_summary_indexed,
            "web_money_cases": web_money_cases,
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


@app.get("/export/subscribers.csv")
def export_subscribers(
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> Response:
    rows = db.scalars(
        select(EmailSubscriber).order_by(
            EmailSubscriber.created_at.desc(),
            EmailSubscriber.id.desc(),
        )
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["site", "email", "status", "source", "consent_version", "consented_at"])
    for row in rows:
        writer.writerow(
            [
                row.site_key,
                row.email,
                row.status,
                row.source,
                row.consent_version,
                row.consented_at.isoformat(),
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="email-subscribers.csv"'},
    )


@app.get("/export/contact-messages.csv")
def export_contact_messages(
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> Response:
    rows = db.scalars(
        select(ContactMessage).order_by(
            ContactMessage.created_at.desc(),
            ContactMessage.id.desc(),
        )
    ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["site", "name", "email", "message", "status", "created_at"])
    for row in rows:
        writer.writerow(
            [
                row.site_key,
                row.name,
                row.email,
                row.message,
                row.status,
                row.created_at.isoformat(),
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contact-messages.csv"'},
    )


@app.get("/export/candidates.csv")
def export_candidates(
    request: Request,
    tier: ResultTier = "all",
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> Response:
    statement = (
        select(Candidate, Domain, Video, YouTubeDomainSignal)
        .join(Domain, Domain.id == Candidate.domain_id)
        .join(Video, Video.id == Candidate.best_video_id)
        .join(YouTubeDomainSignal, YouTubeDomainSignal.domain_id == Domain.id)
        .where(
            Candidate.tier != "rejected",
            ~Candidate.domain_id.in_(select(BoughtDomain.domain_id)),
            Domain.availability_status == "available",
            Domain.availability_source == "porkbun",
            Domain.premium.is_(False),
            Candidate.evaluation_started_at.is_not(None),
            YouTubeDomainSignal.model_version >= 4,
            YouTubeDomainSignal.click_eligible_exposure > 0,
            YouTubeDomainSignal.buy_score > 0,
            YouTubeDomainSignal.monthly_revenue_high_usd > 0,
            YouTubeDomainSignal.spike_video_count == 0,
        )
    )
    if tier == "all":
        statement = statement.where(
            or_(
                *[
                    and_(*_youtube_stage_conditions(stage))
                    for stage in ("watchlist", "day3", "day7", "low")
                ]
            )
        )
    elif tier == "new":
        new_since, _, _ = _dashboard_visit_window(request)
        statement = statement.where(
            Candidate.tier != "rejected",
            Candidate.updated_at >= new_since,
        )
    elif tier in {"watchlist", "day3", "day7", "low"}:
        statement = statement.where(*_youtube_stage_conditions(tier))
    elif tier == "measured":
        statement = statement.where(*_youtube_stage_conditions("day7"))
    else:
        statement = statement.where(Candidate.tier == tier)
    rows = db.execute(statement.order_by(Candidate.score.desc())).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "domain",
            "tier",
            "current_monthly_run_rate",
            "start_monthly_run_rate",
            "day3_monthly_run_rate",
            "day7_monthly_run_rate",
            "evaluation_stage",
            "review_started_at",
            "trend_percent",
            "buy_ready",
            "verified_30_day_window",
            "observation_days",
            "evidence_score",
            "availability",
            "registration_price_usd",
            "linked_videos",
            "exact_links",
            "best_video_title",
            "best_video_url",
            "traffic_confidence",
            "click_eligible_monthly_exposure",
            "expected_outbound_clicks_monthly",
            "monthly_revenue_low_usd",
            "monthly_revenue_high_usd",
            "suggested_purchase_ceiling_usd",
            "buy_score",
            "monetization_route",
        ]
    )
    for candidate, domain, video, signal in rows:
        writer.writerow(
            [
                domain.name,
                candidate.tier,
                candidate.monthly_views,
                candidate.start_monthly_views,
                candidate.day3_monthly_views,
                candidate.day7_monthly_views,
                candidate.evaluation_stage,
                candidate.evaluation_started_at.isoformat()
                if candidate.evaluation_started_at is not None
                else "",
                candidate.trend_percent,
                candidate.buy_ready,
                candidate.verified_30d,
                candidate.observation_days,
                candidate.score,
                domain.availability_status,
                domain.registrar_price_usd or "",
                candidate.video_count,
                candidate.link_count,
                video.title,
                f"https://www.youtube.com/watch?v={video.id}",
                signal.traffic_confidence if signal is not None else "collecting",
                signal.click_eligible_exposure if signal is not None else 0,
                signal.expected_clicks_monthly if signal is not None else 0,
                signal.monthly_revenue_low_usd if signal is not None else 0.0,
                signal.monthly_revenue_high_usd if signal is not None else 0.0,
                signal.max_purchase_price_usd if signal is not None else 0.0,
                signal.buy_score if signal is not None else 0.0,
                signal.monetization_route if signal is not None else "",
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
            "clickability_score",
            "semantic_location",
            "link_survival_days",
            "expected_clicks_monthly",
            "monthly_revenue_low_usd",
            "monthly_revenue_high_usd",
            "recommended_max_purchase_usd",
            "estimated_payback_months",
            "monetization_route",
            "economics_confidence",
            "risk_score",
            "safety_flags",
        ]
    )
    for opportunity, domain, page, site, link, verification, economics, observation in rows:
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
                observation.clickability_score if observation else "",
                observation.semantic_location if observation else "",
                observation.survival_days if observation else "",
                economics.expected_clicks_monthly if economics else "",
                economics.monthly_revenue_low_usd if economics else "",
                economics.monthly_revenue_high_usd if economics else "",
                economics.max_purchase_price_usd if economics else "",
                economics.estimated_payback_months if economics else "",
                economics.monetization_route if economics else "",
                economics.confidence if economics else "",
                economics.risk_score if economics else "",
                " | ".join(economics.safety_flags or []) if economics else "",
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
    if system != "web":
        raise HTTPException(
            status_code=400,
            detail="YouTube results use the permanent Bought/Delete actions",
        )
    if db.get(Domain, domain_id) is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    if system == "youtube":
        result_exists = db.scalar(
            select(func.count()).select_from(Candidate).where(Candidate.domain_id == domain_id)
        )
    else:
        result_exists = db.scalar(
            select(func.count()).select_from(Opportunity).where(Opportunity.domain_id == domain_id)
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


@app.post("/admin/youtube-domain-action")
def apply_youtube_domain_action(
    domain_id: int = Form(),
    domain_action: YouTubeDomainAction = Form(),
    return_to: str = Form(default="/?view=youtube"),
    _: str = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        if domain_action == "bought":
            move_youtube_domain_to_bought(db, domain_id)
        else:
            hard_delete_domain(db, domain_id)
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=_safe_next_path(return_to), status_code=303)


@app.post("/api/link-hunter/proof")
def trigger_link_hunter_proof(_: None = Depends(require_admin_token)) -> dict[str, object]:
    raise HTTPException(status_code=410, detail="Web Link Hunter has been retired")


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
