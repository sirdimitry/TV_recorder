"""Публичная ссылка на файл в Nextcloud (вида https://<хост>/s/<токен>) — так
ОНФ (cloud.onf.ru) и, вероятно, другие организации на своём Nextcloud,
делятся видео с журналистами. У Nextcloud есть стандартный маршрут
.../download, отдающий сам файл напрямую, без всякого антибота — не
специфично для onf.ru, сработает на любом Nextcloud."""

from __future__ import annotations

from httpx import AsyncClient as SafeAsyncClient

import html
import re

import httpx

from utils.logger import logger
from .common import DEFAULT_UA



URL_RE = re.compile(r"^(https?://[^/]+/s/[A-Za-z0-9]+)")
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"', re.IGNORECASE)


async def resolve(url: str) -> tuple[str, list[str]] | None:
    """(заголовок, [прямая ссылка на файл]) — .../download отдаёт сам файл
    напрямую, og:title на странице шаринга обычно и есть исходное имя файла
    (проверено на cloud.onf.ru). None, если это не похоже на такую ссылку."""
    match = URL_RE.match(url)
    if not match:
        return None
    share_url = match.group(1)

    title = None
    try:
        async with SafeAsyncClient(
            timeout=8, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}, proxy=None,
        ) as client:
            resp = await client.get(share_url)
            resp.raise_for_status()
            page = resp.text
    except Exception as exc:  # noqa: BLE001 — название необязательно, дальше берём то, что есть
        logger.info("[probe] Nextcloud: не удалось получить страницу шаринга: %s", exc)
        page = ""

    og_match = _OG_TITLE_RE.search(page)
    if og_match:
        title = html.unescape(og_match.group(1)).strip()
        title = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", title)  # расширение добавит download_link сам

    return title or "media", [f"{share_url}/download"]
