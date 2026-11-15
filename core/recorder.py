# core/recorder.py
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Dict
from core import recording_progress as progress_policy
from core.recording_progress import RecordingProgress
from core.monitor_profile import MODES, monitor_profile
from core.sleep_inhibitor import SleepInhibitor
from core.disk_space import DiskGuard, DiskSpaceError, ensure_space
from core.recording_recovery import FRAGMENT_FLAGS, prepare_recording, finalize_recording, release_recording
from core.live_stream import LiveThumbnailStream
from core.media_probe import COMPLETED, PARTIAL, classify_media, assess_probe, duration_tolerance
from core.screen_capture import (build_screen_capture_cmd, build_timestretch_cmd, find_loopback_audio_index,
                                  find_screen_device_index, get_retina_scale_factor)
from core.stream_resolver import RECONNECT_OPTS, hls_opts, resolve_variant_url
from utils.config import Config
from utils.filenames import unique_media_path
from utils.logger import logger

SNAPSHOT_FPS = 4  # активных записей может быть много одновременно (1-16+) — держим частоту скромной


from core.recording_task import RecordingTask


class Recorder:
    CHANNEL_HEADERS = {
        "Первый канал": {"ua": "Mozilla/5.0", "ref": "https://www.1tv.ru"},
        "Россия 1": {"ua": "Mozilla/5.0", "ref": "https://smotrim.ru"},
        "НТВ": {"ua": "Mozilla/5.0", "ref": "https://www.ntv.ru"},
        "Матч ТВ": {"ua": "HlsWinkPlayer", "ref": "https://matchtv.ru"},
        "ТНТ": {"ua": "WINK/1.40.1 (AndroidTV/9) HlsWinkPlayer"},
        "Муз-ТВ": {"ua": "Dalvik/2.1.0 (Linux; U; Android 10)", "ref": "https://muz-tv.ru"}
    }

    @classmethod
    def channel_headers(cls, channel: dict) -> dict:
        """Единые HTTP-заголовки канала для проверки, просмотра и записи."""
        info = cls.CHANNEL_HEADERS.get(channel.get('name', ''), {})
        referer = info.get('ref', 'https://www.google.com')
        headers = {
            'User-Agent': info.get('ua', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'),
            'Referer': referer,
            'Origin': referer,
        }
        if channel.get('user_agent'):
            headers['User-Agent'] = channel['user_agent']
        headers.update(channel.get('headers') or {})
        return headers
    
    def __init__(self, history=None, auto_join=True):
        self.auto_join = auto_join
        self.history = history
        self.history_error_callback = None
        self.power_error_callback = None
        self._sleep_guard = SleepInhibitor(on_error=self._power_notice)
        self._screen_guard = SleepInhibitor(on_error=self._power_notice, keep_display_awake=True)
        self.tasks: Dict[str, RecordingTask] = {}
        self._lock = threading.RLock()
        self._timer_thread: Optional[threading.Thread] = None
        self._running = False
        self._ui_callbacks: list = []  # несколько подписчиков (панель записей, списки каналов/ссылок)
        self._snapshot_consumers = 0
        saved_mode = Config._read_settings().get('snapshot_mode', 'economy')
        self.snapshot_mode = saved_mode if saved_mode in MODES else 'economy'
        self._closing = False
        self._storage_callback = None
        self._stop_timers: Dict[str, threading.Timer] = {}
        self._wait_threads: set[threading.Thread] = set()
        self._screen_capture_confirmation: Optional[Callable[[str, bool, bool], bool]] = None
        self._screen_capture_error_callback: Optional[Callable[[str], None]] = None

    def _power_notice(self, message):
        if self.power_error_callback and not self._closing:
            self.power_error_callback(message)

    def _history_event(self, task, event, detail=None, *, state=None):
        if self.history is None:
            return
        from core.recording_history import safe_error
        if state is None:
            state = ('paused' if task.is_paused else 'recording' if task.is_recording else
                     'retrying' if task.recovering else task.result_status or 'finalizing')
        parts = [{**part, 'error': safe_error(part.get('error'))} for part in task.parts]
        active = state in {'recording', 'paused', 'connecting', 'reconnecting', 'retrying', 'finalizing'}
        expected = task.expected_seconds
        actual = task.actual_seconds
        missing = task.missing_seconds
        if active:
            expected = (max(0, task.deadline-task.start_time) if task.is_live and task.deadline is not None
                        and task.start_time else task.content_expected_duration)
            saved = [part.get('duration') for part in parts if part.get('duration') is not None]
            actual = sum(saved) if saved else None
            missing = None
        snapshot = dict(task_id=task.task_id, name=safe_error(task.channel_name), source=task.source,
                        state=state, started_at=task.started_at.isoformat() if task.started_at else None,
                        finished_at=task.finished_at.isoformat() if task.finished_at else None,
                        output_path=task.output_path, capture_path=str(task.capture_path) if task.capture_path else None,
                        expected_seconds=expected, actual_seconds=actual,
                        missing_seconds=missing, expected_audio=task.expected_audio,
                        retries=task.retry_count, pauses=task.pause_count, parts=parts,
                        error=safe_error(task.error_message), merged_path=task.merged_path)
        try:
            self.history.save(snapshot, event, detail)
        except Exception as error:
            logger.exception('Не удалось сохранить историю записи')
            if not task.history_error:
                task.history_error = str(error)
                if self.history_error_callback and not self._closing:
                    try:
                        self.history_error_callback('Историю записи не удалось сохранить. Медиафайлы не удалены.')
                    except Exception:
                        logger.exception('Не удалось показать ошибку истории')

    def _progress_changed(self, task):
        state = task.connection_state
        if state != task.history_last_state:
            task.history_last_state = state
            self._history_event(task, 'connection_state', {'state': state}, state=state)
        self._notify_ui()

    def _update_session_assessment(self, task):
        durations = [part.get('duration') for part in task.parts
                     if part.get('status') in (COMPLETED, PARTIAL)]
        task.actual_seconds = sum(value for value in durations if value is not None) if any(
            value is not None for value in durations) else None
        if task.is_live and task.start_time:
            end = task.stop_timestamp or task.part_ended_wall or time.time()
            if task.deadline is not None:
                end = min(end, task.deadline)
            task.expected_seconds = max(0.0, end-task.start_time)
        elif not task.stop_requested:
            task.expected_seconds = task.content_expected_duration or task.duration_limit_seconds
        task.missing_seconds = (max(0, task.expected_seconds-task.actual_seconds)
                                if task.expected_seconds is not None and task.actual_seconds is not None else None)
        if (task.missing_seconds is not None
                and task.missing_seconds > duration_tolerance(task.expected_seconds)):
            if task.result_status == COMPLETED:
                task.result_status, task.success = PARTIAL, False
            reason = (f'Недобор длительности: ожидалось {task.expected_seconds:.1f} с, '
                      f'сохранено {task.actual_seconds:.1f} с, не хватает {task.missing_seconds:.1f} с')
            if reason not in task.error_message:
                task.error_message = (task.error_message+'; '+reason).strip('; ')

    def set_storage_callback(self, callback):
        self._storage_callback = callback

    def _storage_notice(self, message):
        logger.warning(message)
        if self._storage_callback:
            try:
                self._storage_callback(message)
            except Exception:
                logger.exception('Не удалось показать предупреждение о диске')

    def _check_output_directory(self, output):
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            ensure_space(output.parent, reserve=DiskGuard.reserved_bytes(output.parent), writable=True)
            return True
        except OSError as error:
            self._storage_notice(str(error))
            return False

    def _refresh_task_metrics(self, task):
        # Called by recording workers/timer, never by a Tk callback.
        current = task.output_path
        try:
            if task.capture_path and Path(task.capture_path).is_file():
                current = task.capture_path
        except OSError:
            pass
        paths = ([task.merged_path] if task.merged_path else
                 [part['path'] for part in task.parts]+[current])
        sizes = []
        for path in {Path(path) for path in paths if path}:
            try:
                if path.is_file():
                    sizes.append(path.stat().st_size)
            except OSError:
                pass
        task.file_size_bytes = sum(sizes) if sizes else (task.recorded_bytes or None)
        try:
            task.output_available = Path(task.output_path).is_file()
        except OSError:
            task.output_available = False
        guard = task.disk_guard
        if guard is not None:
            task.disk_free_bytes = guard.last_free_bytes
            task.disk_reserved_bytes = guard.last_reserved_bytes
            task.disk_error = guard.last_error
            if (guard.last_free_bytes is not None and
                    guard.last_free_bytes >= 3*1024**3+guard.last_reserved_bytes):
                task.storage_warning = ''

    def _watch_task_disk(self, task):
        def warning(message):
            task.storage_warning = message
            self._storage_notice(message)
            self._notify_ui()
        def stop(message):
            if not task.is_recording or task.process.poll() is not None:
                return
            task.storage_error = message
            self.stop_recording(task.task_id)
            self._storage_notice(message)
            try:
                task.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                task.process.kill()
        task.disk_guard = DiskGuard(Path(task.output_path).parent,
                                    reserve_file=task.capture_path, reserve_factor=task.speed_factor or 1,
                                    on_warning=warning, on_stop=stop)
        task.disk_guard.start()

    def _watch_task_progress(self, task):
        process = task.process
        def stall():
            with self._lock:
                if (task.stop_requested or task.is_paused or self._closing
                        or process is not task.process or process.poll() is not None):
                    return
                task.stalled = True
                task.connection_state = 'stalled'
                task.phase = 'stopping'
                self._history_event(task, 'stalled', state='reconnecting')
                logger.warning("Recorder: нет продвижения записи '%s', завершаем часть", task.channel_name)
                self._terminate_task_process(task)
            self._notify_ui()
            try:
                process.wait(timeout=progress_policy.STOP_GRACE)
            except subprocess.TimeoutExpired:
                process.kill()
        def deadline():
            self.stop_recording(task.task_id)
            try:
                process.wait(timeout=progress_policy.STOP_GRACE)
            except subprocess.TimeoutExpired:
                process.kill()
        task.progress_monitor = RecordingProgress(
            process, task, stall, lambda: self._progress_changed(task), deadline)
        task.progress_monitor.start()

    def _retry_allowed(self, task):
        return (task.is_live and not task.stop_requested and not task.storage_error
                and not self._closing
                and (not task.finalization_error or task.result_status == 'processing_error')
                and (task.deadline is None or time.time() < task.deadline)
                and (task.stalled or task.ended_early or not task.success))

    def _restart_part(self, task, *, after_pause=False):
        output = Path(task.base_output_path)
        output = output.with_name(f'{output.stem}_part{task.part_number+1:03d}{output.suffix}')
        if not self._check_output_directory(output):
            task.storage_error = 'Папка или свободное место не позволяют возобновить запись'
            return False
        cmd = list(task.command)
        if not task.audio_url:
            fresh_url = resolve_variant_url(task.source_stream_url,
                                            request_headers=task.headers)
            cmd[cmd.index('-i')+1] = fresh_url
            task.stream_url = fresh_url
        if after_pause:
            # Resume at the newest HLS segment rather than buffered DVR content.
            while '-live_start_index' in cmd:
                index = cmd.index('-live_start_index')
                del cmd[index:index+2]
            inputs = [i for i, value in enumerate(cmd[:-1])
                      if value == '-i' and '.m3u8' in cmd[i+1].lower()]
            for index in reversed(inputs):
                cmd[index:index] = ['-live_start_index', '-1']
        remaining = task.deadline - time.time() if task.deadline is not None else None
        if remaining is not None and remaining <= 0:
            return False
        if '-t' in cmd:
            index = cmd.index('-t')
            cmd[index+1] = str(remaining)
        elif remaining is not None:
            cmd[-1:-1] = ['-t', str(remaining)]
        capture, marker = prepare_recording(output, uuid.uuid4().hex)
        cmd[-1] = str(capture)
        try:
            with self._lock:
                if (task.stop_requested or self._closing
                        or (task.deadline is not None and time.time() >= task.deadline)):
                    release_recording(marker)
                    return False
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                task.process = process
                task.part_started_wall = time.time()
                task.part_ended_wall = None
                task.part_number += 1
                task.output_path = str(output)
                task.capture_path, task.recovery_marker = capture, marker
                task.duration_limit_seconds = remaining
                task.is_recording = True
                task.finished_at = None
                task.final_duration = 0
                task.result_status = task.success = None
                task.ended_early = task.stalled = False
                task.error_message = task.finalization_error = ''
                task.media_seconds = task.recorded_bytes = 0
                task.connection_state = 'connecting'
                task.phase = 'connecting'
            self._history_event(task, 'part_started', {'number': task.part_number}, state='connecting')
            self._watch_task_disk(task)
            self._watch_task_progress(task)
            self._start_snapshot_stream(task)
            self._notify_ui()
            return True
        except Exception:
            release_recording(marker)
            logger.exception('Recorder: не удалось запустить следующую часть')
            return False

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

    def set_screen_capture_callbacks(self, confirmation: Callable[[str, bool, bool], bool],
                                     unavailable: Callable[[str], None]):
        """Подключает UI-подтверждение рискованных режимов захвата экрана."""
        self._screen_capture_confirmation = confirmation
        self._screen_capture_error_callback = unavailable

    def find_active_task_id(self, channel_name: str) -> Optional[str]:
        """ID текущей активной (незавершённой) записи для этого имени, если есть."""
        with self._lock:
            for task in self.tasks.values():
                if task.channel_name == channel_name and (task.is_recording or task.recovering):
                    return task.task_id
        return None

    @classmethod
    def build_output_path(cls, channel_name: str, recorded_at: datetime | None = None) -> Path:
        """Builds a portable filename so macOS and Windows handle it identically."""
        return unique_media_path(Config.get_recordings_dir(), channel_name, recorded_at)

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
                       duration_limit_seconds: Optional[float] = None,
                       start_deadline_timestamp: Optional[float] = None,
                       is_live_channel: bool = False,
                       expect_audio: Optional[bool] = None,
                       expected_duration_seconds: Optional[float] = None) -> str:
        with self._lock:
            if self._closing:
                logger.warning(f"Recorder: запуск '{channel_name}' отклонён — приложение закрывается")
                return ""
        Config.init_dirs()
        output_file = Path(output_path)
        if not self._check_output_directory(output_file):
            return ""

        task_id = uuid.uuid4().hex
        source_stream_url = stream_url

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
            headers_dict = self.channel_headers({'name': channel_name, 'url': stream_url})
            ua = headers_dict['User-Agent']
            ref = headers_dict.get('Referer', '')
            headers = ''.join(f"{k}: {v}\r\n" for k, v in headers_dict.items())

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
            if start_deadline_timestamp is not None:
                remaining = start_deadline_timestamp - time.time()
                if remaining <= 0:
                    logger.warning(f"Recorder: окно записи '{channel_name}' закончилось до запуска ffmpeg")
                    return ""
                duration_opts = ['-t', str(min(duration_limit_seconds, remaining)
                                           if duration_limit_seconds else remaining)]
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
                '-movflags', FRAGMENT_FLAGS, '-frag_duration', '1000000', '-flush_packets', '1',
                str(output_file)
            ]
        else:
            # Если это HLS-мастер-плейлист с несколькими битрейтами, пишем
            # вариант ближе к 720p/3-5 Мбит вместо того, что выберет сам
            # ffmpeg (обычно самый тяжёлый) — без перекодирования, просто
            # другой исходный вариант для copy-режима.
            stream_url = resolve_variant_url(stream_url, user_agent=ua, referer=ref,
                                             request_headers=headers_dict)

            if start_deadline_timestamp is not None:
                remaining = start_deadline_timestamp - time.time()
                if remaining <= 0:
                    logger.warning(f"Recorder: окно записи '{channel_name}' закончилось до запуска ffmpeg")
                    return ""
                duration_opts = ['-t', str(min(duration_limit_seconds, remaining)
                                           if duration_limit_seconds else remaining)]

            # У части "instant DVR/catch-up" HLS-источников (обнаружено на
            # "Доверие" — rt-mos-htlive.cdn.ngenix.net) сегменты в плейлисте
            # именованы абсолютным UTC-таймштампом и живут в узком катящемся
            # окне (~60с). #EXT-X-MEDIA-SEQUENCE у них не растёт, из-за чего
            # HLS-демуксер ffmpeg не опознаёт поток как живой и по умолчанию
            # пытается начать с САМОГО СТАРОГО сегмента в списке — тот к
            # моменту реального HTTP-запроса (плюс наши reconnect-задержки)
            # часто уже успевает вылететь из окна: "Error when loading first
            # segment". -live_start_index принудительно стартует НЕ с начала
            # списка, а в −3 сегментах от конца (штатное поведение ffmpeg для
            # уже верно опознанных live-потоков — тут просто форсируем его
            # независимо от эвристики). Только для настоящих ТВ-каналов, не
            # для "Мои ссылки" (там seek_seconds может означать позицию в
            # конечном VOD-ролике, где ЛЮБОЙ сдвиг от начала — уже баг).
            live_opts = (['-live_start_index', '-3']
                         if is_live_channel and not seek_seconds and '.m3u8' in stream_url.lower() else [])

            cmd = [
                'ffmpeg', '-y',
                *RECONNECT_OPTS, *hls_opts(stream_url), *live_opts,
                *seek_opts,
                '-headers', headers,
                '-i', stream_url,
                '-c', 'copy',
                *duration_opts,
                '-err_detect', 'ignore_err',
                '-max_error_rate', '100',
                '-fflags', '+genpts+discardcorrupt',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', FRAGMENT_FLAGS, '-frag_duration', '1000000', '-flush_packets', '1',
                str(output_file)
            ]

        task = RecordingTask(task_id, channel_name, stream_url, str(output_file), source)
        task.on_complete = on_complete
        task.headers = headers_dict
        task.audio_url = audio_url
        task.clip_start_seconds = seek_seconds
        task.clip_end_seconds = clip_end_seconds
        task.duration_limit_seconds = float(duration_opts[1]) if duration_opts else None
        task.is_live = is_live_channel
        task.expected_audio = expect_audio if expect_audio is not None else bool(audio_url or is_live_channel)
        task.content_expected_duration = expected_duration_seconds or duration_limit_seconds
        task.source_stream_url = source_stream_url
        task.deadline = start_deadline_timestamp
        if is_live_channel and duration_limit_seconds and task.deadline is None:
            task.deadline = time.time() + duration_limit_seconds
        if not self._check_output_directory(output_file):
            return ""
        task.capture_path, task.recovery_marker = prepare_recording(output_file, task_id)
        cmd[-1] = str(task.capture_path)
        cmd[2:2] = ['-progress', 'pipe:1', '-nostats']
        task.command = list(cmd)

        try:
            with self._lock:
                if self._closing:
                    release_recording(task.recovery_marker)
                    return ""
                self._sleep_guard.acquire(task.task_id)
                task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                task.is_recording = True
                task.start_time = time.time()
                task.part_started_wall = task.start_time
                task.started_at = datetime.now()
                self.tasks[task_id] = task
            
            logger.info(f"Recorder: Начата запись '{channel_name}' → {output_file} (source: {source})")
            
            self._history_event(task, 'started', state='connecting')
            self._watch_task_disk(task)
            self._watch_task_progress(task)
            wait_thread = threading.Thread(target=self._wait_for_task, args=(task,), daemon=False)
            with self._lock:
                self._wait_threads.add(wait_thread)
            wait_thread.start()
            with self._lock:
                snapshots_requested = self._snapshot_consumers > 0
            if snapshots_requested:
                self._start_snapshot_stream(task)

            if not self._running:
                self._start_timer_loop()
            
            # Уведомляем UI немедленно
            self._notify_ui()
            
            return task_id
            
        except Exception as e:
            release_recording(task.recovery_marker)
            self._sleep_guard.release(task.task_id)
            logger.error(f"Recorder: Ошибка запуска ffmpeg: {e}")
            return ""

    def start_browser_recording(self, channel_name: str, url: str, output_path: str,
                                 source: str = "manual", on_complete: Optional[Callable] = None,
                                 speed_factor: Optional[float] = None,
                                 start_deadline_timestamp: Optional[float] = None) -> str:
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
        with self._lock:
            if self._closing:
                logger.warning(f"Recorder: запуск '{channel_name}' отклонён — приложение закрывается")
                return ""
        Config.init_dirs()
        output_file = Path(output_path)
        if not self._check_output_directory(output_file):
            return ""

        screen_index = find_screen_device_index()
        if screen_index is None:
            logger.error("Recorder: не найдено устройство 'Capture screen' для avfoundation")
            if self._screen_capture_error_callback:
                self._screen_capture_error_callback(channel_name)
            return ""
        audio_index = find_loopback_audio_index()
        if audio_index is None:
            logger.warning("Recorder: устройство BlackHole не найдено — запись экрана будет без звука "
                            "(нужен виртуальный аудио-loopback и Multi-Output Device в Audio MIDI Setup)")

        task_id = uuid.uuid4().hex

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

        needs_full_screen = crop is None
        needs_silent_capture = audio_index is None
        if needs_full_screen or needs_silent_capture:
            if self._screen_capture_confirmation is None:
                logger.error("Recorder: рискованный режим захвата не подтверждён интерфейсом")
                browser_proc.terminate()
                return ""
            try:
                allowed = self._screen_capture_confirmation(
                    channel_name, needs_full_screen, needs_silent_capture)
            except Exception as error:
                logger.error(f"Recorder: не удалось запросить разрешение на захват: {error}")
                allowed = False
            if not allowed:
                logger.info(f"Recorder: пользователь отменил захват экрана '{channel_name}'")
                browser_proc.terminate()
                return ""

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
        if start_deadline_timestamp is not None and time.time() >= start_deadline_timestamp:
            logger.warning(f"Recorder: окно записи '{channel_name}' закончилось до запуска захвата экрана")
            browser_proc.terminate()
            return ""

        task = RecordingTask(task_id, channel_name, url, str(output_file), source)
        task.on_complete = on_complete
        task.is_screen_capture = True
        task.deadline = start_deadline_timestamp
        task.browser_proc = browser_proc
        # Растяжку по времени (см. _timestretch_task_output) включаем
        # ТОЛЬКО если ускорение подтверждено — иначе запись, реально
        # шедшая в нормальном темпе, была бы ошибочно замедлена в
        # speed_factor раз при "восстановлении".
        task.speed_factor = speed_factor if speed_confirmed else None
        task.screen_capture_has_audio = audio_index is not None
        task.expected_audio = task.screen_capture_has_audio
        if not self._check_output_directory(output_file):
            browser_proc.terminate()
            return ""
        task.capture_path, task.recovery_marker = prepare_recording(
            output_file, task_id, speed=task.speed_factor,
            has_audio=task.screen_capture_has_audio)
        cmd = build_screen_capture_cmd(str(task.capture_path), screen_index, audio_index,
                                       crop=crop, framerate=capture_framerate)
        flag_index = cmd.index('-movflags')
        cmd[flag_index + 1] = FRAGMENT_FLAGS
        cmd[-1:-1] = ['-frag_duration', '1000000', '-flush_packets', '1']
        cmd[2:2] = ['-progress', 'pipe:1', '-nostats']

        try:
            with self._lock:
                if self._closing:
                    release_recording(task.recovery_marker)
                    browser_proc.terminate()
                    return ""
                self._sleep_guard.acquire(task.task_id)
                self._screen_guard.acquire(task.task_id)
                task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                task.is_recording = True
                task.start_time = time.time()
                task.part_started_wall = task.start_time
                task.started_at = datetime.now()
                self.tasks[task_id] = task

            logger.info(f"Recorder: Начата запись экрана '{channel_name}' → {output_file} (source: {source})")

            self._history_event(task, 'started', state='connecting')
            self._watch_task_disk(task)
            self._watch_task_progress(task)
            wait_thread = threading.Thread(target=self._wait_for_task, args=(task,), daemon=False)
            with self._lock:
                self._wait_threads.add(wait_thread)
            wait_thread.start()
            threading.Thread(target=self._watch_browser_proc, args=(task,), daemon=True).start()

            if not self._running:
                self._start_timer_loop()

            self._notify_ui()
            return task_id

        except Exception as e:
            release_recording(task.recovery_marker)
            self._sleep_guard.release(task.task_id)
            self._screen_guard.release(task.task_id)
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
    def _leave_paused_state(task: 'RecordingTask', resume_process: bool = True):
        """Снимает паузу и учитывает её длительность перед остановкой/выходом."""
        if not task.is_paused:
            return
        if resume_process and not task.is_live and task.process and task.process.poll() is None:
            # Остановленный SIGSTOP процесс сначала нужно продолжить: иначе
            # он может не обработать SIGINT/SIGTERM и не дописать контейнер.
            task.process.send_signal(signal.SIGCONT)
        task.total_paused_duration += max(0.0, time.time() - task.pause_time)
        task.is_paused = False

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
        Recorder._leave_paused_state(task)
        if task.is_screen_capture:
            task.process.send_signal(signal.SIGINT)
        else:
            task.process.terminate()

    def stop_recording(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)

        if task and (task.is_recording or task.recovering):
            task.phase = 'stopping'
            task.stop_requested = True
            task.stop_timestamp = task.stop_timestamp or time.time()
            task.retry_cancel.set()
            self._history_event(task, 'stop_requested')
        if task and task.process and task.is_recording and task.process.poll() is None:
            logger.info(f"Recorder: Остановка записи '{task.channel_name}' (task: {task_id})")
            task.final_duration = task.get_elapsed_time()
            task.stop_requested = True
            self._terminate_task_process(task)
            task.is_recording = False
            self._stop_snapshot_stream(task)
            if task.browser_proc and task.browser_proc.poll() is None:
                task.browser_proc.terminate()
            self._notify_ui()

    def schedule_stop(self, task_id: str, delay_seconds: float):
        """Регистрирует отменяемый таймер остановки записи."""
        def stop_from_timer():
            with self._lock:
                self._stop_timers.pop(task_id, None)
                if self._closing:
                    return
            self.stop_recording(task_id)

        timer = threading.Timer(max(0.0, delay_seconds), stop_from_timer)
        timer.daemon = True
        with self._lock:
            if self._closing:
                return
            previous = self._stop_timers.pop(task_id, None)
            self._stop_timers[task_id] = timer
        if previous:
            previous.cancel()
        timer.start()
    
    def pause_recording(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task or task.stop_requested or self._closing:
                return
            if task.is_live:
                if task.is_paused:
                    self._leave_paused_state(task, resume_process=False)
                    self._history_event(task, 'resumed', state='connecting')
                    logger.info("Recorder: продолжение эфира '%s' в новой части", task.channel_name)
                elif task.is_recording and task.process and task.process.poll() is None:
                    task.final_duration = task.get_elapsed_time()
                    task.pause_time = time.time()
                    task.is_paused = True
                    if not task.pause_pending:
                        task.pause_count += 1
                    task.pause_pending = True
                    task.connection_state = 'paused'
                    task.phase = 'paused'
                    self._history_event(task, 'paused', state='paused')
                    process = task.process
                    # A live source must not remain frozen with open network reads.
                    process.terminate()
                    self._stop_snapshot_stream(task)
                    def ensure_stopped():
                        try:
                            process.wait(timeout=progress_policy.STOP_GRACE)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    threading.Thread(target=ensure_stopped, daemon=True, name='LivePauseStop').start()
                    logger.info("Recorder: пауза '%s' — текущая часть завершается, эфир пропускается",
                                task.channel_name)
                else:
                    return
            elif task.process and task.is_recording and task.process.poll() is None:
                if not task.is_paused:
                    task.process.send_signal(signal.SIGSTOP)
                    task.is_paused = True
                    task.pause_time = time.time()
                    self._stop_snapshot_stream(task)
                else:
                    self._leave_paused_state(task)
                    self._start_snapshot_stream(task)
            else:
                return
        self._notify_ui()

    def _wait_live_pause(self, task):
        task.finished_at = None
        task.connection_state = 'paused'
        task.phase = 'paused'
        self._notify_ui()
        while (task.is_paused and not task.stop_requested and not self._closing
               and not task.storage_error and not task.finalization_error):
            if task.deadline is not None and time.time() >= task.deadline:
                task.stop_requested = True
                task.stop_timestamp = task.stop_timestamp or time.time()
                break
            task.retry_cancel.wait(.2)
        return (not task.stop_requested and not self._closing and not task.storage_error
                and not task.finalization_error
                and (task.deadline is None or time.time() < task.deadline))

    def remove_task(self, task_id: str):
        with self._lock:
            if task_id in self.tasks:
                del self.tasks[task_id]
                self._notify_ui()
    
    def get_all_tasks(self) -> list:
        with self._lock:
            return list(self.tasks.values())
    
    def _wait_for_task(self, task: RecordingTask):
        task.recovering = task.is_live
        try:
            while True:
                self._finish_task(task, notify=False)
                task.parts.append({'path': task.output_path, 'status': task.result_status,
                                   'stalled': task.stalled, 'error': task.error_message,
                                   'paused': task.pause_pending,
                                   'started_at': datetime.fromtimestamp(task.part_started_wall).isoformat() if task.part_started_wall else None,
                                   'finished_at': datetime.fromtimestamp(task.part_ended_wall).isoformat() if task.part_ended_wall else None,
                                   'duration': task.part_assessment.actual_duration if task.part_assessment else None,
                                   'container_duration': task.part_probe.duration if task.part_probe else None,
                                   'expected_duration': task.part_assessment.expected_duration if task.part_assessment else None,
                                   'missing_seconds': task.part_assessment.missing_seconds if task.part_assessment else None,
                                   'has_video': task.part_probe.has_video if task.part_probe else False,
                                   'has_audio': task.part_probe.has_audio if task.part_probe else False})
                self._history_event(task, 'part_finished', {'number': task.part_number},
                                    state='paused' if task.pause_pending else 'finalizing')
                if task.disk_guard:
                    task.disk_guard.close()
                if task.is_live and task.pause_pending:
                    if not self._wait_live_pause(task):
                        break
                    task.pause_pending = False
                    if not self._restart_part(task, after_pause=True):
                        break
                    continue
                if not self._retry_allowed(task):
                    break
                if task.retry_count >= len(progress_policy.RETRY_DELAYS):
                    task.recovery_exhausted = True
                    break
                delay = progress_policy.RETRY_DELAYS[task.retry_count]
                task.retry_count += 1
                task.connection_state = 'retrying'
                task.phase = 'retrying'
                self._history_event(task, 'retry_scheduled', {'attempt': task.retry_count, 'delay': delay}, state='retrying')
                task.result_status = task.success = None
                self._notify_ui()
                if task.deadline is not None:
                    delay = min(delay, max(0, task.deadline-time.time()))
                if task.retry_cancel.wait(delay) or task.stop_requested or self._closing:
                    break
                if not self._restart_part(task):
                    break
            if task.retry_count or task.pause_count:
                # A later good part cannot hide a gap or loss in an earlier part.
                readable = [part for part in task.parts if part['status'] in (COMPLETED, PARTIAL)]
                if readable:
                    task.output_path = readable[-1]['path']
                    task.result_status = PARTIAL
                else:
                    task.result_status = 'processing_error'
                task.success = False
                task.error_message = (('Запись сохранена с паузами; эфир во время паузы пропущен. '
                                       if task.pause_count and not task.retry_count else
                                       'Запись сохранена отдельными частями после обрыва. ')
                                      + ('Лимит восстановления исчерпан. ' if task.recovery_exhausted else '')
                                      + (task.storage_error or task.parts[-1]['error'] or ''))
            task.finished_at = datetime.now()
            self._update_session_assessment(task)
            self._auto_join_parts(task)
            task.phase = task.result_status
            self._refresh_task_metrics(task)
            self._history_event(task, 'completed', state=task.result_status)
            self._notify_completion(task)
        finally:
            self._sleep_guard.release(task.task_id)
            self._screen_guard.release(task.task_id)
            self._leave_paused_state(task, resume_process=False)
            task.finished_at = task.finished_at or datetime.now()
            task.recovering = False
            task.is_recording = False
            if self.history is not None:
                self.history.release(task.task_id)
            if task.progress_monitor:
                task.progress_monitor.close()
            if task.disk_guard:
                task.disk_guard.close()
            with self._lock:
                timer = self._stop_timers.pop(task.task_id, None)
                self._wait_threads.discard(threading.current_thread())
            if timer:
                timer.cancel()
            self._notify_ui()

    def _auto_join_parts(self, task):
        if not self.auto_join or len(task.parts) < 2 or task.merged_path:
            return
        readable = [part for part in task.parts if part['status'] in (COMPLETED, PARTIAL)]
        if len(readable) < 2:
            return
        from core.recording_merge import merge_mp4_parts
        base = Path(task.base_output_path)
        output = base.with_name(base.stem+'_joined.mp4')
        task.merging = True
        task.phase = 'joining'
        self._history_event(task, 'parts_merge_started', state='finalizing')
        self._notify_ui()
        try:
            result = merge_mp4_parts([part['path'] for part in readable], output)
            task.merged_path = task.output_path = str(result)
            self._history_event(task, 'parts_merged', {'path': str(result)}, state='finalizing')
        except Exception as error:
            from core.recording_history import safe_error
            logger.exception('Автоматическое объединение не удалось; части сохранены')
            message = 'Автоматическое объединение не удалось; исходные части сохранены: '+safe_error(error)
            task.error_message = (task.error_message+'; '+message).strip('; ')
            self._history_event(task, 'parts_merge_failed', {'error': safe_error(error)}, state='finalizing')
        finally:
            task.merging = False

    def _notify_completion(self, task):
        if task.on_complete:
            try:
                task.on_complete(task.success, task.channel_name, task.output_path,
                                 task.ended_early, task.result_status)
            except Exception:
                logger.exception('Recorder: ошибка callback завершения')

    def _finish_task(self, task: RecordingTask, notify=True):
        if not task.process:
            return

        # communicate() сохранял весь stderr многoчасовой записи в памяти.
        # Читаем поток непрерывно, но держим только последние строки для
        # диагностики ошибки. stdout ffmpeg для записи не используется.
        stderr_tail = deque(maxlen=200)

        def drain_stderr():
            try:
                for line in task.process.stderr:
                    stderr_tail.append(line)
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        task.process.wait()
        task.part_ended_wall = time.time()
        if task.progress_monitor:
            task.progress_monitor.close()
        if task.disk_guard:
            task.disk_guard.stop_monitor()
        stderr_thread.join(timeout=1)
        for pipe in (task.process.stdout, task.process.stderr):
            close = getattr(pipe, 'close', None)
            if callable(close):
                close()
        returncode = task.process.returncode
        if not (task.is_live and task.pause_pending):
            self._leave_paused_state(task, resume_process=False)
        
        if task.final_duration == 0:
            task.final_duration = task.get_elapsed_time()
        task.is_recording = False
        task.finished_at = datetime.now()
        self._stop_snapshot_stream(task)
        if task.browser_proc and task.browser_proc.poll() is None:
            task.browser_proc.terminate()

        task.phase = 'finalizing'
        self._notify_ui()
        # При штатной остановке кнопкой/таймером код зависит от платформы и
        # сигнала; окончательное качество всё равно определит ffprobe ниже.
        process_ok = (returncode in (0, 255) or task.stop_requested) and not task.storage_error and not task.stalled
        output_file = Path(task.output_path)
        with self._lock:
            closing = self._closing
        if task.recovery_marker:
            release_recording(task.recovery_marker)
            try:
                output_file = finalize_recording(task.recovery_marker)
            except Exception as error:
                task.finalization_error = str(error)
                if isinstance(error, DiskSpaceError):
                    task.storage_error = str(error)
                task.output_path = str(task.capture_path)
                output_file = task.capture_path
                process_ok = False
                logger.exception("Recorder: исходная запись сохранена для восстановления")
        elif process_ok and task.speed_factor:
            process_ok = self._timestretch_task_output(task)
        expect_audio = (task.expected_audio if task.expected_audio is not None else
                        bool(task.audio_url) or (task.is_screen_capture and task.screen_capture_has_audio))
        task.phase = 'checking'
        self._notify_ui()
        result_status, probe = classify_media(output_file, process_ok, expect_audio=expect_audio)
        expected = None
        if (task.is_live or task.is_screen_capture) and task.part_started_wall:
            end = min(task.part_ended_wall, task.pause_time) if task.pause_pending else task.part_ended_wall
            if task.deadline is not None:
                end = min(end, task.deadline)
            expected = max(0, end-task.part_started_wall) * (task.speed_factor or 1)
        elif not task.stop_requested:
            expected = task.content_expected_duration or task.duration_limit_seconds
        assessment = assess_probe(probe, process_ok, expect_audio=expect_audio, expected_duration=expected)
        result_status = assessment.status
        task.part_probe, task.part_assessment = probe, assessment
        task.expected_seconds, task.actual_seconds = expected, assessment.actual_duration
        task.missing_seconds = assessment.missing_seconds
        success = result_status == COMPLETED
        task.result_status = result_status
        task.success = success
        # Если ffmpeg сам дошёл до конца потока (и мы его об этом не просили) —
        # значит источник закончился раньше, чем длилось окно записи: эфир
        # прервался или закончился сам записываемый файл/ролик.
        # -t завершает FFmpeg самостоятельно. Сверяем длительность файла,
        # чтобы отличить штатный конец фрагмента от преждевременного EOF.
        target_duration = task.duration_limit_seconds if task.is_live else expected
        reached_limit = (target_duration is not None and probe.duration is not None
                         and probe.duration >= target_duration - 0.25)
        task.ended_early = (probe.readable
                            and not task.stop_requested and not reached_limit and not task.pause_pending)
        if success:
            if task.ended_early:
                logger.warning(f"Recorder: '{task.channel_name}' — источник закончился раньше окна записи")
            else:
                logger.info(f"Recorder: Запись '{task.channel_name}' завершена успешно")
        elif result_status == PARTIAL:
            reasons = list(assessment.reasons)
            if task.stalled:
                reasons.append('Запись остановлена: поток перестал продвигаться')
            if task.storage_error:
                reasons.append(task.storage_error)
            if task.finalization_error:
                reasons.append('Сохранён временный MP4: ' + task.finalization_error)
            elif task.speed_factor and closing:
                reasons.append('обратное растяжение ускоренной записи отменено при закрытии приложения')
            elif not process_ok:
                reasons.append(f'ffmpeg завершился с кодом {returncode}')
            task.error_message = '; '.join(reasons) or 'файл сохранён не полностью'
            logger.warning(f"Recorder: Частичная запись '{task.channel_name}': {task.error_message}")
        else:
            stderr = b''.join(stderr_tail)
            err_msg = stderr.decode('utf-8', errors='ignore')[-500:] if stderr else ""
            err_msg = f"{'; '.join(assessment.reasons)}. {err_msg}".strip()
            task.error_message = (task.storage_error + ". " if task.storage_error else "") + err_msg
            logger.error(f"Recorder: Ошибка обработки '{task.channel_name}' (code {returncode}): {err_msg}")

        if notify:
            task.phase = task.result_status
            self._refresh_task_metrics(task)
            self._notify_completion(task)
            self._notify_ui()

    def merge_recording_parts(self, task_id):
        from core.recording_merge import merge_mp4_parts
        with self._lock:
            task = self.tasks[task_id]
            if task.is_recording or task.recovering or task.merging or self._closing:
                raise ValueError('Дождитесь завершения записи перед объединением')
            if task.merged_path:
                return Path(task.merged_path)
            if len(task.parts) < 2 or any(part['status'] not in (COMPLETED, PARTIAL) for part in task.parts):
                raise ValueError('Не все части пригодны для объединения')
            paths = [part['path'] for part in task.parts]
            base = Path(task.base_output_path)
            output = base.with_name(base.stem+'_joined.mp4')
            task.merging = True
            task.merge_error = ''
            self._sleep_guard.acquire('merge:'+task.task_id)
            self._wait_threads.add(threading.current_thread())
        self._notify_ui()
        try:
            result = merge_mp4_parts(paths, output)
            task.merged_path = task.output_path = str(result)
            self._history_event(task, 'parts_merged', {'path': str(result)}, state=task.result_status)
            return result
        finally:
            self._sleep_guard.release('merge:'+task.task_id)
            with self._lock:
                task.merging = False
                self._refresh_task_metrics(task)
                self._wait_threads.discard(threading.current_thread())
            if self.history is not None:
                self.history.release(task.task_id)
            self._notify_ui()

    def remove_completed_tasks(self) -> int:
        """Убирает завершённые строки из интерфейса, не удаляя медиафайлы."""
        with self._lock:
            completed_ids = [task_id for task_id, task in self.tasks.items()
                             if not task.is_recording and not task.recovering and not task.merging and task.result_status is not None]
            for task_id in completed_ids:
                del self.tasks[task_id]
        if completed_ids:
            self._notify_ui()
        return len(completed_ids)

    def _timestretch_task_output(self, task: RecordingTask) -> bool:
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
                return True
            else:
                temp_file.unlink(missing_ok=True)
                err = result.stderr.decode('utf-8', errors='ignore')[-300:] if result.stderr else ''
                logger.error(f"Recorder: не удалось растянуть по времени '{task.channel_name}' — "
                             f"файл остался ускоренным ({task.speed_factor:.0f}x): {err}")
                return False
        except Exception as e:
            temp_file.unlink(missing_ok=True)
            logger.error(f"Recorder: ошибка растяжки по времени '{task.channel_name}' — "
                         f"файл остался ускоренным ({task.speed_factor:.0f}x): {e}")
            return False

    def get_snapshot_profile(self):
        with self._lock:
            count = sum(task.is_recording and not task.is_paused and not task.is_screen_capture
                        for task in self.tasks.values())
            return monitor_profile(self.snapshot_mode, count)

    def set_snapshot_mode(self, mode, *, persist=True):
        if mode not in MODES:
            raise ValueError('Неизвестный режим монитора')
        if persist:
            Config._write_settings({'snapshot_mode': mode})
        with self._lock:
            self.snapshot_mode = mode
            self._sync_snapshot_streams()
        self._notify_ui()

    def _start_snapshot_stream(self, task):
        self._sync_snapshot_streams()

    def _sync_snapshot_streams(self):
        with self._lock:
            profile = self.get_snapshot_profile() if self._snapshot_consumers and not self._closing else None
            for task in self.tasks.values():
                eligible = (profile is not None and task.is_recording and not task.is_paused
                            and not task.is_screen_capture)
                if not eligible:
                    if task.snapshot_stream is not None:
                        self._stop_snapshot_stream(task)
                    continue
                if task.snapshot_stream is not None and task.snapshot_profile == profile:
                    continue
                self._stop_snapshot_stream(task)
                task.snapshot_profile = profile
                generation = task.snapshot_generation
                def on_frame(jpeg_bytes, current=task, expected=generation):
                    with self._lock:
                        if current.snapshot_generation != expected or current.snapshot_stream is None:
                            return
                        current.last_snapshot = jpeg_bytes
                        current.snapshot_seq += 1
                task.snapshot_stream = LiveThumbnailStream(
                    task.stream_url, task.headers, fps=profile.fps, width=profile.width,
                    keyframes_only=profile.keyframes_only, on_frame=on_frame)
                task.snapshot_stream.start()

    def acquire_snapshot_consumer(self):
        with self._lock:
            self._snapshot_consumers += 1
            self._sync_snapshot_streams()

    def release_snapshot_consumer(self):
        with self._lock:
            self._snapshot_consumers = max(0, self._snapshot_consumers-1)
            self._sync_snapshot_streams()

    def _stop_snapshot_stream(self, task):
        with self._lock:
            stream = task.snapshot_stream
            task.snapshot_stream = None
            task.snapshot_profile = None
            task.snapshot_generation += 1
            task.last_snapshot = None
        if stream is not None:
            stream.stop()

    def _start_timer_loop(self):
        self._running = True
        
        def loop():
            while self._running:
                self._sleep_guard.refresh()
                self._screen_guard.refresh()
                self._sync_snapshot_streams()
                with self._lock:
                    metric_tasks = list(self.tasks.values())
                for task in metric_tasks:
                    if (task.is_recording or task.recovering or task.merging
                            or task.phase in ('stopping', 'finalizing', 'checking')):
                        self._refresh_task_metrics(task)
                with self._lock:
                    has_active = any(t.is_recording or t.recovering or t.merging
                                     or t.phase in ('stopping', 'finalizing', 'checking') for t in self.tasks.values())
                
                if has_active:
                    self._notify_ui()
                
                time.sleep(1)
        
        self._timer_thread = threading.Thread(target=loop, daemon=True)
        self._timer_thread.start()
    
    def stop_all(self, timeout: float = 20.0):
        """Запрещает новые задачи и дожидается финализации активных файлов."""
        self._running = False
        with self._lock:
            self._closing = True
            self._sleep_guard.close()
            self._screen_guard.close()
            timers = list(self._stop_timers.values())
            self._stop_timers.clear()
            tasks = list(self.tasks.values())
        for timer in timers:
            timer.cancel()

        for task in tasks:
            task.stop_requested = True
            task.stop_timestamp = task.stop_timestamp or time.time()
            task.retry_cancel.set()
            if task.is_recording or task.recovering:
                self._history_event(task, 'shutdown')
            if task.process and task.is_recording and task.process.poll() is None:
                task.final_duration = task.get_elapsed_time()
                task.stop_requested = True
                self._terminate_task_process(task)
                task.is_recording = False
                self._stop_snapshot_stream(task)
            if task.browser_proc and task.browser_proc.poll() is None:
                task.browser_proc.terminate()

        deadline = time.monotonic() + timeout
        for task in tasks:
            for process in (task.process, task.browser_proc):
                if process and process.poll() is None:
                    try:
                        process.wait(timeout=max(0.1, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        process.kill()

        with self._lock:
            wait_threads = list(self._wait_threads)
        for thread in wait_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._notify_ui()
