# core/scheduler.py
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from core.checker import StreamChecker, StreamStatus
from core.recorder import Recorder
from core.storage import Storage
from core.notifier import Notifier
from utils.config import Config
from utils.logger import logger


class RecordingScheduler:
    """Планировщик записей с предварительной проверкой"""
    
    DAYS_MAP = {
        'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed',
        'thursday': 'thu', 'friday': 'fri', 'saturday': 'sat', 'sunday': 'sun'
    }
    
    # Маппинг чисел (0-6) в названия дней для cron
    NUM_TO_DAY = {
        0: 'mon', 1: 'tue', 2: 'wed', 3: 'thu', 4: 'fri', 5: 'sat', 6: 'sun'
    }
    
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.checker = StreamChecker()
        self.recorder = Recorder()
        self.storage = Storage()
        self.notifier = Notifier()
        self._running = False
    
    def start(self):
        """Запускает планировщик"""
        if not self._running:
            self.scheduler.start()
            self._running = True
            self._load_all_schedules()
            logger.info("Планировщик запущен")
    
    def stop(self):
        """Останавливает планировщик"""
        if self._running:
            self.scheduler.shutdown(wait=False)
            self._running = False
            logger.info("Планировщик остановлен")
    
    def reload_schedules(self):
        """Перезагружает все задачи из хранилища"""
        self.scheduler.remove_all_jobs()
        self._load_all_schedules()
        logger.info("Расписание перезагружено")
    
    def _load_all_schedules(self):
        """Загружает все активные записи из хранилища"""
        schedule_items = self.storage.get_schedule()
        channels = {ch['name']: ch for ch in self.storage.get_channels()}
        
        for idx, item in enumerate(schedule_items):
            if not item.get('enabled', True):
                continue
            
            channel_name = item.get('channel_name')
            channel = channels.get(channel_name)
            
            if not channel:
                logger.warning(f"Канал '{channel_name}' не найден для расписания #{idx}")
                continue
            
            self._add_job(idx, item, channel)
    
    def _add_job(self, idx: int, item: dict, channel: dict):
        """Добавляет задачу в планировщик"""
        days = item.get('days', [])
        start_time = item.get('start_time', '00:00')
        end_time = item.get('end_time', '00:30')
        
        hour, minute = map(int, start_time.split(':'))
        end_h, end_m = map(int, end_time.split(':'))
        
        # Вычисляем длительность
        start_dt = datetime.now().replace(hour=hour, minute=minute)
        end_dt = datetime.now().replace(hour=end_h, minute=end_m)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        duration = int((end_dt - start_dt).total_seconds())
        
        # Дни недели для cron (конвертируем числа и строки в формат cron)
        day_list = []
        for d in days:
            if isinstance(d, int):
                day_list.append(self.NUM_TO_DAY.get(d, str(d)))
            else:
                day_list.append(self.DAYS_MAP.get(d, d))
        day_of_week = ','.join(day_list) if day_list else '*'
        
        trigger = CronTrigger(
            hour=hour, minute=minute,
            day_of_week=day_of_week
        )
        
        job_id = f"recording_{idx}"
        self.scheduler.add_job(
            self._pre_record_check,
            trigger=trigger,
            args=[channel, duration, item],
            id=job_id,
            replace_existing=True
        )
        
        logger.info(f"Задача добавлена: {channel['name']} в {start_time} ({day_of_week})")
    
    def _pre_record_check(self, channel: dict, duration: int, schedule_item: dict):
        """Проверка перед записью (вызывается планировщиком)"""
        channel_name = channel.get('name', 'Unknown')
        logger.info(f"Предварительная проверка: {channel_name}")
        
        status, message = self.checker.check(channel)
        
        if status == StreamStatus.RED:
            logger.error(f"Запись отменена: {channel_name} — {message}")
            self.notifier.send(
                "❌ Запись отменена",
                f"{channel_name}\n{message}"
            )
            return
        
        if status == StreamStatus.YELLOW:
            logger.warning(f"Запись с предупреждением: {channel_name} — {message}")
            self.notifier.send(
                "⚠️ Запись начата с предупреждением",
                f"{channel_name}\n{message}"
            )
        
        # Запускаем запись
        self.recorder.start_recording(
            channel=channel,
            duration_seconds=duration,
            on_complete=lambda path: self._on_recording_complete(channel_name, path),
            on_error=lambda err: self._on_recording_error(channel_name, err)
        )
    
    def _on_recording_complete(self, channel_name: str, file_path: str):
        self.notifier.send(
            "✅ Запись завершена",
            f"{channel_name}\n{file_path}"
        )
    
    def _on_recording_error(self, channel_name: str, error: str):
        self.notifier.send(
            "❌ Ошибка записи",
            f"{channel_name}\n{error}"
        )