# core/scheduler.py
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from core.checker import StreamChecker, StreamStatus
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

        for index, item in enumerate(schedule_items):
            if not item.get('enabled', True):
                continue

            channel_name = item.get('channel_name')
            channel = channels.get(channel_name)
            if not channel:
                logger.warning(f"Канал '{channel_name}' не найден для расписания #{index}")
                continue

            self._add_job(index, item, channel)

    def _add_job(self, index: int, item: dict, channel: dict):
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
            args=[channel, duration, item, index],
            id=f"recording_{index}",
            replace_existing=True,
        )
        logger.info(f"Задача добавлена: {channel['name']} в {start_time} ({day_of_week})")

    def _pre_record_check(self, channel: dict, duration: int, schedule_item: dict, index: int):
        """Проверка перед записью, вызываемая планировщиком."""
        channel_name = channel.get('name', 'Unknown')
        logger.info(f"Предварительная проверка: {channel_name}")
        self._notify_status(index, 'checking')
        status, message = self.checker.check(channel)

        if status == StreamStatus.RED:
            logger.error(f"Запись отменена: {channel_name} — {message}")
            self.notifier.send("❌ Запись отменена", f"{channel_name}\n{message}")
            self._notify_status(index, 'failed')
            return

        if status == StreamStatus.YELLOW:
            logger.warning(f"Запись с предупреждением: {channel_name} — {message}")
            self.notifier.send("⚠️ Запись начата с предупреждением", f"{channel_name}\n{message}")

        output_path = self.recorder.build_output_path(channel_name)
        self._notify_status(index, 'recording')
        task_id = self.recorder.start_recording(
            channel_name=channel_name,
            stream_url=channel.get('url', ''),
            output_path=str(output_path),
            source='schedule',
            on_complete=lambda success, name, path, idx=index: self._on_recording_complete(success, name, path, idx),
        )
        if not task_id:
            self._on_recording_error(channel_name, 'Не удалось запустить запись')
            self._notify_status(index, 'failed')
            return

        timer = threading.Timer(duration, self.recorder.stop_recording, args=[task_id])
        timer.daemon = True
        timer.start()

    def _on_recording_complete(self, success: bool, channel_name: str, file_path: str, index: Optional[int] = None):
        if success:
            self.notifier.send("✅ Запись завершена", f"{channel_name}\n{file_path}")
            if index is not None:
                self._notify_status(index, 'completed')
        else:
            self._on_recording_error(channel_name, 'ffmpeg завершился с ошибкой')
            if index is not None:
                self._notify_status(index, 'failed')

    def _on_recording_error(self, channel_name: str, error: str):
        self.notifier.send("❌ Ошибка записи", f"{channel_name}\n{error}")
