# core/link_resolver.py
"""Резолвит произвольную ссылку (YouTube/VK/RuTube/Twitch/страница с медиа)
в прямой(ые) URL для ffmpeg через yt-dlp.

Без скачивания и без перекодирования: yt-dlp только узнаёт, ГДЕ лежит
уже закодированный поток (и есть ли отдельно audio-only дорожка — так
устроено у YouTube на большинстве разрешений), а копирует байты в файл
уже наш ffmpeg через '-c copy', как и для обычных IPTV-каналов.

У многих сайтов российских телеканалов (программные страницы вроде
otr-online.ru) нет отдельного экстрактора в yt-dlp, но сама прямая
ссылка на HLS/DASH прямо лежит открытым текстом в HTML/JS страницы —
так устроено у большинства встраиваемых плееров (webcaster.pro и т.п.).
Поэтому если yt-dlp не справился, вторым шагом просто ищем .m3u8/.mpd
в исходнике страницы.

Известное ограничение: некоторые CDN (например cdnvideo.ru — плеер
"Aloha", встречается на otr-online.ru) отдают 403 на найденную ссылку
независимо от User-Agent/Referer/Origin — похоже, доступ подписывается
токеном, который генерирует JS самого плеера, а не проверяется по
заголовкам. Обойти это без разбора конкретного JS нельзя, а разбирать
JS каждого такого сайта — не масштабируется. В таких случаях резолвер
просто вернёт ok=False с понятной причиной ("HTTP 403…" от ffmpeg/
проверки потока выше по стеку), а не будет тихо ломаться.
"""
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import requests

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

from utils.logger import logger

TARGET_HEIGHT = 720
# h264+aac в приоритете (максимальная совместимость плеера/Finder),
# иначе — лучшее, что есть в пределах 720p, иначе — вообще лучшее.
FORMAT_SELECTOR = (
    f'bv*[height<={TARGET_HEIGHT}][vcodec^=avc1]+ba[acodec^=mp4a]/'
    f'bv*[height<={TARGET_HEIGHT}]+ba/'
    f'b[height<={TARGET_HEIGHT}]/best'
)

_STREAM_URL_TAIL = r'(?:https?:)?\\?/\\?/[^\s"\'<>\\]+?\.(?:m3u8|mpd)'
# Большинство встраиваемых плееров кладут ссылку на поток под ключом
# source (JS-объект, JSON-конфиг или query-параметр вида ...&source=//..)
# — ищем именно так в первую очередь, иначе жадный общий поиск слишком
# легко подхватывает URL самого плеера-обёртки, а не то, что внутри него.
_SOURCE_KEY_RE = re.compile(r'source["\']?\s*[:=]\s*["\']?(' + _STREAM_URL_TAIL + r')', re.IGNORECASE)
_STREAM_URL_RE = re.compile(_STREAM_URL_TAIL, re.IGNORECASE)
_TITLE_RE = re.compile(r'<title[^>]*>([^<]+)</title>', re.IGNORECASE)
_OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)


@dataclass
class LinkInfo:
    ok: bool
    title: str = ''
    thumbnail: str = ''
    is_live: bool = False
    duration: Optional[float] = None  # секунды; None для эфира или если источник её не сообщает
    video_url: Optional[str] = None
    audio_url: Optional[str] = None  # None, если поток уже единый (video_url содержит и звук)
    headers: Optional[dict] = None  # заголовки (в первую очередь User-Agent), нужные именно ДЛЯ ЭТОЙ ссылки
    error: str = ''


def resolve_link(url: str, timeout: int = 15) -> LinkInfo:
    """Разбирает страницу/ссылку: сперва через yt-dlp, а если для этого
    сайта у него нет экстрактора — пробует найти прямую ссылку на поток
    прямо в HTML страницы. Ничего не скачивает."""
    if not url:
        return LinkInfo(ok=False, error="Пустая ссылка")

    if YTDLP_AVAILABLE:
        result = _resolve_via_ytdlp(url, timeout)
        if result.ok:
            return result
        fallback = _resolve_via_html_scrape(url, timeout)
        if fallback.ok:
            logger.info(f"LinkResolver: yt-dlp не знает '{url}', нашли поток напрямую в HTML")
            return fallback
        # Если запасной путь дошёл до конкретной причины (нашли ссылку на
        # поток, но источник её не отдаёт) — это полезнее, чем общее
        # "нет экстрактора" от yt-dlp.
        if fallback.title:
            return fallback
        return result  # иначе исходная ошибка yt-dlp обычно информативнее

    return _resolve_via_html_scrape(url, timeout)


