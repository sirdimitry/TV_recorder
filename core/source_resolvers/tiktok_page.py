"""Fallback for public TikTok pages when the yt-dlp extractor is challenged."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from httpx import AsyncClient as SafeAsyncClient


_VIDEO_ID = re.compile(r"/video/(\d+)")
_DATA = re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)
_UA = "python-httpx/0.28.1"


def is_tiktok_video(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return (host == "tiktok.com" or host.endswith(".tiktok.com")) and bool(_VIDEO_ID.search(urlparse(url).path))


def extract(page: str, expected_id: str) -> tuple[str, str, int, float] | None:
    match = _DATA.search(page)
    if not match:
        return None
    try:
        state = json.loads(match.group(1))
        item = state["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
        if str(item["id"]) != expected_id:
            return None
        video = item["video"]
        media_url = video["playAddr"]
        parsed = urlparse(media_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host == "tiktok.com" or host.endswith(".tiktok.com")):
            return None
        title = str(item.get("desc") or f"TikTok {expected_id}").strip()[:120]
        return media_url, title, int(video.get("height") or 0), float(video.get("duration") or 0)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def resolve(url: str) -> tuple[str, str, int, float, str] | None:
    if not is_tiktok_video(url):
        return None
    async with SafeAsyncClient(timeout=15, follow_redirects=True, proxy=None,
                               headers={"User-Agent": _UA}) as client:
        response = await client.get(url)
        response.raise_for_status()
        result = extract(response.text, _VIDEO_ID.search(urlparse(url).path).group(1))
        if not result:
            return None
        # TikTok's CDN checks the anonymous session established by the page
        # request. Keep it in memory only; never log or persist this header.
        cookie = "; ".join(f"{item.name}={item.value}" for item in client.cookies.jar
                           if item.domain.endswith("tiktok.com"))
        return (*result, cookie)


DOWNLOAD_HEADERS = {"User-Agent": _UA, "Referer": "https://www.tiktok.com/"}
