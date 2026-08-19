from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.commoncrawl import CommonCrawlClient
from app.models import DroppedDomain, ProviderQuery, utcnow


def _candidate_drops(db: Session, limit: int) -> list[DroppedDomain]:
    commoncrawl_done = set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "commoncrawl",
                ProviderQuery.endpoint == "url_index",
                ProviderQuery.status == "complete",
            )
        ).all()
    )
    dataforseo_done = set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )
    recent = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(250)
    ).all()
    eligible = [
        item
        for item in recent
        if item.name not in commoncrawl_done and item.name not in dataforseo_done
    ]
    eligible.sort(
        key=lambda item: (
            0 if item.name.endswith(".com") else 1,
            len(item.name),
            -item.first_seen_at.timestamp(),
        )
    )
    return eligible[:limit]


def run_commoncrawl_prefilter(
    db: Session,
    *,
    batch_size: int = 10,
    index_count: int = 2,
    client: CommonCrawlClient | None = None,
) -> dict[str, Any]:
    """Cache free historical-presence signals before any paid backlink proof."""
    if batch_size < 1 or batch_size > 25:
        raise ValueError("Common Crawl prefilter batch must be between 1 and 25")
    if index_count < 1 or index_count > 5:
        raise ValueError("Common Crawl prefilter index count must be between 1 and 5")

    candidates = _candidate_drops(db, batch_size)
    counters: dict[str, Any] = {
        "candidates": len(candidates),
        "checked": 0,
        "with_capture": 0,
        "without_capture": 0,
        "index_requests": 0,
        "provider_cost_usd": 0.0,
        "errors": 0,
        "error_details": [],
    }
    if not candidates:
        return counters

    cc = client or CommonCrawlClient()
    try:
        indexes = cc.latest_indexes(index_count)
    except Exception as exc:
        counters["errors"] = 1
        counters["error_details"] = [f"collection list: {exc}"[:500]]
        return counters

    for dropped in candidates:
        query = ProviderQuery(
            provider="commoncrawl",
            endpoint="url_index",
            target=dropped.name,
            status="running",
            cost_usd=0.0,
        )
        db.add(query)
        db.commit()
        db.refresh(query)
        try:
            presence = cc.domain_presence(dropped.name, indexes)
            query.status = "complete"
            query.provider_task_id = ",".join(presence.indexes_with_capture)[:64]
            query.row_count = presence.capture_index_count
            query.completed_at = utcnow()
            query.error = None
            counters["checked"] += 1
            counters["index_requests"] += len(presence.indexes_checked)
            if presence.capture_index_count:
                counters["with_capture"] += 1
            else:
                counters["without_capture"] += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            current = db.get(ProviderQuery, query.id)
            if current is not None:
                current.status = "failed"
                current.error = str(exc)[:2000]
                current.completed_at = utcnow()
                db.commit()
            counters["errors"] += 1
            if len(counters["error_details"]) < 5:
                counters["error_details"].append(f"{dropped.name}: {exc}"[:500])

    return counters
