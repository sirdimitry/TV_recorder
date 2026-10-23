""""vipler" — общий видеоплеер сети сайтов (ren.tv, iz.ru, 5-tv.ru,
sport-express.ru — виден общий словарь тем в его собственном JS,
window.config...themes={"ren.tv":...,"iz.ru":...,"5-tv.ru":...}). Раньше
статья вставляла видео как <iframe class="igi-player" src="/video/embed/<id>">
прямо в статический HTML, а embed-страница публиковала ссылку на поток
открытым текстом в window.config = {...,"sources":[{"hls":"...",
"reserve":{"mp4":"..."}}]} — обычный HTTP-запрос без браузера. Подход
перенесён из core/link_resolver.py соседнего проекта tv_recorder.

Часть сайтов (замечено на ren.tv) с тех пор сменила видео-платформу на
PlatformCraft — <iframe> в статье больше нет (только <meta property=
"og:video">), а сама embed-страница вместо window.config с готовой ссылкой
несёт объект PlatformCraft (id, path, video: "video.api.platformcraft.ru/id"
— это, впрочем, не публичный редиректор, а требующий подписи API, дёргать
напрямую нельзя, см. их доку). Прямую ссылку на файл (CDN вида
vod2<site>.cdnvideo.ru/<site>/<path>) достаём вместо этого сниффингом
настоящего сетевого ответа headless-браузера на embed-странице (см.
_sniff_embed_video) — так же, как resolvers/browser_fallback.py, но
целимся сразу в embed-страницу, а не в статью (плеер в самой статье часто
не запускает загрузку видео без клика)."""

from __future__ import annotations

from core.source_resolvers.common import guard_browser_route, block_browser_websocket

import html
import re

import httpx

from .common import extract_title_tag, fetch_with_retries

_IFRAME_RE = re.compile(r'<iframe[^>]+src="([^"]*/video/embed/\d+[^"]*)"', re.IGNORECASE)
# Запасной путь — часть статей (замечено на ren.tv) больше не встраивает
# <iframe> прямо в HTML, но ту же embed-ссылку всё равно публикует в
# <meta property="og:video" content="...">.
_OG_VIDEO_RE = re.compile(r'<meta[^>]+property="og:video"[^>]*content="([^"]+)"', re.IGNORECASE)
# Ещё один запасной путь (замечено на ren.tv, "/longread/"-статьи) — ни
# <iframe>, ни og:video, а плеер собирается на лету JS-ом из
# <link href="https://ren.tv/video/embed/ID"> — обычного href/src-атрибута с
# той же embed-ссылкой внутри, просто не на ожидаемом теге. Ищем саму
# ссылку по содержимому, не привязываясь к тому, в каком теге она лежит —
# сайт менял разметку уже дважды (iframe -> og:video -> link), проще не
# гадать, какой тег будет в следующий раз.
_EMBED_HREF_RE = re.compile(r'(?:src|href)="([^"]*/video/embed/\d+[^"]*)"', re.IGNORECASE)
_HLS_RE = re.compile(r'"hls"\s*:\s*"([^"]+)"')
_MP4_RE = re.compile(r'"reserve"\s*:\s*\{\s*"mp4"\s*:\s*"([^"]+)"')


