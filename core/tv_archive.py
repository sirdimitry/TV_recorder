"""OnTVTime archive sessions, availability checks and bounded HLS downloads."""
from __future__ import annotations

import asyncio
import difflib
import html
import math
import re
import shutil
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import httpx
import truststore

from core.archive_media import resolve_public
from core.archive_media import ArchiveProgress as EncodeProgress, ArchiveCanceled as _StoppedByUser, _probe_duration, _run_auto_video
_NO_WINDOW_KWARGS = {}
from utils.logger import logger
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15"

MOSCOW = timezone(timedelta(hours=3))
BASE_URL = "https://www.ontvtime.ru"
MAX_ARCHIVE_SECONDS = 4 * 3600


class ArchiveUnavailable(Exception):
    """The requested recording is absent, expired or incomplete."""


class ArchiveSourceError(Exception):
    """The service could not be checked; this does not establish absence."""


@dataclass(frozen=True)
class ArchiveChannel:
    slug: str
    name: str
    aliases: tuple[str, ...] = ()


CHANNELS = (
    ArchiveChannel("zvezda", "Звезда", ("звезда", "звездв", "zvezda", "телеканал звезда")),
    ArchiveChannel("ort", "Первый канал", ("первый", "первый канал", "1 канал", "орт", "1тв")),
    ArchiveChannel("russia1", "Россия 1", ("россия 1", "россия1", "рtr", "ртр")),
    ArchiveChannel("russia24", "Россия 24", ("россия 24", "россия24")),
    ArchiveChannel("russiak", "Культура", ("культура", "россия к", "россия культура")),
    ArchiveChannel("rentv", "РЕН ТВ", ("рен тв", "рентв", "рен", "rentv", "ren tv")),
    ArchiveChannel("tvc", "ТВЦ", ("твц", "тв центр", "твцентр", "tvc")),
    ArchiveChannel("channel5", "Пятый канал", ("пятый", "пятый канал", "5 канал", "5 тв", "5тв")),
    ArchiveChannel("otr", "ОТР", ("отр", "otr", "общественное телевидение россии")),
    ArchiveChannel("mir", "МИР", ("мир", "mir")),
    ArchiveChannel("mir24", "МИР 24", ("мир 24", "мир24", "mir24")),
    ArchiveChannel("moskva24", "Москва 24", ("москва 24", "москва24", "м24")),
    ArchiveChannel("rbc", "РБК", ("рбк", "rbc")),
    ArchiveChannel("ctv", "Центральное телевидение", ("цтв", "центральное телевидение")),
    ArchiveChannel("che", "Че", ("че", "че!", "чё", "che")),
    ArchiveChannel("spas", "Спас", ("спас", "spas")),
    ArchiveChannel("karusel-3", "Карусель", ("карусель", "karusel")),
)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold().replace("ё", "е")))


def resolve_channel(value: str) -> ArchiveChannel:
    value = _normalize(value)
    aliases = {_normalize(alias): channel for channel in CHANNELS
               for alias in (channel.name, channel.slug, *channel.aliases)}
    if value in aliases:
        return aliases[value]
    # Only unambiguous, close spelling mistakes are corrected.
    scored = sorted(((difflib.SequenceMatcher(None, value, alias).ratio(), alias)
                     for alias in aliases if len(alias) >= 5 and len(value) >= 5), reverse=True)
    if scored and scored[0][0] >= .8:
        best = aliases[scored[0][1]]
        if not any(score >= scored[0][0] - .06 and aliases[alias] != best for score, alias in scored[1:]):
            return best
    raise ValueError("Не нашёл такой канал в архиве Онтайм. Доступны: " + ", ".join(c.name for c in CHANNELS) + ".")


@dataclass(frozen=True)
class ArchiveRequest:
    channel: ArchiveChannel
    start: datetime
    end: datetime

    @property
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class Segment:
    url: str = field(repr=False)
    duration: float
    gap: bool = False


@dataclass
class ArchivePlan:
    request: ArchiveRequest
    page_url: str
    offset: float
    segments: tuple[Segment, ...] = field(repr=False)
    cached: dict[int, bytes] = field(default_factory=dict, repr=False)
    manifest_url: str = field(default="", repr=False)


def _check_cancel(progress):
    if progress is not None and progress.stop_requested:
        raise _StoppedByUser("Остановлено пользователем.")
    if progress is not None and progress.stalled:
        raise ArchiveSourceError("Сайт перестал передавать запись. Попробуйте позже.")


