from __future__ import annotations

from collections import defaultdict
from datetime import UTC
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

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
from app.stackexchange import StackExchangeClient

DEFAULT_STACKEXCHANGE_SITES = ("stackoverflow", "superuser", "webmasters")


class _BodyLinkParser(HTMLParser):
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


def _exact_links(body: str, domain: str) -> list[tuple[str, str, tuple[str, ...]]]:
    parser = _BodyLinkParser()
    parser.feed(body or "")
    target = _normalize_host(domain)
    return [
        (href, anchor, rel)
        for href, anchor, rel in parser.links
        if (host := _normalize_host(href)) and (host == target or host.endswith(f".{target}"))
    ]


def _get_or_create_domain(db: Session, name: str) -> Domain:
    domain = db.scalar(select(Domain).where(Domain.name == name))
    if domain is None:
        domain = Domain(name=name)
        db.add(domain)
        db.flush()
    return domain


def _get_or_create_site(db: Session, hostname: str) -> SourceSite:
    site = db.scalar(select(SourceSite).where(SourceSite.hostname == hostname))
    if site is None:
        site = SourceSite(hostname=hostname, source_type="stackexchange")
        db.add(site)
        db.flush()
    else:
        site.source_type = "stackexchange"
        site.last_seen_at = utcnow()
    return site


def _get_or_create_page(
    db: Session,
    site: SourceSite,
    question: dict[str, Any],
) -> SourcePage | None:
    url = str(question.get("link") or "").strip()
    if not url:
        return None
    page = db.scalar(select(SourcePage).where(SourcePage.url == url))
    if page is None:
        page = SourcePage(site_id=site.id, url=url)
        db.add(page)
        db.flush()
    page.site_id = site.id
    page.title = str(question.get("title") or "")
    page.http_status = 200
    page.last_seen_at = utcnow()
    return page


def _save_metric(db: Session, page: SourcePage, question: dict[str, Any]) -> None:
    captured = utcnow()
    snapshot = db.scalar(
        select(SourceMetricSnapshot).where(
            SourceMetricSnapshot.source_page_id == page.id,
            SourceMetricSnapshot.provider == "stackexchange",
            SourceMetricSnapshot.capture_date == captured.date(),
        )
    )
    if snapshot is None:
        snapshot = SourceMetricSnapshot(
            source_page_id=page.id,
            provider="stackexchange",
            capture_date=captured.date(),
        )
        db.add(snapshot)
    snapshot.captured_at = captured
    snapshot.raw_metrics = {
        "view_count": int(question.get("view_count") or 0),
        "score": int(question.get("score") or 0),
        "answer_count": int(question.get("answer_count") or 0),
        "is_answered": bool(question.get("is_answered")),
        "creation_date": int(question.get("creation_date") or 0),
        "tags": list(question.get("tags") or []),
    }


def _save_question_links(
    db: Session,
    *,
    domain_name: str,
    site_name: str,
    question: dict[str, Any],
) -> tuple[int, int]:
    body = str(question.get("body") or "")
    matches = _exact_links(body, domain_name)
    if not matches:
        return 0, 0

    page_host = _normalize_host(str(question.get("link") or ""))
    if not page_host:
        return 0, 0
    domain = _get_or_create_domain(db, domain_name)
    site = _get_or_create_site(db, page_host)
    page = _get_or_create_page(db, site, question)
    if page is None:
        return 0, 0
    _save_metric(db, page, question)

    created = 0
    saved = 0
    title = str(question.get("title") or "")
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
        link.context_before = title
        link.context_after = ""
        link.semantic_location = "question_body"
        link.dofollow = not bool({"nofollow", "ugc", "sponsored"}.intersection(rel))
        link.provider_live = True
        link.last_seen_at = utcnow()
        saved += 1
    return saved, created


