from __future__ import annotations

from collections import defaultdict
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hackernews import HackerNewsSearchClient
from app.models import (
    Domain,
    DroppedDomain,
    ProviderQuery,
    SourceLink,
    SourceMetricSnapshot,
    SourcePage,
    SourceSite,
    utcnow,
)


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, tuple[str, ...]]] = []
        self._href = ""
        self._rel: tuple[str, ...] = ()
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        self._href = values.get("href", "")
        self._rel = tuple(part.lower() for part in values.get("rel", "").split() if part)
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        anchor = " ".join(" ".join(self._text).split())
        self.links.append((self._href, anchor, self._rel))
        self._href = ""
        self._rel = ()
        self._text = []


def _normalize_host(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"
    host = (urlparse(raw).hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _host_matches(value: str, domain: str) -> bool:
    host = _normalize_host(value)
    target = _normalize_host(domain)
    return bool(host and target and (host == target or host.endswith(f".{target}")))


def _exact_links(hit: dict[str, Any], domain: str) -> list[tuple[str, str, tuple[str, ...]]]:
    matches: list[tuple[str, str, tuple[str, ...]]] = []
    direct_url = str(hit.get("url") or "").strip()
    if direct_url and _host_matches(direct_url, domain):
        matches.append((direct_url, str(hit.get("title") or domain), ()))

    for field in ("story_text", "comment_text"):
        parser = _HrefParser()
        parser.feed(str(hit.get(field) or ""))
        for href, anchor, rel in parser.links:
            if _host_matches(href, domain):
                matches.append((href, anchor, rel))

    deduped: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for href, anchor, rel in matches:
        deduped.setdefault(href.strip(), (href.strip(), anchor, rel))
    return list(deduped.values())


def _get_or_create_domain(db: Session, name: str) -> Domain:
    domain = db.scalar(select(Domain).where(Domain.name == name))
    if domain is None:
        domain = Domain(name=name)
        db.add(domain)
        db.flush()
    return domain


def _get_or_create_hn_site(db: Session) -> SourceSite:
    hostname = "news.ycombinator.com"
    site = db.scalar(select(SourceSite).where(SourceSite.hostname == hostname))
    if site is None:
        site = SourceSite(hostname=hostname, source_type="hackernews")
        db.add(site)
        db.flush()
    else:
        site.source_type = "hackernews"
        site.last_seen_at = utcnow()
    return site


def _get_or_create_page(db: Session, hit: dict[str, Any]) -> SourcePage | None:
    object_id = str(hit.get("objectID") or "").strip()
    if not object_id.isdigit():
        return None
    page_url = f"https://news.ycombinator.com/item?id={object_id}"
    page = db.scalar(select(SourcePage).where(SourcePage.url == page_url))
    if page is None:
        site = _get_or_create_hn_site(db)
        page = SourcePage(site_id=site.id, url=page_url)
        db.add(page)
        db.flush()
    page.title = str(hit.get("title") or hit.get("story_title") or "Hacker News item")
    page.http_status = 200
    page.last_seen_at = utcnow()
    return page


def _save_metric(db: Session, page: SourcePage, hit: dict[str, Any]) -> None:
    captured = utcnow()
    snapshot = db.scalar(
        select(SourceMetricSnapshot).where(
            SourceMetricSnapshot.source_page_id == page.id,
            SourceMetricSnapshot.provider == "hackernews",
            SourceMetricSnapshot.capture_date == captured.date(),
        )
    )
    if snapshot is None:
        snapshot = SourceMetricSnapshot(
            source_page_id=page.id,
            provider="hackernews",
            capture_date=captured.date(),
        )
        db.add(snapshot)
    snapshot.captured_at = captured
    snapshot.raw_metrics = {
        "points": int(hit.get("points") or 0),
        "num_comments": int(hit.get("num_comments") or 0),
        "created_at_i": int(hit.get("created_at_i") or 0),
        "object_id": str(hit.get("objectID") or ""),
        "story_id": hit.get("story_id"),
        "tags": list(hit.get("_tags") or []),
    }


def _save_hit_links(
    db: Session,
    *,
    domain_name: str,
    hit: dict[str, Any],
) -> tuple[int, int]:
    matches = _exact_links(hit, domain_name)
    if not matches:
        return 0, 0
    page = _get_or_create_page(db, hit)
    if page is None:
        return 0, 0
    domain = _get_or_create_domain(db, domain_name)
    _save_metric(db, page, hit)

    created = 0
    saved = 0
    context = str(hit.get("story_title") or hit.get("title") or "")
    points = max(0, int(hit.get("points") or 0))
    comments = max(0, int(hit.get("num_comments") or 0))
    provider_rank = min(100.0, points * 1.5 + comments * 0.25)
    for href, anchor, rel in matches:
        link = db.scalar(
            select(SourceLink).where(
                SourceLink.source_page_id == page.id,
                SourceLink.domain_id == domain.id,
                SourceLink.target_url == href,
            )
        )
        if link is None:
            link = SourceLink(
                source_page_id=page.id,
                domain_id=domain.id,
                target_url=href,
            )
            db.add(link)
            created += 1
        link.anchor_text = anchor
        link.context_before = context
        link.context_after = ""
        link.semantic_location = "hn_item"
        link.dofollow = not bool({"nofollow", "ugc", "sponsored"}.intersection(rel))
        link.provider_live = True
        link.provider_rank = provider_rank
        link.last_seen_at = utcnow()
        saved += 1
    return saved, created


def _completed_targets(db: Session) -> set[str]:
    return set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "hackernews",
                ProviderQuery.endpoint == "domain_search",
                ProviderQuery.status == "complete",
            )
        ).all()
    )


