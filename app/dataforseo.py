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
    """Small client for the paid endpoints used by the web-wide Link Hunter."""

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
                "rank_scale": "one_hundred",
            },
        )

    def bulk_backlink_summaries(self, targets: list[str]) -> DataForSEOResponse:
        # The provider supports up to 1,000 URLs, but no more than 100 distinct
        # domains in one bulk-pages-summary request. Link Hunter feeds domains.
        if not targets or len(targets) > 100:
            raise ValueError("bulk backlink summary requires 1-100 domain targets")
        return self._post(
            "backlinks/bulk_pages_summary/live",
            {
                "targets": targets,
                "include_subdomains": True,
                "rank_scale": "one_hundred",
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
                "rank_scale": "one_hundred",
                "limit": limit,
                "order_by": ["page_from_rank,desc", "domain_from_rank,desc"],
            },
        )

    def bulk_traffic_estimation(self, targets: list[str]) -> DataForSEOResponse:
        if not targets or len(targets) > 1000:
            raise ValueError("bulk traffic estimation requires 1-1000 targets")
        return self._post(
            "dataforseo_labs/google/bulk_traffic_estimation/live",
            {"targets": targets},
        )
