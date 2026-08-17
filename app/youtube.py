from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

YOUTUBE_API_ROOT = "https://www.googleapis.com/youtube/v3"


class YouTubeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchPage:
    video_ids: list[str]
    next_page_token: str | None


@dataclass(frozen=True)
class YouTubeVideo:
    id: str
    title: str
    channel_id: str
    channel_title: str
    description: str
    published_at: datetime | None
    view_count: int


def _chunks(items: Iterable[str], size: int = 50) -> Iterable[list[str]]:
    chunk: list[str] = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class YouTubeClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise YouTubeError("YOUTUBE_API_KEY is not configured")
        request_params = {**params, "key": self.api_key}
        try:
            response = httpx.get(
                f"{YOUTUBE_API_ROOT}/{path}",
                params=request_params,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise YouTubeError(f"YouTube request failed: {exc}") from exc
        if response.status_code >= 400:
            message = response.text[:500]
            try:
                payload = response.json()
                message = payload.get("error", {}).get("message", message)
            except ValueError:
                pass
            raise YouTubeError(f"YouTube API {response.status_code}: {message}")
        return response.json()

    def search_videos(
        self,
        query: str,
        published_before: datetime,
        page_token: str | None = None,
        max_results: int = 50,
    ) -> SearchPage:
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "order": "viewCount",
            "maxResults": min(max_results, 50),
            "publishedBefore": published_before.isoformat().replace("+00:00", "Z"),
            "safeSearch": "none",
        }
        if page_token:
            params["pageToken"] = page_token
        payload = self._get("search", params)
        ids = [
            item.get("id", {}).get("videoId")
            for item in payload.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        return SearchPage(video_ids=ids, next_page_token=payload.get("nextPageToken"))

    def fetch_videos(self, video_ids: Iterable[str]) -> list[YouTubeVideo]:
        videos: list[YouTubeVideo] = []
        unique_ids = list(dict.fromkeys(video_ids))
        for chunk in _chunks(unique_ids, 50):
            payload = self._get(
                "videos",
                {
                    "part": "snippet,statistics,status",
                    "id": ",".join(chunk),
                    "maxResults": 50,
                },
            )
            for item in payload.get("items", []):
                snippet = item.get("snippet", {})
                statistics = item.get("statistics", {})
                status = item.get("status", {})
                if status.get("privacyStatus", "public") != "public":
                    continue
                videos.append(
                    YouTubeVideo(
                        id=item["id"],
                        title=snippet.get("title", ""),
                        channel_id=snippet.get("channelId", ""),
                        channel_title=snippet.get("channelTitle", ""),
                        description=snippet.get("description", ""),
                        published_at=_parse_datetime(snippet.get("publishedAt")),
                        view_count=int(statistics.get("viewCount", 0)),
                    )
                )
        return videos


def exact_domain_in_description(domain: str, description: str) -> bool:
    """Boundary-aware exact-domain check used by the dropped-domain route."""
    import re

    pattern = re.compile(rf"(?i)(?<![a-z0-9-])(?:www\.)?{re.escape(domain)}(?![a-z0-9.-])")
    return bool(pattern.search(description or ""))
