"""Wink/Ngenix catch-up with bounded VOD URLs and verified broadcast dates."""
import asyncio
import hashlib
import json
import math
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import truststore

from core.recorder import Recorder
from core.storage import Storage
from core.tv_archive import ArchivePlan, ArchiveSession, ArchiveSourceError, ArchiveUnavailable, MOSCOW, Segment, _check_cancel
from utils.config import Config

CATALOG_URL = 'https://raw.githubusercontent.com/CrocoUser/zabava-project/main/zabava-full.m3u'
CATALOG_SOURCE = 'https://github.com/CrocoUser/zabava-project'


@dataclass(frozen=True)
class StreamArchiveChannel:
    slug: str
    name: str
    url: str = field(repr=False)
    headers: dict = field(default_factory=dict, repr=False, compare=False)
    provider: str = 'ngenix'
    group: str = 'Мои потоки'


def parse_catalog(text):
    """Retain every region/time-shift, not the live tab's federal shortlist."""
    result, seen = [], set()
    name, group, agent = '', '', 'HlsWinkPlayer'
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#EXTINF:'):
            name = line.split(',', 1)[-1].strip()
            match = re.search(r'group-title="([^"]+)"', line)
            group = match[1] if match else 'Другие'
            agent = 'HlsWinkPlayer'
        elif line.startswith('#EXTVLCOPT:http-user-agent='):
            agent = line.split('=', 1)[1]
        elif line and not line.startswith('#') and name:
            try:
                url = validate_stream_url(line)
            except ValueError:
                continue
            if url not in seen:
                seen.add(url)
                result.append(StreamArchiveChannel(stream_slug(url), name, url,
                              {'User-Agent': agent}, group=group))
            name = ''
    return tuple(result)


def stream_slug(url):
    return 'stream_' + hashlib.sha256(url.encode()).hexdigest()[:20]


def validate_stream_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if (parts.scheme not in {'http', 'https'} or not host.endswith('.ngenix.net')
            or not parts.path.endswith('.m3u8') or parts.username or parts.password):
        raise ValueError('Нужна ссылка .m3u8 на поток Wink/Ngenix (домен *.ngenix.net).')
    return url


def vod_url(url, start, end):
    """Both bounds are essential: utcstart alone can silently return live TV."""
    validate_stream_url(url)
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in ('offset', 'utcstart', 'utcend', 'useseq'):
        query.pop(key, None)
    query.update(utcstart=str(math.floor(start.timestamp())), utcend=str(math.ceil(end.timestamp())))
    return urlunsplit(parts._replace(query=urlencode(query)))


def stream_channels(storage=None):
    storage = storage or Storage()
    result = []
    sources = storage.get_channels()
    try:
        extra = json.loads((Config.DATA_DIR / 'archive_streams.json').read_text(encoding='utf-8'))
        if isinstance(extra, list):
            sources += extra
    except (OSError, ValueError):
        pass
    seen = set()
    for channel in sources:
        try:
            url = validate_stream_url(channel.get('url', ''))
        except ValueError:
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(StreamArchiveChannel(stream_slug(url),
                                          channel['name'], url, Recorder.channel_headers(channel)))
    # Bundle the complete public catalog for immediate/offline startup. The
    # controller updates its cached copy independently of UI construction.
    for path in (Config.DATA_DIR / 'zabava-full.m3u', Config.BASE_DIR / 'data' / 'zabava-full.m3u'):
        try:
            catalog = parse_catalog(path.read_text(encoding='utf-8-sig'))
            if not catalog:
                continue
        except OSError:
            continue
        for channel in catalog:
            if channel.url not in seen:
                result.append(channel)
                seen.add(channel.url)
        break
    return tuple(result)


def parse_timed_playlist(text, manifest_url, request):
    if not text.lstrip().startswith('#EXTM3U'):
        raise ArchiveSourceError('Источник не вернул HLS-плейлист.')
    if '#EXT-X-ENDLIST' not in text:
        raise ArchiveSourceError('Источник вернул эфир вместо ограниченного архива. Время просмотра не подтверждено.')
    cursor, duration, first, previous_end = None, None, None, None
    segments = []
    gap = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXT-X-KEY:') and 'METHOD=NONE' not in line:
            raise ArchiveSourceError('Защищённый поток не поддерживается.')
        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            try:
                cursor = datetime.fromisoformat(line.split(':', 1)[1].replace('Z', '+00:00'))
                if cursor.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ArchiveSourceError('Некорректная дата HLS-фрагмента.') from None
        elif line.startswith('#EXTINF:'):
            try:
                duration = float(line.split(':', 1)[1].split(',')[0])
                if not math.isfinite(duration) or not 0 < duration <= 60:
                    raise ValueError()
            except ValueError:
                raise ArchiveSourceError('Некорректная длительность HLS-фрагмента.') from None
        elif line == '#EXT-X-GAP':
            gap = True
        elif line and not line.startswith('#'):
            url = urljoin(manifest_url, line)
            validate_stream_url(url.rsplit('/', 1)[0] + '/playlist.m3u8')
            stamp = re.search(r'/timeline/(\d{13})\.(?:ts|m4s)(?:\?|$)', url)
            if stamp:
                timestamp = datetime.fromtimestamp(int(stamp[1]) / 1000, MOSCOW)
                if cursor is not None and abs((timestamp - cursor).total_seconds()) > .5:
                    raise ArchiveSourceError('Временные метки архива не совпадают.')
                cursor = timestamp
            if cursor is None or duration is None:
                raise ArchiveSourceError('Поток не сообщает подтверждённую дату и время записи.')
            end = cursor + timedelta(seconds=duration)
            if end > request.start and cursor < request.end:
                if gap or (previous_end is not None and abs((cursor - previous_end).total_seconds()) > .5):
                    raise ArchiveUnavailable('В выбранном интервале есть пропуск.')
                if first is None:
                    first = cursor
                segments.append(Segment(url, duration))
                previous_end = end
            cursor, duration, gap = end, None, False
    if first is None or first > request.start or previous_end < request.end:
        raise ArchiveUnavailable('Этот интервал не сохранился в архиве канала.')
    return tuple(segments), (request.start - first).total_seconds(), first


