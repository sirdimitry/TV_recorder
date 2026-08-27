# core/recorder.py
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Dict
from core.live_stream import LiveThumbnailStream
from core.screen_capture import (build_screen_capture_cmd, build_timestretch_cmd, find_loopback_audio_index,
                                  find_screen_device_index, get_retina_scale_factor)
from core.stream_resolver import RECONNECT_OPTS, hls_opts, resolve_variant_url
from utils.config import Config
from utils.filenames import safe_filename
from utils.logger import logger

SNAPSHOT_FPS = 4  # активных записей может быть много одновременно (1-16+) — держим частоту скромной


def _format_clip_mmss(total_seconds: float) -> str:
    """Секунды -> "мм:сс" — та же семантика, что и gui/app_window.py:
    _format_mmss (позиция в ролике), задублирована здесь, чтобы core/ не
    тянул зависимость от gui/."""
    total = round(total_seconds)
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


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

    def format_clip_range(self) -> Optional[str]:
        """Диапазон позиций В САМОМ РОЛИКЕ ("38:30–42:30"), а не время на
        часах — для "Мои ссылки" с заданным "С:"/"До:". None, если это не
        такая запись (обычный канал/эфир — тогда в интерфейсе используется
        format_recording_period)."""
        if self.clip_start_seconds is None:
            return None
        start_label = _format_clip_mmss(self.clip_start_seconds)
        end_label = _format_clip_mmss(self.clip_end_seconds) if self.clip_end_seconds else '…'
        return f"{start_label}–{end_label}"


