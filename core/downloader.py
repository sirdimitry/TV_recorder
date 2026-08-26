# core/downloader.py
"""Раздел "Загрузки": разовое скачивание произвольной ссылки в один
готовый медиафайл. Использует тот же движок поиска потока, что и "Мои
ссылки" (core/link_resolver.py: yt-dlp -> HTML-скрейп -> sniff через
встроенный браузер), и тот же принцип записи — ffmpeg с '-c copy',
без перекодирования (core/recorder.py делает то же самое для эфиров).

Отличие от Recorder — модель здесь одноразовая, а не "включить/выключить":
задача сама доходит до конца потока и завершается, без пауз, без
живых миниатюр, без браузерного screen-capture режима (тот нужен только
записи "живого сейчас", скачивать так уже готовый файл незачем)."""
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from core.link_resolver import resolve_link
from core.stream_resolver import RECONNECT_OPTS, hls_opts, resolve_variant_url
from utils.filenames import safe_filename
from utils.logger import logger


class DownloadTask:
    def __init__(self, task_id: str, url: str, target_height: int, output_dir: Path):
        self.task_id = task_id
        self.url = url
        self.target_height = target_height
        self.output_dir = output_dir
        self.name = url
        self.thumbnail = ''
        self.duration: Optional[float] = None
        self.output_path = ''
        self.process: Optional[subprocess.Popen] = None
        # resolving -> downloading -> done | error | canceled
        self.status = 'resolving'
        self.error_message = ''
        self.progress: Optional[float] = None  # 0-100, None пока неизвестно (не тот же duration или ещё не начали)
        self.elapsed_seconds: float = 0.0
        self.speed_bps: Optional[float] = None  # байт/сек, сглаженная скорость записи файла
        self.downloaded_bytes: Optional[int] = None  # сколько уже записано — единственный индикатор, когда duration неизвестен
        self.eta_seconds: Optional[float] = None  # оценка по реальному времени, None пока не из чего считать
        self.created_at = datetime.now()
        self.finished_at: Optional[datetime] = None
        self.on_complete: Optional[Callable] = None

    def to_dict(self) -> Dict:
        """Снимок задачи как есть — используется и для живого обновления
        строки в интерфейсе (там 'downloading' должен доходить как есть,
        не подменяться), и для сохранения в core/storage.py на случай
        перезапуска приложения. За превращение зависшего на 'downloading'
        значения в честную ошибку (задача была прервана закрытием
        приложения, реального процесса за ней уже нет) отвечает
        DownloadList.load_downloads() — она видит эти данные только один
        раз, при самой первой загрузке с диска."""
        return {
            'id': self.task_id,
            'url': self.url,
            'name': self.name,
            'thumbnail': self.thumbnail,
            'duration': self.duration,
            'target_height': self.target_height,
            'output_path': self.output_path,
            'status': self.status,
            'error_message': self.error_message,
            'progress': self.progress,
            'speed_bps': self.speed_bps,
            'downloaded_bytes': self.downloaded_bytes,
            'eta_seconds': self.eta_seconds,
        }


def build_download_path(title: str, output_dir: Path) -> Path:
    """Аналог Recorder.build_output_path, но пишет в указанную (пользователем
    выбранную) папку загрузок, а не в папку записей."""
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    safe_name = safe_filename(title)
    return output_dir / f"{safe_name}_{timestamp}.mp4"


