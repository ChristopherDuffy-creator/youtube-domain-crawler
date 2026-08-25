from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import distinct, func, select

from app.database import SessionLocal
from app.pilot_sites import PILOT_SITES, pilot_site_events


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None, help="ISO-8601 UTC baseline; excludes earlier smoke-test traffic")
    args = parser.parse_args()
    since = parse_since(args.since)

    result: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "since": since.isoformat() if since else None,
        "domains": {},
    }

    with SessionLocal() as db:
        for domain in PILOT_SITES:
            base_filters = [pilot_site_events.c.domain == domain]
            if since is not None:
                base_filters.append(pilot_site_events.c.created_at >= since)

            pageviews = int(
                db.scalar(
                    select(func.count())
                    .select_from(pilot_site_events)
                    .where(*base_filters, pilot_site_events.c.event_type == "pageview")
                )
                or 0
            )
            sessions = int(
                db.scalar(
                    select(func.count(distinct(pilot_site_events.c.session_id)))
                    .select_from(pilot_site_events)
                    .where(*base_filters, pilot_site_events.c.event_type == "pageview")
                )
                or 0
            )
            interest_clicks = int(
                db.scalar(
                    select(func.count())
                    .select_from(pilot_site_events)
                    .where(*base_filters, pilot_site_events.c.event_type == "interest_click")
                )
                or 0
            )
            outbound_clicks = int(
                db.scalar(
                    select(func.count())
                    .select_from(pilot_site_events)
                    .where(*base_filters, pilot_site_events.c.event_type == "outbound_click")
                )
                or 0
            )
            clicks = interest_clicks + outbound_clicks
            top_paths = [
                {"path": path, "pageviews": int(count)}
                for path, count in db.execute(
                    select(pilot_site_events.c.path, func.count().label("n"))
                    .where(*base_filters, pilot_site_events.c.event_type == "pageview")
                    .group_by(pilot_site_events.c.path)
                    .order_by(func.count().desc())
                    .limit(10)
                ).all()
            ]
            last_event = db.scalar(
                select(func.max(pilot_site_events.c.created_at)).where(*base_filters)
            )
            result["domains"][domain] = {
                "pageviews": pageviews,
                "unique_sessions": sessions,
                "interest_clicks": interest_clicks,
                "outbound_clicks": outbound_clicks,
                "all_cta_clicks": clicks,
                "clicks_per_session": round(clicks / sessions, 4) if sessions else 0.0,
                "top_paths": top_paths,
                "last_event": last_event.isoformat() if last_event else None,
            }

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