class Recorder:
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
        safe_name = safe_filename(channel_name)
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
                       extra_headers: Optional[dict] = None,
                       seek_seconds: Optional[float] = None,
                       clip_end_seconds: Optional[float] = None,
                       duration_limit_seconds: Optional[float] = None) -> str:
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

        # -ss ПЕРЕД -i — это seek на уровне демуксера (быстрый, к ближайшему
        # keyframe), а не перекодирование с обрезкой после -i (то было бы
        # медленным decode+encode, а тут везде -c copy). Имеет смысл только
        # для готовых VOD-ссылок с известной длительностью ("Мои ссылки" —
        # позиция в самом ролике, см. gui/app_window.py: _add_link_dialog);
        # для живого эфира seek_seconds не передаётся вовсе.
        seek_opts = ['-ss', str(seek_seconds)] if seek_seconds else []
        if seek_seconds:
            logger.info(f"Recorder: запись '{channel_name}' с позиции {seek_seconds:.0f}с ролика")

        # Раздельные видео/аудио-дорожки (audio_url) с независимым -ss перед
        # каждым -i дают рассинхрон: видео демуксер снапает seek НАЗАД к
        # ближайшему опорному кадру (может занести на несколько секунд
        # раньше цели — нормальное поведение GOP), а звук перематывается
        # точно. -avoid_negative_ts make_zero потом сдвигает ОБА потока на
        # одну и ту же (видео-)величину, чтобы избежать отрицательных
        # меток — и именно поэтому звук оказывается сдвинут ВПЕРЁД на весь
        # этот зазор: первые несколько секунд видео идут без звука. Замерено
        # напрямую на реальном VK-потоке: зазор доходил до ~6с.
        #
        # Фикс — двухступенчатый seek, стандартный приём именно для этого
        # случая: сначала грубо перематываем ОБА потока на SEEK_MARGIN
        # секунд РАНЬШЕ цели (одинаково для видео и звука — раз оба гарантированно
        # покрывают запас в SEEK_MARGIN секунд ДО цели, где бы видео реально
        # не снапнулось внутри этого окна), затем один точный -ss ПОСЛЕ -map
        # обрезает оба уже смикшированных потока в ту же самую точку разом —
        # рассинхрона между ними больше нет, обрезка идёт по общему таймлайну
        # вывода, а не по двум независимым.
        SEEK_MARGIN = 15.0
        rough_seek_seconds = max(0.0, seek_seconds - SEEK_MARGIN) if seek_seconds else None
        precise_trim_seconds = (seek_seconds - rough_seek_seconds) if seek_seconds else None
        dual_track_seek_opts = ['-ss', str(rough_seek_seconds)] if rough_seek_seconds else []
        dual_track_trim_opts = ['-ss', str(precise_trim_seconds)] if precise_trim_seconds else []

        # -t ПОСЛЕ -i — ограничение по длительности САМОГО КОНТЕНТА (сколько
        # секунд видео попадёт в файл), а не по часам. Без -re (реального
        # временного темпа) -c copy читает VOD настолько быстро, насколько
        # отдаёт CDN — практика показала под 60x реального времени, то есть
        # "запиши 30 секунд" по одному только таймеру на потоке (см.
        # gui/app_window.py: _record_link_now) успевало бы скачать многие
        # МИНУТЫ контента раньше, чем таймер вообще сработает. -t считает
        # по временным меткам самого потока и остановит запись куда точнее
        # и быстрее — таймер на стороне приложения остаётся просто
        # подстраховкой на случай, если -t почему-то не сработает.
        duration_opts = ['-t', str(duration_limit_seconds)] if duration_limit_seconds else []
        if duration_limit_seconds:
            logger.info(f"Recorder: запись '{channel_name}' ограничена {duration_limit_seconds:.0f}с контента (-t)")

        if audio_url:
            # Видео и звук — уже отдельные закодированные дорожки (типично
            # для YouTube на 720p+): просто мультиплексируем их в один файл,
            # без пересчёта варианта — yt-dlp уже выбрал конкретный поток.
            cmd = [
                'ffmpeg', '-y',
                *RECONNECT_OPTS, *hls_opts(stream_url), *dual_track_seek_opts, '-headers', headers, '-i', stream_url,
                *RECONNECT_OPTS, *hls_opts(audio_url), *dual_track_seek_opts, '-headers', headers, '-i', audio_url,
                '-map', '0:v:0', '-map', '1:a:0',
                *dual_track_trim_opts,
                '-c', 'copy',
                *duration_opts,
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
                *RECONNECT_OPTS, *hls_opts(stream_url),
                *seek_opts,
                '-headers', headers,
                '-i', stream_url,
                '-c', 'copy',
                *duration_opts,
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
        task.clip_start_seconds = seek_seconds
        task.clip_end_seconds = clip_end_seconds

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
                                 source: str = "manual", on_complete: Optional[Callable] = None,
                                 speed_factor: Optional[float] = None) -> str:
        """Запись через захват экрана: открывает ссылку в отдельном окне-браузере
        и параллельно пишет экран через ffmpeg/avfoundation — для сайтов, чью
        прямую ссылку на поток получить не удалось (core/link_resolver.py).
        Окно никогда не уходит в macOS-fullscreen — пишется именно область
        под окном (см. gui/browser_capture.py), а fullscreen окна сдвинул бы
        его границы и рассинхронизировал их с уже посчитанной обрезкой.

        speed_factor — тот самый "резервный" фолбэк для сайтов, где не
        помогает даже sniff (JS-плеер рисует ролик так, что ни прямая
        ссылка на поток, ни сетевые запросы её не выдают): страница играет
        ролик на ускоренной скорости (gui/browser_capture.py: SPEED_CONTROL_JS,
        playbackRate), экранное время записи короче реальной длины ролика
        во столько же раз, а после записи core/screen_capture.py:
        build_timestretch_cmd растягивает файл обратно до нормальной
        скорости. Кадровая частота исходного захвата поднимается
        пропорционально — иначе после растяжки видео будет дёрганым."""
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

        browser_args = [url, channel_name]
        if speed_factor and speed_factor > 1:
            browser_args += ['--speed', str(speed_factor)]
            logger.info(f"Recorder: запись '{channel_name}' на ускоренном воспроизведении x{speed_factor:.0f} "
                        f"(последний резервный способ — растянем обратно по времени после записи)")

        try:
            browser_proc = subprocess.Popen(
                Config.browser_capture_command(*browser_args),
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

        speed_confirmed = False
        if speed_factor and speed_factor > 1:
            # Ускорение реально применяется только если на странице
            # физически нашёлся <video> (SPEED_CONTROL_JS в
            # gui/browser_capture.py) — на части сайтов сам ролик живёт в
            # чужом (cross-origin) iframe, куда со страницы принципиально
            # не залезть, и подтверждение просто не придёт. Ждём отдельно
            # от геометрии (та печатается сразу при появлении окна, а
            # подтверждение — уже после реальной загрузки страницы и
            # плеера, которая на части сайтов занимает 15-20с).
            speed_confirmed = self._wait_for_speed_confirmation(browser_proc)
            if not speed_confirmed:
                logger.warning(f"Recorder: не удалось включить ускоренное воспроизведение для '{channel_name}' "
                                f"(видео не нашлось на странице — возможно, в чужом iframe) — "
                                f"пишем как обычно, без ускорения и без растяжки по времени")

        # При ускорении картинка на экране реально меняется быстрее — без
        # поднятия кадровой частоты захвата после обратной растяжки
        # (setpts) видео получится дёрганым (мало исходных кадров на
        # растянутый интервал времени).
        capture_framerate = 60 if speed_confirmed else 30
        cmd = build_screen_capture_cmd(str(output_file), screen_index, audio_index, crop=crop,
                                        framerate=capture_framerate)

        task = RecordingTask(task_id, channel_name, url, str(output_file), source)
        task.on_complete = on_complete
        task.is_screen_capture = True
        task.browser_proc = browser_proc
        # Растяжку по времени (см. _timestretch_task_output) включаем
        # ТОЛЬКО если ускорение подтверждено — иначе запись, реально
        # шедшая в нормальном темпе, была бы ошибочно замедлена в
        # speed_factor раз при "восстановлении".
        task.speed_factor = speed_factor if speed_confirmed else None
        task.screen_capture_has_audio = audio_index is not None

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

    def _wait_for_speed_confirmation(self, browser_proc: subprocess.Popen, timeout: float = 20.0) -> bool:
        """Читает строку "SPEED_ACTIVE" из того же stdout (после GEOMETRY,
        см. _read_browser_geometry — тот же поток, ничего не теряется между
        двумя последовательными чтениями). Печатается из
        _RelayoutApi.report_speed_active() в gui/browser_capture.py, только
        когда SPEED_CONTROL_JS реально нашла <video> на странице и
        выставила playbackRate. Таймаут больше, чем у геометрии — тут ждём
        уже настоящую загрузку страницы и инициализацию плеера, которая на
        части сайтов (см. gui/browser_capture.py: LOADING_HTML) занимает
        15-20с."""
        import select
        deadline = time.time() + timeout
        while time.time() < deadline:
            if browser_proc.poll() is not None:
                return False
            try:
                ready, _, _ = select.select([browser_proc.stdout], [], [], 0.5)
            except Exception:
                ready = [browser_proc.stdout]
            if not ready:
                continue
            line = browser_proc.stdout.readline()
            if not line:
                continue
            if line.strip() == 'SPEED_ACTIVE':
                return True
        return False

    def _watch_browser_proc(self, task: RecordingTask):
        """Если пользователь закрыл окно-браузер вручную, не нажимая Стоп —
        останавливаем запись экрана вместе с ним, а не пишем пустой рабочий стол."""
        if not task.browser_proc:
            return
        task.browser_proc.wait()
        if task.is_recording:
            logger.info(f"Recorder: окно-браузер '{task.channel_name}' закрыто — останавливаем запись экрана")
            self.stop_recording(task.task_id)

    @staticmethod
    def _terminate_task_process(task: 'RecordingTask'):
        """SIGTERM надёжно останавливает обычный -c copy ffmpeg (стрим
        каналов/ссылок), но запись экрана (-f avfoundation, см.
        core/screen_capture.py) на macOS по SIGTERM может просто зависнуть
        или не записать корректный файл — известная особенность
        avfoundation-захвата. SIGINT ffmpeg сам обрабатывает как штатную
        "мягкую" остановку (как Ctrl+C/клавиша q в интерактивном режиме):
        дописывает корректный трейлер и выходит. Раньше отправлялся
        SIGTERM всегда — судя по всему, именно это давало запись экрана,
        которая не останавливалась таймером "До:" вовремя."""
        if task.is_screen_capture:
            task.process.send_signal(signal.SIGINT)
        else:
            task.process.terminate()

    def stop_recording(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)

        if task and task.process:
            logger.info(f"Recorder: Остановка записи '{task.channel_name}' (task: {task_id})")
            task.final_duration = task.get_elapsed_time()
            task.stop_requested = True
            self._terminate_task_process(task)
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
        if success and task.speed_factor:
            self._timestretch_task_output(task)
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

    def _timestretch_task_output(self, task: RecordingTask):
        """Растягивает файл, записанный на ускоренном playbackRate, обратно
        до нормальной скорости (см. start_browser_recording). Идёт СИНХРОННО
        в потоке _wait_for_task — этот поток и так уже фоновый и больше
        ничего не делает, on_complete всё равно должен получить финальный,
        уже поправленный файл, а не сырой быстрый. Если растяжка почему-то
        не удалась — оставляем исходный (ускоренный) файл как есть, чтобы
        человек хотя бы не остался совсем без записи."""
        output_file = Path(task.output_path)
        temp_file = output_file.with_name(output_file.stem + '_stretch_tmp' + output_file.suffix)
        cmd = build_timestretch_cmd(str(output_file), str(temp_file), task.speed_factor,
                                     has_audio=task.screen_capture_has_audio)
        try:
            timeout = max(300.0, task.get_elapsed_time() * 3)
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if result.returncode == 0 and temp_file.is_file() and temp_file.stat().st_size > 1024:
                output_file.unlink()
                temp_file.rename(output_file)
                logger.info(f"Recorder: '{task.channel_name}' — запись растянута обратно по времени "
                            f"(записывалась на x{task.speed_factor:.0f})")
            else:
                temp_file.unlink(missing_ok=True)
                err = result.stderr.decode('utf-8', errors='ignore')[-300:] if result.stderr else ''
                logger.error(f"Recorder: не удалось растянуть по времени '{task.channel_name}' — "
                             f"файл остался ускоренным ({task.speed_factor:.0f}x): {err}")
        except Exception as e:
            temp_file.unlink(missing_ok=True)
            logger.error(f"Recorder: ошибка растяжки по времени '{task.channel_name}' — "
                         f"файл остался ускоренным ({task.speed_factor:.0f}x): {e}")

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
                    self._terminate_task_process(task)
                    task.is_recording = False
                    self._stop_snapshot_stream(task)
                    if task.browser_proc and task.browser_proc.poll() is None:
                        task.browser_proc.terminate()
            self.tasks.clear()
        self._notify_ui()
