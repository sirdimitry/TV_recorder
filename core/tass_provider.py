# core/tass_provider.py
"""Отдельный, изолированный провайдер для tass.ru — обычный HTTP-запрос
получает на этот сайт открытую страницу антибот-защиты ServicePipe:

    Forbidden
    Datetime / IP / ID / Origin
    "If you are not a bot, please copy the report..."

Мы её НЕ обходим — ни подменой отпечатка браузера, ни ротацией прокси,
ни решением капчи, ни stealth-патчами. Вместо этого запускаем настоящий,
полноценный браузерный движок (Playwright) и читаем то, что он сам
находит на странице: сетевые запросы и DOM реального проигрывателя.

Playwright сначала пробует запустить уже установленный у человека Google
Chrome (channel="chrome" — ничего лишнего не скачивает, если Chrome и так
есть). Если Chrome не найден — переходит на собственный Chromium из
пакета playwright; если ЕГО тоже ещё нет на диске (первый запуск на чистой
машине), качает один раз сам (см. _ensure_bundled_chromium), без ручных
команд в терминале — то, ради чего это вообще было переписано под
Playwright: чтобы работало и у людей без Chrome вообще.

Проверено вживую (см. историю разработки): голый, ничем не модифицированный
Playwright.chromium.launch(headless=False) — БЕЗ единого stealth-патча —
проходит защиту ServicePipe и получает настоящую статью. А вот
headless=True на том же самом коде получает пустую страницу — то есть
защита реально отличает headless от обычного отображаемого окна (это
наблюдаемый факт о поведении сайта, а не что-то, что мы подделываем).
Раз headless не проходит по-честному, мы НЕ патчим Chromium под headless
похожим на headed (это и есть stealth) — просто запускаем видимое окно,
как и любой другой браузерный фолбэк в этом проекте (см.
gui/browser_capture.py — та же логика: видимое окно вместо скрытого,
когда сайту это важно).

Используется как ПОСЛЕДНИЙ рубеж — core/link_resolver.py вызывает этот
модуль только для tass.ru и только после того, как обычный путь (yt-dlp +
HTML-скрейп) уже провалился; на остальные сайты модуль никак не влияет.
"""
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

from utils.logger import logger

# Сильный сигнал "это точно медиа" — расширение самого URL или MIME-тип
# ответа. Одних только слов "manifest/playlist/video/stream/media" в URL
# недостаточно (могут встречаться в аналитике, рекламных пикселях и т.п.)
# — такие совпадения только логируются для диагностики, но не считаются
# найденным потоком (см. _consider).
_MANIFEST_EXT_RE = re.compile(r'\.(m3u8|mpd|mp4|m4s)(\?|#|$)', re.IGNORECASE)
_MANIFEST_KEYWORD_RE = re.compile(r'(manifest|playlist|video|stream|media)', re.IGNORECASE)
_MANIFEST_MIME = {
    'application/vnd.apple.mpegurl', 'application/x-mpegurl',
    'video/mp4', 'application/dash+xml',
}
# Порядок предпочтения, если нашлось сразу несколько типов ссылок.
_EXT_PRIORITY = ('.m3u8', '.mpd', '.mp4', '.m4s')

_PLAY_JS = """
() => {
    var v = document.querySelector('video');
    if (!v) return 'no-video';
    if (!v.paused) return 'already-playing';
    var p = v.play();
    if (p && p.catch) p.catch(function() {});
    return 'play-requested';
}
"""

_DOM_VIDEO_SRC_JS = """
() => {
    var v = document.querySelector('video');
    if (!v) return null;
    return v.currentSrc || v.src || null;
}
"""