def _resolve_via_ytdlp(url: str, timeout: int) -> LinkInfo:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'format': FORMAT_SELECTOR,
        'socket_timeout': timeout,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning(f"LinkResolver: yt-dlp не смог разобрать '{url}': {e}")
        return LinkInfo(ok=False, error=str(e)[:200])

    if info is None:
        return LinkInfo(ok=False, error="Пустой ответ от источника")

    title = info.get('title') or url
    thumbnail = info.get('thumbnail') or ''
    is_live = bool(info.get('is_live'))
    duration = info.get('duration') if not is_live else None

    requested = info.get('requested_formats')
    if requested and len(requested) >= 2:
        video_url = requested[0].get('url')
        audio_url = requested[1].get('url')
        headers = requested[0].get('http_headers') or info.get('http_headers')
    else:
        video_url = (requested[0].get('url') if requested else None) or info.get('url')
        audio_url = None
        headers = (requested[0].get('http_headers') if requested else None) or info.get('http_headers')

    if not video_url:
        return LinkInfo(ok=False, title=title, thumbnail=thumbnail,
                         error="Не удалось получить прямую ссылку на поток")

    # Некоторые источники (например VK: vkvd*.okcdn.ru) выдают подписанные
    # ссылки, привязанные к User-Agent, с которым их запросил yt-dlp — с
    # любым другим значением CDN отвечает HTTP 400, даже если сама ссылка
    # верна. Поэтому дальше используем именно эти заголовки, а не свой
    # дефолтный User-Agent.
    return LinkInfo(ok=True, title=title, thumbnail=thumbnail, is_live=is_live, duration=duration,
                     video_url=video_url, audio_url=audio_url, headers=headers)


def _resolve_via_html_scrape(url: str, timeout: int) -> LinkInfo:
    """Запасной путь для сайтов без экстрактора в yt-dlp: тянем страницу и
    ищем прямую ссылку на .m3u8/.mpd прямо в её HTML/JS (в т.ч. заэкранированную
    внутри JSON — вида `\\/\\/host\\/path.m3u8`)."""
    try:
        resp = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
    except Exception as e:
        return LinkInfo(ok=False, error=f"Страница недоступна: {e}"[:200])

    if resp.status_code != 200:
        return LinkInfo(ok=False, error=f"HTTP {resp.status_code} при загрузке страницы")

    html = resp.text
    source_match = _SOURCE_KEY_RE.search(html)
    if source_match:
        stream_url = source_match.group(1)
    else:
        generic_match = _STREAM_URL_RE.search(html)
        if not generic_match:
            return LinkInfo(ok=False, error="Не нашли прямую ссылку на поток на странице")
        stream_url = generic_match.group(0)
    stream_url = stream_url.replace('\\/', '/')
    if stream_url.startswith('//'):
        stream_url = 'https:' + stream_url
    elif not stream_url.startswith('http'):
        stream_url = urljoin(url, stream_url)

    title_match = _TITLE_RE.search(html)
    title = title_match.group(1).strip() if title_match else url
    thumb_match = _OG_IMAGE_RE.search(html)
    thumbnail = thumb_match.group(1) if thumb_match else ''

    # Ссылка на поток нашлась в HTML, но некоторые CDN (см. модульный
    # докстринг про cdnvideo.ru/Aloha) всё равно отдают 403 независимо от
    # заголовков — без этой проверки пользователь узнал бы об этом только
    # когда реальная запись уже провалилась. Проверяем сразу.
    try:
        check = requests.get(stream_url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0', 'Referer': url})
        if check.status_code != 200:
            return LinkInfo(ok=False, title=title, thumbnail=thumbnail,
                             error=f"Нашли ссылку на поток, но источник отвечает HTTP {check.status_code} "
                                   f"(вероятно, CDN блокирует доступ)")
    except Exception as e:
        return LinkInfo(ok=False, title=title, thumbnail=thumbnail, error=f"Поток недоступен: {e}"[:200])

    # Такие встроенные плееры почти всегда оказываются прямым эфиром
    # канала, а не конкретным нарезанным роликом — длительность неизвестна.
    return LinkInfo(ok=True, title=title, thumbnail=thumbnail, is_live=True, video_url=stream_url)


def guess_type(url: str) -> str:
    """Грубое определение платформы по домену — только для бейджа в UI."""
    u = url.lower()
    if 'youtube.com' in u or 'youtu.be' in u:
        return 'youtube'
    if 'vk.com' in u or 'vkvideo.ru' in u:
        return 'vk'
    if 'rutube.ru' in u:
        return 'rutube'
    if 'twitch.tv' in u:
        return 'twitch'
    if '1tv.ru' in u:
        return '1tv'
    return 'other'
