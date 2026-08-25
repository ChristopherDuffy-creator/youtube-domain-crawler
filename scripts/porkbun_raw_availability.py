from __future__ import annotations

import json
import time

import httpx

from app.config import get_settings

ROOT = "https://api.porkbun.com/api/json/v3"
DOMAINS = ("satvic.yoga", "teamgerardiperformance.com")


def main() -> int:
    settings = get_settings()
    body = {"apikey": settings.porkbun_api_key, "secretapikey": settings.porkbun_secret_api_key}
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Expandosaurus/1.0"}
    for index, domain in enumerate(DOMAINS):
        if index:
            time.sleep(max(11.0, settings.porkbun_min_interval_seconds))
        try:
            r = httpx.post(f"{ROOT}/domain/checkDomain/{domain}", headers=headers, json=body, timeout=30.0)
            try:
                payload = r.json()
            except ValueError:
                payload = {"raw": r.text[:500]}
            safe = {
                "domain": domain,
                "http": r.status_code,
                "status": payload.get("status"),
                "message": payload.get("message"),
                "response": payload.get("response"),
            }
            print(json.dumps(safe, sort_keys=True))
        except Exception as exc:
            print(json.dumps({"domain": domain, "error": type(exc).__name__}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
