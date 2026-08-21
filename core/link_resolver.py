# core/link_resolver.py
"""Резолвит произвольную ссылку (YouTube/VK/RuTube/Twitch/страница с медиа)
в прямой(ые) URL для ffmpeg через yt-dlp.

Без скачивания и без перекодирования: yt-dlp только узнаёт, ГДЕ лежит
уже закодированный поток (и есть ли отдельно audio-only дорожка — так
устроено у YouTube на большинстве разрешений), а копирует байты в файл
уже наш ffmpeg через '-c copy', как и для обычных IPTV-каналов.
"""
from dataclasses import dataclass
from typing import Optional

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


@dataclass
class LinkInfo:
    ok: bool
    title: str = ''
    thumbnail: str = ''
    is_live: bool = False
    video_url: Optional[str] = None
    audio_url: Optional[str] = None  # None, если поток уже единый (video_url содержит и звук)
    headers: Optional[dict] = None  # заголовки (в первую очередь User-Agent), нужные именно ДЛЯ ЭТОЙ ссылки
    error: str = ''


def resolve_link(url: str, timeout: int = 15) -> LinkInfo:
    """Разбирает страницу/ссылку через yt-dlp. Ничего не скачивает."""
    if not YTDLP_AVAILABLE:
        return LinkInfo(ok=False, error="yt-dlp не установлен")
    if not url:
        return LinkInfo(ok=False, error="Пустая ссылка")

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
        logger.warning(f"LinkResolver: не удалось разобрать '{url}': {e}")
        return LinkInfo(ok=False, error=str(e)[:200])

    if info is None:
        return LinkInfo(ok=False, error="Пустой ответ от источника")

    title = info.get('title') or url
    thumbnail = info.get('thumbnail') or ''
    is_live = bool(info.get('is_live'))

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
    return LinkInfo(ok=True, title=title, thumbnail=thumbnail, is_live=is_live,
                     video_url=video_url, audio_url=audio_url, headers=headers)


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
