# core/scheduler.py
import threading
from datetime import datetime, timedelta
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
            logger.info("Планировщик запущен")

    def stop(self):
        """Останавливает планировщик."""
        if self._running:
            self.scheduler.shutdown(wait=False)
            self._running = False
            logger.info("Планировщик остановлен")

    def reload_schedules(self):
        """Перезагружает все задачи из хранилища."""
        self.scheduler.remove_all_jobs()
        self._load_all_schedules()
        logger.info("Расписание перезагружено")

    def _load_all_schedules(self):
        """Загружает все активные записи из хранилища."""
        schedule_items = self.storage.get_schedule()
        channels = {channel['name']: channel for channel in self.storage.get_channels()}
        links = {link['name']: link for link in self.storage.get_links()}
        browser_links = {link['name']: link for link in self.storage.get_browser_links()}

        for index, item in enumerate(schedule_items):
            if not item.get('enabled', True):
                continue

            name = item.get('channel_name')
            source_type = item.get('source_type', 'channel')
            if source_type == 'browser':
                source_map = browser_links
            elif source_type == 'link':
                source_map = links
            else:
                source_map = channels
            target = source_map.get(name)
            if not target:
                kind = {'link': 'Ссылка', 'browser': 'Ссылка (браузер)'}.get(source_type, 'Канал')
                logger.warning(f"{kind} '{name}' не найден(а) для расписания #{index}")
                continue

            self._add_job(index, item, target)

    def _add_job(self, index: int, item: dict, target: dict):
        """Добавляет задачу в планировщик."""
        days = item.get('days', [])
        start_time = item.get('start_time', '00:00')
        end_time = item.get('end_time', '00:30')
        hour, minute = map(int, start_time.split(':'))
        end_hour, end_minute = map(int, end_time.split(':'))

        start_dt = datetime.now().replace(hour=hour, minute=minute)
        end_dt = datetime.now().replace(hour=end_hour, minute=end_minute)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        duration = int((end_dt - start_dt).total_seconds())

        day_of_week = ','.join(self.DAYS_MAP.get(day, str(day)) for day in days) if days else '*'
        trigger = CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week)
        self.scheduler.add_job(
            self._pre_record_check,
            trigger=trigger,
            args=[target, duration, item, index],
            id=f"recording_{index}",
            replace_existing=True,
        )
        logger.info(f"Задача добавлена: {target['name']} в {start_time} ({day_of_week})")

    def _pre_record_check(self, target: dict, duration: int, schedule_item: dict, index: int):
        """Проверка перед записью, вызываемая планировщиком."""
        name = target.get('name', 'Unknown')
        source_type = schedule_item.get('source_type', 'channel')
        logger.info(f"Предварительная проверка: {name} ({source_type})")
        self._notify_status(index, 'checking')

        extra_headers = None
        if source_type == 'browser':
            # Ссылка из вкладки "Браузер" (link_resolver не смог получить
            # прямую ссылку на поток при добавлении) — вместо обычной
            # pre-record проверки сразу запускаем захват экрана.
            output_path = self.recorder.build_output_path(name)
            self._notify_status(index, 'recording')
            task_id = self.recorder.start_browser_recording(
                channel_name=name,
                url=target.get('url', ''),
                output_path=str(output_path),
                source='schedule',
                on_complete=lambda success, n, path, early, idx=index: self._on_recording_complete(success, n, path, early, idx),
            )
            if not task_id:
                self._on_recording_error(name, 'Не удалось начать запись экрана')
                self._notify_status(index, 'failed')
                return
            timer = threading.Timer(duration, self.recorder.stop_recording, args=[task_id])
            timer.daemon = True
            timer.start()
            return

        if source_type == 'link':
            info = resolve_link(target.get('url', ''))
            if not info.ok:
                logger.error(f"Запись отменена: {name} — {info.error}")
                self.notifier.send("❌ Запись отменена", f"{name}\n{info.error}")
                self._notify_status(index, 'failed')
                return
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
            video_url, audio_url = target.get('url', ''), None

        output_path = self.recorder.build_output_path(name)
        self._notify_status(index, 'recording')
        task_id = self.recorder.start_recording(
            channel_name=name,
            stream_url=video_url,
            output_path=str(output_path),
            source='schedule',
            on_complete=lambda success, n, path, early, idx=index: self._on_recording_complete(success, n, path, early, idx),
            audio_url=audio_url,
            extra_headers=extra_headers,
        )
        if not task_id:
            self._on_recording_error(name, 'Не удалось запустить запись')
            self._notify_status(index, 'failed')
            return

        timer = threading.Timer(duration, self.recorder.stop_recording, args=[task_id])
        timer.daemon = True
        timer.start()

    def _on_recording_complete(self, success: bool, channel_name: str, file_path: str,
                                ended_early: bool = False, index: Optional[int] = None):
        if success and ended_early:
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
