# core/scheduler.py
from datetime import datetime, timedelta
import json
import threading
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.checker import StreamChecker, StreamStatus
from core.link_resolver import resolve_link
from core.notifier import Notifier
from core.recorder import Recorder
from core.storage import Storage
from utils.config import Config
from utils.logger import logger
from utils.timecode import parse_clock_time


class RecordingScheduler:
    """Планировщик записей с предварительной проверкой."""

    DAYS_MAP = {
        'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed',
        'thursday': 'thu', 'friday': 'fri', 'saturday': 'sat', 'sunday': 'sun',
        0: 'mon', 1: 'tue', 2: 'wed', 3: 'thu', 4: 'fri', 5: 'sat', 6: 'sun',
    }

    def __init__(self, recorder: Recorder | None = None):
        self.scheduler = BackgroundScheduler()
        self.checker = StreamChecker()
        self.recorder = recorder or Recorder()
        self.storage = Storage()
        self.notifier = Notifier()
        self._running = False
        self._lock = threading.RLock()
        self._watchdog_stop = threading.Event()
        self._entries = []
        self._attempts = {}
        self._attempt_counts = {}
        self._deadlines = {}
        self._status_callback: Optional[Callable[[int, str], None]] = None

    def set_status_callback(self, callback: Callable[[int, str], None]):
        """Регистрирует callback(index, status), вызываемый при смене статуса
        конкретной строки расписания: 'checking' / 'recording' / 'completed' / 'failed'."""
        self._status_callback = callback

    def _notify_status(self, index: int, status: str):
        if self._status_callback:
            try:
                self._status_callback(index, status)
            except Exception as e:
                logger.error(f"Scheduler: ошибка status callback: {e}")

    def start(self):
        """Запускает планировщик."""
        if not self._running:
            self.scheduler.start()
            self._running = True
            self._load_all_schedules()
            self._watchdog_stop.clear()
            threading.Thread(target=self._watch_schedules, daemon=True,
                             name='RecordingScheduleWatchdog').start()
            logger.info("Планировщик запущен")

    def stop(self):
        """Останавливает планировщик."""
        if self._running:
            with self._lock:
                self._running = False
                self._watchdog_stop.set()
            self.scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")

    def reload_schedules(self):
        """Перезагружает все задачи из хранилища."""
        with self._lock:
            self.scheduler.remove_all_jobs()
            self._load_all_schedules()
        logger.info("Расписание перезагружено")

    def _load_all_schedules(self):
        """Загружает все активные записи из хранилища."""
        schedule_items = self.storage.get_schedule()
        channel_list = self.storage.get_channels()
        link_list = self.storage.get_links()
        channels = {channel['name']: channel for channel in channel_list}
        links = {link['name']: link for link in link_list}
        channels_by_id = {channel.get('id'): channel for channel in channel_list if channel.get('id')}
        links_by_id = {link.get('id'): link for link in link_list if link.get('id')}

        entries = []
        for index, item in enumerate(schedule_items):
            if not item.get('enabled', True):
                continue

            name = item.get('channel_name')
            source_type = item.get('source_type', 'channel')
            source_map = links if source_type == 'link' else channels
            source_id_map = links_by_id if source_type == 'link' else channels_by_id
            target = source_id_map.get(item.get('source_id')) or source_map.get(name)
            if not target:
                kind = 'Ссылка' if source_type == 'link' else 'Канал'
                logger.warning(f"{kind} '{name}' не найден(а) для расписания #{index}")
                continue

            self._add_job(index, item, target)
            entries.append((target, item, index))
        with self._lock:
            self._entries = entries

    @classmethod
    def _active_window(cls, item: dict, now: datetime):
        """Find today's or yesterday's occurrence, including overnight windows."""
        if not item.get('enabled', True):
            return None
        start_time = parse_clock_time(item.get('start_time', '00:00'))
        if start_time is None:
            raise ValueError('Некорректное время начала записи')
        for day in (now, now - timedelta(days=1)):
            if item.get('recording_date'):
                if day.strftime('%Y-%m-%d') != item['recording_date']:
                    continue
            elif item.get('days'):
                weekday = cls.DAYS_MAP[day.weekday()]
                if weekday not in [cls.DAYS_MAP.get(d, str(d)) for d in item['days']]:
                    continue
            start = day.replace(hour=start_time[0], minute=start_time[1],
                                second=start_time[2], microsecond=0)
            end = cls._end_deadline(item, start)
            if start <= now < end:
                return start, end
        return None

    def _watch_schedules(self):
        # Never sleep until the next cron occurrence: macOS can pause long
        # relative waits during system sleep. Short waits recover after wake.
        while not self._watchdog_stop.is_set():
            try:
                self.scheduler.wakeup()
                self.check_schedules()
            except Exception:
                logger.exception('Scheduler: ошибка контрольной проверки расписания')
            self._watchdog_stop.wait(5)

    def check_schedules(self, now=None):
        now = now or datetime.now()
        with self._lock:
            if not self._running:
                return
            entries = list(self._entries)
            expired = [task_id for task_id, end in self._deadlines.items() if end <= now]
            for task_id in expired:
                del self._deadlines[task_id]
            self._attempts = {key: retry for key, retry in self._attempts.items()
                              if key[1] > now - timedelta(days=2)}
            self._attempt_counts = {key: count for key, count in self._attempt_counts.items()
                                    if key[1] > now - timedelta(days=2)}
        for task_id in expired:
            self.recorder.stop_recording(task_id)
        for target, item, index in entries:
            try:
                self._run_scheduled_recording(target, item, index, now=now, background=True)
            except Exception:
                logger.exception('Scheduler: ошибка восстановления строки #%s', index)

    def _run_scheduled_recording(self, target, item, index, *, now=None, background=False):
        now = now or datetime.now()
        window = self._active_window(item, now)
        if window is None:
            return
        start, deadline = window
        # Stable across row deletion/reordering/reload; shared by cron and
        # recovery so they can never launch the same occurrence twice.
        key = (json.dumps(item, sort_keys=True, ensure_ascii=False), start)
        with self._lock:
            if not self._running or (target, item, index) not in self._entries:
                return
            if key in self._attempts:
                retry_at = self._attempts[key]
                if retry_at is None or retry_at > now:
                    return
            if self._attempt_counts.get(key, 0) >= 4:
                return
            self._attempt_counts[key] = self._attempt_counts.get(key, 0) + 1
            self._attempts[key] = None

        def record():
            try:
                with self._lock:
                    if not self._running or (target, item, index) not in self._entries:
                        return
                if (now - start).total_seconds() > 5:
                    logger.warning("Scheduler: восстановление пропущенного запуска '%s' "
                                   "(%s — %s), запись оставшейся части",
                                   target['name'], start, deadline)
                success = self._pre_record_check(target, item, index, deadline=deadline)
            except Exception:
                logger.exception("Scheduler: ошибка запуска '%s'", target['name'])
                self._notify_status(index, 'failed')
                success = False
            if not success:
                with self._lock:
                    self._attempts[key] = datetime.now() + timedelta(seconds=30)

        if background:
            threading.Thread(target=record, daemon=True, name='ScheduledRecordingRecovery').start()
        else:
            record()

    def _add_job(self, index: int, item: dict, target: dict):
        """Добавляет задачу в планировщик."""
        days = item.get('days', [])
        start_time = item.get('start_time', '00:00')
        hour, minute, second = parse_clock_time(start_time)

        day_of_week = ','.join(self.DAYS_MAP.get(day, str(day)) for day in days) if days else '*'
        date_fields = {}
        if item.get('recording_date'):
            recording_date = datetime.strptime(item['recording_date'], '%Y-%m-%d')
            date_fields = dict(year=recording_date.year, month=recording_date.month, day=recording_date.day)
            day_of_week = '*'
        trigger = CronTrigger(hour=hour, minute=minute, second=second,
                              day_of_week=day_of_week, **date_fields)
        self.scheduler.add_job(
            self._run_scheduled_recording,
            trigger=trigger,
            args=[target, item, index],
            id=f"recording_{index}",
            replace_existing=True,
        )
        logger.info(f"Задача добавлена: {target['name']} в {start_time} ({item.get('recording_date') or day_of_week})")

    @staticmethod
    def _end_deadline(schedule_item: dict, now: Optional[datetime] = None) -> datetime:
        """Абсолютное время «До» для текущего запуска, включая переход через полночь."""
        now = now or datetime.now()
        start_hour, start_minute, start_second = parse_clock_time(schedule_item.get('start_time', '00:00'))
        end_hour, end_minute, end_second = parse_clock_time(schedule_item.get('end_time', '00:30'))
        start_seconds = start_hour * 3600 + start_minute * 60 + start_second
        end_seconds = end_hour * 3600 + end_minute * 60 + end_second
        deadline = now.replace(hour=end_hour, minute=end_minute, second=end_second, microsecond=0)
        if end_seconds <= start_seconds:
            deadline += timedelta(days=1)
        return deadline

    def _schedule_stop_at(self, task_id: str, deadline: datetime) -> float:
        """Ставит остановку точно на «До» и возвращает оставшиеся секунды."""
        remaining = max(0.0, (deadline - datetime.now()).total_seconds())
        self.recorder.schedule_stop(task_id, remaining)
        with self._lock:
            self._deadlines[task_id] = deadline
        return remaining

    def _pre_record_check(self, target: dict, schedule_item: dict, index: int, *, deadline=None):
        """Проверка перед записью, вызываемая планировщиком."""
        name = target.get('name', 'Unknown')
        source_type = schedule_item.get('source_type', 'channel')
        deadline = deadline or self._end_deadline(schedule_item)
        if deadline <= datetime.now():
            logger.error(f"Запись '{name}' не начата: время окончания уже прошло")
            self._notify_status(index, 'failed')
            return
        logger.info(f"Предварительная проверка: {name} ({source_type})")
        self._notify_status(index, 'checking')

        extra_headers = None
        if source_type == 'link':
            info = resolve_link(target.get('url', ''))
            if not info.ok:
                # Прямую ссылку получить не удалось никаким автоматическим
                # способом (link_resolver уже перепробовал yt-dlp/HTML-скрейп/
                # скрытый браузер-снифф) — последний рубеж: настоящее видимое
                # окно браузера и запись самого экрана под ним, тот же
                # переход, что и при ручной записи из "Мои ссылки"
                # (см. AppWindow._record_link_now). Раньше это была отдельная
                # вкладка "Браузер" с отдельным source_type — теперь один и
                # тот же пункт расписания сам решает по факту резолва.
                logger.warning(f"Прямой поток для '{name}' не найден ({info.error}) — пробуем через браузер")
                if deadline <= datetime.now():
                    logger.error(f"Запись '{name}' не начата: поиск источника занял всё окно расписания")
                    self._notify_status(index, 'failed')
                    return
                output_path = self.recorder.build_output_path(name)
                with self._lock:
                    if not self._running or (target, schedule_item, index) not in self._entries:
                        return
                self._notify_status(index, 'recording')
                task_id = self.recorder.start_browser_recording(
                    channel_name=name,
                    url=info.player_url or target.get('player_url') or target.get('url', ''),
                    output_path=str(output_path),
                    source='schedule',
                    start_deadline_timestamp=deadline.timestamp(),
                    on_complete=lambda success, n, path, early, result, idx=index:
                        self._on_recording_complete(success, n, path, early, result, idx,
                                                    schedule_item=schedule_item, retry_allowed=False),
                )
                if not task_id:
                    self._on_recording_error(name, 'Не удалось начать запись экрана')
                    self._notify_status(index, 'failed')
                    return
                remaining = self._schedule_stop_at(task_id, deadline)
                logger.info(f"Scheduler: '{name}' будет остановлен через {remaining:.1f}с, "
                            f"строго в {deadline:%H:%M:%S}")
                return True
            video_url, audio_url, extra_headers = info.video_url, info.audio_url, info.headers
        else:
            status, message = self.checker.check(target)
            if status == StreamStatus.RED:
                logger.error(f"Запись отменена: {name} — {message}")
                self.notifier.send("❌ Запись отменена", f"{name}\n{message}")
                self._notify_status(index, 'failed')
                return
            if status == StreamStatus.YELLOW:
                logger.warning(f"Запись с предупреждением: {name} — {message}")
                self.notifier.send("⚠️ Запись начата с предупреждением", f"{name}\n{message}")
            # audio_url — для каналов, у которых видео и звук на CDN лежат
            # раздельными HLS-рендициями (см. MANUAL_FIXES в m3u_parser.py:
            # "Россия 24"/"Россия К" через stream.smotrim.ru — обычный 'url'
            # там video-only, без этого поля запись выходит совсем без звука).
            video_url, audio_url = target.get('url', ''), target.get('audio_url')
            extra_headers = self.recorder.channel_headers(target)

        remaining = (deadline - datetime.now()).total_seconds()
        if remaining <= 0:
            logger.error(f"Запись '{name}' не начата: подготовка заняла всё окно расписания")
            self._notify_status(index, 'failed')
            return

        output_path = self.recorder.build_output_path(name)
        with self._lock:
            if not self._running or (target, schedule_item, index) not in self._entries:
                return
        self._notify_status(index, 'recording')
        task_id = self.recorder.start_recording(
            channel_name=name,
            stream_url=video_url,
            output_path=str(output_path),
            source='schedule',
            on_complete=lambda success, n, path, early, result, idx=index:
                self._on_recording_complete(success, n, path, early, result, idx,
                                            schedule_item=schedule_item,
                                            retry_allowed=False),
            audio_url=audio_url,
            extra_headers=extra_headers,
            duration_limit_seconds=remaining if source_type == 'link' else None,
            start_deadline_timestamp=deadline.timestamp(),
            is_live_channel=(source_type != 'link'),
            expect_audio=target.get('expect_audio', True) if source_type != 'link' else bool(audio_url),
            expected_duration_seconds=(min(remaining, info.duration) if source_type == 'link'
                and isinstance(getattr(info, 'duration', None), (int, float)) else remaining),
        )
        if not task_id:
            self._on_recording_error(name, 'Не удалось запустить запись')
            self._notify_status(index, 'failed')
            return

        remaining = self._schedule_stop_at(task_id, deadline)
        logger.info(f"Scheduler: '{name}' будет остановлен через {remaining:.1f}с, "
                    f"строго в {deadline:%H:%M:%S}")
        return True

    def _on_recording_complete(self, success: bool, channel_name: str, file_path: str,
                                ended_early: bool = False, result_status: str = 'completed',
                                index: Optional[int] = None, *, schedule_item=None, retry_allowed=True):
        if retry_allowed and schedule_item is not None and (not success or ended_early):
            now = datetime.now()
            window = self._active_window(schedule_item, now)
            if window is not None:
                key = (json.dumps(schedule_item, sort_keys=True, ensure_ascii=False), window[0])
                with self._lock:
                    self._attempts[key] = now + timedelta(seconds=30)
                logger.warning("Scheduler: '%s' оборвался внутри окна, повтор через 30с", channel_name)
        if result_status == 'partial':
            self.notifier.send("⚠️ Запись сохранена частично", f"{channel_name}\n{file_path}")
            if index is not None:
                self._notify_status(index, 'partial')
        elif result_status == 'processing_error':
            self._on_recording_error(channel_name, 'Итоговый файл не прошёл проверку ffprobe')
            if index is not None:
                self._notify_status(index, 'processing_error')
        elif success and ended_early:
            self.notifier.send("⚠️ Запись завершена раньше срока",
                                f"{channel_name}\nЭфир или файл закончились раньше, чем длилось окно записи.\n{file_path}")
            if index is not None:
                self._notify_status(index, 'ended_early')
        elif success:
            self.notifier.send("✅ Запись завершена", f"{channel_name}\n{file_path}")
            if index is not None:
                self._notify_status(index, 'completed')
        else:
            self._on_recording_error(channel_name, 'ffmpeg завершился с ошибкой')
            if index is not None:
                self._notify_status(index, 'failed')

    def _on_recording_error(self, channel_name: str, error: str):
        self.notifier.send("❌ Ошибка записи", f"{channel_name}\n{error}")
