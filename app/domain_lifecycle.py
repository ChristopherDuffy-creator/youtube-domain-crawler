from __future__ import annotations

import hashlib
import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    BoughtDomain,
    Candidate,
    DashboardDecision,
    DeletedDomainFingerprint,
    Domain,
    DroppedDomain,
    PilotSiteEvent,
    ProviderQuery,
    Video,
    VideoDomain,
    VideoRefreshState,
    YouTubeDomainSignal,
    utcnow,
)


def _normalise_domain_name(name: str) -> str:
    return name.strip().lower().strip(".")


def domain_fingerprint(name: str) -> str:
    return hashlib.sha256(_normalise_domain_name(name).encode("utf-8")).hexdigest()


def suppressed_domain_names(db: Session, names: list[str] | set[str]) -> set[str]:
    """Return plaintext inputs whose one-way deletion tombstone exists."""
    normalised = {_normalise_domain_name(name) for name in names if name.strip()}
    if not normalised:
        return set()
    by_hash = {domain_fingerprint(name): name for name in normalised}
    found: set[str] = set()
    hashes = list(by_hash)
    for start in range(0, len(hashes), 500):
        chunk = hashes[start : start + 500]
        found.update(
            db.scalars(
                select(DeletedDomainFingerprint.domain_hash).where(
                    DeletedDomainFingerprint.domain_hash.in_(chunk)
                )
            ).all()
        )
    return {by_hash[value] for value in found}


def bought_domain_names(db: Session, names: list[str] | set[str]) -> set[str]:
    normalised = {_normalise_domain_name(name) for name in names if name.strip()}
    if not normalised:
        return set()
    found: set[str] = set()
    values = list(normalised)
    for start in range(0, len(values), 500):
        found.update(
            db.scalars(
                select(BoughtDomain.domain_name).where(
                    BoughtDomain.domain_name.in_(values[start : start + 500])
                )
            ).all()
        )
    return found


def get_or_create_unsuppressed_domain(db: Session, name: str) -> Domain | None:
    normalised = _normalise_domain_name(name)
    if normalised in suppressed_domain_names(db, {normalised}):
        return None
    if normalised in bought_domain_names(db, {normalised}):
        return None
    domain = db.scalar(select(Domain).where(Domain.name == normalised))
    if domain is None:
        domain = Domain(name=normalised)
        db.add(domain)
        db.flush()
    return domain


def scrub_domain_from_text(value: str, domain_name: str) -> str:
    """Remove a deleted domain and its URL forms from retained source text."""
    domain = _normalise_domain_name(domain_name)
    if not value or not domain:
        return value
    pattern = re.compile(
        rf"(?i)(?<![a-z0-9.-])(?:https?://)?(?:[a-z0-9-]+\.)*{re.escape(domain)}"
        r"(?:/[^\s<>\"']*)?"
    )
    return " ".join(pattern.sub("[deleted domain]", value).split())


def move_youtube_domain_to_bought(
    db: Session,
    domain_id: int,
    *,
    require_candidate: bool = True,
) -> BoughtDomain:
    """Snapshot a purchase and remove the candidate from every ranking queue."""
    domain = db.get(Domain, domain_id)
    if domain is None:
        raise LookupError("Domain not found")
    candidate = db.scalar(select(Candidate).where(Candidate.domain_id == domain_id))
    if candidate is None and require_candidate:
        raise LookupError("YouTube candidate not found")
    signal = db.get(YouTubeDomainSignal, domain_id)
    bought = db.scalar(select(BoughtDomain).where(BoughtDomain.domain_id == domain_id))
    if bought is None:
        bought = BoughtDomain(
            domain_id=domain.id,
            domain_name=domain.name,
            source_system="youtube",
            original_tier=candidate.tier if candidate is not None else "pending",
            monthly_views=candidate.monthly_views if candidate is not None else 0,
            start_monthly_views=candidate.start_monthly_views if candidate is not None else 0,
            day3_monthly_views=candidate.day3_monthly_views if candidate is not None else 0,
            day7_monthly_views=candidate.day7_monthly_views if candidate is not None else 0,
            evidence_score=candidate.score if candidate is not None else 0.0,
            buy_score=signal.buy_score if signal is not None else 0.0,
            monthly_revenue_low_usd=(signal.monthly_revenue_low_usd if signal is not None else 0.0),
            monthly_revenue_high_usd=(signal.monthly_revenue_high_usd if signal is not None else 0.0),
            suggested_purchase_ceiling_usd=(signal.max_purchase_price_usd if signal is not None else 0.0),
            registrar_price_usd=domain.registrar_price_usd,
            best_video_id=candidate.best_video_id if candidate is not None else None,
        )
        db.add(bought)
    else:
        bought.updated_at = utcnow()

    domain.excluded_reason = "bought"

    db.execute(
        delete(DashboardDecision).where(
            DashboardDecision.system == "youtube",
            DashboardDecision.domain_id == domain_id,
        )
    )
    if candidate is not None:
        db.delete(candidate)
    db.flush()
    return bought


def migrate_legacy_youtube_bought_decisions(db: Session) -> int:
    """Move old reversible Bought labels into the permanent purchase table."""
    domain_ids = db.scalars(
        select(DashboardDecision.domain_id).where(
            DashboardDecision.system == "youtube",
            DashboardDecision.status == "bought",
        )
    ).all()
    migrated = 0
    for domain_id in domain_ids:
        if db.get(Domain, domain_id) is None:
            db.execute(
                delete(DashboardDecision).where(
                    DashboardDecision.system == "youtube",
                    DashboardDecision.domain_id == domain_id,
                )
            )
            continue
        move_youtube_domain_to_bought(db, domain_id, require_candidate=False)
        migrated += 1
    db.commit()
    return migrated


def hard_delete_domain(
    db: Session,
    domain_id: int,
    *,
    require_candidate: bool = True,
) -> str:
    """Delete a domain graph and retain only a non-reversible suppression hash."""
    domain = db.get(Domain, domain_id)
    if domain is None:
        raise LookupError("Domain not found")
    if require_candidate and db.scalar(select(Candidate.id).where(Candidate.domain_id == domain_id)) is None:
        raise LookupError("YouTube candidate not found")
    domain_name = domain.name
    fingerprint = domain_fingerprint(domain_name)
    if db.get(DeletedDomainFingerprint, fingerprint) is None:
        db.add(DeletedDomainFingerprint(domain_hash=fingerprint))

    videos = db.scalars(
        select(Video)
        .join(VideoDomain, VideoDomain.video_id == Video.id)
        .where(VideoDomain.domain_id == domain_id)
        .distinct()
    ).all()
    video_ids = [video.id for video in videos]
    for video in videos:
        video.description = scrub_domain_from_text(video.description, domain_name)

    db.execute(delete(ProviderQuery).where(ProviderQuery.target == domain_name))
    db.execute(delete(PilotSiteEvent).where(PilotSiteEvent.domain == domain_name))
    db.execute(delete(DroppedDomain).where(DroppedDomain.name == domain_name))
    db.delete(domain)
    db.flush()

    for video_id in video_ids:
        active_links = db.scalar(
            select(func.count())
            .select_from(VideoDomain)
            .where(VideoDomain.video_id == video_id, VideoDomain.active.is_(True))
        )
        if not active_links:
            state = db.get(VideoRefreshState, video_id)
            if state is not None:
                db.delete(state)
    db.flush()
    return domain_name
