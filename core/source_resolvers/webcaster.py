"""Многие региональные каналы (замечено на otr-online.ru, наверняка не только
там) встраивают видео через виджет webcaster.pro — статья ссылается только
на страницу-обёртку iframe, у которой самой по себе нет формата, понятного
yt-dlp. Внутри неё, впрочем, есть настоящая прямая ссылка на XML-конфиг с
форматами (bl.webcaster.pro/feed/start/free_...) — yt-dlp её уже умеет
разбирать сам (extractor/webcaster.py), нужно только её найти и отдать
yt-dlp вместо страницы статьи.

Ссылку на iframe отдельные страницы кладут по-разному: часть — в
og:video (чтобы соцсети могли встроить видео у себя), а часть (замечено
на страницах программ otr-online.ru, например .../programmy/segodnya-v-
rossii/...) — прямо статическим тегом <iframe src="...webcaster.pro...">
в HTML статьи, вообще без og:video. Проверяем оба варианта.

Не привязано к конкретному сайту-владельцу виджета — сработает для любой
страницы, которая его встраивает, кто бы её ни размещал."""

from __future__ import annotations

import html
import re

from .common import fetch_with_retries

_OG_VIDEO_RE = re.compile(r'<meta property="og:video"[^>]*content="([^"]*webcaster\.pro[^"]*)"', re.IGNORECASE)
_IFRAME_RE = re.compile(r'<iframe[^>]+src="([^"]*webcaster\.pro[^"]*)"', re.IGNORECASE)
_CONFIG_RE = re.compile(r'data-config="config=([^"]+)"')


async def resolve(url: str) -> str | None:
    """Если статья встраивает видео через виджет webcaster.pro — возвращает
    прямую ссылку на его XML-конфиг (её yt-dlp уже понимает сам), иначе None."""
    page = await fetch_with_retries(url, log_tag="webcaster: страница")
    if page is None:
        return None

    match = _OG_VIDEO_RE.search(page) or _IFRAME_RE.search(page)
    if not match:
        return None
    iframe_url = html.unescape(match.group(1))
    if iframe_url.startswith("//"):
        iframe_url = f"https:{iframe_url}"

    iframe_page = await fetch_with_retries(iframe_url, log_tag="webcaster: iframe")
    if iframe_page is None:
        return None

    config_match = _CONFIG_RE.search(iframe_page)
    if not config_match:
        return None
    return html.unescape(config_match.group(1))
