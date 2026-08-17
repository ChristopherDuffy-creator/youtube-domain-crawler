from __future__ import annotations

import html
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
