from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


class CommonCrawlError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommonCrawlPresence:
    domain: str
    indexes_checked: tuple[str, ...]
    indexes_with_capture: tuple[str, ...]

    @property
    def capture_index_count(self) -> int:
        return len(self.indexes_with_capture)


class CommonCrawlClient:
    """Small, polite client for Common Crawl's free CDXJ URL index."""

    def __init__(
        self,
        *,
        base_url: str = "https://index.commoncrawl.org",
        timeout_seconds: float = 20.0,
        min_interval_seconds: float = 0.75,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.headers = {
            "Accept": "application/json, text/plain;q=0.9",
            "User-Agent": "Expandosaurus-Link-Hunter/0.4 (historical-domain prefilter)",
        }

    def latest_indexes(self, limit: int = 2) -> list[str]:
        if limit < 1 or limit > 5:
            raise ValueError("Common Crawl index limit must be between 1 and 5")
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers=self.headers,
            trust_env=False,
        ) as client:
            response = client.get(f"{self.base_url}/collinfo.json")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise CommonCrawlError("Common Crawl collection list was malformed")
        indexes: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            value = str(item.get("id") or "").strip()
            if value.startswith("CC-MAIN-"):
                indexes.append(value)
            if len(indexes) >= limit:
                break
        if not indexes:
            raise CommonCrawlError("Common Crawl returned no usable crawl indexes")
        return indexes

    def _index_has_capture(self, client: httpx.Client, index_id: str, domain: str) -> bool:
        response = client.get(
            f"{self.base_url}/{index_id}-index",
            params={
                "url": f"{domain}/*",
                "output": "json",
                "limit": "1",
            },
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return bool(response.text.strip())

    def domain_presence(self, domain: str, index_ids: list[str]) -> CommonCrawlPresence:
        clean_domain = domain.strip().lower().strip(".")
        if not clean_domain or "." not in clean_domain:
            raise ValueError("Common Crawl target must be a domain name")
        if not index_ids:
            raise ValueError("At least one Common Crawl index is required")

        hits: list[str] = []
        checked: list[str] = []
        with httpx.Client(
            timeout=self.timeout_seconds,
            headers=self.headers,
            trust_env=False,
        ) as client:
            for position, index_id in enumerate(index_ids):
                if not index_id.startswith("CC-MAIN-"):
                    raise ValueError("Unexpected Common Crawl index identifier")
                if self._index_has_capture(client, index_id, clean_domain):
                    hits.append(index_id)
                checked.append(index_id)
                if position < len(index_ids) - 1 and self.min_interval_seconds:
                    time.sleep(self.min_interval_seconds)

        return CommonCrawlPresence(
            domain=clean_domain,
            indexes_checked=tuple(checked),
            indexes_with_capture=tuple(hits),
        )