class TassProvider:
    """Достаёт прямую ссылку на видео tass.ru через Playwright в видимом
    окне — сперва пробует уже установленный Google Chrome, при его
    отсутствии переходит на свой Chromium (скачивая его при необходимости).
    См. докстринг модуля про то, почему именно headed-режим и почему это
    не обход защиты, а честный запуск настоящего браузера."""

    def is_available(self) -> bool:
        # Chromium может быть ещё не скачан на чистой машине — это не
        # является недоступностью, resolve() сам скачает его при первом
        # обращении (см. _ensure_bundled_chromium). "Недоступно" — только
        # если сам пакет playwright не установлен вообще, тут уже ничего
        # не поделать без переустановки приложения.
        return PLAYWRIGHT_AVAILABLE

    def _ensure_bundled_chromium(self, p) -> bool:
        """Playwright-Chromium уже стоит на диске — ничего не делаем.
        Иначе (первый запуск на чистой машине, Chrome при этом тоже не
        нашёлся) скачиваем один раз через собственный установочный драйвер
        playwright (тот же механизм, что и у команды `playwright install
        chromium`, но без обращения к системному python -m — внутри
        собранного .app sys.executable не питоновский интерпретатор, а сам
        драйвер playwright — отдельный бинарник внутри пакета, работает
        одинаково что в dev-режиме, что в frozen)."""
        if Path(p.chromium.executable_path).exists():
            return True
        logger.info("[TASS] Chrome не найден и встроенный Chromium ещё не скачан — "
                    "скачиваем один раз (~150-200 МБ, разово)")
        try:
            import subprocess

            from playwright._impl._driver import compute_driver_executable, get_driver_env
            driver_executable, driver_cli = compute_driver_executable()
            result = subprocess.run(
                [str(driver_executable), str(driver_cli), 'install', 'chromium'],
                env=get_driver_env(), capture_output=True, text=True, timeout=900,
            )
            if result.returncode != 0:
                logger.error(f"[TASS] Не удалось скачать Chromium: {(result.stderr or '')[-500:]}")
                return False
        except Exception as e:
            logger.error(f"[TASS] Ошибка при скачивании Chromium: {e}")
            return False
        logger.info("[TASS] Chromium установлен")
        return True

    def _launch_browser(self, p):
        """Сперва системный Chrome (channel="chrome" — ничего не качает,
        если он уже есть), иначе — Chromium из пакета playwright (при
        необходимости скачав его, см. _ensure_bundled_chromium). И то, и
        другое — реальные, немодифицированные движки в видимом окне, без
        какого-либо stealth поверх."""
        try:
            browser = p.chromium.launch(headless=False, channel="chrome")
            logger.info("[TASS] Используем установленный Google Chrome")
            return browser
        except Exception as e:
            logger.info(f"[TASS] Google Chrome не найден ({e.__class__.__name__}) — "
                        f"используем встроенный Chromium")
        if not self._ensure_bundled_chromium(p):
            return None
        return p.chromium.launch(headless=False)

    def _consider(self, u: str, mime: Optional[str], seen: set, candidates: list):
        if not u or u in seen:
            return
        is_ext_match = bool(_MANIFEST_EXT_RE.search(u))
        is_mime_match = mime in _MANIFEST_MIME if mime else False
        if not (is_ext_match or is_mime_match):
            if u.startswith('http') and _MANIFEST_KEYWORD_RE.search(u):
                logger.debug(f"[TASS] Похожий на медиа URL (по ключевому слову, не подтверждён): {u}")
            return
        seen.add(u)
        candidates.append((u, mime))
        logger.info(f"[TASS] Possible HLS/media manifest: {u}")

    def _pick_best(self, candidates: list) -> Optional[str]:
        for ext in _EXT_PRIORITY:
            for u, _mime in reversed(candidates):
                if u.split('?', 1)[0].lower().endswith(ext):
                    return u
        return candidates[-1][0] if candidates else None

    def _scan_dom_video(self, page) -> Optional[str]:
        """Приоритет 3 из требуемого порядка (Network > JSON/API > DOM/video
        element > HTML > обычный HTTP): TASS кладёт готовую ссылку прямо в
        атрибут src самого <video> (preload="auto"), а не тянет её JS-плеером
        через fetch/XHR — чистый сетевой перехват это пропускает. Прямое
        чтение currentSrc/src у реального DOM-элемента надёжнее и не зависит
        от того, успели ли мы поймать сам сетевой запрос."""
        try:
            return page.evaluate(_DOM_VIDEO_SRC_JS)
        except Exception:
            return None

    def _try_play(self, page):
        try:
            result = page.evaluate(_PLAY_JS)
            logger.info(f"[TASS] Play requested: {result}")
        except Exception as e:
            logger.debug(f"[TASS] Play() не выполнился: {e}")

    def _resolve_hls_variant(self, master_url: str, headers: dict) -> Optional[str]:
        try:
            resp = requests.get(master_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
            body = resp.text
        except Exception:
            return None
        if '#EXT-X-STREAM-INF' not in body:
            return master_url  # уже media playlist, не master

        logger.info("[TASS] HLS master detected")
        lines = body.splitlines()
        variants = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF') and i + 1 < len(lines):
                m_res = re.search(r'RESOLUTION=\d+x(\d+)', line)
                m_bw = re.search(r'BANDWIDTH=(\d+)', line)
                height = int(m_res.group(1)) if m_res else 0
                bw = int(m_bw.group(1)) if m_bw else 0
                variant_url = urljoin(master_url, lines[i + 1].strip())
                variants.append((height, bw, variant_url))
        if not variants:
            return master_url

        variants.sort(key=lambda v: (v[0], v[1]), reverse=True)
        heights = sorted({v[0] for v in variants if v[0]}, reverse=True)
        logger.info(f"[TASS] Available qualities: {heights or ['?']}")
        best = variants[0]
        logger.info(f"[TASS] Selected quality: {best[0] or '?'}p")
        return best[2]

    def resolve(self, url: str, timeout: float = 60.0) -> Optional[dict]:
        """Возвращает {'video_url','headers','title'} или None, если за
        timeout секунд ничего подходящего не нашлось (Playwright/Chromium
        не установлены, страница не открылась, или плеер не отдал ссылку
        на видео)."""
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("[TASS] Пакет playwright не установлен — pip install playwright")
            return None

        logger.info("[TASS] Connecting to Chromium")
        try:
            with sync_playwright() as p:
                # headless=False намеренно — см. докстринг модуля: headless-режим
                # эту защиту не проходит, а патчить его под вид обычного окна
                # означало бы как раз тот stealth, которого делать не просим.
                browser = self._launch_browser(p)
                if browser is None:
                    return None
                try:
                    context = browser.new_context(viewport={'width': 1400, 'height': 900})
                    page = context.new_page()

                    seen: set = set()
                    candidates: list = []
                    page.on('request', lambda req: self._consider(req.url, None, seen, candidates))

                    def on_response(resp):
                        try:
                            mime = resp.headers.get('content-type', '').split(';')[0].strip()
                        except Exception:
                            mime = None
                        self._consider(resp.url, mime, seen, candidates)

                    page.on('response', on_response)

                    logger.info(f"[TASS] Page found: открываем статью {url}")
                    try:
                        page.goto(url, timeout=25000, wait_until='domcontentloaded')
                    except PlaywrightTimeoutError:
                        # Рекламные/трекинговые запросы на странице TASS не
                        # затихают долго (замечено вживую) — 'domcontentloaded'
                        # обычно уже успевает сработать раньше; если нет —
                        # статья всё равно могла отрисоваться частично,
                        # продолжаем и просто ждём видео ниже.
                        logger.warning("[TASS] Страница грузится дольше ожидаемого — продолжаем")

                    logger.info("[TASS] Waiting for video")
                    try:
                        real_ua = page.evaluate("() => navigator.userAgent")
                    except Exception:
                        real_ua = None

                    deadline = time.time() + timeout
                    play_attempted = False
                    play_at = time.time() + 3.0  # дать странице секунду-другую догрузиться перед Play

                    while time.time() < deadline and not candidates:
                        if not play_attempted and time.time() >= play_at:
                            play_attempted = True
                            self._try_play(page)

                        dom_src = self._scan_dom_video(page)
                        if dom_src:
                            mime = 'video/mp4' if dom_src.split('?', 1)[0].lower().endswith('.mp4') else None
                            self._consider(dom_src, mime, seen, candidates)
                            if candidates:
                                break

                        time.sleep(0.5)

                    best = self._pick_best(candidates)
                    if not best:
                        logger.warning("[TASS] Видео не найдено за отведённое время — убедитесь, что статья "
                                        "открывается и видео действительно есть на странице.")
                        return None

                    headers = {'User-Agent': real_ua or 'Mozilla/5.0', 'Referer': url}
                    try:
                        cookies = context.cookies()
                    except Exception:
                        cookies = []
                    cookie_header = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
                    if cookie_header:
                        headers['Cookie'] = cookie_header

                    video_url = best
                    if best.split('?', 1)[0].lower().endswith('.m3u8'):
                        video_url = self._resolve_hls_variant(best, headers) or best

                    try:
                        title = page.title()
                    except Exception:
                        title = None

                    logger.info(f"[TASS] Downloading with ffmpeg: {video_url}")
                    logger.info("[TASS] Complete")
                    return {'video_url': video_url, 'headers': headers, 'title': title}
                finally:
                    browser.close()
        except PlaywrightError as e:
            logger.error(f"[TASS] Ошибка Playwright: {e}")
            return None
