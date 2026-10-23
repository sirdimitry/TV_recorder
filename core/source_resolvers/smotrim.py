"""smotrim.ru: встроенный экстрактор yt-dlp (smotrim.py) целится в
player.smotrim.ru — сайт этот бэкенд уже не использует (отдаёт на любой
путь под ним просто SPA-заглушку, отсюда и 403/пустой JSON). Реальный,
рабочий и не требующий авторизации бэкенд нашёлся вручную, подсмотрев
сетевые запросы настоящей страницы: player-api.smotrim.ru/api/v1/video/<id>
отдаёт JSON с прямой ссылкой на HLS (streams.m3u8) — просто скармливаем эту
ссылку yt-dlp, он с обычным .m3u8 прекрасно справляется сам.

id достаём из /video/<id>, /video/id/<id> (обычная прямая ссылка на ролик
и ссылка на сам iframe-плеер соответственно) или из #playing_video=<id> —
так ссылается конкретный эпизод со страницы шоу/сезона
(.../brand/<id>#playing_video=<id>); фрагмент после # браузер на сервер не
отправляет, но раз он у нас есть в самой строке ссылки — просто читаем его.

Радио (.../brand/<id>#playing_audio=<id>) — тот же сайт, но отдельный API
(player-api.smotrim.ru/api/v1/audio/<id> вместо .../video/<id>) и отдельная,
уже готовая прямая ссылка на mp3 в JSON (streams.mp3), без HLS — см.
resolve_audio. Раньше такие ссылки не распознавались вообще и уходили в
общий generic-путь yt-dlp, который на смотриме без разбора хватает первую
попавшуюся картинку со страницы (обложку эпизода) и пытается скачать её как
видео — реальный, живой пример такого промаха см. в истории обсуждения."""

from __future__ import annotations

from httpx import AsyncClient as SafeAsyncClient

import html
import re
from dataclasses import dataclass

import httpx

from utils.logger import logger
from .common import DEFAULT_UA, fetch_with_retries



URL_RE = re.compile(r"^https?://(?:(?:www\.)?smotrim\.ru|(?:player|embed)\.smotrim\.ru)/")
_VIDEO_ID_RE = re.compile(r"(?:/video/(?:id/)?|playing_video=)(\d+)")
# Два разных вида ссылок на один и тот же аудио-эпизод: .../brand/<id>#playing_audio=<id>
# (ссылается конкретный эпизод со страницы шоу/сезона) и просто /audio/<id> —
# отдельная прямая ссылка на страницу эпизода (замечено вручную, раньше не
# распознавалась вообще и уходила в тот же generic-промах с обложкой, что
# описан в докстринге модуля, только для другого вида ссылки).
_AUDIO_ID_RE = re.compile(r"(?:playing_audio=|/audio/)(\d+)")
# Многие региональные каналы встраивают плеер smotrim.ru через iframe (см.
# alaniatv.ru) — сама статья на smotrim.ru не ссылается, экстрактор его не
# ищет, но раз плеер тот же самый — video_id вытащить и скачать всё равно можно.
# Встречаются оба хоста, player.smotrim.ru и embed.smotrim.ru — один и тот же
# плеер, просто разные страницы сайта на него ссылаются по-разному.
_IFRAME_RE = re.compile(r'<iframe[^>]+src="(https?://(?:player|embed)\.smotrim\.ru/iframe/video/id/\d+[^"]*)"')


async def _fetch_stream(video_id: str) -> tuple[str, str | None] | None:
    """(ссылка на HLS-поток, заголовок) по id ролика — None, если API
    smotrim.ru ответил не так, как ожидалось (тогда пробовать больше нечего,
    свежих источников на этот случай нет)."""
    try:
        async with SafeAsyncClient(timeout=10, headers={"User-Agent": DEFAULT_UA}, proxy=None) as client:
            resp = await client.get(f"https://player-api.smotrim.ru/api/v1/video/{video_id}")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — сообщение уходит пользователю как есть
        logger.info("[probe] smotrim.ru: API не ответил как ожидалось: %s", exc)
        return None

    payload = data.get("data") or {}
    m3u8 = (payload.get("streams") or {}).get("m3u8")
    if not m3u8:
        return None

    brand = payload.get("brand") or {}
    title = (
        (payload.get("fragment") or {}).get("title")
        or (brand.get("title") if brand.get("type") == "film" else None)
        or (payload.get("episode") or {}).get("title")
        or (payload.get("brand") or {}).get("title")
    )
    return m3u8, title


async def resolve(url: str) -> tuple[str, str | None] | None:
    """Прямая ссылка на smotrim.ru — id вынимаем из самой ссылки. None, если
    это не smotrim.ru вообще (см. resolve_embed для случая, когда плеер
    smotrim.ru встроен на СТОРОННЕЙ странице)."""
    if not URL_RE.match(url):
        return None
    id_match = _VIDEO_ID_RE.search(url)
    if not id_match:
        return None
    return await _fetch_stream(id_match.group(1))


def is_audio_url(url: str) -> bool:
    """Ссылка на радио/аудио-эпизод (.../brand/<id>#playing_audio=<id>) — их
    нужно сразу вести как mp3 (см. bot/telegram_bot.py:_handle_smotrim_audio_link),
    в обход обычного видео-пайплайна: тут и качества-то никакого нет."""
    return bool(URL_RE.match(url) and _AUDIO_ID_RE.search(url))


async def resolve_embed(url: str) -> tuple[str, str | None] | None:
    """Если СТОРОННЯЯ страница (не сам smotrim.ru) встраивает плеер smotrim.ru
    через iframe (замечено на региональных каналах вроде alaniatv.ru) —
    достаём video_id из адреса iframe и резолвим тем же путём, что и обычную
    ссылку на smotrim.ru. None, если такого встраивания на странице нет."""
    page = await fetch_with_retries(url, log_tag="smotrim: страница")
    if page is None:
        return None
    iframe_match = _IFRAME_RE.search(page)
    if not iframe_match:
        return None
    id_match = _VIDEO_ID_RE.search(html.unescape(iframe_match.group(1)))
    if not id_match:
        return None
    return await _fetch_stream(id_match.group(1))