def _completed_sites(db: Session) -> dict[str, set[str]]:
    completed: dict[str, set[str]] = defaultdict(set)
    rows = db.execute(
        select(ProviderQuery.target, ProviderQuery.endpoint).where(
            ProviderQuery.provider == "stackexchange",
            ProviderQuery.status == "complete",
            ProviderQuery.endpoint.like("url_search:%"),
        )
    ).all()
    for target, endpoint in rows:
        completed[target].add(endpoint.split(":", 1)[-1])
    return completed


def _candidate_drops(db: Session, sites: tuple[str, ...], limit: int) -> list[DroppedDomain]:
    done_sites = _completed_sites(db)
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
    recent = db.scalars(
        select(DroppedDomain).order_by(DroppedDomain.first_seen_at.desc()).limit(300)
    ).all()
    required = set(sites)
    eligible = [
        item
        for item in recent
        if item.name not in paid_done and not required.issubset(done_sites.get(item.name, set()))
    ]
    eligible.sort(
        key=lambda item: (
            0 if commoncrawl_hits.get(item.name, 0) > 0 else 1,
            0 if item.name.endswith(".com") else 1,
            len(item.name),
            -item.first_seen_at.replace(tzinfo=UTC).timestamp()
            if item.first_seen_at.tzinfo is None
            else -item.first_seen_at.timestamp(),
        )
    )
    return eligible[:limit]


def run_stackexchange_prefilter(
    db: Session,
    *,
    batch_size: int = 5,
    sites: tuple[str, ...] = DEFAULT_STACKEXCHANGE_SITES,
    min_views: int = 1_000,
    client: StackExchangeClient | None = None,
) -> dict[str, Any]:
    """Find exact dropped-domain links on high-view Q&A pages at zero provider cost."""
    if batch_size < 1 or batch_size > 20:
        raise ValueError("Stack Exchange batch size must be between 1 and 20")
    if not sites or len(sites) > 5:
        raise ValueError("Stack Exchange sites must contain between 1 and 5 entries")

    candidates = _candidate_drops(db, sites, batch_size)
    done_sites = _completed_sites(db)
    counters: dict[str, Any] = {
        "candidates": len(candidates),
        "queries": 0,
        "questions_matched": 0,
        "exact_links_saved": 0,
        "new_links": 0,
        "domains_with_links": 0,
        "quota_remaining": None,
        "backoff_events": 0,
        "provider_cost_usd": 0.0,
        "errors": 0,
        "error_details": [],
    }
    if not candidates:
        return counters

    api = client or StackExchangeClient()
    stop_for_quota = False
    for dropped in candidates:
        if stop_for_quota:
            break
        domain_had_link = False
        for site in sites:
            if site in done_sites.get(dropped.name, set()):
                continue
            query = ProviderQuery(
                provider="stackexchange",
                endpoint=f"url_search:{site}",
                target=dropped.name,
                status="running",
                cost_usd=0.0,
            )
            db.add(query)
            db.commit()
            db.refresh(query)
            try:
                response = api.search_url(
                    site=site,
                    domain=dropped.name,
                    min_views=min_views,
                    page_size=20,
                )
                saved_for_query = 0
                new_for_query = 0
                matched_questions = 0
                for question in response.items:
                    saved, created = _save_question_links(
                        db,
                        domain_name=dropped.name,
                        site_name=site,
                        question=question,
                    )
                    if saved:
                        matched_questions += 1
                        saved_for_query += saved
                        new_for_query += created
                        domain_had_link = True
                query.status = "complete"
                query.row_count = saved_for_query
                query.provider_task_id = f"quota:{response.quota_remaining}"
                query.completed_at = utcnow()
                query.error = None
                db.commit()

                counters["queries"] += 1
                counters["questions_matched"] += matched_questions
                counters["exact_links_saved"] += saved_for_query
                counters["new_links"] += new_for_query
                counters["quota_remaining"] = response.quota_remaining
                if response.backoff_seconds:
                    counters["backoff_events"] += 1
                if response.quota_remaining and response.quota_remaining <= 50:
                    stop_for_quota = True
                    break
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
                    counters["error_details"].append(f"{site}:{dropped.name}: {exc}"[:500])
        if domain_had_link:
            counters["domains_with_links"] += 1

    return counters
