"""YouTube task lifecycle. Deliberately separate from live recording recovery."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time

from core.media_probe import assess_probe, probe_media, COMPLETED
from core.download_progress import configure_streams, update_progress

ACTIVE = ('resolving', 'downloading', 'retrying', 'pausing', 'processing')
RETRY_DELAYS = (2, 5, 10)


def command(task):
    args = [task.url, task.work_dir, str(task.target_height)]
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--youtube-transfer-worker', *args]
    return [sys.executable, str(Path(__file__).with_name('youtube_transfer.py')), *args]


def stop_process(task):
    process = task.process
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGINT)
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def run(owner, task):
    from core.downloader import build_download_path
    try:
        for attempt in range(len(RETRY_DELAYS)+1):
            if task.pause_requested.is_set() or owner._closing or task.status == 'canceled':
                break
            if not owner._check_task_directory(task):
                task.status = 'paused'
                break
            task.status = 'resolving'
            task.storage_error = ''
            owner._notify_ui()
            with owner._lock:
                if task.pause_requested.is_set() or owner._closing or task.status == 'canceled':
                    break
                task.process = subprocess.Popen(command(task), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                 text=True, bufsize=1, start_new_session=True)
            task.last_activity = time.monotonic()
            done = threading.Event()
            owner._watch_task_disk(task)
            watcher = threading.Thread(target=watch, args=(task, done), daemon=True)
            watcher.start()
            try:
                for line in task.process.stdout:
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    task.last_activity = time.monotonic()
                    kind = event.get('event')
                    if kind == 'metadata':
                        task.name = event.get('title') or task.name
                        task.thumbnail = event.get('thumbnail') or ''
                        task.duration = event.get('duration')
                        configure_streams(task, event.get('streams', []))
                        if not task.output_path:
                            task.output_path = str(build_download_path(task.name, task.output_dir))
                    elif kind == 'progress':
                        if task.pause_requested.is_set():
                            continue
                        task.status = 'downloading'
                        update_progress(task, event)
                    elif kind == 'failed':
                        task.error_message = event.get('message', '')
                        if event.get('fatal'):
                            task.pause_requested.set()
                    elif kind == 'processing' and not task.pause_requested.is_set():
                        task.status = 'processing'
                        task.speed_bps = task.eta_seconds = None
                    owner._notify_ui()
                task.process.wait()
            finally:
                done.set()
                watcher.join(timeout=1)
                task.process.stdout.close()
                task.disk_guard.close()
                task.disk_guard = None
            if task.pause_requested.is_set() or owner._closing or task.status == 'canceled':
                break
            output = Path(task.work_dir)/'media.mp4'
            if task.process.returncode == 0 and output.is_file():
                task.status = 'processing'
                owner._notify_ui()
                probe = probe_media(output)
                assessment = assess_probe(probe, True, expect_audio=True, expected_duration=task.duration)
                task.actual_duration, task.missing_seconds = assessment.actual_duration, assessment.missing_seconds
                if assessment.status == COMPLETED:
                    # Same filesystem, atomic publish; never replace a user's file.
                    if not task.output_path:
                        task.output_path = str(build_download_path(task.name, task.output_dir))
                    with owner._lock:
                        if owner._closing or task.status == 'canceled' or task.pause_requested.is_set():
                            break
                        if Path(task.output_path).exists():
                            if not os.path.samefile(output, task.output_path):
                                raise OSError('Итоговый путь уже занят; временные файлы сохранены')
                        else:
                            os.link(output, task.output_path)
                        task.status, task.progress, task.error_message = 'done', 100, ''
                    shutil.rmtree(task.work_dir)
                    owner._notify_ui()
                    if task.on_complete:
                        task.on_complete(True, task, '')
                    return
                task.error_message = '; '.join(assessment.reasons)
                # Do not silently restart or append to a malformed final container.
                task.status = 'paused'
                task.error_message += '. Части сохранены; требуется проверка источника.'
                break
            if task.storage_error:
                task.error_message = task.storage_error
                break
            if attempt < len(RETRY_DELAYS):
                task.status = 'retrying'
                task.error_message = f'Связь прервана. Повтор {attempt+1}/{len(RETRY_DELAYS)}; части сохранены.'
                owner._notify_ui()
                if task.pause_requested.wait(RETRY_DELAYS[attempt]):
                    break
            else:
                task.error_message = 'Автоматические попытки исчерпаны. Части сохранены — нажмите «Продолжить», когда сеть появится.'
    except Exception as error:
        task.error_message = 'Не удалось продолжить загрузку. Части сохранены. '+str(error)
    finally:
        if task.status != 'done' and task.status != 'canceled':
            task.status = 'paused'
            task.error_message = task.error_message or 'Загрузка на паузе. Временные части сохранены.'
        task.speed_bps = task.eta_seconds = None
        owner._notify_ui()


def watch(task, done):
    while not done.wait(1):
        if time.monotonic()-task.last_activity > 90 and task.status != 'processing':
            stop_process(task)
            if not done.wait(5) and task.process.poll() is None:
                task.process.kill()
            return
