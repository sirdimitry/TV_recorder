"""Совсем общий последний рубеж для сайтов без специфичного плеера (никакого
webcaster.pro/vipler/smotrim) — ищем прямую ссылку на видеофайл прямо в HTML.
Порядок — от самых надёжных семантических меток к самой шумной последней
(замечено на 360.ru: голая ссылка на .mp4 просто зашита в inline JSON без
каких-либо тегов/атрибутов вокруг). Не привязано к конкретному сайту."""

from __future__ import annotations

import html
import re

import httpx

from utils.logger import logger
from .common import extract_title_tag, fetch_with_retries



_VIDEO_TAG_SRC_RE = re.compile(r'<video[^>]+src="([^"]+)"', re.IGNORECASE)
_OG_VIDEO_RE = re.compile(r'<meta property="og:video(?::url)?"[^>]*content="([^"]+)"', re.IGNORECASE)
_JSONLD_CONTENT_URL_RE = re.compile(r'"contentUrl"\s*:\s*"([^"]+)"')
_BARE_M3U8_RE = re.compile(r'["\']([^"\']+\.m3u8[^"\']*)["\']')
_BARE_MP4_RE = re.compile(r'["\']([^"\']+\.mp4[^"\']*)["\']')
# Частые куски пути у превьюшек/спрайтов, которые тоже .mp4/.m3u8, но не то
# видео, что нужно — если попался такой URL, ищем следующий по списку выше.
_SKIP_MARKERS = ("sprite", "preview", "thumb", "poster")


def _resolve_relative(candidate: str, page_url: str) -> str:
    if candidate.startswith("//"):
        return f"https:{candidate}"
    if candidate.startswith("/"):
        return str(httpx.URL(page_url).join(candidate))
    return candidate


async def resolve(url: str) -> tuple[str, str | None] | None:
    """(прямая ссылка на видеофайл, заголовок страницы) — None, если ничего
    похожего в HTML не нашлось."""
    page = await fetch_with_retries(url, log_tag="direct-media: страница")
    if page is None:
        return None

    media_url = None
    for pattern in (_VIDEO_TAG_SRC_RE, _OG_VIDEO_RE, _JSONLD_CONTENT_URL_RE, _BARE_M3U8_RE, _BARE_MP4_RE):
        for match in pattern.finditer(page):
            # Двойное экранирование (JSON-строка внутри HTML-атрибута/другого
            # JSON, замечено на 360.ru: \"file_link\":\"https://...mp4\") даёт
            # висячий "\" перед закрывающей кавычкой — сама кавычка в захват
            # не попадает (её съедает закрывающая часть паттерна), а вот этот
            # предшествующий ей backslash остаётся хвостом строки. rstrip чинит.
            candidate = html.unescape(match.group(1)).replace("\\/", "/").rstrip("\\")
            if any(marker in candidate.lower() for marker in _SKIP_MARKERS):
                continue
            # _BARE_M3U8_RE/_BARE_MP4_RE ловят вообще любую кавычную строку,
            # заканчивающуюся на .m3u8/.mp4 — попадаются и не-ссылки вроде
            # download="Название статьи.mp4" (просто имя файла для скачивания,
            # без пути). Настоящая ссылка/путь всегда содержит хотя бы один "/".
            if "/" not in candidate:
                continue
            media_url = candidate
            break
        if media_url:
            break
    if not media_url:
        return None
    media_url = _resolve_relative(media_url, url)

    logger.info("[direct-media] нашёл прямую ссылку в HTML: %s", media_url)
    return media_url, extract_title_tag(page)