async def _sniff_embed_video(embed_url: str, referer: str) -> str | None:
    """Запасной путь для PlatformCraft-сайтов (см. докстринг модуля) — открывает
    embed-страницу настоящим headless-браузером и ждёт реальный сетевой ответ с
    видео (Content-Type video/* или URL на .mp4/.m3u8). None, если Playwright не
    установлен или так ничего и не прилетело за время ожидания."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    def _looks_like_media(resp_url: str, content_type: str) -> bool:
        if content_type.startswith("video/") or "mpegurl" in content_type:
            return True
        path = resp_url.lower().split("?", 1)[0]
        return path.endswith(".mp4") or path.endswith(".m3u8")

    media_url: str | None = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            try:
                # НЕ подставляем DEFAULT_UA (Safari/Mac) сюда — это настоящий
                # Chromium, и часть плееров (замечено на PlatformCraft/ren.tv)
                # сами выбирают HLS/mp4 по User-Agent: с чужим (Safari) UA на
                # Chromium-движке плеер вообще не запускает загрузку видео —
                # ни одного сетевого запроса на сам файл (проверено вручную:
                # с этим UA — пусто, с родным UA Chromium — сразу находит
                # mp4). Родной UA Playwright всё равно свежий и не палится
                # как автоматизация сам по себе.
                page = await browser.new_page(service_workers="block")

                def _on_response(response) -> None:
                    nonlocal media_url
                    if media_url:
                        return
                    content_type = (response.headers.get("content-type") or "").lower()
                    if _looks_like_media(response.url, content_type):
                        media_url = response.url

                page.on("response", _on_response)
                try:
                    # networkidle тут не годится — часть страниц (замечено на
                    # ren.tv) держит сетевую активность (реклама/аналитика)
                    # непрерывно и networkidle никогда не наступает.
                    await page.context.route("**/*", guard_browser_route)
                    await page.context.route_web_socket("**/*", block_browser_websocket)
                    await page.goto(embed_url, wait_until="domcontentloaded", timeout=20000, referer=referer)
                except Exception:  # noqa: BLE001 — всё равно ждём ответ ниже
                    pass
                if not media_url:
                    # Плеер сам не запускает загрузку видео без клика (обычный
                    # браузерный autoplay-policy — проверено вручную) — кликаем
                    # по чему-нибудь похожему на плеер/кнопку play.
                    try:
                        await page.click("video, .player, [class*=play]", timeout=3000)
                    except Exception:  # noqa: BLE001 — нет такого элемента — не страшно, попробуем без клика
                        pass
                if not media_url:
                    await page.wait_for_timeout(6000)
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001 — это последний запасной путь, не критично
        return None
    return media_url


async def resolve(url: str) -> tuple[str, str | None] | None:
    """Если статья встраивает видео через плеер "vipler" — возвращает (прямую
    ссылку на поток [.m3u8 или .mp4], заголовок статьи), иначе None. Заголовок
    берём сразу из уже скачанной страницы статьи — экстрактор yt-dlp для
    голого .m3u8/.mp4 URL ничего осмысленного не подскажет, а второй раз
    ходить за <title> отдельным запросом незачем. Для скачивания самого
    потока сайт требует Referer на исходную статью — см. использование в
    core/link_download.py."""
    page = await fetch_with_retries(url, log_tag="vipler: страница")
    if page is None:
        return None

    iframe_match = _IFRAME_RE.search(page)
    if iframe_match:
        embed_url = html.unescape(iframe_match.group(1))
    else:
        og_match = _OG_VIDEO_RE.search(page)
        if og_match:
            embed_url = html.unescape(og_match.group(1))
        else:
            href_match = _EMBED_HREF_RE.search(page)
            if not href_match:
                return None
            embed_url = html.unescape(href_match.group(1))
    if embed_url.startswith("//"):
        embed_url = f"https:{embed_url}"
    elif embed_url.startswith("/"):
        embed_url = str(httpx.URL(url).join(embed_url))

    embed_page = await fetch_with_retries(embed_url, referer=url, log_tag="vipler: embed-страница")
    if embed_page is None:
        return None

    stream_url = None
    hls_match = _HLS_RE.search(embed_page)
    if hls_match:
        stream_url = html.unescape(hls_match.group(1).replace("\\/", "/"))
    else:
        mp4_match = _MP4_RE.search(embed_page)
        if mp4_match:
            stream_url = html.unescape(mp4_match.group(1).replace("\\/", "/"))

    if not stream_url:
        # Старый window.config с готовой ссылкой не нашёлся — похоже, сайт
        # уже на PlatformCraft (см. докстринг модуля), пробуем сниффингом.
        stream_url = await _sniff_embed_video(embed_url, url)

    if not stream_url:
        return None

    return stream_url, extract_title_tag(page)
