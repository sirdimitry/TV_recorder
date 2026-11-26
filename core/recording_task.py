"""State and time labels of one recording, independent of orchestration."""
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional, Callable
from core.live_stream import LiveThumbnailStream
from utils.timecode import format_clip_time


class RecordingTask:
    def __init__(self, task_id: str, channel_name: str, stream_url: str, 
                 output_path: str, source: str = "manual"):
        self.task_id = task_id
        self.channel_name = channel_name
        self.stream_url = stream_url
        self.output_path = output_path
        self.recovery_marker = None
        self.capture_path = None
        self.finalization_error = ""
        self.storage_error = ""
        self.storage_warning = ''
        self.disk_free_bytes = None
        self.disk_reserved_bytes = 0
        self.disk_error = ''
        self.file_size_bytes = None
        self.output_available = False
        self.merge_error = ''
        self.phase = 'connecting'
        self.disk_guard = None
        self.progress_monitor = None
        self.media_seconds = 0.0
        self.recorded_bytes = 0
        self.connection_state = 'connecting'
        self.is_live = False
        self.recovering = False
        self.stalled = False
        self.retry_count = 0
        self.retry_cancel = threading.Event()
        self.deadline = None
        self.command = None
        self.base_output_path = output_path
        self.parts = []
        self.recovery_exhausted = False
        self.pause_pending = False
        self.pause_count = 0
        self.part_number = 1
        self.merging = False
        self.merged_path = ''
        self.expected_audio = None
        self.content_expected_duration = None
        self.expected_seconds = None
        self.actual_seconds = None
        self.missing_seconds = None
        self.part_started_wall = 0.0
        self.part_ended_wall = None
        self.part_assessment = None
        self.part_probe = None
        self.stop_timestamp = None
        self.history_error = ''
        self.history_last_state = None
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
        self.result_status: Optional[str] = None
        self.error_message = ""
        self.on_complete: Optional[Callable] = None
        self.stop_requested = False  # True, если остановку инициировали мы (кнопка/расписание/выход)
        self.ended_early = False  # True, если ffmpeg сам дошёл до конца потока раньше, чем мы попросили его остановиться
        self.duration_limit_seconds: Optional[float] = None
        self.headers: Optional[dict] = None  # заголовки, с которыми реально шла запись — для live-превью того же потока
        # Если видео и звук у источника — раздельные HLS-рендиции (см.
        # 'Россия 24'/'Россия К' в core/m3u_parser.py), stream_url отдаёт
        # только видео — здесь адрес именно звуковой дорожки, чтобы
        # прослушивание в мониторе (gui/recording_monitor.py) знало, что
        # слушать, а не пыталось достать звук оттуда, где его нет.
        self.audio_url: Optional[str] = None
        self.last_snapshot: Optional[bytes] = None  # JPEG-байты последнего пойманного кадра
        self.snapshot_seq = 0  # растёт при каждом новом кадре — по нему потребители понимают, что картинку пора перерисовать
        self.snapshot_stream: Optional[LiveThumbnailStream] = None
        self.snapshot_profile = None
        self.snapshot_generation = 0
        self.is_screen_capture = False  # запись через захват экрана (core/screen_capture.py), а не -c copy потока
        self.browser_proc: Optional[subprocess.Popen] = None  # окно-браузер (gui/browser_capture.py) для screen_capture
        # Позиция в САМОМ ролике ("С:"/"До:" из диалога добавления ссылки,
        # см. gui/app_window.py: _add_link_dialog) — не время на часах.
        # Только для "Мои ссылки" с заданным seek; для каналов/эфира остаётся
        # None, и интерфейс показывает обычный час:минута диапазон записи
        # (format_recording_period), а не эти поля.
        self.clip_start_seconds: Optional[float] = None
        self.clip_end_seconds: Optional[float] = None
        # Ускоренная запись экрана (см. Recorder.start_browser_recording) —
        # во сколько раз плеер играл быстрее реального времени; None/1 —
        # обычная запись без ускорения, растяжку по времени делать не надо.
        self.speed_factor: Optional[float] = None
        self.screen_capture_has_audio: bool = True
    
    def get_elapsed_time(self) -> int:
        if not self.is_recording and self.final_duration > 0:
            return self.final_duration
        if not self.is_recording:
            return 0
        if self.is_paused:
            return int(self.pause_time - self.start_time - self.total_paused_duration)
        return int(time.time() - self.start_time - self.total_paused_duration)
    
    def format_elapsed_time(self) -> str:
        durations = [part.get('container_duration') for part in self.parts
                     if part.get('status') in ('completed', 'partial')]
        if self.result_status is not None and not self.is_recording and not self.recovering and durations and all(
                duration is not None for duration in durations):
            seconds = int(sum(durations) + 0.5)
        else:
            seconds = self.get_elapsed_time()
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def format_recording_period(self) -> str:
        """Дата и фактический диапазон записи для интерфейса."""
        started_at = self.started_at or datetime.fromtimestamp(self.start_time)
        ended_at = self.finished_at
        if ended_at and ended_at.date() != started_at.date():
            return f"{started_at:%d.%m.%Y · %H:%M} – {ended_at:%d.%m.%Y · %H:%M}"
        end_label = ended_at.strftime('%H:%M') if ended_at else 'сейчас'
        return f"{started_at:%d.%m.%Y · %H:%M} – {end_label}"

    def format_clip_range(self) -> Optional[str]:
        """Диапазон позиций В САМОМ РОЛИКЕ ("00:38:30–00:42:30"), а не время на
        часах — для "Мои ссылки" с заданным "С:"/"До:". None, если это не
        такая запись (обычный канал/эфир — тогда в интерфейсе используется
        format_recording_period)."""
        if self.clip_start_seconds is None:
            return None
        start_label = format_clip_time(self.clip_start_seconds)
        end_label = format_clip_time(self.clip_end_seconds) if self.clip_end_seconds else '…'
        return f"{start_label}–{end_label}"


