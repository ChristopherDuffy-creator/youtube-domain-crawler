from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.dataforseo import estimate_provider_proof_max_cost_usd
from app.models import DroppedDomain, ProviderQuery


def build_provider_proof_preview(db: Session, settings: Settings) -> dict[str, Any]:
    """Describe the next provider proof without making any network/provider calls."""
    already_checked = set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )
    recent_drops = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(250)
    ).all()
    targets = [drop.name for drop in recent_drops if drop.name not in already_checked][
        : settings.link_hunter_proof_batch_size
    ]

    estimated_max_cost = estimate_provider_proof_max_cost_usd(
        len(targets), settings.link_hunter_backlinks_per_domain
    )
    max_source_pages = len(targets) * settings.link_hunter_backlinks_per_domain

    return {
        "targets": targets,
        "target_count": len(targets),
        "backlinks_per_domain": settings.link_hunter_backlinks_per_domain,
        "max_source_pages": max_source_pages,
        "estimated_max_cost_usd": estimated_max_cost,
        "configured_cost_cap_usd": settings.link_hunter_proof_max_cost_usd,
        "within_cost_cap": estimated_max_cost <= settings.link_hunter_proof_max_cost_usd,
        "dataforseo_configured": settings.dataforseo_enabled,
        "link_hunter_enabled": settings.link_hunter_enabled,
        "paid_requests_made": 0,
    }
