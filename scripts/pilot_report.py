from __future__ import annotations

import argparse
import json

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
    response = httpx.get(
        f"{BASE_URL}/ops/pilot-metrics",
        params=params,
        headers={"X-Admin-Token": settings.admin_token},
        timeout=30.0,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