def _check_host(url: str, *, media: bool = False):
    parsed = urlsplit(url)
    valid = re.fullmatch(r"s\d+\.ontvtime\.ru", parsed.hostname or "") if media else parsed.hostname == "www.ontvtime.ru"
    if not valid or parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ArchiveSourceError("Сайт вернул неподдерживаемый адрес архива.")


def parse_playlist(text: str, manifest_url: str, needed: float) -> tuple[Segment, ...]:
    if not text.lstrip().startswith("#EXTM3U"):
        raise ArchiveSourceError("Не удалось прочитать список фрагментов архива.")
    result, total, duration, gap, previous_number = [], 0., None, False, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line:
            raise ArchiveSourceError("Этот архив использует неподдерживаемый защищённый поток.")
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                raise ArchiveSourceError("Некорректная длительность фрагмента архива.") from None
            if not math.isfinite(duration) or not 0 < duration <= 60:
                raise ArchiveSourceError("Некорректная длительность фрагмента архива.")
        elif line == "#EXT-X-GAP":
            gap = True
        elif line and not line.startswith("#"):
            url = urljoin(manifest_url, line)
            _check_host(url, media=True)
            if urlsplit(url).netloc != urlsplit(manifest_url).netloc or not urlsplit(url).path.startswith(
                    urlsplit(manifest_url).path.rsplit("/", 1)[0] + "/"):
                raise ArchiveSourceError("Сайт вернул неподдерживаемый фрагмент архива.")
            number = re.search(r"hls(\d+)\.ts$", urlsplit(url).path)
            if duration is None or number is None:
                raise ArchiveSourceError("Формат архива изменился.")
            number = int(number[1])
            if previous_number is not None and number != previous_number + 1:
                gap = True
            if gap:
                raise ArchiveUnavailable("В записи есть пропуск.")
            result.append(Segment(url, duration))
            total += duration
            if total + .05 >= needed:
                return tuple(result)
            previous_number, duration, gap = number, None, False
    raise ArchiveUnavailable("Архив не покрывает весь запрошенный интервал.")


class _ArchiveTransport(httpx.AsyncBaseTransport):
    """Try all checked IPs, retaining the hostname for Host and TLS verification.

    OnTVTime currently advertises multiple addresses and one can stop answering
    TLS. Pinning the chosen IP also works through the configured SOCKS proxy.
    """
    def __init__(self, factory):
        self.factory = factory
        self.transports = {}
        self.preferred = {}

    async def handle_async_request(self, request):
        host, port = request.url.host, request.url.port or 443
        _check_host(str(request.url), media=host != 'www.ontvtime.ru')
        answers = await asyncio.to_thread(resolve_public, host, port)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        addresses.sort(key=lambda address: address != self.preferred.get(host))
        if host not in self.transports:
            self.transports[host] = self.factory()
        transport = self.transports[host]
        for index, address in enumerate(addresses):
            extensions = dict(request.extensions)
            extensions['sni_hostname'] = host
            extensions['timeout'] = {**extensions.get('timeout', {}), 'connect': 8.}
            pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                                   headers=request.headers, stream=request.stream, extensions=extensions)
            try:
                response = await transport.handle_async_request(pinned)
                self.preferred[host] = address
                return response
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses)-1:
                    raise

    async def aclose(self):
        await asyncio.gather(*(transport.aclose() for transport in self.transports.values()))


