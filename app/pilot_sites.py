from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


PILOT_SESSION_COOKIE = "expandosaurus_pilot_sid"
PILOT_SESSION_SECONDS = 180 * 24 * 60 * 60

pilot_metadata = MetaData()
pilot_site_events = Table(
    "pilot_site_events",
    pilot_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("domain", String(255), nullable=False, index=True),
    Column("event_type", String(40), nullable=False, index=True),
    Column("path", String(1000), nullable=False, default="/"),
    Column("session_id", String(80), nullable=False, index=True),
    Column("referrer", Text, nullable=True),
    Column("offer_id", String(80), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True),
)


@dataclass(frozen=True)
class PilotSite:
    domain: str
    site_name: str
    eyebrow: str
    headline: str
    subheadline: str
    primary_cta: str
    offer_env: str
    category: str
    disclosure: str = ""
    legacy_paths: tuple[str, ...] = ()


PILOT_SITES: dict[str, PilotSite] = {
    "craftsheaven.club": PilotSite(
        domain="craftsheaven.club",
        site_name="Crafts Heaven",
        eyebrow="WOODWORKING & DIY",
        headline="Woodworking Plans for Your Next Project",
        subheadline=(
            "Practical project ideas, plan resources and woodworking guides for makers "
            "who would rather build something than scroll past it."
        ),
        primary_cta="See Recommended Woodworking Plans",
        offer_env="CRAFTSHEAVEN_OFFER_URL",
        category="woodworking",
        legacy_paths=("/woodworkingplans", "/woodworking-plans"),
    ),
    "satvic.yoga": PilotSite(
        domain="satvic.yoga",
        site_name="Satvic Yoga Guide",
        eyebrow="YOGA • BALANCE • WELLBEING",
        headline="A Simpler Path Into Satvic Yoga",
        subheadline=(
            "Independent guides and carefully selected resources for yoga, breathwork, "
            "mindful movement and a more balanced daily practice."
        ),
        primary_cta="Explore Recommended Yoga Resources",
        offer_env="SATVIC_YOGA_OFFER_URL",
        category="yoga",
    ),
    "teamgerardiperformance.com": PilotSite(
        domain="teamgerardiperformance.com",
        site_name="Online Training Guide",
        eyebrow="ONLINE PERSONAL TRAINING",
        headline="Looking for an Online Personal Training Program?",
        subheadline=(
            "Compare current online coaching and training options designed around "
            "strength, consistency and measurable progress."
        ),
        primary_cta="Compare Training Options",
        offer_env="TEAM_GERARDI_OFFER_URL",
        category="fitness",
        disclosure=(
            "Independent site. Not affiliated with, endorsed by, or operated by Gerardi "
            "Performance or any previous operator of this domain."
        ),
    ),
}


def normalize_host(value: str) -> str:
    host = (value or "").strip().lower().split(":", 1)[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def get_pilot_site(host: str) -> PilotSite | None:
    return PILOT_SITES.get(normalize_host(host))


def pilot_sites_enabled() -> bool:
    raw = os.getenv("PILOT_SITES_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def ensure_pilot_schema(engine: Engine) -> None:
    pilot_metadata.create_all(bind=engine, tables=[pilot_site_events], checkfirst=True)


def offer_url(site: PilotSite) -> str:
    value = os.getenv(site.offer_env, "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return value


def safe_offer_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", value or "")[:64]
    return cleaned or "main"


def record_pilot_event(
    db: Session,
    *,
    site: PilotSite,
    event_type: str,
    path: str,
    session_id: str,
    referrer: str | None = None,
    offer_id: str | None = None,
) -> None:
    db.execute(
        insert(pilot_site_events).values(
            domain=site.domain,
            event_type=event_type[:40],
            path=(path or "/")[:1000],
            session_id=session_id[:80],
            referrer=(referrer or "")[:2000] or None,
            offer_id=(offer_id or "")[:80] or None,
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
