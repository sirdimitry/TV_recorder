# core/recorder.py
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
import re
from typing import Optional, Callable, Dict
from utils.config import Config
from utils.logger import logger


class RecordingTask:
    def __init__(self, task_id: str, channel_name: str, stream_url: str, 
                 output_path: str, source: str = "manual"):
        self.task_id = task_id
        self.channel_name = channel_name
        self.stream_url = stream_url
        self.output_path = output_path
        self.source = source
        self.process: Optional[subprocess.Popen] = None
        self.is_recording = False
        self.is_paused = False
        self.start_time = 0
        self.pause_time = 0
        self.total_paused_duration = 0
        self.final_duration = 0
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.success: Optional[bool] = None
        self.error_message = ""
        self.on_complete: Optional[Callable] = None
    
    def get_elapsed_time(self) -> int:
        if not self.is_recording and self.final_duration > 0:
            return self.final_duration
        if not self.is_recording:
            return 0
        if self.is_paused:
            return int(self.pause_time - self.start_time - self.total_paused_duration)
        return int(time.time() - self.start_time - self.total_paused_duration)
    
    def format_elapsed_time(self) -> str:
        seconds = self.get_elapsed_time()
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def format_recording_period(self) -> str:
        """Returns the actual recording time range for the interface."""
        started_at = self.started_at or datetime.fromtimestamp(self.start_time)
        ended_at = self.finished_at
        start_label = started_at.strftime('%H:%M')
        end_label = ended_at.strftime('%H:%M') if ended_at else 'now'
        return f"{start_label} – {end_label}"


