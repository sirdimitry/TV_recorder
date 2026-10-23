"""tvzvezda.ru: на каждой странице встроен фоновый плеер "Радио Звезда"
(<audio src=".../radio/...">) — generic-экстрактор yt-dlp путает его с
видео статьи (это единственный <audio>/<video>-подобный источник, который
он вообще видит в готовом HTML) и виснет на нём: это бесконечный live-
эфир, скачивание зависает на "0.0%" на много минут и в итоге падает по
таймауту, настоящее видео статьи вообще не при делах. Само видео сайт
передаёт не тегом, а через собственный JSON-стейт страницы (Angular,
<script id="tvzvezda-state">{...}</script>) — внутри у объекта нужной
статьи есть поле media.video.url с прямой ссылкой на HLS
(cdn.tvzvezda.ru/.../index.m3u8, yt-dlp такую ссылку понимает сам).
Ищем в дереве JSON объект с "url", совпадающим с путём текущей страницы —
так же на статью может ссылаться "похожее"/related-видео с другой датой,
отбор по url отсекает их."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from utils.logger import logger
from .common import fetch_with_retries



URL_RE = re.compile(r"^https?://(?:www\.)?tvzvezda\.ru/")
_STATE_RE = re.compile(r'<script id="tvzvezda-state"[^>]*>(.*?)</script>', re.DOTALL)


def _find_video(node, path: str) -> tuple[str, str] | None:
    if isinstance(node, dict):
        media = node.get("media")
        if node.get("url") == path and isinstance(media, dict):
            video = media.get("video")
            if isinstance(video, dict) and video.get("url"):
                return video["url"], node.get("title") or ""
        for value in node.values():
            found = _find_video(value, path)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_video(item, path)
            if found:
                return found
    return None


async def resolve(url: str) -> tuple[str, str | None] | None:
    """(ссылка на HLS-поток настоящего видео статьи, заголовок) — None, если
    это не tvzvezda.ru, страница не отдала свой JSON-стейт, или у статьи
    вообще нет видео (обычная текстовая новость без ролика)."""
    if not URL_RE.match(url):
        return None
    page = await fetch_with_retries(url, log_tag="tvzvezda.ru: страница")
    if page is None:
        return None
    state_match = _STATE_RE.search(page)
    if not state_match:
        return None
    try:
        state = json.loads(state_match.group(1))
    except Exception as exc:  # noqa: BLE001 — просто не нашли видео этим путём
        logger.info("[probe] tvzvezda.ru: не удалось разобрать state JSON: %s", exc)
        return None

    # ".../<slug>.html/player" — тот же материал, отдельный "полноэкранный
    # плеер"-вид (замечено на реальной ссылке пользователя) — но в самом
    # JSON-стейте статья по-прежнему числится под обычным url, БЕЗ
    # "/player" на конце. Раз сам стейт про этот суффикс не знает, ищем
    # без него.
    path = urlparse(url).path.removesuffix("/player")
    found = _find_video(state, path)
    if not found:
        return None
    video_url, title = found
    return video_url, (title or None)
