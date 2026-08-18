from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


class DataForSEOError(RuntimeError):
    pass


@dataclass(frozen=True)
class DataForSEOResponse:
    result: dict[str, Any]
    task_cost_usd: float
    task_id: str


class DataForSEOClient:
    """Small client for the paid endpoints used by Link Hunter Phase B."""

    def __init__(self, settings: Settings):
        if not settings.dataforseo_enabled:
            raise DataForSEOError("DataForSEO credentials are not configured")
        self.base_url = settings.dataforseo_base_url.rstrip("/")
        self.timeout = settings.dataforseo_timeout_seconds
        self.auth = (settings.dataforseo_login, settings.dataforseo_password)

    def _post(self, path: str, payload: dict[str, Any]) -> DataForSEOResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        with httpx.Client(auth=self.auth, timeout=self.timeout) as client:
            response = client.post(url, json=[payload])
            response.raise_for_status()
            body = response.json()

        if int(body.get("status_code", 0)) != 20000:
            raise DataForSEOError(body.get("status_message") or "DataForSEO request failed")

        tasks = body.get("tasks") or []
        if not tasks:
            raise DataForSEOError("DataForSEO response contained no task")
        task = tasks[0]
        if int(task.get("status_code", 0)) != 20000:
            raise DataForSEOError(task.get("status_message") or "DataForSEO task failed")

        results = task.get("result") or []
        result = results[0] if results else {}
        return DataForSEOResponse(
            result=result,
            task_cost_usd=float(task.get("cost") or 0.0),
            task_id=str(task.get("id") or ""),
        )

    def backlink_summary(self, target: str) -> DataForSEOResponse:
        return self._post(
            "backlinks/summary/live",
            {
                "target": target,
                "backlinks_status_type": "live",
                "include_subdomains": True,
                "exclude_internal_backlinks": True,
            },
        )

    def backlinks(self, target: str, limit: int = 25) -> DataForSEOResponse:
        return self._post(
            "backlinks/backlinks/live",
            {
                "target": target,
                "mode": "as_is",
                "backlinks_status_type": "live",
                "include_subdomains": True,
                "exclude_internal_backlinks": True,
                "limit": limit,
                "order_by": ["page_from_rank,desc", "domain_from_rank,desc"],
            },
        )
