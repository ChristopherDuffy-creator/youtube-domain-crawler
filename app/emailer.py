from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from app.config import Settings

RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailCandidate:
    domain: str
    tier: str
    monthly_views: int
    score: float
    video_title: str
    video_id: str
    price_usd: float | None


@dataclass(frozen=True)
class EmailPendingCandidate:
    domain: str
    tier: str
    monthly_views: int
    observation_days: float
    availability: str
    score: float
    video_title: str
    video_id: str
    reason: str


@dataclass(frozen=True)
class EmailRunIssue:
    job: str
    occurred_at: str
    message: str


@dataclass(frozen=True)
class DailyDigest:
    priority_count: int
    qualified_count: int
    watchlist_count: int
    pending_count: int
    target: int
    cumulative_videos: int
    cumulative_domains: int
    cumulative_dropped: int
    exact_links: int
    longest_observation_days: float
    feed_count: int
    work: Mapping[str, int]
    pending: Mapping[str, int]
    availability: Mapping[str, int]
    qualified_candidates: list[EmailCandidate]
    pending_candidates: list[EmailPendingCandidate]
    issues: list[EmailRunIssue]


def send_email(settings: Settings, subject: str, body_html: str) -> str | None:
    if not settings.email_enabled:
        return None
    try:
        response = httpx.post(
            RESEND_ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
                "User-Agent": "YouTubeDomainCrawler/0.1",
            },
            json={
                "from": settings.alert_from,
                "to": [settings.alert_email],
                "subject": subject,
                "html": body_html,
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise EmailError(f"Email request failed: {exc}") from exc
    if response.status_code >= 400:
        raise EmailError(
            f"Email service returned HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        return response.json().get("id")
    except ValueError:
        return None


def render_candidate_table(candidates: list[EmailCandidate]) -> str:
    if not candidates:
        return "<p>No new qualifying domains today.</p>"
    rows: list[str] = []
    for item in candidates:
        price = f"${item.price_usd:,.2f}" if item.price_usd is not None else "—"
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(item.domain)}</strong></td>"
            f"<td>{html.escape(item.tier.title())}</td>"
            f"<td>{item.monthly_views:,}</td>"
            f"<td>{item.score:.1f}</td>"
            f"<td>{price}</td>"
            f'<td><a href="https://www.youtube.com/watch?v={html.escape(item.video_id)}">'
            f"{html.escape(item.video_title[:90])}</a></td>"
            "</tr>"
        )
    return (
        "<table cellpadding='8' cellspacing='0' border='1' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px'>"
        "<thead><tr><th>Domain</th><th>Tier</th><th>30-day views</th>"
        "<th>Score</th><th>Price</th><th>Best linked video</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_pending_candidate_table(candidates: list[EmailPendingCandidate]) -> str:
    if not candidates:
        return "<p>No active pending candidates are available to rank yet.</p>"
    rows: list[str] = []
    for item in candidates:
        traffic = f"{item.monthly_views:,}" if item.monthly_views else "Collecting"
        observation = (
            f"{item.observation_days:.1f} days"
            if item.observation_days >= 1
            else "Under 1 day"
        )
        rows.append(
            "<tr>"
            f"<td><strong>{html.escape(item.domain)}</strong></td>"
            f"<td>{html.escape(item.tier.title())}</td>"
            f"<td>{traffic}<br><small>{observation}</small></td>"
            f"<td>{html.escape(item.availability.replace('_', ' '))}</td>"
            f"<td>{item.score:.1f}</td>"
            f"<td>{html.escape(item.reason)}</td>"
            f'<td><a href="https://www.youtube.com/watch?v={html.escape(item.video_id)}">'
            f"{html.escape(item.video_title[:75])}</a></td>"
            "</tr>"
        )
    return (
        "<table cellpadding='7' cellspacing='0' border='1' "
        "style='border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;"
        "width:100%;border-color:#d7dce5'>"
        "<thead><tr><th>Domain</th><th>Tier</th><th>30-day views</th>"
        "<th>Availability</th><th>Score</th><th>What it is waiting for</th>"
        "<th>Best linked video</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _stat_cards(items: list[tuple[str, int]]) -> str:
    cards = "".join(
        "<td style='padding:12px;border:1px solid #d7dce5;background:#f7f9fc'>"
        f"<div style='font-size:22px;font-weight:700'>{value:,}</div>"
        f"<div style='font-size:12px;color:#596579'>{html.escape(label)}</div></td>"
        for label, value in items
    )
    return (
        "<table cellpadding='0' cellspacing='6' border='0' style='width:100%;"
        f"font-family:Arial,sans-serif'><tr>{cards}</tr></table>"
    )


def _metric_table(items: list[tuple[str, int]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td style='padding:6px 9px;border-bottom:1px solid #e5e8ee'>{html.escape(label)}</td>"
        f"<td style='padding:6px 9px;border-bottom:1px solid #e5e8ee;text-align:right'>"
        f"<strong>{value:,}</strong></td></tr>"
        for label, value in items
    )
    return (
        "<table cellpadding='0' cellspacing='0' border='0' "
        f"style='width:100%;font-family:Arial,sans-serif;font-size:14px'>{rows}</table>"
    )


def render_daily_digest(report: DailyDigest) -> str:
    work_labels = [
        ("Successful crawler jobs", report.work.get("successful_runs", 0)),
        ("Failed crawler jobs", report.work.get("failed_runs", 0)),
        ("YouTube searches used", report.work.get("search_calls", 0)),
        ("Videos returned by YouTube", report.work.get("videos_returned", 0)),
        ("New videos saved", report.work.get("new_videos", 0)),
        ("New external domains saved", report.work.get("new_domains", 0)),
        ("New exact description links", report.work.get("new_links", 0)),
        ("Video view snapshots updated", report.work.get("videos_updated", 0)),
        ("Availability checks completed", report.work.get("availability_checked", 0)),
        ("Fresh dropped names loaded", report.work.get("drops_loaded", 0)),
        ("Dropped names searched on YouTube", report.work.get("drops_searched", 0)),
        ("Exact dropped-domain matches", report.work.get("dropped_matches", 0)),
    ]
    pending_labels = [
        ("All pending exact-link domains", report.pending.get("total", 0)),
        ("Collecting the first 24-hour baseline", report.pending.get("initial", 0)),
        ("Early traffic projection available", report.pending.get("projected", 0)),
        ("Waiting for full 27-day verification", report.pending.get("verification", 0)),
        ("Waiting for exact registrar confirmation", report.pending.get("registrar", 0)),
    ]
    availability_labels = [
        ("Ordinary registration confirmed", report.availability.get("available", 0)),
        ("Likely available (RDAP/DNS)", report.availability.get("likely_available", 0)),
        ("Registered", report.availability.get("registered", 0)),
        ("Premium/aftermarket", report.availability.get("premium_or_aftermarket", 0)),
        ("Unknown/conflicting", report.availability.get("unknown_or_conflicting", 0)),
        ("Checks with errors in the last 24h", report.work.get("availability_errors", 0)),
    ]

    if report.issues:
        status = "Needs attention"
        status_colour = "#a61b29"
    elif report.longest_observation_days < 27:
        status = "Running — collecting traffic history"
        status_colour = "#8a5a00"
    else:
        status = "Running normally"
        status_colour = "#08785d"

    issues_html = "<p>No crawler errors were recorded in the last 24 hours.</p>"
    if report.issues:
        issues_html = "<ul>" + "".join(
            "<li>"
            f"<strong>{html.escape(issue.job.replace('_', ' '))}</strong> "
            f"({html.escape(issue.occurred_at)}): {html.escape(issue.message[:500])}"
            "</li>"
            for issue in report.issues
        ) + "</ul>"

    feed_note = (
        f"{report.feed_count} automatic fresh dropped-domain feed"
        f"{'s are' if report.feed_count != 1 else ' is'} configured."
        if report.feed_count
        else "No automatic dropped-domain feed is configured."
    )
    qualifying_total = report.priority_count + report.qualified_count
    return (
        "<div style='max-width:1100px;margin:auto;color:#172033;font-family:Arial,sans-serif'>"
        "<h1 style='margin-bottom:5px'>Daily YouTube domain crawler report</h1>"
        f"<p style='margin-top:0;color:{status_colour}'><strong>{status}</strong></p>"
        + _stat_cards(
            [
                ("Priority", report.priority_count),
                ("Qualified", report.qualified_count),
                ("Watchlist", report.watchlist_count),
                ("Pending", report.pending_count),
            ]
        )
        + f"<p><strong>{qualifying_total}</strong> of the target "
        f"<strong>{report.target}</strong> domains currently qualify.</p>"
        "<h2>Work completed in the last 24 hours</h2>"
        + _metric_table(work_labels)
        + "<h2>Current pending pipeline</h2>"
        + _metric_table(pending_labels)
        + f"<p>Longest traffic observation: <strong>{report.longest_observation_days:.1f} "
        "days</strong>. A candidate needs 27–35 days of measured traffic before it can be "
        "called Qualified or Priority.</p>"
        + render_pending_candidate_table(report.pending_candidates)
        + "<h2>Availability position</h2>"
        + _metric_table(availability_labels)
        + f"<p>{html.escape(feed_note)}</p>"
        "<h2>Qualified and priority domains</h2>"
        + render_candidate_table(report.qualified_candidates)
        + "<h2>Cumulative ledger</h2>"
        + _stat_cards(
            [
                ("Videos checked", report.cumulative_videos),
                ("Domains checked", report.cumulative_domains),
                ("Dropped names loaded", report.cumulative_dropped),
                ("Active exact links", report.exact_links),
            ]
        )
        + "<h2>Errors and warnings</h2>"
        + issues_html
        + "<p style='font-size:12px;color:#596579;margin-top:24px'>Watchlist starts at "
        "5,000 projected monthly views. Qualification requires ordinary registration "
        "confirmation plus at least 20,000 views measured over a real 27–35 day window.</p>"
        "</div>"
    )
