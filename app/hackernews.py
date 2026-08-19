from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class HackerNewsSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class HackerNewsSearchResult:
    hits: tuple[dict[str, Any], ...]
    total_hits: int


class HackerNewsSearchClient:
    """Tiny anonymous client for HN Search powered by Algolia."""

    def __init__(
        self,
        *,
        base_url: str = "https://hn.algolia.com/api/v1",
        timeout_seconds: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "Expandosaurus-Link-Hunter/0.6 (expired-link discovery)",
        }

    def search_domain(self, domain: str, *, hits_per_page: int = 50) -> HackerNewsSearchResult:
        clean_domain = domain.strip().lower().strip(".")
        if not clean_domain or "." not in clean_domain:
            raise ValueError("Hacker News target must be a domain name")
        if hits_per_page < 1 or hits_per_page > 100:
            raise ValueError("hits_per_page must be between 1 and 100")

        with httpx.Client(
            timeout=self.timeout_seconds,
            headers=self.headers,
            trust_env=False,
        ) as client:
            response = client.get(
                f"{self.base_url}/search",
                params={
                    "query": clean_domain,
                    "tags": "(story,comment)",
                    "hitsPerPage": str(hits_per_page),
                },
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise HackerNewsSearchError("HN Search returned a malformed response")
        raw_hits = payload.get("hits") or []
        if not isinstance(raw_hits, list):
            raise HackerNewsSearchError("HN Search returned malformed hits")
        hits = tuple(item for item in raw_hits if isinstance(item, dict))
        return HackerNewsSearchResult(
            hits=hits,
            total_hits=max(0, int(payload.get("nbHits") or 0)),
        )
