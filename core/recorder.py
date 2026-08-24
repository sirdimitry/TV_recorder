# core/recorder.py
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import re
from typing import Optional, Callable, Dict
from core.live_stream import LiveThumbnailStream
from core.screen_capture import (build_screen_capture_cmd, find_loopback_audio_index,
                                  find_screen_device_index, get_retina_scale_factor)
from core.stream_resolver import resolve_variant_url
from utils.config import Config
from utils.logger import logger

SNAPSHOT_FPS = 4  # активных записей может быть много одновременно (1-16+) — держим частоту скромной


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
        self.stop_requested = False  # True, если остановку инициировали мы (кнопка/расписание/выход)
        self.ended_early = False  # True, если ffmpeg сам дошёл до конца потока раньше, чем мы попросили его остановиться
        self.headers: Optional[dict] = None  # заголовки, с которыми реально шла запись — для live-превью того же потока
        self.last_snapshot: Optional[bytes] = None  # JPEG-байты последнего пойманного кадра
        self.snapshot_seq = 0  # растёт при каждом новом кадре — по нему потребители понимают, что картинку пора перерисовать
        self.snapshot_stream: Optional[LiveThumbnailStream] = None
        self.is_screen_capture = False  # запись через захват экрана (core/screen_capture.py), а не -c copy потока
        self.browser_proc: Optional[subprocess.Popen] = None  # окно-браузер (gui/browser_capture.py) для screen_capture
    
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
        self._ui_callbacks: list = []  # несколько подписчиков (панель записей, списки каналов/ссылок)

    def set_ui_callback(self, callback: Callable):
        """Регистрирует callback для обновления UI (вызывается из любого потока).
        Можно вызывать несколько раз — панель записей и списки каналов/ссылок
        подписываются каждый на своё обновление."""
        self._ui_callbacks.append(callback)

    def remove_ui_callback(self, callback: Callable):
        """Снимает подписку — важно для окон, которые можно закрыть и открыть
        заново (иначе список подписчиков растёт с каждым открытием)."""
        if callback in self._ui_callbacks:
            self._ui_callbacks.remove(callback)

    def find_active_task_id(self, channel_name: str) -> Optional[str]:
        """ID текущей активной (незавершённой) записи для этого имени, если есть."""
        with self._lock:
            for task in self.tasks.values():
                if task.channel_name == channel_name and task.is_recording:
                    return task.task_id
        return None

    @classmethod
    def build_output_path(cls, channel_name: str, recorded_at: datetime | None = None) -> Path:
        """Builds a portable filename so macOS and Windows handle it identically."""
        timestamp = (recorded_at or datetime.now()).strftime('%Y-%m-%d_%H-%M-%S')
        latin_name = channel_name.translate(cls.TRANSLITERATION)
        safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', latin_name).strip('_') or 'channel'
        return Config.get_recordings_dir() / f"{safe_name}_{timestamp}.mp4"

    def _notify_ui(self):
        """Безопасный вызов всех UI callback'ов из любого потока"""
        for callback in self._ui_callbacks:
            try:
                callback()
            except Exception as e:
                logger.error(f"Recorder: Ошибка UI callback: {e}")
    
    def start_recording(self, channel_name: str, stream_url: str,
                       output_path: str, source: str = "manual",
                       on_complete: Optional[Callable] = None,
                       audio_url: Optional[str] = None,
                       extra_headers: Optional[dict] = None) -> str:
        Config.init_dirs()
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        task_id = f"{channel_name}_{int(time.time())}"

        if extra_headers:
            # Ссылки, разобранные через yt-dlp (core/link_resolver.py),
            # приходят со своими заголовками — некоторые CDN (например VK:
            # vkvd*.okcdn.ru) подписывают URL под конкретный User-Agent и
            # отвечают HTTP 400 на любой другой, даже если сама ссылка верна.
            ua = extra_headers.get('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')
            ref = extra_headers.get('Referer', '')
            headers = ''.join(f"{k}: {v}\r\n" for k, v in extra_headers.items())
            headers_dict = dict(extra_headers)
        else:
            headers_info = self.CHANNEL_HEADERS.get(channel_name, {})
            ua = headers_info.get("ua", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
            ref = headers_info.get("ref", "https://www.google.com")
            headers = f"User-Agent: {ua}\r\nReferer: {ref}\r\nOrigin: {ref}\r\n"
            headers_dict = {"User-Agent": ua, "Referer": ref, "Origin": ref}

        # -allowed_extensions — опция HLS-демуксера (снимает ограничение на
        # расширения сегментов у капризных CDN); для прямого файла (.mp4 и
        # т.п., не .m3u8) ffmpeg её просто не знает и падает с "Option
        # allowed_extensions not found" ещё до открытия потока — замечено
        # на VK/okcdn.ru (прямой .mp4-подобный URL без .m3u8), но касалось
        # бы и любой другой напрямую найденной .mp4-ссылки (см.
        # link_resolver.py). Добавляем опцию только когда поток реально HLS.
        def hls_opts(u: str):
            return ['-allowed_extensions', 'ALL'] if '.m3u8' in u.lower() else []

        if audio_url:
            # Видео и звук — уже отдельные закодированные дорожки (типично
            # для YouTube на 720p+): просто мультиплексируем их в один файл,
            # без пересчёта варианта — yt-dlp уже выбрал конкретный поток.
            cmd = [
                'ffmpeg', '-y',
                *hls_opts(stream_url), '-headers', headers, '-i', stream_url,
                *hls_opts(audio_url), '-headers', headers, '-i', audio_url,
                '-map', '0:v:0', '-map', '1:a:0',
                '-c', 'copy',
                '-err_detect', 'ignore_err',
                '-fflags', '+genpts+discardcorrupt',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                str(output_file)
            ]
        else:
            # Если это HLS-мастер-плейлист с несколькими битрейтами, пишем
            # вариант ближе к 720p/3-5 Мбит вместо того, что выберет сам
            # ffmpeg (обычно самый тяжёлый) — без перекодирования, просто
            # другой исходный вариант для copy-режима.
            stream_url = resolve_variant_url(stream_url, user_agent=ua, referer=ref)

            cmd = [
                'ffmpeg', '-y',
                *hls_opts(stream_url),
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
        task.headers = headers_dict
        
        try:
            task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            task.is_recording = True
            task.start_time = time.time()
            task.started_at = datetime.now()
            
            with self._lock:
                self.tasks[task_id] = task
            
            logger.info(f"Recorder: Начата запись '{channel_name}' → {output_file} (source: {source})")
            
            threading.Thread(target=self._wait_for_task, args=(task,), daemon=True).start()
            self._start_snapshot_stream(task)

            if not self._running:
                self._start_timer_loop()
            
            # Уведомляем UI немедленно
            self._notify_ui()
            
            return task_id
            
        except Exception as e:
            logger.error(f"Recorder: Ошибка запуска ffmpeg: {e}")
            return ""

    def start_browser_recording(self, channel_name: str, url: str, output_path: str,
                                 source: str = "manual", on_complete: Optional[Callable] = None) -> str:
        """Запись через захват экрана: открывает ссылку в отдельном окне-браузере
        и параллельно пишет экран через ffmpeg/avfoundation — для сайтов, чью
        прямую ссылку на поток получить не удалось (core/link_resolver.py).
        Окно никогда не уходит в macOS-fullscreen — пишется именно область
        под окном (см. gui/browser_capture.py), а fullscreen окна сдвинул бы
        его границы и рассинхронизировал их с уже посчитанной обрезкой."""
        Config.init_dirs()
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        screen_index = find_screen_device_index()
        if screen_index is None:
            logger.error("Recorder: не найдено устройство 'Capture screen' для avfoundation")
            return ""
        audio_index = find_loopback_audio_index()
        if audio_index is None:
            logger.warning("Recorder: устройство BlackHole не найдено — запись экрана будет без звука "
                            "(нужен виртуальный аудио-loopback и Multi-Output Device в Audio MIDI Setup)")

        task_id = f"{channel_name}_{int(time.time())}"

        browser_script = Path(__file__).resolve().parent.parent / 'gui' / 'browser_capture.py'
        try:
            browser_proc = subprocess.Popen(
                [sys.executable, str(browser_script), url, channel_name],
                stdout=subprocess.PIPE, text=True, bufsize=1)
        except Exception as e:
            logger.error(f"Recorder: не удалось открыть окно-браузер: {e}")
            return ""

        crop = self._read_browser_geometry(browser_proc)
        if crop:
            logger.info(f"Recorder: пишем только окно-браузер '{channel_name}' ({crop[2]}x{crop[3]}px), "
                         f"не весь экран")
        else:
            logger.warning(f"Recorder: не удалось получить границы окна-браузера '{channel_name}' — "
                            f"придётся писать весь экран целиком")

        cmd = build_screen_capture_cmd(str(output_file), screen_index, audio_index, crop=crop)

        task = RecordingTask(task_id, channel_name, url, str(output_file), source)
        task.on_complete = on_complete
        task.is_screen_capture = True
        task.browser_proc = browser_proc

        try:
            task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            task.is_recording = True
            task.start_time = time.time()
            task.started_at = datetime.now()

            with self._lock:
                self.tasks[task_id] = task

            logger.info(f"Recorder: Начата запись экрана '{channel_name}' → {output_file} (source: {source})")

            threading.Thread(target=self._wait_for_task, args=(task,), daemon=True).start()
            threading.Thread(target=self._watch_browser_proc, args=(task,), daemon=True).start()

            if not self._running:
                self._start_timer_loop()

            self._notify_ui()
            return task_id

        except Exception as e:
            logger.error(f"Recorder: Ошибка запуска записи экрана: {e}")
            browser_proc.terminate()
            return ""

    def _read_browser_geometry(self, browser_proc: subprocess.Popen, timeout: float = 10.0):
        """Читает строку "GEOMETRY:x,y,w,h" (в points) из stdout окна-браузера
        (gui/browser_capture.py печатает её сразу после появления окна) и
        переводит в пиксели — эта область экрана и обрежется в записи,
        вместо того чтобы писать весь дисплей целиком."""
        import select
        deadline = time.time() + timeout
        while time.time() < deadline:
            if browser_proc.poll() is not None:
                return None
            try:
                ready, _, _ = select.select([browser_proc.stdout], [], [], 0.5)
            except Exception:
                ready = [browser_proc.stdout]
            if not ready:
                continue
            line = browser_proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            if not line.startswith('GEOMETRY:'):
                continue
            try:
                x, y, w, h = (float(v) for v in line[len('GEOMETRY:'):].split(','))
            except Exception:
                return None
            scale = get_retina_scale_factor()
            return (round(x * scale), round(y * scale), round(w * scale), round(h * scale))
        return None

    def _watch_browser_proc(self, task: RecordingTask):
        """Если пользователь закрыл окно-браузер вручную, не нажимая Стоп —
        останавливаем запись экрана вместе с ним, а не пишем пустой рабочий стол."""
        if not task.browser_proc:
            return
        task.browser_proc.wait()
        if task.is_recording:
            logger.info(f"Recorder: окно-браузер '{task.channel_name}' закрыто — останавливаем запись экрана")
            self.stop_recording(task.task_id)

    def stop_recording(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)

        if task and task.process:
            logger.info(f"Recorder: Остановка записи '{task.channel_name}' (task: {task_id})")
            task.final_duration = task.get_elapsed_time()
            task.stop_requested = True
            task.process.terminate()
            task.is_recording = False
            self._stop_snapshot_stream(task)
            if task.browser_proc and task.browser_proc.poll() is None:
                task.browser_proc.terminate()
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
        self._stop_snapshot_stream(task)
        if task.browser_proc and task.browser_proc.poll() is None:
            task.browser_proc.terminate()

        success = returncode == 0 or returncode == 255
        output_file = Path(task.output_path)
        has_usable_file = output_file.is_file() and output_file.stat().st_size > 1024
        success = success and has_usable_file
        task.success = success
        # Если ffmpeg сам дошёл до конца потока (и мы его об этом не просили) —
        # значит источник закончился раньше, чем длилось окно записи: эфир
        # прервался или закончился сам записываемый файл/ролик.
        task.ended_early = success and not task.stop_requested
        if success:
            if task.ended_early:
                logger.warning(f"Recorder: '{task.channel_name}' — источник закончился раньше окна записи")
            else:
                logger.info(f"Recorder: Запись '{task.channel_name}' завершена успешно")
        else:
            err_msg = stderr.decode('utf-8', errors='ignore')[-500:] if stderr else ""
            if not has_usable_file:
                err_msg = "Recording finished without creating a usable video file. " + err_msg
            task.error_message = err_msg
            logger.error(f"Recorder: Ошибка записи '{task.channel_name}' (code {returncode}): {err_msg}")

        if task.on_complete:
            task.on_complete(success, task.channel_name, task.output_path, task.ended_early)

        self._notify_ui()

    def _start_snapshot_stream(self, task: RecordingTask):
        """Непрерывный поток кадров (~4 fps) на саму задачу — централизованно,
        один ffmpeg-процесс на запись, а не по одному на каждого, кто её
        отображает (панель записей + окно-монитор независимо друг от друга
        дублировали бы одни и те же вызовы)."""
        def on_frame(jpeg_bytes: bytes):
            task.last_snapshot = jpeg_bytes
            task.snapshot_seq += 1
            # _notify_ui() здесь не дёргаем: при нескольких записях это было
            # бы широковещательное уведомление всем подписчикам на каждый
            # кадр каждой задачи. Панель записей и монитор сами опрашивают
            # snapshot_seq с нужной им частотой.

        task.snapshot_stream = LiveThumbnailStream(
            task.stream_url, task.headers, fps=SNAPSHOT_FPS, on_frame=on_frame)
        task.snapshot_stream.start()

    def _stop_snapshot_stream(self, task: RecordingTask):
        if task.snapshot_stream is not None:
            task.snapshot_stream.stop()
            task.snapshot_stream = None

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
                    task.stop_requested = True
                    task.process.terminate()
                    task.is_recording = False
                    self._stop_snapshot_stream(task)
                    if task.browser_proc and task.browser_proc.poll() is None:
                        task.browser_proc.terminate()
            self.tasks.clear()
        self._notify_ui()
