from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.provider_budget import effective_provider_run_limit_usd

# Current DataForSEO public pricing checked 2026-08-19. The proof uses these
# conservative per-request/per-row ceilings to refuse an oversized batch before
# making any paid request. Actual task costs are still recorded from responses.
BACKLINK_REQUEST_USD = 0.024
BACKLINK_ROW_USD = 0.000036
LABS_REQUEST_USD = 0.012
LABS_ITEM_USD = 0.00012
MAX_BULK_SUMMARY_DOMAINS = 100
MAX_TRAFFIC_TARGETS = 1000


class DataForSEOError(RuntimeError):
    pass


@dataclass(frozen=True)
class DataForSEOResponse:
    result: dict[str, Any]
    task_cost_usd: float
    task_id: str


def estimate_provider_proof_max_cost_usd(
    summary_domain_count: int,
    deep_domain_count: int,
    backlinks_per_domain: int,
) -> float:
    """Return the worst-case provider spend implied by the configured proof limits.

    The cheap bulk-summary screen may contain far more domains than the detailed
    proof. The estimate assumes every selected deep target returns its full row
    limit and every returned source page requires a traffic-estimation item. This
    deliberately overestimates a normal proof so the configured spend cap is
    checked before the first paid call.
    """
    if summary_domain_count < 0:
        raise ValueError("summary_domain_count cannot be negative")
    if summary_domain_count > MAX_BULK_SUMMARY_DOMAINS:
        raise ValueError(f"proof cost estimate supports at most {MAX_BULK_SUMMARY_DOMAINS} domains")
    if deep_domain_count < 0:
        raise ValueError("deep_domain_count cannot be negative")
    if backlinks_per_domain <= 0 or backlinks_per_domain > 1000:
        raise ValueError("backlinks_per_domain must be between 1 and 1000")
    if summary_domain_count == 0 and deep_domain_count == 0:
        return 0.0

    # A deep-proof target may already have a permanent BacklinkSummary from an
    # earlier batch. In that case we deliberately pay no new summary cost and
    # spend the run only on the strongest cached winner candidates.
    summary_cost = (
        BACKLINK_REQUEST_USD + BACKLINK_ROW_USD * summary_domain_count
        if summary_domain_count
        else 0.0
    )
    detailed_cost = deep_domain_count * (
        BACKLINK_REQUEST_USD + BACKLINK_ROW_USD * backlinks_per_domain
    )
    source_pages = deep_domain_count * backlinks_per_domain
    traffic_requests = (
        (source_pages + MAX_TRAFFIC_TARGETS - 1) // MAX_TRAFFIC_TARGETS if source_pages else 0
    )
    traffic_cost = traffic_requests * LABS_REQUEST_USD + source_pages * LABS_ITEM_USD
    return round(summary_cost + detailed_cost + traffic_cost, 6)


class DataForSEOClient:
    """Small client for the paid endpoints used by the web-wide Link Hunter."""

    def __init__(self, settings: Settings):
        if not settings.dataforseo_enabled:
            raise DataForSEOError("DataForSEO credentials are not configured")
        self.settings = settings
        self.base_url = settings.dataforseo_base_url.rstrip("/")
        self.timeout = settings.dataforseo_timeout_seconds
        self.auth = (settings.dataforseo_login, settings.dataforseo_password)

    def _post(self, path: str, payload: dict[str, Any]) -> DataForSEOResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        with httpx.Client(
            auth=self.auth,
            timeout=self.timeout,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "Expandosaurus-Link-Hunter/0.3",
            },
        ) as client:
            response = client.post(url, json=[payload])
            response.raise_for_status()
            body = response.json()

        if not isinstance(body, dict):
            raise DataForSEOError("DataForSEO returned a malformed response")
        if int(body.get("status_code", 0)) != 20000:
            raise DataForSEOError(body.get("status_message") or "DataForSEO request failed")

        tasks = body.get("tasks") or []
        if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
            raise DataForSEOError("DataForSEO response contained no valid task")
        task = tasks[0]
        if int(task.get("status_code", 0)) != 20000:
            raise DataForSEOError(task.get("status_message") or "DataForSEO task failed")

        results = task.get("result") or []
        if not isinstance(results, list):
            raise DataForSEOError("DataForSEO task returned malformed results")
        result = results[0] if results else {}
        if not isinstance(result, dict):
            raise DataForSEOError("DataForSEO task returned a malformed result")
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
        # DataForSEO allows up to 1,000 targets, but at most 100 distinct
        # domains in a bulk-pages-summary request. Link Hunter feeds domains,
        # so 100 domain targets is the safe per-call ceiling here.
        if not targets or len(targets) > MAX_BULK_SUMMARY_DOMAINS:
            raise ValueError(
                f"bulk backlink summary requires 1-{MAX_BULK_SUMMARY_DOMAINS} domain targets"
            )

        deep_domain_count = min(len(targets), self.settings.link_hunter_proof_batch_size)
        source_page_ceiling = deep_domain_count * self.settings.link_hunter_backlinks_per_domain
        if source_page_ceiling > MAX_TRAFFIC_TARGETS:
            raise DataForSEOError(
                "Proof configuration could produce more than 1,000 source pages; "
                "reduce the proof batch size or backlinks-per-domain before spending"
            )
        estimated_max = estimate_provider_proof_max_cost_usd(
            len(targets),
            deep_domain_count,
            self.settings.link_hunter_backlinks_per_domain,
        )
        approved_cap = effective_provider_run_limit_usd(self.settings)
        if estimated_max > approved_cap:
            raise DataForSEOError(
                f"Proof worst-case provider estimate ${estimated_max:.4f} exceeds configured "
                f"cap ${approved_cap:.4f}"
            )

        return self._post(
            "backlinks/bulk_pages_summary/live",
            {
                "targets": targets,
                "include_subdomains": True,
                "rank_scale": "one_hundred",
            },
        )

    def backlinks(self, target: str, limit: int = 25) -> DataForSEOResponse:
        # The proof is trying to find valuable independent source sites, not 25
        # links from one prolific domain. One-per-domain maximizes evidence
        # diversity, while the bulk summary still preserves aggregate counts.
        return self._post(
            "backlinks/backlinks/live",
            {
                "target": target,
                "mode": "one_per_domain",
                "backlinks_status_type": "live",
                "include_subdomains": True,
                "exclude_internal_backlinks": True,
                "rank_scale": "one_hundred",
                "filters": ["page_from_status_code", "=", 200],
                "limit": limit,
                "order_by": ["page_from_rank,desc", "domain_from_rank,desc"],
            },
        )

    def bulk_traffic_estimation(self, targets: list[str]) -> DataForSEOResponse:
        if not targets or len(targets) > MAX_TRAFFIC_TARGETS:
            raise ValueError(f"bulk traffic estimation requires 1-{MAX_TRAFFIC_TARGETS} targets")
        return self._post(
            "dataforseo_labs/google/bulk_traffic_estimation/live",
            {
                "targets": targets,
                "item_types": ["organic"],
            },
        )
