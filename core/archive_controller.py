"""Archive work runs on a dedicated asyncio loop; the UI only polls events."""
import asyncio
import json
import os
import tempfile
import threading
import httpx
import ssl
import truststore
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue

from core.archive_media import ArchiveCanceled, ArchiveProgress
from core.tv_archive import (ArchiveRequest, ArchiveSession, ArchiveSourceError,
                             ArchiveUnavailable, CHANNELS, MOSCOW, MAX_ARCHIVE_SECONDS)
from core.storage import Storage
from core.stream_archive import (StreamArchiveChannel, StreamArchiveSession, stream_channels,
                                 parse_catalog, validate_stream_url, CATALOG_URL)
from utils.config import Config
from utils.filenames import unique_media_path
from utils.logger import logger


def build_request(channel, day, start, end, *, now=None):
    try:
        date_value = datetime.strptime(day.strip(), '%d.%m.%Y').date()
        def clock(value):
            value = value.strip()
            return datetime.strptime(value, '%H:%M:%S' if value.count(':') == 2 else '%H:%M').time()
        begin = datetime.combine(date_value, clock(start), MOSCOW)
        finish = datetime.combine(date_value, clock(end), MOSCOW)
    except ValueError:
        raise ValueError('Введите дату ДД.ММ.ГГГГ и время ЧЧ:ММ или ЧЧ:ММ:СС.') from None
    if finish < begin:
        finish += timedelta(days=1)
    if not 0 < (finish - begin).total_seconds() <= MAX_ARCHIVE_SECONDS:
        raise ValueError('Выберите интервал от 1 секунды до 4 часов. Конец раньше начала — следующий день.')
    if finish > (now or datetime.now(MOSCOW)):
        raise ValueError('Этот интервал ещё не закончился. Выберите прошедшее время.')
    return ArchiveRequest(channel, begin, finish)


def build_interval_request(channel, begin, finish, *, now=None):
    try:
        def parse(value):
            fmt = '%d.%m.%Y %H:%M:%S' if value.count(':') == 2 else '%d.%m.%Y %H:%M'
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=MOSCOW)
        start, end = parse(begin), parse(finish)
    except ValueError:
        raise ValueError('Введите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ:СС.') from None
    if not 0 < (end-start).total_seconds() <= MAX_ARCHIVE_SECONDS:
        raise ValueError('Конец должен быть позже начала. Длительность фрагмента — до 4 часов.')
    if end > (now or datetime.now(MOSCOW)):
        raise ValueError('Этот интервал ещё не закончился. Выберите прошедшее время.')
    return ArchiveRequest(channel, start, end)


def session_for(channel):
    return StreamArchiveSession(channel) if isinstance(channel, StreamArchiveChannel) else ArchiveSession()