class Downloader:
    def __init__(self):
        self.tasks: Dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._ui_callbacks: list = []

    def set_ui_callback(self, callback: Callable):
        self._ui_callbacks.append(callback)

    def remove_ui_callback(self, callback: Callable):
        if callback in self._ui_callbacks:
            self._ui_callbacks.remove(callback)

    def _notify_ui(self):
        for callback in self._ui_callbacks:
            try:
                callback()
            except Exception as e:
                logger.error(f"Downloader: ошибка UI callback: {e}")

    def get_all_downloads(self) -> list:
        with self._lock:
            return list(self.tasks.values())

    def start_download(self, url: str, target_height: int, output_dir: Path,
                        on_complete: Optional[Callable] = None) -> str:
        task_id = uuid.uuid4().hex[:12]
        task = DownloadTask(task_id, url, target_height, Path(output_dir))
        task.on_complete = on_complete

        with self._lock:
            self.tasks[task_id] = task
        self._notify_ui()
        logger.info(f"Downloader: добавлена загрузка '{url}' (id: {task_id}, до {target_height}p)")

        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task_id

    def cancel_download(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
        if not task:
            return
        if task.process and task.process.poll() is None:
            task.process.terminate()
        task.status = 'canceled'
        # Оборванный на середине файл (moov в конце из-за +faststart)
        # всё равно не воспроизведётся — не оставляем мусор в папке загрузок.
        if task.output_path:
            try:
                Path(task.output_path).unlink(missing_ok=True)
            except OSError:
                pass
        self._notify_ui()

    def remove_task(self, task_id: str):
        with self._lock:
            self.tasks.pop(task_id, None)
        self._notify_ui()

    def _run(self, task: DownloadTask):
        info = resolve_link(task.url, target_height=task.target_height)
        if task.status == 'canceled':
            # Отменили, пока резолвили ссылку (ffmpeg ещё не запускался,
            # cancel_download() тут ничего не остановил) — не затираем
            # отмену тем, что резолв в итоге всё-таки успел завершиться.
            return
        if not info.ok:
            task.status = 'error'
            task.error_message = info.error or 'Не удалось найти поток'
            logger.warning(f"Downloader: не удалось разобрать '{task.url}' (id: {task.task_id}): {task.error_message}")
            self._notify_ui()
            if task.on_complete:
                task.on_complete(False, task, task.error_message)
            return

        task.name = info.title or task.url
        task.thumbnail = info.thumbnail or ''
        task.duration = info.duration
        task.status = 'downloading'
        self._notify_ui()

        output_path = build_download_path(task.name, task.output_dir)
        task.output_dir.mkdir(parents=True, exist_ok=True)
        task.output_path = str(output_path)

        headers_dict = info.headers or {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        ua = headers_dict.get('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')
        ref = headers_dict.get('Referer', '')
        headers = ''.join(f"{k}: {v}\r\n" for k, v in headers_dict.items())

        # -progress pipe:1 -nostats: структурированные key=value строки в
        # stdout вместо текстовых "frame=... time=..." в stderr — оттуда и
        # берём прогресс (см. _watch_progress). Настоящие ошибки ffmpeg
        # по-прежнему пишет в stderr, -nostats их не трогает.
        progress_opts = ['-progress', 'pipe:1', '-nostats']

        video_url = info.video_url
        if info.audio_url:
            cmd = [
                'ffmpeg', '-y', *progress_opts,
                *RECONNECT_OPTS, *hls_opts(video_url), '-headers', headers, '-i', video_url,
                *RECONNECT_OPTS, *hls_opts(info.audio_url), '-headers', headers, '-i', info.audio_url,
                '-map', '0:v:0', '-map', '1:a:0',
                '-c', 'copy',
                '-err_detect', 'ignore_err',
                '-fflags', '+genpts+discardcorrupt',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                str(output_path)
            ]
        else:
            video_url = resolve_variant_url(video_url, user_agent=ua, referer=ref,
                                             target_height=task.target_height)
            cmd = [
                'ffmpeg', '-y', *progress_opts,
                *RECONNECT_OPTS, *hls_opts(video_url),
                '-headers', headers,
                '-i', video_url,
                '-c', 'copy',
                '-err_detect', 'ignore_err',
                '-fflags', '+genpts+discardcorrupt',
                '-avoid_negative_ts', 'make_zero',
                '-movflags', '+faststart',
                str(output_path)
            ]

        try:
            task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, bufsize=1)
        except Exception as e:
            task.status = 'error'
            task.error_message = f'Не удалось запустить ffmpeg: {e}'
            logger.error(f"Downloader: {task.error_message}")
            self._notify_ui()
            if task.on_complete:
                task.on_complete(False, task, task.error_message)
            return

        logger.info(f"Downloader: начато скачивание '{task.name}' → {output_path}")

        # stdout (прогресс) и stderr (диагностика на случай ошибки) нужно
        # читать одновременно отдельными потоками — иначе при заполнении
        # непрочитанного буфера одного из них ffmpeg зависнет намертво.
        stderr_lines = []

        def drain_stderr():
            try:
                for line in task.process.stderr:
                    stderr_lines.append(line)
                    if len(stderr_lines) > 300:
                        stderr_lines.pop(0)
            except Exception:
                pass

        threading.Thread(target=drain_stderr, daemon=True).start()
        self._watch_progress(task)

        task.process.wait()
        returncode = task.process.returncode

        if task.status == 'canceled':
            # cancel_download уже почистил файл и выставил статус — ничего
            # поверх этого дописывать не нужно (иначе затрём статус 'error').
            return

        has_usable_file = output_path.is_file() and output_path.stat().st_size > 1024
        success = (returncode == 0 or returncode == 255) and has_usable_file
        task.finished_at = datetime.now()

        if success:
            task.status = 'done'
            task.progress = 100.0
            task.speed_bps = None
            task.eta_seconds = None
            logger.info(f"Downloader: '{task.name}' скачан → {output_path}")
        else:
            task.status = 'error'
            err_msg = ''.join(stderr_lines)[-500:]
            if not has_usable_file:
                err_msg = "Не удалось создать файл. " + err_msg
            task.error_message = err_msg
            logger.error(f"Downloader: ошибка скачивания '{task.name}' (code {returncode}): {err_msg}")

        self._notify_ui()
        if task.on_complete:
            task.on_complete(success, task, task.error_message)

    def _watch_progress(self, task: DownloadTask):
        """Читает key=value строки из -progress pipe:1: out_time_us даёт
        проценты от известной длительности ролика, total_size — сколько
        байт уже записано в файл (по нему считаем реальную скорость и,
        вместе с процентом, оценку оставшегося времени — не по позиции
        в видео, а по настоящему часам, иначе на -c copy, который обычно
        в разы быстрее реального времени ролика, ETA была бы мимо).
        Уведомления троттлятся — иначе на быстром скачивании (на диске уже
        готовый файл, ffmpeg просто копирует байты) UI-callback звался бы
        на каждую строку, десятки раз в секунду."""
        last_notify = 0.0
        download_started = time.time()
        last_size = 0
        last_size_time = download_started
        try:
            for line in task.process.stdout:
                line = line.strip()
                if line.startswith('out_time_us='):
                    try:
                        task.elapsed_seconds = int(line.split('=', 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    if task.duration:
                        task.progress = max(0.0, min(100.0, task.elapsed_seconds / task.duration * 100))
                        if task.progress > 0.5:
                            wall_elapsed = time.time() - download_started
                            estimated_total = wall_elapsed * 100 / task.progress
                            task.eta_seconds = max(0.0, estimated_total - wall_elapsed)
                elif line.startswith('total_size='):
                    try:
                        total_size = int(line.split('=', 1)[1])
                    except ValueError:
                        continue
                    now = time.time()
                    dt = now - last_size_time
                    if dt > 0 and total_size >= last_size:
                        instant_speed = (total_size - last_size) / dt
                        # Сглаживаем (EMA) — сырые значения между двумя
                        # соседними -progress тиками слишком дёрганые для
                        # человекочитаемого "сейчас качается со скоростью N".
                        task.speed_bps = (instant_speed if task.speed_bps is None
                                           else 0.6 * task.speed_bps + 0.4 * instant_speed)
                    last_size = total_size
                    last_size_time = now
                    task.downloaded_bytes = total_size
                elif line.startswith('progress='):
                    now = time.time()
                    if now - last_notify >= 0.5 or line.endswith('end'):
                        last_notify = now
                        self._notify_ui()
        except Exception:
            pass