@dataclass
class StreamArchivePlan(ArchivePlan):
    headers: dict = field(default_factory=dict, repr=False)
    media_start: datetime | None = None


class StreamArchiveSession(ArchiveSession):
    """Reuse checked segment assembly and exact trim from the OnTVTime path."""
    def __init__(self, channel, *, transport=None):
        self.channel = channel
        self.headers = channel.headers
        self.client = httpx.AsyncClient(
            headers=self.headers, timeout=15, follow_redirects=True, transport=transport,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    async def _read(self, url, *, limit=2_000_000, media=False, headers=None, progress=None, prefix=False):
        parts = urlsplit(url)
        validate_stream_url(urlunsplit(parts._replace(path='/playlist.m3u8')))
        _check_cancel(progress)
        try:
            request_headers = {**self.headers, **({'Range': 'bytes=0-563'} if prefix else {})}
            async with self.client.stream('GET', url, headers=request_headers) as response:
                if response.status_code in {404, 410}:
                    raise ArchiveUnavailable('Этот интервал недоступен или уже удалён из архива.')
                if response.status_code in {401, 403}:
                    raise ArchiveSourceError('Источник отказал в подключении. Проверьте адрес и User-Agent потока.')
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    _check_cancel(progress)
                    if prefix:
                        body.extend(chunk[:max(0, 564-len(body))])
                        if len(body) >= 564:
                            break
                        continue
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ArchiveSourceError('Источник вернул слишком большой фрагмент.')
                return bytes(body), response.encoding or 'utf-8'
        except httpx.HTTPError as error:
            raise ArchiveSourceError('Не удалось связаться с потоком. Попробуйте другое время или повторите подключение.') from error

    async def probe(self, request, progress=None, *, quick=False):
        if not 0 < request.duration <= 4 * 3600 or request.end > datetime.now(MOSCOW):
            raise ValueError('Выберите прошедший интервал длительностью до 4 часов.')
        master_url = vod_url(self.channel.url, request.start, request.end)
        body, _ = await self._read(master_url, progress=progress)
        text = body.decode('utf-8-sig', errors='replace')
        media_url = master_url
        lines = text.splitlines()
        variants = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF:'):
                bw = re.search(r'BANDWIDTH=(\d+)', line)
                height = re.search(r'RESOLUTION=\d+x(\d+)', line)
                path = next((value.strip() for value in lines[i+1:] if value.strip() and not value.startswith('#')), None)
                if path:
                    variants.append((int(height[1]) if height else 0, int(bw[1]) if bw else 0,
                                     urljoin(master_url, path)))
        if variants:
            suitable = [item for item in variants if item[0] <= 720 and item[1] <= 5_000_000] or variants
            media_url = max(suitable, key=lambda item: item[:2])[2]
            # Pin the same absolute bounds even if a master drops query fields.
            media_url = vod_url(media_url, request.start, request.end)
            body, _ = await self._read(media_url, progress=progress)
            text = body.decode('utf-8-sig', errors='replace')
        segments, offset, first = parse_timed_playlist(text, media_url, request)
        plan = StreamArchivePlan(request, self.headers.get('Referer', ''), offset, segments,
                                 manifest_url=media_url, headers=self.headers, media_start=first)
        indices = list(dict.fromkeys((0, len(segments)-1)))
        if quick:
            for i in indices:
                chunk, _ = await self._read(segments[i].url, prefix=True, progress=progress)
                if len(chunk) < 564 or any(chunk[n] != 0x47 for n in (0, 188, 376)):
                    raise ArchiveSourceError('Источник не передаёт транспортный видеопоток.')
        else:
            chunks = await asyncio.gather(*(self._segment(segments[i], self.headers, progress) for i in indices))
            plan.cached.update(zip(indices, chunks))
        return plan

    async def live_available(self):
        body, _ = await self._read(self.channel.url)
        text = body.decode('utf-8-sig', errors='replace')
        paths = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
        if not text.startswith('#EXTM3U') or not paths:
            return False
        url = urljoin(self.channel.url, paths[0])
        if '#EXT-X-STREAM-INF:' in text:
            body, _ = await self._read(url)
            text = body.decode('utf-8-sig', errors='replace')
            paths = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
            if not paths or '#EXTINF:' not in text:
                return False
            url = urljoin(url, paths[0])
        if any(line.startswith('#EXT-X-KEY:') and 'METHOD=NONE' not in line for line in text.splitlines()):
            raise ArchiveSourceError('Защищённый поток не поддерживается.')
        body, _ = await self._read(url, prefix=True)
        return len(body) >= 564 and all(body[n] == 0x47 for n in (0, 188, 376))