class ArchiveController:
    def __init__(self):
        self.events = Queue()
        self.progress = ArchiveProgress()
        self._closed = False
        self._checking = set()
        self.catalog = {'ngenix': stream_channels(), 'ontvtime': CHANNELS}
        self._download = None
        self._preview_session = None
        self._preview_generation = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name='archive-network')
        self._thread.start()
        self.cache_path = Config.DATA_DIR / 'archive_status.json'
        try:
            self.cached = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if isinstance(self.cached, list):
                self.cached = {item['slug']: item for item in self.cached}
            if not isinstance(self.cached, dict):
                self.cached = {}
        except (OSError, ValueError, KeyError, TypeError):
            self.cached = {}

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()

    def _submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def refresh(self, provider='ngenix'):
        if provider in self._checking or self._closed:
            return False
        self._checking.add(provider)
        self._submit(self._check_channels(provider))
        return True

    async def _update_catalog(self):
        try:
            async with httpx.AsyncClient(timeout=15, verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)) as client:
                response = await client.get(CATALOG_URL)
                response.raise_for_status()
                if len(response.content) > 2_000_000 or len(parse_catalog(response.text)) < 100:
                    raise ValueError('Некорректный каталог Забавы')
                path = Config.DATA_DIR / 'zabava-full.m3u'
                def save():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix('.m3u.tmp')
                    temporary.write_text(response.text, encoding='utf-8')
                    os.replace(temporary, path)
                await asyncio.to_thread(save)
                self.catalog['ngenix'] = await asyncio.to_thread(stream_channels)
                self.events.put(('catalog', 'ngenix', self.catalog['ngenix']))
        except Exception as error:
            logger.warning('Каталог Забавы: используем сохранённую копию (%s)', type(error).__name__)

    async def _check_channels(self, provider):
        semaphore = asyncio.Semaphore(6 if provider == 'ngenix' else 3)
        results = {}
        async def check(channel):
            result = {'state': 'unknown', 'dates': [], 'message': '',
                      'checked_at': datetime.now(MOSCOW).isoformat()}
            async with semaphore:
                try:
                    async with asyncio.timeout(28 if provider == 'ngenix' else 55), session_for(channel) as session:
                        now = datetime.now(MOSCOW)
                        if provider == 'ngenix':
                            start = (now - timedelta(hours=1)).replace(microsecond=0)
                            try:
                                await session.probe(ArchiveRequest(channel, start, start + timedelta(seconds=8)), quick=True)
                            except (ArchiveUnavailable, ArchiveSourceError):
                                if await session.live_available():
                                    result.update(state='live_only', message='Эфир доступен; архив за последний час не подтверждён.')
                                else:
                                    result.update(state='unavailable', message='Источник не передаёт видео.')
                                return
                        else:
                            dates = await session.calendar(channel)
                            result['dates'] = dates
                            day = datetime.fromisoformat(dates[-1]).date()
                            start = ((now - timedelta(minutes=5)).replace(second=0, microsecond=0)
                                     if day == now.date() else datetime.combine(day, datetime.min.time(), MOSCOW)
                                     + timedelta(hours=12))
                            await session.probe(ArchiveRequest(channel, start, start + timedelta(seconds=8)))
                        result['state'] = 'available'
                        result['message'] = 'Архив доступен · начало и конец проверены'
                except ArchiveUnavailable as error:
                    result.update(state='unavailable', message=str(error))
                except Exception as error:
                    result['message'] = ('Не удалось связаться с сайтом. Повторите проверку.'
                                         if isinstance(error, TimeoutError) else str(error))
                finally:
                    results[channel.slug] = result
                    if (result['state'] == 'unavailable'
                            and self.cached.get(channel.slug, {}).get('state') == 'available'):
                        result['message'] += ' Ранее канал был доступен.'
                    if not self._closed:
                        self.cached[channel.slug] = result
                        self.events.put(('channel', channel.slug, result))
            if not self._closed and len(results) % 25 == 0:
                self._persist_cache()
        try:
            if provider == 'ngenix':
                await self._update_catalog()
            # Put favourite/main-server streams first. Regional variants still
            # all get checked, with at most six requests in flight.
            await asyncio.gather(*(check(channel) for channel in self.catalog[provider]))
            self.cached.update(results)
            self._persist_cache()
        except Exception:
            logger.exception('Архив: ошибка проверки каналов')
        finally:
            self._checking.discard(provider)
            if not self._closed:
                self.events.put(('checked', provider, results))

    def _persist_cache(self):
        Storage._save_json(object.__new__(Storage), self.cache_path,
                          [{'slug': slug, **item} for slug, item in self.cached.items()])

    def add_stream(self, name, url):
        validate_stream_url(url)
        if not name.strip():
            raise ValueError('Введите название канала.')
        path = Config.DATA_DIR / 'archive_streams.json'
        try:
            streams = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            streams = []
        streams = [item for item in streams if item.get('url') != url]
        streams.append({'name': name.strip(), 'url': url, 'headers': {'User-Agent': 'HlsWinkPlayer'}})
        Storage._save_json(object.__new__(Storage), path, streams)
        self.catalog['ngenix'] = stream_channels()
        return next(channel for channel in self.catalog['ngenix'] if channel.url == url)

    def preview(self, request):
        self._preview_generation += 1
        generation = self._preview_generation
        self._submit(self._preview(request, generation))

    async def _preview(self, request, generation):
        session = session_for(request.channel)
        try:
            await session.__aenter__()
            plan = await session.probe(request)
            if generation != self._preview_generation or self._closed:
                return
            await self._close_preview()
            if generation != self._preview_generation or self._closed:
                return
            self._preview_session = session
            self.events.put(('preview', generation, plan))
            session = None
        except Exception as error:
            if generation == self._preview_generation and not self._closed:
                self.events.put(('preview_error', generation, str(error)))
        finally:
            if session is not None:
                await session.__aexit__(None, None, None)

    async def _close_preview(self):
        if self._preview_session is not None:
            await self._preview_session.__aexit__(None, None, None)
            self._preview_session = None

    def stop_preview(self):
        self._preview_generation += 1
        if not self._closed:
            self._submit(self._close_preview())

    def download(self, request, directory):
        if self._closed or (self._download is not None and not self._download.done()):
            return False
        self.progress = ArchiveProgress()
        self._download = self._submit(self._save(request, Path(directory), self.progress))
        return True

    async def _save(self, request, directory, progress):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            title = f'{request.channel.name}_{request.start:%Y-%m-%d_%H-%M-%S}_{request.end:%H-%M-%S}'
            output = unique_media_path(directory, title)
            # Work files stay on the destination volume; incomplete results
            # never masquerade as a finished MP4 and cancel removes all parts.
            with tempfile.TemporaryDirectory(prefix='.tvrec-archive-', dir=directory) as work:
                async with session_for(request.channel) as session:
                    plan = await session.probe(request, progress)
                    result = await session.download(plan, Path(work), progress)
                    if progress.stop_requested:
                        raise ArchiveCanceled()
                    os.replace(result, output)
            progress.percent = '100%'
            progress.stage = 'Готово'
            self.events.put(('saved', None, str(output)))
            logger.info('Архив: сохранён %s, %s — %s → %s', request.channel.name,
                        request.start.isoformat(), request.end.isoformat(), output)
        except (ArchiveCanceled, asyncio.CancelledError):
            self.events.put(('canceled', None, 'Загрузка отменена'))
        except Exception as error:
            logger.warning('Архив: загрузка не завершена: %s', error)
            self.events.put(('download_error', None, str(error)))

    def cancel(self):
        self.progress.stop_requested = True

    def shutdown(self):
        self._closed = True
        self.cancel()
        self._preview_generation += 1
        async def cleanup():
            await self._close_preview()
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._persist_cache()
        future = self._submit(cleanup())
        try:
            future.result(timeout=8)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
