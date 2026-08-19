from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class StackExchangeError(RuntimeError):
    pass


@dataclass(frozen=True)
class StackExchangeSearchResult:
    items: tuple[dict[str, Any], ...]
    quota_remaining: int
    backoff_seconds: int


class StackExchangeClient:
    """Small anonymous client for free exact-URL discovery on Stack Exchange."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.stackexchange.com/2.3",
        timeout_seconds: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._next_request_at = 0.0
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "Expandosaurus-Link-Hunter/0.5 (expired-link discovery)",
        }

    def _respect_backoff(self) -> None:
        wait = self._next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def search_url(
        self,
        *,
        site: str,
        domain: str,
        min_views: int = 1_000,
        page_size: int = 20,
    ) -> StackExchangeSearchResult:
        clean_site = site.strip().lower()
        clean_domain = domain.strip().lower().strip(".")
        if not clean_site:
            raise ValueError("Stack Exchange site is required")
        if not clean_domain or "." not in clean_domain:
            raise ValueError("Stack Exchange target must be a domain name")
        if min_views < 0:
            raise ValueError("min_views must be non-negative")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")

        self._respect_backoff()
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers=self.headers,
            trust_env=False,
        ) as client:
            response = client.get(
                f"{self.base_url}/search/advanced",
                params={
                    "site": clean_site,
                    "url": f"*{clean_domain}*",
                    "views": str(min_views),
                    "sort": "votes",
                    "order": "desc",
                    "pagesize": str(page_size),
                    "filter": "withbody",
                },
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise StackExchangeError("Stack Exchange returned a malformed response")
        if payload.get("error_id"):
            message = str(payload.get("error_message") or payload.get("error_name") or "API error")
            raise StackExchangeError(message)
        raw_items = payload.get("items") or []
        if not isinstance(raw_items, list):
            raise StackExchangeError("Stack Exchange returned malformed items")
        items = tuple(item for item in raw_items if isinstance(item, dict))
        backoff = max(0, int(payload.get("backoff") or 0))
        if backoff:
            self._next_request_at = max(self._next_request_at, time.monotonic() + backoff)
        return StackExchangeSearchResult(
            items=items,
            quota_remaining=max(0, int(payload.get("quota_remaining") or 0)),
            backoff_seconds=backoff,
        )
