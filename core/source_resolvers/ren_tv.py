"""Resolve REN TV news and programme players unsupported by yt-dlp."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from .common import extract_title_tag, fetch_with_retries


def is_ren_page(url: str) -> bool:
    return urlparse(url).hostname in {"ren.tv", "www.ren.tv"}


_SOURCE_RE = re.compile(r'"((?:https?://)?vod-[\w.-]+\.ren\.cdnvideo\.ru/[^"\s]+?\.mp4(?:\?[^"\s]*)?)"', re.I)
_HEIGHT_RE = re.compile(r'property="og:video:height" content="(\d+)"', re.I)
_DURATION_RE = re.compile(r'property="video:duration" content="(\d+)"', re.I)


class _PlayerParser(HTMLParser):
    found = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (attrs.get("data-video-id", "").isdigit()
                and attrs.get("data-video-type") in {"edition", "video"}):
            self.found = True


def is_ren_media(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme in {"http", "https"}
            and re.fullmatch(r"vod-[\w.-]+\.ren\.cdnvideo\.ru", parsed.hostname or "") is not None
            and parsed.path.lower().endswith(".mp4"))


def extract(page: str) -> tuple[str, str | None, int, float] | None:
    """Require a REN player before using the CDN source in its page data."""
    player = _PlayerParser()
    player.feed(page)
    if not player.found:
        return None
    page = page.replace(r"\u002F", "/").replace(r"\/", "/")
    match = _SOURCE_RE.search(page)
    if not match:
        return None
    source = match.group(1).replace(r"\u002F", "/").replace(r"\/", "/")
    if not source.startswith(("http://", "https://")):
        source = "http://" + source
    parsed = urlparse(source)
    if not is_ren_media(source):
        return None
    # This CDN currently presents a certificate for a different hostname;
    # its public HTTP endpoint supports byte ranges and serves the same media.
    source = parsed._replace(scheme="http").geturl()
    height = _HEIGHT_RE.search(page)
    duration = _DURATION_RE.search(page)
    return source, extract_title_tag(page), int(height.group(1)) if height else 0, float(duration.group(1)) if duration else 0.0


async def resolve(url: str) -> tuple[str, str | None, int, float] | None:
    if not is_ren_page(url):
        return None
    page = await fetch_with_retries(url, timeout=12, retries=1, log_tag="ren.tv")
    return extract(html.unescape(page)) if page else None
