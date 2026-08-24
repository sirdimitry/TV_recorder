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
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
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


def _format_selector(target_height: int = TARGET_HEIGHT) -> str:
    """h264+aac в приоритете (максимальная совместимость плеера/Finder),
    иначе — лучшее, что есть в пределах target_height, иначе — вообще
    лучшее. Параметризовано (не модульная константа) — раздел "Загрузки"
    просит конкретное разрешение от пользователя, а не всегда 720p."""
    return (
        f'bv*[height<={target_height}][vcodec^=avc1]+ba[acodec^=mp4a]/'
        f'bv*[height<={target_height}]+ba/'
        f'b[height<={target_height}]/best'
    )

_STREAM_URL_TAIL = r'(?:https?:)?\\?/\\?/[^\s"\'<>\\]+?\.(?:m3u8|mpd|mp4)'
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


def resolve_link(url: str, timeout: int = 15, target_height: int = TARGET_HEIGHT) -> LinkInfo:
    """Разбирает страницу/ссылку: сперва через yt-dlp, а если для этого
    сайта у него нет экстрактора — пробует найти прямую ссылку на поток
    прямо в HTML страницы, а если и там пусто (сайт рисует плеер через
    JS) — последним рубежом открывает её в настоящем браузерном движке
    и слушает его собственные сетевые запросы (см. _resolve_via_browser_sniff).
    Ничего не скачивает.

    target_height влияет только на выбор варианта у yt-dlp (там реально
    есть из чего выбирать) — у HTML-скрейпа и sniff-пути отдаётся то, что
    нашлось; если это HLS-мастер-плейлист, конкретный битрейт под
    target_height выбирается позже, при сборке команды ffmpeg
    (см. core/stream_resolver.py: resolve_variant_url)."""
    if not url:
        return LinkInfo(ok=False, error="Пустая ссылка")

    ytdlp_result = None
    if YTDLP_AVAILABLE:
        ytdlp_result = _resolve_via_ytdlp(url, timeout, target_height)
        if ytdlp_result.ok:
            return ytdlp_result

    fallback = _resolve_via_html_scrape(url, timeout)
    if fallback.ok:
        if ytdlp_result is not None:
            logger.info(f"LinkResolver: yt-dlp не знает '{url}', нашли поток напрямую в HTML")
        return fallback

    # 1tv.ru рисует плеер через JS (iframe на static.1tv.ru появляется уже
    # после гидратации React) — ни у yt-dlp, ни в сыром HTML ссылки нет.
    # Но сам JS-плеер внутри просто дёргает свой собственный публичный
    # JSON-эндпоинт по числовому id из URL — тот же вызов можно сделать
    # напрямую, без браузера вообще (см. _resolve_1tv).
    onetv = _resolve_1tv(url, timeout)
    if onetv is not None:
        return onetv

    # Ни у yt-dlp, ни в сыром HTML ничего не нашлось — если у страницы
    # есть своя embed-страница (og:video), сначала пробуем прогнать через
    # браузер именно её (там меньше постороннего шума в сетевых запросах,
    # чем на всей статье), иначе — саму страницу.
    sniffed = _resolve_via_browser_sniff(fallback.player_url or url, url, timeout)
    if sniffed:
        stream_url, body = sniffed
        if stream_url.split('?', 1)[0].lower().endswith('.mp4'):
            duration = _probe_mp4_duration(stream_url, url, timeout)
        else:
            duration = _detect_duration(body, stream_url, url, timeout) if body else None
        logger.info(f"LinkResolver: нашли поток для '{url}' через встроенный браузер")
        return LinkInfo(ok=True, title=fallback.title or url, thumbnail=fallback.thumbnail,
                         is_live=duration is None, duration=duration,
                         video_url=stream_url, player_url=fallback.player_url,
                         headers={'User-Agent': _DEFAULT_UA, 'Referer': url})

    if ytdlp_result is not None:
        # Если запасной путь дошёл до конкретной причины (нашли ссылку на
        # поток, но источник её не отдаёт) — это полезнее, чем общее
        # "нет экстрактора" от yt-dlp.
        if fallback.title:
            return fallback
        return ytdlp_result  # иначе исходная ошибка yt-dlp обычно информативнее
    return fallback


