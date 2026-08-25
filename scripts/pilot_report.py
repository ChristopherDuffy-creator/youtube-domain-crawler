from __future__ import annotations

import argparse
import json
import time

import httpx

from app.config import get_settings


BASE_URL = "https://youtube-domain-crawler-production.up.railway.app"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 UTC baseline; excludes earlier smoke-test traffic",
    )
    args = parser.parse_args()
    settings = get_settings()

    params = {"since": args.since} if args.since else {}
    last_error: Exception | None = None
    for attempt in range(12):
        try:
            response = httpx.get(
                f"{BASE_URL}/ops/pilot-metrics",
                params=params,
                headers={"X-Admin-Token": settings.admin_token},
                timeout=30.0,
            )
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2, sort_keys=True))
            return 0
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == 11:
                break
            time.sleep(5)

    raise SystemExit(f"Pilot report endpoint unavailable after retries: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
