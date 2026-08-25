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
    feature_cards: tuple[tuple[str, str], ...]
    intro_title: str
    intro_text: str
    guide_title: str
    guide_intro: str
    disclosure: str = ""
    legacy_paths: tuple[str, ...] = ()
    indexable: bool = True


PILOT_SITES: dict[str, PilotSite] = {
    "craftsheaven.club": PilotSite(
        domain="craftsheaven.club",
        site_name="Crafts Heaven",
        eyebrow="WOODWORKING • PLANS • DIY",
        headline="Find a Woodworking Project Worth Building",
        subheadline=(
            "A practical starting point for makers looking for woodworking plans, project "
            "ideas and useful build resources — from simple weekend jobs to more ambitious pieces."
        ),
        primary_cta="See Recommended Woodworking Plans",
        offer_env="CRAFTSHEAVEN_OFFER_URL",
        category="woodworking",
        feature_cards=(
            (
                "Plans you can actually use",
                "Focus on clear project ideas, sensible dimensions and build resources rather than endless inspiration with no next step.",
            ),
            (
                "Pick the right difficulty",
                "Start with projects that match your tools, available time and current skill level, then work upwards.",
            ),
            (
                "Build, finish, improve",
                "Useful guidance should help with the whole job: planning, materials, assembly, sanding, finishing and the next project.",
            ),
        ),
        intro_title="You came here to make something",
        intro_text=(
            "The old links to this domain were associated with woodworking-plan intent, including "
            "the /woodworkingplans path. We preserve that topic so a visitor following an old link "
            "still lands somewhere useful instead of on an unrelated page."
        ),
        guide_title="How to choose a woodworking plan",
        guide_intro=(
            "Before paying for a plan bundle or course, check that the project matches your tools, "
            "skill level, available space and the kind of finished piece you actually want to build."
        ),
        legacy_paths=("/woodworkingplans", "/woodworking-plans"),
    ),
    "satvic.yoga": PilotSite(
        domain="satvic.yoga",
        site_name="Satvic Yoga Guide",
        eyebrow="YOGA • BALANCE • WELLBEING",
        headline="A Simpler Path Into Satvic Yoga",
        subheadline=(
            "Independent guides and carefully selected resources for yoga, breathwork, "
            "mindful movement and a calmer, more balanced daily practice."
        ),
        primary_cta="Explore Recommended Yoga Resources",
        offer_env="SATVIC_YOGA_OFFER_URL",
        category="yoga",
        feature_cards=(
            (
                "Practice, not perfection",
                "Build a routine you can repeat. Consistency, comfortable progression and awareness matter more than chasing difficult poses.",
            ),
            (
                "Breath and movement together",
                "Explore resources that treat breathing, mobility and mindful movement as parts of one practice rather than separate tricks.",
            ),
            (
                "Bring it into daily life",
                "A satvic approach can extend beyond the mat through rest, attention, food choices and habits that support steadiness and clarity.",
            ),
        ),
        intro_title="A broad tradition, approached independently",
        intro_text=(
            "Satvic or sattvic is a longstanding yogic concept. This site is an independent guide "
            "to the topic and is not presented as the website of any particular yoga school, teacher or movement."
        ),
        guide_title="Choosing a yoga resource that you will actually use",
        guide_intro=(
            "A good program should fit your current mobility, experience, schedule and goals. "
            "Look for clear progression, credible instruction and a format you can sustain consistently."
        ),
    ),
    "teamgerardiperformance.com": PilotSite(
        domain="teamgerardiperformance.com",
        site_name="Online Training Guide",
        eyebrow="ONLINE PERSONAL TRAINING",
        headline="Looking for an Online Personal Training Program?",
        subheadline=(
            "Compare current online coaching and training options around strength, consistency, "
            "accountability and measurable progress — then choose what fits you."
        ),
        primary_cta="Compare Training Options",
        offer_env="TEAM_GERARDI_OFFER_URL",
        category="fitness",
        feature_cards=(
            (
                "Compare coaching styles",
                "Some programs are highly hands-on; others are app-led or template-based. Match the level of support to what you will actually use.",
            ),
            (
                "Know what you are paying for",
                "Look at programming, check-ins, nutrition support, progress reviews, cancellation terms and whether coaching is genuinely personalised.",
            ),
            (
                "Choose around your goal",
                "Strength, fat loss, general fitness and athletic performance can require very different programming. Start with the outcome, not the brand name.",
            ),
        ),
        intro_title="An independent comparison starting point",
        intro_text=(
            "This domain previously pointed visitors toward a specific online-training offer. The current site "
            "does not continue or represent that business; it provides independent information and alternative options."
        ),
        guide_title="What to compare in an online coach",
        guide_intro=(
            "Compare the actual service rather than the sales page: programming frequency, trainer access, "
            "check-ins, nutrition support, progress reviews, cancellation terms and whether plans adapt when life changes."
        ),
        disclosure=(
            "Independent site. Not affiliated with, endorsed by, or operated by Gerardi "
            "Performance or any previous operator of this domain."
        ),
        # We want to measure surviving inbound links, not deliberately rank for
        # the active Gerardi Performance business name in search engines.
        indexable=False,
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
