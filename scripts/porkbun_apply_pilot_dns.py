from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

ROOT = "https://api.porkbun.com/api/json/v3"


@dataclass(frozen=True)
class DomainDnsPlan:
    domain: str
    railway_target: str
    verification_token: str


PLANS = (
    DomainDnsPlan(
        domain="craftsheaven.club",
        railway_target="vmtfizrp.up.railway.app",
        verification_token="railway-verify=3a67149b51df6802e08eb529249fb64608fc1c8d5605d927f6462e8a19e8d749",
    ),
    DomainDnsPlan(
        domain="satvic.yoga",
        railway_target="ehqpjets.up.railway.app",
        verification_token="railway-verify=b6ea20d8e71c2820ccb6148fbc8455d7b838b34132209fdf27250962d01bfd7d",
    ),
    DomainDnsPlan(
        domain="teamgerardiperformance.com",
        railway_target="koiib8jf.up.railway.app",
        verification_token="railway-verify=4946991dab92ae4d0d20fafd168bae330b91ad03fd7c763ac184affcd9670dc3",
    ),
)

WEB_ROOT_TYPES = {"A", "AAAA", "CNAME", "ALIAS"}


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError:
        return {"status": "ERROR", "message": response.text[:300]}


def _is_success(payload: dict[str, Any]) -> bool:
    return str(payload.get("status", "")).upper() == "SUCCESS"


def _auth(settings) -> tuple[dict[str, str], dict[str, str]]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Expandosaurus/1.0",
    }
    creds = {
        "apikey": settings.porkbun_api_key,
        "secretapikey": settings.porkbun_secret_api_key,
    }
    return headers, creds


def _retrieve(domain: str, headers: dict[str, str], settings) -> dict[str, Any]:
    read_headers = dict(headers)
    read_headers["X-API-Key"] = settings.porkbun_api_key
    read_headers["X-Secret-API-Key"] = settings.porkbun_secret_api_key
    response = httpx.get(f"{ROOT}/dns/retrieve/{domain}", headers=read_headers, timeout=30.0)
    return _payload(response)


def _root_record(record: dict[str, Any], domain: str) -> bool:
    name = str(record.get("name") or "").rstrip(".").lower()
    return name in {"", "@", domain.lower()}


def _verify_record(record: dict[str, Any], domain: str) -> bool:
    name = str(record.get("name") or "").rstrip(".").lower()
    return name in {"_railway-verify", f"_railway-verify.{domain}"}