class ArchiveSession:
    """Keep the site's playback session alive from preflight through download."""
    def __init__(self, *, transport=None):
        # Native certificate validation also retrieves this site's missing intermediates on Windows.
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if transport is None:
            transport = _ArchiveTransport(lambda: httpx.AsyncHTTPTransport(
                verify=context, trust_env=False,
            ))
        self.client = httpx.AsyncClient(
            verify=context, transport=transport,
            headers={"User-Agent": DEFAULT_UA}, follow_redirects=True, timeout=25,
        )

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.client.__aexit__(*args)

    async def calendar(self, channel: ArchiveChannel):
        body, encoding = await self._read(f'{BASE_URL}/records/{channel.slug}.html')
        page = html.unescape(body.decode(encoding, errors='replace'))
        dates = sorted(set(re.findall(r'task=select_time[^\"\'<>]*start_record=(\d{4}-\d{2}-\d{2})', page)))
        if not dates:
            raise ArchiveSourceError('Не удалось прочитать календарь канала. Повторите проверку.')
        return dates

    async def _read(self, url, *, limit=2_000_000, media=False, headers=None, progress=None):
        for attempt in range(2):
            task = asyncio.create_task(self._read_once(url, limit=limit, media=media, headers=headers, progress=progress))
            try:
                while not task.done():
                    _check_cancel(progress)
                    await asyncio.wait({task}, timeout=.25)
                return await task
            except httpx.HTTPError as exc:
                if attempt:
                    raise ArchiveSourceError("Не удалось связаться с архивом Онтайм. Попробуйте позже.") from exc
                await asyncio.sleep(.3)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _read_once(self, url, *, limit, media, headers, progress):
        _check_host(url, media=media)
        _check_cancel(progress)
        async with self.client.stream("GET", url, headers=headers) as response:
            _check_host(str(response.url), media=media)
            if response.status_code in {404, 410}:
                raise ArchiveUnavailable("Запись отсутствует.")
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                _check_cancel(progress)
                body.extend(chunk)
                if len(body) > limit:
                    raise ArchiveSourceError("Сайт вернул слишком большой фрагмент.")
            return bytes(body), response.encoding or "utf-8"

    def _cookie(self, name):
        return unquote(next((item.value for item in reversed(list(self.client.cookies.jar))
                             if item.name == name), ""))

    async def _segment(self, segment, headers, progress):
        data, _ = await self._read(segment.url, media=True, limit=8*1024*1024,
                                   headers=headers, progress=progress)
        # A successful HTTP status alone is insufficient: require MPEG-TS packets.
        if len(data) < 3*188 or not any(data[i:i+1] == b"G" and data[i+188:i+189] == b"G"
                                       and data[i+376:i+377] == b"G" for i in range(min(188, len(data)-376))):
            if not data or any(term in data.lower() for term in (b"not found", b"no data", b"no record")):
                raise ArchiveUnavailable("Видеофрагмент отсутствует.")
            raise ArchiveSourceError("Сайт не вернул видеофрагмент. Попробуйте позже.")
        return data

    async def probe(self, request: ArchiveRequest, progress=None) -> ArchivePlan:
        if not 0 < request.duration <= MAX_ARCHIVE_SECONDS:
            raise ValueError("Длительность одного фрагмента — от 1 секунды до 4 часов.")
        if request.end > datetime.now(MOSCOW):
            raise ArchiveUnavailable("Запись ещё не появилась.")
        channel_url = f"{BASE_URL}/records/{request.channel.slug}.html"
        body, encoding = await self._read(channel_url, progress=progress)
        page = html.unescape(body.decode(encoding, errors="replace"))
        dates = set(re.findall(r"task=select_time[^\"'<>]*start_record=(\d{4}-\d{2}-\d{2})", page))
        ids = set(re.findall(r"task=(?:select_time|view_record)&id=(\d+)", page))
        if not dates or len(ids) != 1:
            raise ArchiveSourceError("Не удалось прочитать календарь архива этого канала.")
        if request.start.date().isoformat() not in dates or (request.end-timedelta(seconds=1)).date().isoformat() not in dates:
            raise ArchiveUnavailable("Дата находится вне доступного архива.")
        # The site's recordings are split into eight-second blocks. Preserve the
        # fractional offset for the precise final trim instead of discarding it.
        aligned = int(request.start.timestamp()) // 8 * 8
        params = {"option": "com_content", "task": "view_record", "id": next(iter(ids)),
                  "start_record": datetime.fromtimestamp(aligned, MOSCOW).strftime("%Y-%m-%d-%H-%M-%S")}
        page_url = BASE_URL + "/index.php?" + urlencode(params)
        body, encoding = await self._read(page_url, progress=progress)
        page = body.decode(encoding, errors="replace")
        host, sid = self._cookie("tv"), self._cookie("tv2")
        gid = re.search(r"var\s+gid\s*=\s*['\"]([A-Za-z0-9]{16,128})['\"]", page)
        if self._cookie("tv3") == "1":
            alternate = re.search(r"var\s+gid2\s*=\s*['\"]([A-Za-z0-9]{16,128})['\"]", page)
            gid = alternate or gid
        if not gid or not re.fullmatch(r"[A-Za-z0-9]{16,96}", sid):
            raise ArchiveSourceError("Не удалось открыть сеанс архивного плеера.")
        manifest = f"https://{host}/stream/{gid[1]}/a{sid}playlist.m3u8?time={aligned}"
        _check_host(manifest, media=True)
        if self._cookie("tv1") != str(aligned):
            raise ArchiveSourceError("Сайт не подтвердил запрошенное время архива.")
        headers = {"Referer": page_url, "Origin": BASE_URL}
        body, _ = await self._read(manifest, media=True, headers=headers, progress=progress)
        offset = request.start.timestamp() - aligned
        segments = parse_playlist(body.decode("utf-8-sig", errors="replace"), manifest, request.duration + offset)
        plan = ArchivePlan(request, page_url, offset, segments, manifest_url=manifest)
        # Read both boundaries before announcing that the recording is available.
        indices = list(dict.fromkeys((0, len(segments)-1)))
        data = await asyncio.gather(*(self._segment(segments[i], headers, progress) for i in indices), return_exceptions=True)
        for item in data:
            if isinstance(item, BaseException):
                raise item
        plan.cached.update(zip(indices, data))
        return plan

    async def download(self, plan: ArchivePlan, work_dir: Path, progress: EncodeProgress) -> Path:
        progress.stage = "Скачивание фрагментов"
        source = work_dir / "archive.ffconcat"
        total = 0
        transfer_limit = 32 * 1024**3
        headers = {"Referer": plan.page_url, "Origin": BASE_URL}
        with source.open("x", encoding="ascii") as manifest:
            manifest.write("ffconcat version 1.0\n")
            for offset in range(0, len(plan.segments), 4):
                _check_cancel(progress)
                indices = range(offset, min(offset+4, len(plan.segments)))
                async def read(index):
                    if index in plan.cached:
                        return plan.cached[index]
                    return await self._segment(plan.segments[index], headers, progress)
                chunks = await asyncio.gather(*(read(index) for index in indices), return_exceptions=True)
                for chunk in chunks:
                    if isinstance(chunk, BaseException):
                        raise chunk
                for index, chunk in zip(indices, chunks):
                    if shutil.disk_usage(work_dir).free < len(chunk) + 2*1024**3:
                        raise ArchiveSourceError("Недостаточно места для записи архива. Повторите запрос позже.")
                    total += len(chunk)
                    if total > transfer_limit:
                        raise ArchiveSourceError("Запись слишком большая. Выберите более короткий интервал.")
                    part = work_dir / f"part-{index:05}.ts"
                    with part.open("xb") as output:
                        output.write(chunk)
                    manifest.write(f"file '{part.name}'\nduration {plan.segments[index].duration}\n")
                progress.percent = f"{int(min(offset+4, len(plan.segments))/len(plan.segments)*100)}%"
        # Each archive block can reset/jump its MPEG-TS timestamps. The trusted,
        # locally generated concat list normalizes them to the playlist timeline.
        progress.stage = "Сборка видео"
        remuxed = work_dir / "archive_input.mp4"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "1", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(remuxed),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, **_NO_WINDOW_KWARGS,
        )
        async def drain():
            tail = bytearray()
            while chunk := await proc.stderr.read(4096):
                tail.extend(chunk)
                del tail[:-4096]
            return bytes(tail)
        stderr = asyncio.create_task(drain())
        try:
            async with asyncio.timeout(180):
                while proc.returncode is None:
                    _check_cancel(progress)
                    await asyncio.sleep(.2)
                if proc.returncode:
                    raise ArchiveSourceError("Не удалось собрать видеофрагменты архива.")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await stderr
        actual = await _probe_duration(remuxed)
        if actual + .5 < plan.request.duration + plan.offset:
            raise ArchiveUnavailable("Архивная запись обрывается до конца интервала.")
        _check_cancel(progress)
        progress.stage = "Точная обрезка"
        output = work_dir / "archive.mp4"
        await _run_auto_video(
            ["ffmpeg", "-y", "-ss", str(plan.offset), "-i", str(remuxed),
             "-t", str(plan.request.duration), "-map", "0:v:0", "-map", "0:a:0?"],
            ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)],
            duration=plan.request.duration, progress=progress,
        )
        actual = await _probe_duration(output)
        if abs(actual - plan.request.duration) > .5:
            raise ArchiveSourceError("Не удалось точно подготовить весь запрошенный интервал. Попробуйте позже.")
        return output
