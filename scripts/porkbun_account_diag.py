from __future__ import annotations

import json
import httpx

from app.config import get_settings
from app.availability import PORKBUN_ROOT


def main() -> int:
    settings = get_settings()
    headers = {
        "Accept": "application/json",
        "X-API-Key": settings.porkbun_api_key,
        "X-Secret-API-Key": settings.porkbun_secret_api_key,
        "User-Agent": "Expandosaurus/1.0",
    }
    out = {}
    for label, path in {
        "balance": "/account/balance",
        "domains": "/domain/listAll?start=0&includeLabels=no",
        "api_settings": "/account/apiSettings",
    }.items():
        try:
            r = httpx.get(f"{PORKBUN_ROOT}{path}", headers=headers, timeout=20.0)
            payload = r.json()
        except Exception as exc:
            out[label] = {"error": str(exc)}
            continue
        safe = {k: v for k, v in payload.items() if k not in {"apikey", "secretapikey"}}
        if label == "domains":
            domains = safe.get("domains") or []
            safe = {
                "status": safe.get("status"),
                "domain_count": len(domains),
                "total": safe.get("total") or safe.get("totalCount"),
                "requestId": safe.get("requestId"),
            }
        out[label] = {"http": r.status_code, **safe}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