def _candidate_drops(db: Session, limit: int) -> list[DroppedDomain]:
    completed = _completed_targets(db)
    paid_done = set(
        db.scalars(
            select(ProviderQuery.target).where(
                ProviderQuery.provider == "dataforseo",
                ProviderQuery.endpoint == "bulk_backlink_summary",
                ProviderQuery.status == "complete",
            )
        ).all()
    )
    commoncrawl_hits = {
        target: int(row_count or 0)
        for target, row_count in db.execute(
            select(ProviderQuery.target, ProviderQuery.row_count).where(
                ProviderQuery.provider == "commoncrawl",
                ProviderQuery.endpoint == "url_index",
                ProviderQuery.status == "complete",
            )
        ).all()
    }
    stackexchange_hits = {
        target: sum(int(value or 0) for value in values)
        for target, values in defaultdict(list, {
            target: [] for target in []
        }).items()
    }
    for target, row_count in db.execute(
        select(ProviderQuery.target, ProviderQuery.row_count).where(
            ProviderQuery.provider == "stackexchange",
            ProviderQuery.endpoint.like("url_search:%"),
            ProviderQuery.status == "complete",
        )
    ).all():
        stackexchange_hits[target] = stackexchange_hits.get(target, 0) + int(row_count or 0)

    recent = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(300)
    ).all()
    eligible = [
        item
        for item in recent
        if item.name not in completed and item.name not in paid_done
    ]
    eligible.sort(
        key=lambda item: (
            0 if stackexchange_hits.get(item.name, 0) > 0 else 1,
            0 if commoncrawl_hits.get(item.name, 0) > 0 else 1,
            0 if item.name.endswith(".com") else 1,
            len(item.name),
            -item.first_seen_at.timestamp(),
        )
    )
    return eligible[:limit]


def run_hackernews_prefilter(
    db: Session,
    *,
    batch_size: int = 10,
    hits_per_page: int = 50,
    client: HackerNewsSearchClient | None = None,
) -> dict[str, Any]:
    """Find exact dropped-domain links in HN stories/comments at zero provider cost."""
    if batch_size < 1 or batch_size > 25:
        raise ValueError("Hacker News batch size must be between 1 and 25")
    if hits_per_page < 1 or hits_per_page > 100:
        raise ValueError("Hacker News hits_per_page must be between 1 and 100")

    candidates = _candidate_drops(db, batch_size)
    counters: dict[str, Any] = {
        "candidates": len(candidates),
        "queries": 0,
        "search_hits": 0,
        "items_with_exact_links": 0,
        "exact_links_saved": 0,
        "new_links": 0,
        "domains_with_links": 0,
        "provider_cost_usd": 0.0,
        "errors": 0,
        "error_details": [],
    }
    if not candidates:
        return counters

    api = client or HackerNewsSearchClient()
    for dropped in candidates:
        query = ProviderQuery(
            provider="hackernews",
            endpoint="domain_search",
            target=dropped.name,
            status="running",
            cost_usd=0.0,
        )
        db.add(query)
        db.commit()
        db.refresh(query)
        domain_had_link = False
        try:
            response = api.search_domain(dropped.name, hits_per_page=hits_per_page)
            saved_for_query = 0
            new_for_query = 0
            exact_items = 0
            for hit in response.hits:
                saved, created = _save_hit_links(db, domain_name=dropped.name, hit=hit)
                if saved:
                    exact_items += 1
                    saved_for_query += saved
                    new_for_query += created
                    domain_had_link = True
            query.status = "complete"
            query.row_count = saved_for_query
            query.provider_task_id = f"hits:{response.total_hits}"
            query.completed_at = utcnow()
            query.error = None
            db.commit()

            counters["queries"] += 1
            counters["search_hits"] += response.total_hits
            counters["items_with_exact_links"] += exact_items
            counters["exact_links_saved"] += saved_for_query
            counters["new_links"] += new_for_query
            if domain_had_link:
                counters["domains_with_links"] += 1
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
