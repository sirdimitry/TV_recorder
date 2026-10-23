"""1tv.ru: новости, выпуски передач и короткие ссылки через JSON API плеера.

У новостей используем ID из адреса; у выпусков читаем video_id из
data-playlist-url после перехода по редиректам короткой ссылки.

Отдаём HLS-манифест (sources[].src с type=application/x-mpegURL), а не
прогрессивные ссылки из mbr (hd/sd/ld) — те на практике оказались урезанными:
подпись "hd" на деле означает максимум ~360p (проверено вручную — балансировщик
1tv.ru на прямой запрос файла с более высоким битрейтом в имени просто
редиректит на "sd"). Настоящие 720p/1080p варианты 1tv.ru отдаёт только через
HLS: сам master.m3u8 перечисляет полный список битрейтов прямо в своём имени
(..._,350,950,3800,8000,.mp4.urlset/master.m3u8) — 8000 (~1080p) в mbr не
попадает вообще. yt-dlp прекрасно умеет читать такие манифесты сам (см.
использование в core/link_download.py — тот же путь, что и для webcaster.pro/
vipler)."""

from __future__ import annotations

from httpx import AsyncClient as SafeAsyncClient

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from utils.logger import logger
from .common import DEFAULT_UA



_ID_RE = re.compile(r"1tv\.ru/(?:[^/?#]*/)*?(\d+)(?:[/?#]|$)")
_HLS_TYPE = "application/x-mpegURL"


def is_page(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and parts.hostname in {"1tv.ru", "www.1tv.ru"}


class _PlayerParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.selector: tuple[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        if self.selector is not None:
            return
        playlist = dict(attrs).get("data-playlist-url")
        if not playlist:
            return
        parts = urlsplit(playlist)
        if (parts.netloc and not is_page("https:" + playlist if playlist.startswith("//") else playlist)):
            return
        if parts.scheme not in {"", "http", "https"} or parts.path not in {"/playlist", "/video_materials.json"}:
            return
        params = parse_qs(parts.query)
        for key in ("video_id", "news_id"):
            values = params.get(key, [])
            if len(values) == 1 and re.fullmatch(r"[0-9]+", values[0]):
                self.selector = key, values[0]
                return


async def resolve(url: str) -> tuple[str, str] | None:
    """HLS и заголовок новости/выпуска, включая короткие ссылки /-/… .

    У выпусков ID находится в data-playlist-url плеера, а не в адресе.
    Запрашиваем single=true, чтобы не скачать соседний выпуск из плейлиста.
    """
    if not is_page(url):
        return None
    try:
        async with SafeAsyncClient(timeout=8, follow_redirects=True, headers={"User-Agent": DEFAULT_UA},
                                   proxy=None) as client:
            match = _ID_RE.search(url)
            selector = ("news_id", match.group(1)) if match else None
            if selector is None:
                page = await client.get(url)
                page.raise_for_status()
                if not is_page(str(page.url)):
                    return None
                parser = _PlayerParser()
                parser.feed(page.text)
                selector = parser.selector
                if selector is None:
                    match = _ID_RE.search(str(page.url))
                    if not match:
                        return None
                    selector = "news_id", match.group(1)
            key, video_id = selector
            query = urlencode({key: video_id, "single": "true"})
            api_url = f"https://www.1tv.ru/video_materials.json?{query}"
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — просто откатываемся к обычному пути
        logger.info("[probe] 1tv.ru: video_materials.json не ответил как ожидалось: %s", exc)
        return None

    if not isinstance(data, list):
        return None
    items = [item for item in data if isinstance(item, dict)]
    if key == "video_id":
        items = [item for item in items if str(item.get("uid")) == video_id]
    if not items:
        return None
    item = items[0]

    hls_url = next(
        (s.get("src") for s in (item.get("sources") or [])
         if isinstance(s, dict) and s.get("type") == _HLS_TYPE and isinstance(s.get("src"), str)),
        None,
    )
    if not hls_url:
        return None
    if hls_url.startswith("//"):
        hls_url = f"https:{hls_url}"

    title = (item.get("title") or "").strip()
    date_air = (item.get("date_air") or "").replace("-", "_")
    full_title = " ".join(p for p in (date_air, "Первый канал", title) if p) or "media"
    return hls_url, full_title