def _write(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    write_headers = dict(headers)
    write_headers["Idempotency-Key"] = idempotency_key
    response = httpx.post(endpoint, headers=write_headers, json=body, timeout=30.0)
    return _payload(response)


def _dry_then_live(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    key_prefix: str,
) -> dict[str, Any]:
    dry_body = dict(body)
    dry_body["dryRun"] = True
    dry = _write(
        endpoint,
        dry_body,
        headers,
        idempotency_key=f"{key_prefix}-dry-{uuid.uuid4()}",
    )
    if not _is_success(dry):
        return {"status": "ERROR", "stage": "dry-run", "detail": dry}
    live = _write(
        endpoint,
        body,
        headers,
        idempotency_key=f"{key_prefix}-live-{uuid.uuid4()}",
    )
    if not _is_success(live):
        return {"status": "ERROR", "stage": "live", "detail": live}
    return live


def _delete_record(domain: str, record_id: str, headers: dict[str, str], creds: dict[str, str]) -> dict[str, Any]:
    return _dry_then_live(
        f"{ROOT}/dns/delete/{domain}/{record_id}",
        dict(creds),
        headers,
        key_prefix=f"pilot-dns-delete-{domain}-{record_id}",
    )


def _create_record(
    domain: str,
    *,
    name: str,
    record_type: str,
    content: str,
    headers: dict[str, str],
    creds: dict[str, str],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        **creds,
        "name": name,
        "type": record_type,
        "content": content,
        "ttl": 600,
    }
    return _dry_then_live(
        f"{ROOT}/dns/create/{domain}",
        body,
        headers,
        key_prefix=f"pilot-dns-create-{domain}-{record_type}-{name or 'root'}",
    )


def apply_domain(plan: DomainDnsPlan, settings) -> dict[str, Any]:
    headers, creds = _auth(settings)
    current = _retrieve(plan.domain, headers, settings)
    if not _is_success(current):
        return {
            "domain": plan.domain,
            "status": "BLOCKED",
            "code": current.get("code"),
            "message": current.get("message"),
            "next_action": current.get("next_action"),
        }

    records = list(current.get("records") or [])
    wanted_alias = plan.railway_target.rstrip(".").lower()
    wanted_txt = plan.verification_token

    exact_alias = any(
        _root_record(r, plan.domain)
        and str(r.get("type") or "").upper() == "ALIAS"
        and str(r.get("content") or "").rstrip(".").lower() == wanted_alias
        for r in records
    )
    exact_txt = any(
        _verify_record(r, plan.domain)
        and str(r.get("type") or "").upper() == "TXT"
        and str(r.get("content") or "") == wanted_txt
        for r in records
    )

    changes: list[dict[str, Any]] = []

    # Remove only root web-routing records that conflict with Railway. Leave
    # MX, CAA, NS and unrelated TXT records untouched.
    if not exact_alias:
        for record in records:
            rtype = str(record.get("type") or "").upper()
            if not _root_record(record, plan.domain) or rtype not in WEB_ROOT_TYPES:
                continue
            result = _delete_record(plan.domain, str(record.get("id")), headers, creds)
            changes.append({"action": "delete_root_web_record", "type": rtype, "result": result.get("status")})
            if not _is_success(result):
                return {"domain": plan.domain, "status": "ERROR", "changes": changes, "detail": result}
            time.sleep(0.5)

        created = _create_record(
            plan.domain,
            name="",
            record_type="ALIAS",
            content=plan.railway_target,
            headers=headers,
            creds=creds,
        )
        changes.append({"action": "create_alias", "result": created.get("status")})
        if not _is_success(created):
            return {"domain": plan.domain, "status": "ERROR", "changes": changes, "detail": created}
        time.sleep(0.5)

    if not exact_txt:
        # Replace only stale Railway-verification TXT records. Other TXT stays.
        for record in records:
            if not _verify_record(record, plan.domain):
                continue
            if str(record.get("type") or "").upper() != "TXT":
                continue
            result = _delete_record(plan.domain, str(record.get("id")), headers, creds)
            changes.append({"action": "delete_stale_railway_txt", "result": result.get("status")})
            if not _is_success(result):
                return {"domain": plan.domain, "status": "ERROR", "changes": changes, "detail": result}
            time.sleep(0.5)

        created = _create_record(
            plan.domain,
            name="_railway-verify",
            record_type="TXT",
            content=plan.verification_token,
            headers=headers,
            creds=creds,
        )
        changes.append({"action": "create_verification_txt", "result": created.get("status")})
        if not _is_success(created):
            return {"domain": plan.domain, "status": "ERROR", "changes": changes, "detail": created}

    return {
        "domain": plan.domain,
        "status": "SUCCESS",
        "alias": plan.railway_target,
        "verification_host": "_railway-verify",
        "changes": changes,
        "already_correct": not changes,
    }


def main() -> int:
    settings = get_settings()
    results = []
    blocked = False
    failed = False
    for plan in PLANS:
        result = apply_domain(plan, settings)
        results.append(result)
        blocked = blocked or result.get("status") == "BLOCKED"
        failed = failed or result.get("status") == "ERROR"
        time.sleep(0.5)

    print(json.dumps({"results": results}, indent=2))
    # Workflow handles these as warnings; no noisy CI failure email.
    if failed:
        return 2
    if blocked:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
