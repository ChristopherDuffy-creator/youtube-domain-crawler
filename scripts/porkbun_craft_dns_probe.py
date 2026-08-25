from __future__ import annotations

import json

import httpx

from app.config import get_settings

DOMAIN = "craftsheaven.club"
ROOT = "https://api.porkbun.com/api/json/v3"


def main() -> int:
    settings = get_settings()
    if not settings.registrar_enabled:
        print(json.dumps({"status": "ERROR", "message": "Porkbun credentials not configured"}))
        return 0

    headers = {
        "X-API-Key": settings.porkbun_api_key,
        "X-Secret-API-Key": settings.porkbun_secret_api_key,
        "Accept": "application/json",
        "User-Agent": "Expandosaurus/1.0",
    }
    try:
        response = httpx.get(f"{ROOT}/dns/retrieve/{DOMAIN}", headers=headers, timeout=20.0)
        payload = response.json()
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "message": type(exc).__name__}))
        return 0

    if str(payload.get("status", "")).upper() != "SUCCESS":
        print(json.dumps({
            "status": "ERROR",
            "code": payload.get("code"),
            "message": payload.get("message"),
            "next_action": payload.get("next_action"),
        }, indent=2))
        return 0

    safe_records = []
    for record in payload.get("records", []):
        safe_records.append({
            "id": record.get("id"),
            "name": record.get("name"),
            "type": record.get("type"),
            "content": record.get("content"),
            "ttl": record.get("ttl"),
        })
    print(json.dumps({"status": "SUCCESS", "records": safe_records}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
