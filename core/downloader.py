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
        self.created_at = datetime.now()
        self.finished_at: Optional[datetime] = None
        self.on_complete: Optional[Callable] = None

    def to_dict(self) -> Dict:
        """Для core/storage.py — то, что переживает перезапуск приложения."""
        return {
            'id': self.task_id,
            'url': self.url,
            'name': self.name,
            'thumbnail': self.thumbnail,
            'duration': self.duration,
            'target_height': self.target_height,
            'output_path': self.output_path,
            'status': self.status if self.status != 'downloading' else 'error',
            'error_message': self.error_message or ('Прервано закрытием приложения' if self.status == 'downloading' else ''),
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

        video_url = info.video_url
        if info.audio_url:
            cmd = [
                'ffmpeg', '-y',
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
                'ffmpeg', '-y',
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
            task.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            task.status = 'error'
            task.error_message = f'Не удалось запустить ffmpeg: {e}'
            logger.error(f"Downloader: {task.error_message}")
            self._notify_ui()
            if task.on_complete:
                task.on_complete(False, task, task.error_message)
            return

        logger.info(f"Downloader: начато скачивание '{task.name}' → {output_path}")
        _, stderr = task.process.communicate()
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
            logger.info(f"Downloader: '{task.name}' скачан → {output_path}")
        else:
            task.status = 'error'
            err_msg = stderr.decode('utf-8', errors='ignore')[-500:] if stderr else ''
            if not has_usable_file:
                err_msg = "Не удалось создать файл. " + err_msg
            task.error_message = err_msg
            logger.error(f"Downloader: ошибка скачивания '{task.name}' (code {returncode}): {err_msg}")

        self._notify_ui()
        if task.on_complete:
            task.on_complete(success, task, task.error_message)