def _resolve_via_browser_sniff(sniff_url: str, referer: str, timeout: int) -> Optional[tuple]:
    """Открывает sniff_url в настоящем WKWebView (gui/browser_capture.py
    --sniff, отдельным процессом — pywebview не может делить run loop с
    остальным приложением, тот же приём, что и в core/recorder.py для
    записи браузером) и слушает её собственные fetch/XHR/<video src> —
    почти любой JS-плеер (hls.js и т.п.) всё равно тянет .m3u8/.mpd/.mp4
    одним из этих способов, просто в сыром HTML этого не видно. Возвращает
    (url, тело-или-пустая-строка) первой найденной и отвечающей ссылки,
    либо None, если ничего не нашлось или окно не поднялось за таймаут."""
    browser_script = Path(__file__).resolve().parent.parent / 'gui' / 'browser_capture.py'
    try:
        proc = subprocess.Popen(
            [sys.executable, str(browser_script), sniff_url, '--sniff'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except Exception as e:
        logger.warning(f"LinkResolver: не удалось запустить sniff-браузер: {e}")
        return None

    # Независимо от timeout самого HTTP-резолва — sniff нужен свой запас:
    # окну надо подняться, странице догрузиться и плееру начать тянуть
    # поток. gui/browser_capture.py сам закрывается по своему сторожевому
    # таймеру (12с), тут просто небольшой запас поверх него.
    stream_url = None
    deadline = time.time() + 16.0
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            except Exception:
                ready = [proc.stdout]
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            if line.startswith('STREAM:'):
                stream_url = line[len('STREAM:'):]
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    if not stream_url:
        return None

    body = _probe_stream(stream_url, referer, timeout)
    if body is None:
        logger.warning(f"LinkResolver: sniff-браузер нашёл поток '{stream_url}', но он недоступен")
        return None
    return stream_url, body


def _resolve_via_ytdlp(url: str, timeout: int, target_height: int = TARGET_HEIGHT) -> LinkInfo:
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'format': _format_selector(target_height),
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


_ONETV_NEWS_ID_RE = re.compile(r'1tv\.ru/n/(\d+)', re.IGNORECASE)


def _resolve_1tv(url: str, timeout: int) -> Optional[LinkInfo]:
    """1tv.ru/n/<id> статьи рисуют плеер JS-ом (EUMP, static.1tv.ru) —
    в сыром HTML ссылки нет. Но сам плеер после гидратации просто дёргает
    свой публичный JSON: https://www.1tv.ru/video_materials.json?news_id=<id>
    (тип запроса "11" = news_id — виден в query-параметре встраиваемого
    iframe, static.1tv.ru/eump/embeds/public_vod.html?v=<id>:11, разобранном
    вручную из initializers/public_vod.js). Тот же вызов делаем напрямую —
    без браузера вообще, быстрее и надёжнее sniff-а. Возвращает None, если
    URL не похож на /n/<id> или запрос не удался — тогда resolve_link()
    просто идёт дальше по обычной цепочке."""
    match = _ONETV_NEWS_ID_RE.search(url)
    if not match:
        return None
    news_id = match.group(1)
    api_url = f"https://www.1tv.ru/video_materials.json?news_id={news_id}&single=true"
    try:
        resp = requests.get(api_url, headers={'User-Agent': _DEFAULT_UA, 'Referer': url}, timeout=timeout)
        if resp.status_code != 200:
            logger.debug(f"LinkResolver/1tv: '{api_url}' ответил HTTP {resp.status_code}")
            return None
        items = resp.json()
        if not items:
            return None
        item = items[0]
    except Exception as e:
        logger.debug(f"LinkResolver/1tv: не удалось разобрать '{url}': {e}")
        return None

    sources = item.get('sources') or []
    stream_url = next((s['src'] for s in sources if s.get('type') == 'application/x-mpegURL'), None)
    if not stream_url:
        stream_url = next((s['src'] for s in sources if s.get('src')), None)
    if not stream_url:
        return None
    if stream_url.startswith('//'):
        stream_url = 'https:' + stream_url

    logger.info(f"LinkResolver/1tv: нашли поток для '{url}' через video_materials.json")
    return LinkInfo(ok=True, title=item.get('title', url), thumbnail=item.get('poster', ''),
                     is_live=False, duration=item.get('duration'), video_url=stream_url,
                     headers={'User-Agent': _DEFAULT_UA, 'Referer': url})


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
            # хронометражем всё равно подставлялся запасной час). У готового
            # .mp4 тела нет (см. _probe_stream) — длительность через ffprobe.
            if stream_url.split('?', 1)[0].lower().endswith('.mp4'):
                duration = _probe_mp4_duration(stream_url, url, timeout)
            else:
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
    заодно используется для определения хронометража (см. _detect_duration)
    у m3u8/mpd-плейлистов — они текстовые и небольшие. Для готового .mp4
    тело не нужно (и качать его целиком ради проверки доступности не
    нужно тем более — это может быть гигабайт видео) — там достаточно
    HEAD, а длительность отдельно достаёт ffprobe (см. _probe_mp4_duration)."""
    headers = {'User-Agent': _DEFAULT_UA, 'Referer': referer}
    try:
        if stream_url.split('?', 1)[0].lower().endswith('.mp4'):
            check = requests.head(stream_url, timeout=timeout, headers=headers, allow_redirects=True)
            return '' if check.status_code == 200 else None
        check = requests.get(stream_url, timeout=timeout, headers=headers)
        return check.text if check.status_code == 200 else None
    except Exception:
        return None


def _probe_mp4_duration(stream_url: str, referer: str, timeout: int) -> Optional[float]:
    """Длительность готового .mp4 — через ffprobe, а не requests: он читает
    по HTTP только moov-атом (обычно первые/последние килобайты), а не
    качает файл целиком."""
    if not shutil.which('ffprobe'):
        return None
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-headers', f'User-Agent: {_DEFAULT_UA}\r\nReferer: {referer}\r\n',
             '-show_entries', 'format=duration', '-of', 'csv=p=0', stream_url],
            capture_output=True, text=True, timeout=timeout)
        return float(result.stdout.strip())
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