class Recorder:
    TRANSLITERATION = str.maketrans({
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'E',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch',
        'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    })
    CHANNEL_HEADERS = {
        "Первый канал": {"ua": "Mozilla/5.0", "ref": "https://www.1tv.ru"},
        "Россия 1": {"ua": "Mozilla/5.0", "ref": "https://smotrim.ru"},
        "НТВ": {"ua": "Mozilla/5.0", "ref": "https://www.ntv.ru"},
        "Матч ТВ": {"ua": "Restream/5.20408.171030 (mag250)", "ref": "https://matchtv.ru"},
        "Муз-ТВ": {"ua": "Dalvik/2.1.0 (Linux; U; Android 10)", "ref": "https://muz-tv.ru"}
    }
    
    def __init__(self):
        self.tasks: Dict[str, RecordingTask] = {}
        self._lock = threading.Lock()
        self._timer_thread: Optional[threading.Thread] = None
        self._running = False
        self._ui_callback: Optional[Callable] = None  # Callback для UI
    
    def set_ui_callback(self, callback: Callable):
        """Устанавливает callback для обновления UI (вызывается из AppWindow)"""
        self._ui_callback = callback

    @classmethod
    def build_output_path(cls, channel_name: str, recorded_at: datetime | None = None) -> Path:
        """Builds a portable filename so macOS and Windows handle it identically."""
        timestamp = (recorded_at or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')
        latin_name = channel_name.translate(cls.TRANSLITERATION)
        safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', latin_name).strip('_') or 'channel'
        return Config.get_recordings_dir() / f"{safe_name}_{timestamp}.mp4"
    
    def _notify_ui(self):
        """Безопасный вызов UI callback из любого потока"""
        if self._ui_callback:
            try:
                self._ui_callback()
            except Exception as e:
                logger.error(f"Recorder: Ошибка UI callback: {e}")
    
    def start_recording(self, channel_name: str, stream_url: str, 
                       output_path: str, source: str = "manual",
                       on_complete: Optional[Callable] = None) -> str:
        Config.init_dirs()
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        task_id = f"{channel_name}_{int(time.time())}"
        
        headers_info = self.CHANNEL_HEADERS.get(channel_name, {})
        ua = headers_info.get("ua", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
        ref = headers_info.get("ref", "https://www.google.com")
        headers = f"User-Agent: {ua}\r\nReferer: {ref}\r\nOrigin: {ref}\r\n"
        
        cmd = [
            'ffmpeg', '-y',
            '-headers', headers,
            '-i', stream_url,
            '-c', 'copy',
            '-err_detect', 'ignore_err',
            '-max_error_rate', '100',
            '-fflags', '+genpts+discardcorrupt',
            '-avoid_negative_ts', 'make_zero',
            '-movflags', '+faststart',
            str(output_file)
        ]
        
        task = RecordingTask(task_id, channel_name, stream_url, str(output_file), source)
        task.on_complete = on_complete
        
        try:
            task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            task.is_recording = True
            task.start_time = time.time()
            task.started_at = datetime.now()
            
            with self._lock:
                self.tasks[task_id] = task
            
            logger.info(f"Recorder: Начата запись '{channel_name}' → {output_file} (source: {source})")
            
            threading.Thread(target=self._wait_for_task, args=(task,), daemon=True).start()
            
            if not self._running:
                self._start_timer_loop()
            
            # Уведомляем UI немедленно
            self._notify_ui()
            
            return task_id
            
        except Exception as e:
            logger.error(f"Recorder: Ошибка запуска ffmpeg: {e}")
            return ""
    
    def stop_recording(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
        
        if task and task.process:
            logger.info(f"Recorder: Остановка записи '{task.channel_name}' (task: {task_id})")
            task.final_duration = task.get_elapsed_time()
            task.process.terminate()
            task.is_recording = False
            self._notify_ui()
    
    def pause_recording(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
        
        if task and task.process:
            if not task.is_paused:
                task.process.send_signal(19)
                task.is_paused = True
                task.pause_time = time.time()
                logger.info(f"Recorder: Пауза '{task.channel_name}'")
            else:
                task.process.send_signal(18)
                task.total_paused_duration += time.time() - task.pause_time
                task.is_paused = False
                logger.info(f"Recorder: Возобновление '{task.channel_name}'")
            
            self._notify_ui()
    
    def remove_task(self, task_id: str):
        with self._lock:
            if task_id in self.tasks:
                del self.tasks[task_id]
                self._notify_ui()
    
    def get_all_tasks(self) -> list:
        with self._lock:
            return list(self.tasks.values())
    
    def _wait_for_task(self, task: RecordingTask):
        if not task.process:
            return
        
        stdout, stderr = task.process.communicate()
        returncode = task.process.returncode
        
        if task.final_duration == 0:
            task.final_duration = task.get_elapsed_time()
        task.is_recording = False
        task.finished_at = datetime.now()
        
        success = returncode == 0 or returncode == 255
        output_file = Path(task.output_path)
        has_usable_file = output_file.is_file() and output_file.stat().st_size > 1024
        success = success and has_usable_file
        task.success = success
        if success:
            logger.info(f"Recorder: Запись '{task.channel_name}' завершена успешно")
        else:
            err_msg = stderr.decode('utf-8', errors='ignore')[-500:] if stderr else ""
            if not has_usable_file:
                err_msg = "Recording finished without creating a usable video file. " + err_msg
            task.error_message = err_msg
            logger.error(f"Recorder: Ошибка записи '{task.channel_name}' (code {returncode}): {err_msg}")
        
        if task.on_complete:
            task.on_complete(success, task.channel_name, task.output_path)
        
        self._notify_ui()
    
    def _start_timer_loop(self):
        self._running = True
        
        def loop():
            while self._running:
                with self._lock:
                    has_active = any(t.is_recording for t in self.tasks.values())
                
                if has_active:
                    self._notify_ui()
                
                time.sleep(1)
        
        self._timer_thread = threading.Thread(target=loop, daemon=True)
        self._timer_thread.start()
    
    def stop_all(self):
        self._running = False
        with self._lock:
            for task in self.tasks.values():
                if task.process and task.is_recording:
                    task.final_duration = task.get_elapsed_time()
                    task.process.terminate()
                    task.is_recording = False
            self.tasks.clear()
        self._notify_ui()
