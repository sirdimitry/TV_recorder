"""Общее для резолверов конкретных сайтов (соседние модули в этом пакете) —
HTTP-запрос с повторами и заголовок настоящего браузера, который часть сайтов
требует, чтобы не отвечать 403 (см. _DEFAULT_UA)."""

from __future__ import annotations

from httpx import AsyncClient as SafeAsyncClient

import asyncio
import re

import httpx

from utils.logger import logger



_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Голое "Mozilla/5.0" часть сайтов (замечено на tass.ru, ren.tv) читает как
# явный признак бота и отвечает 403 — полноценная строка настоящего браузера
# проходит без вопросов (перепроверено на практике в core/link_resolver.py
# соседнего проекта tv_recorder).
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


async def fetch_with_retries(
    url: str, *, referer: str | None = None, timeout: float = 30, retries: int = 3, log_tag: str = "",
) -> str | None:
    """GET с повторами — обвязки вроде webcaster.pro/vipler периодически не
    укладываются в таймаут на случайном шаге цепочки (замечено то на XML-
    конфиге, то на самом iframe, то на embed-странице) — не постоянный сбой,
    почти всегда помогает повтор через пару секунд."""
    headers = {"User-Agent": DEFAULT_UA}
    if referer:
        headers["Referer"] = referer
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            async with SafeAsyncClient(
                timeout=timeout, follow_redirects=True, headers=headers, proxy=None,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.info("[probe] %sпопытка %d/%d получить %s не удалась: %s", f"{log_tag}: " if log_tag else "", attempt, retries, url, exc)
            if attempt < retries:
                await asyncio.sleep(2)
    logger.info("[probe] %sне удалось получить %s: %s", f"{log_tag}: " if log_tag else "", url, last_exc)
    return None


def extract_title_tag(page: str) -> str | None:
    """Настоящий <title> уже скачанной страницы — общий кусочек логики,
    которым пользуются несколько резолверов (vipler, direct_media и т.п.),
    чтобы не тянуть отдельно html/re в каждый модуль."""
    import html

    match = _TITLE_TAG_RE.search(page)
    if not match:
        return None
    return re.sub(r"\s+", " ", html.unescape(match.group(1)).strip()) or None


async def guard_browser_route(route):
    from urllib.parse import urlsplit
    parsed = urlsplit(route.request.url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
        await route.abort()
    else:
        await route.continue_()


async def block_browser_websocket(route):
    await route.close()
