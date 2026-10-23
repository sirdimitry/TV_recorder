"""Additive adapters for srch-dwnld 7660027; existing resolvers stay as fallbacks."""
import asyncio
from urllib.parse import urlsplit

from core.source_resolvers import direct_media, nextcloud, ren_tv, smotrim, tiktok_page, tv1, tvzvezda, vipler, webcaster
from core.source_resolvers.common import DEFAULT_UA
from utils.logger import logger


def resolve_expanded(url, timeout, target_height, *, allow_browser=True):
    from core.link_resolver import LinkInfo, _resolve_via_ytdlp
    async def resolve():
        host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
        result = None
        if tiktok_page.is_tiktok_video(url):
            item = await tiktok_page.resolve(url)
            if item:
                media, title, _, duration, cookie = item
                headers = dict(tiktok_page.DOWNLOAD_HEADERS)
                if cookie:
                    headers['Cookie'] = cookie
                return LinkInfo(ok=True, video_url=media, title=title, duration=duration, headers=headers)
        elif host in {'1tv.ru'}:
            result = await tv1.resolve(url)
        elif host in {'smotrim.ru', 'player.smotrim.ru', 'embed.smotrim.ru'}:
            result = await smotrim.resolve(url)
        elif host == 'tvzvezda.ru':
            result = await tvzvezda.resolve(url)
        elif host == 'ren.tv':
            item = await ren_tv.resolve(url)
            if item:
                media, title, _, duration = item
                return LinkInfo(ok=True, video_url=media, title=title or url, duration=duration,
                                headers={'User-Agent': DEFAULT_UA, 'Referer': url})
        elif nextcloud.URL_RE.match(url):
            item = await nextcloud.resolve(url)
            if item:
                return LinkInfo(ok=True, title=item[0], video_url=item[1][0],
                                headers={'User-Agent': DEFAULT_UA, 'Referer': url})
        # Social sites have their own extractors and permission semantics.
        # An unrelated HTML <video> there may be an advertisement.
        if host in {'vk.com', 'vkvideo.ru', 'ok.ru', 'youtube.com', 'youtu.be', 'tass.ru'}:
            return None
        if result:
            return LinkInfo(ok=True, video_url=result[0], title=result[1] or url,
                            headers={'User-Agent': DEFAULT_UA, 'Referer': url})
        config = await webcaster.resolve(url)
        if config:
            info = await asyncio.to_thread(_resolve_via_ytdlp, config, max(30, timeout), target_height, url)
            if info.ok:
                return info
        embedded = await smotrim.resolve_embed(url)
        if embedded:
            result = embedded
        elif allow_browser and host in {'ren.tv', 'iz.ru', '5-tv.ru', 'sport-express.ru'}:
            result = await vipler.resolve(url)
        if not result:
            result = await direct_media.resolve(url)
        if result:
            return LinkInfo(ok=True, video_url=result[0], title=result[1] or url,
                            headers={'User-Agent': DEFAULT_UA, 'Referer': url})
        return None
    try:
        return asyncio.run(asyncio.wait_for(resolve(), timeout=max(15, timeout * 2)))
    except Exception as error:
        logger.debug('Дополнительный обработчик srch-dwnld: %s', type(error).__name__)
        return None
