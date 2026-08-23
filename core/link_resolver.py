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
from urllib.parse import unquote, urljoin

import requests

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

from utils.logger import logger

# Голое "Mozilla/5.0" читается частью сайтов (замечено на tass.ru) как явный
# признак бота и получает HTTP 403 — полноценная строка настоящего браузера
# проходит без вопросов.
_DEFAULT_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
               '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')

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
# У некоторых сайтов сама страница рендерит видео через JS-компонент,
# который в нашем встроенном браузере (WKWebView) не хочет отрисовываться
# (замечено на otr-online.ru: обычные новостные статьи, в отличие от
# программных страниц, кладут плеер в Vue-компонент, который просто не
# гидрируется в WKWebView) — а вот прямая ссылка на сам плеер-фрейм,
# которую сайты для того и публикуют в og:video (чтобы соцсети могли
# встроить видео у себя), открывается и работает нормально.
_OG_VIDEO_RE = re.compile(r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
# webcaster.pro (используется на otr-online.ru и, вероятно, других
# российских телеканалах) не кладёт .m3u8 прямо в HTML страницы —
# сама страница плеера лишь ссылается на XML-конфиг ("data-config"),
# тот в ответ даёт ещё один XML с адресом "media/start", а уже ОН,
# при обращении, отдаёт финальный редирект-XML со списком .m3u8-дорожек.
# Проверено вручную на конкретном примере — см. _resolve_webcaster_player.
_WEBCASTER_CONFIG_RE = re.compile(r'data-config=["\'][^"\']*config=([^"\'&]+)', re.IGNORECASE)
_XML_VIDEO_RE = re.compile(r'<video><!\[CDATA\[([^\]]+)]]></video>', re.IGNORECASE)
_XML_TRACK_RE = re.compile(r'<track[^>]*><!\[CDATA\[([^\]]+\.m3u8[^\]]*)]]></track>', re.IGNORECASE)


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
    player_url: Optional[str] = None  # og:video страницы — для режима браузера открываем его вместо самой страницы
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

    # yt-dlp's generic 'html5'-экстрактор просто берёт первый <audio>/<video>
    # тег со страницы — если настоящее видео рендерится через JS (как часто
    # бывает у российских новостных сайтов), а на странице ЕЩЁ и есть, скажем,
    # радио-плеер (обычный <audio>), он уверенно вернёт этот radio-поток как
    # "видео" (vcodec: none) — на выходе шум вместо кадра при живом звуке.
    vcodec = (requested[0].get('vcodec') if requested else None) or info.get('vcodec')
    if vcodec == 'none':
        logger.warning(f"LinkResolver: yt-dlp ({info.get('extractor')}) нашёл только аудио-поток "
                        f"для '{url}' — это не видео, игнорируем")
        return LinkInfo(ok=False, title=title, thumbnail=thumbnail,
                         error="Найден только звук без видео (похоже, реальное видео на JS)")

    # Некоторые источники (например VK: vkvd*.okcdn.ru) выдают подписанные
    # ссылки, привязанные к User-Agent, с которым их запросил yt-dlp — с
    # любым другим значением CDN отвечает HTTP 400, даже если сама ссылка
    # верна. Поэтому дальше используем именно эти заголовки, а не свой
    # дефолтный User-Agent.
    return LinkInfo(ok=True, title=title, thumbnail=thumbnail, is_live=is_live, duration=duration,
                     video_url=video_url, audio_url=audio_url, headers=headers)


def _resolve_webcaster_player(player_url: str, referer: str, timeout: int) -> Optional[str]:
    """Проходит трёхшаговую цепочку webcaster.pro и возвращает первый
    найденный .m3u8 или None, если что-то на пути пошло не так. Каждый шаг
    отдельно логируется — этот путь специфичен для одного вендора и
    заведомо более хрупкий, чем обычный HTML-поиск, полезно видеть, на
    каком именно шаге он подвёл."""
    headers = {'User-Agent': _DEFAULT_UA, 'Referer': referer}
    try:
        r1 = requests.get(player_url, headers=headers, timeout=timeout)
        if r1.status_code != 200:
            logger.debug(f"LinkResolver/webcaster: страница плеера '{player_url}' ответила HTTP {r1.status_code}")
            return None
        m = _WEBCASTER_CONFIG_RE.search(r1.text)
        if not m:
            logger.debug(f"LinkResolver/webcaster: не нашли data-config на '{player_url}'")
            return None
        # Значение атрибута само являет собой процент-закодированный URL
        # (вложенный внутрь HTML-атрибута) — без unquote получаем на выходе
        # буквальные %3D/%26 вместо =/& и запрос уходит по битому адресу.
        config_url = unquote(m.group(1).replace('&amp;', '&').replace('\\/', '/'))

        r2 = requests.get(config_url, headers=headers, timeout=timeout)
        if r2.status_code != 200:
            logger.debug(f"LinkResolver/webcaster: config '{config_url}' ответил HTTP {r2.status_code}")
            return None
        vm = _XML_VIDEO_RE.search(r2.text)
        if not vm:
            logger.debug(f"LinkResolver/webcaster: не нашли <video> в ответе config")
            return None
        media_url = vm.group(1).replace('&amp;', '&')

        r3 = requests.get(media_url, headers=headers, timeout=timeout)
        if r3.status_code != 200:
            logger.debug(f"LinkResolver/webcaster: media/start '{media_url}' ответил HTTP {r3.status_code}")
            return None
        tm = _XML_TRACK_RE.search(r3.text)
        if not tm:
            logger.debug(f"LinkResolver/webcaster: не нашли <track> с .m3u8 в ответе media/start")
            return None
        return tm.group(1).replace('&amp;', '&').strip()
    except Exception as e:
        logger.debug(f"LinkResolver/webcaster: цепочка для '{player_url}' оборвалась: {e}")
        return None


def _resolve_via_html_scrape(url: str, timeout: int) -> LinkInfo:
    """Запасной путь для сайтов без экстрактора в yt-dlp: тянем страницу и
    ищем прямую ссылку на .m3u8/.mpd прямо в её HTML/JS (в т.ч. заэкранированную
    внутри JSON — вида `\\/\\/host\\/path.m3u8`)."""
    try:
        resp = requests.get(url, timeout=timeout, headers={'User-Agent': _DEFAULT_UA})
    except Exception as e:
        logger.warning(f"LinkResolver: страница '{url}' недоступна: {e}")
        return LinkInfo(ok=False, error=f"Страница недоступна: {e}"[:200])

    if resp.status_code != 200:
        logger.warning(f"LinkResolver: страница '{url}' ответила HTTP {resp.status_code}")
        return LinkInfo(ok=False, error=f"HTTP {resp.status_code} при загрузке страницы")

    html = resp.text
    # Заголовок/картинку/og:video достаём сразу — если ссылки на поток не
    # найдётся, они всё равно пригодятся: пусть об этом узнает пользователь,
    # а не только "yt-dlp не знает такой сайт".
    title_match = _TITLE_RE.search(html)
    title = title_match.group(1).strip() if title_match else url
    thumb_match = _OG_IMAGE_RE.search(html)
    thumbnail = thumb_match.group(1) if thumb_match else ''
    video_match = _OG_VIDEO_RE.search(html)
    player_url = video_match.group(1).replace('&amp;', '&') if video_match else None

    source_match = _SOURCE_KEY_RE.search(html)
    stream_url = None
    if source_match:
        stream_url = source_match.group(1)
    else:
        generic_match = _STREAM_URL_RE.search(html)
        if generic_match:
            stream_url = generic_match.group(0)

    if stream_url:
        stream_url = stream_url.replace('\\/', '/')
        if stream_url.startswith('//'):
            stream_url = 'https:' + stream_url
        elif not stream_url.startswith('http'):
            stream_url = urljoin(url, stream_url)

        # Ссылка на поток нашлась в HTML, но некоторые CDN (см. модульный
        # докстринг про cdnvideo.ru/Aloha) всё равно отдают 403 независимо
        # от заголовков — без этой проверки пользователь узнал бы об этом
        # только когда реальная запись уже провалилась. Проверяем сразу.
        body = _probe_stream(stream_url, url, timeout)
        if body is not None:
            # #EXT-X-ENDLIST в плейлисте — значит это уже готовый ролик с
            # известной длительностью, а не прямой эфир (раньше здесь всегда
            # стояло is_live=True, из-за чего у роликов с реальным
            # хронометражем всё равно подставлялся запасной час).
            duration = _detect_duration(body, stream_url, url, timeout)
            return LinkInfo(ok=True, title=title, thumbnail=thumbnail,
                             is_live=duration is None, duration=duration,
                             video_url=stream_url, player_url=player_url)
        logger.warning(f"LinkResolver: нашли поток '{stream_url}' на странице '{url}', но он недоступен")

    # Прямого потока в HTML не нашлось (или он не отвечает) — если страница
    # ссылается на webcaster.pro (og:video), пробуем дойти до .m3u8 через
    # его собственную XML-цепочку конфигов, вместо того чтобы сразу сдаться.
    if player_url and 'webcaster.pro' in player_url:
        # У этой цепочки бывают реальные сетевые подвисания на стороне
        # webcaster.pro (замечены TLS-таймауты) — держим её короткой, чтобы
        # не подвешивать диалог добавления ссылки на минуту в худшем случае.
        webcaster_timeout = min(timeout, 6)
        webcaster_stream = _resolve_webcaster_player(player_url, url, webcaster_timeout)
        webcaster_body = _probe_stream(webcaster_stream, url, webcaster_timeout) if webcaster_stream else None
        if webcaster_body is not None:
            logger.info(f"LinkResolver: дошли до потока через цепочку webcaster.pro для '{url}'")
            duration = _detect_duration(webcaster_body, webcaster_stream, url, webcaster_timeout)
            return LinkInfo(ok=True, title=title, thumbnail=thumbnail,
                             is_live=duration is None, duration=duration,
                             video_url=webcaster_stream, player_url=player_url,
                             headers={'User-Agent': _DEFAULT_UA, 'Referer': url})

    logger.warning(f"LinkResolver: на странице '{url}' не нашли рабочую ссылку на поток")
    return LinkInfo(ok=False, title=title, thumbnail=thumbnail, player_url=player_url,
                     error="Не нашли прямую ссылку на поток на странице")


def _probe_stream(stream_url: str, referer: str, timeout: int) -> Optional[str]:
    """Возвращает тело ответа, если поток отвечает 200, иначе None. Тело
    заодно используется для определения хронометража (см. _detect_duration)."""
    try:
        check = requests.get(stream_url, timeout=timeout, headers={'User-Agent': _DEFAULT_UA, 'Referer': referer})
        return check.text if check.status_code == 200 else None
    except Exception:
        return None


_EXTINF_RE = re.compile(r'#EXTINF:([\d.]+)')
_VARIANT_RE = re.compile(r'^(?!#)(\S+\.m3u8\S*)$', re.MULTILINE)


def _detect_duration(m3u8_body: str, base_url: str, referer: str, timeout: int, depth: int = 0) -> Optional[float]:
    """HLS-плейлист сам говорит, конечный он или нет: #EXT-X-ENDLIST есть
    только у уже отснятого ролика (VOD) — сумма #EXTINF и даёт его реальную
    длительность. У живого эфира этого тега нет и не будет — тогда None
    (дальше используется запасной час, как и раньше). Мастер-плейлист
    (список битрейт-вариантов, без самих сегментов) не содержит эту
    информацию напрямую — заходим в первый вариант на один уровень глубже."""
    if '#EXT-X-ENDLIST' in m3u8_body:
        durations = _EXTINF_RE.findall(m3u8_body)
        if durations:
            return sum(float(d) for d in durations)
        return None

    if depth == 0 and '#EXT-X-STREAM-INF' in m3u8_body:
        variant_match = _VARIANT_RE.search(m3u8_body)
        if variant_match:
            variant_url = urljoin(base_url, variant_match.group(1).strip())
            body = _probe_stream(variant_url, referer, timeout)
            if body:
                return _detect_duration(body, variant_url, referer, timeout, depth=1)

    return None


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
