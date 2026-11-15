# core/live_stream.py
"""Continuous JPEG thumbnails. Low output FPS alone does not avoid decoding
intermediate video frames; keyframes_only explicitly skips those in the decoder.
"""
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

from core.stream_resolver import RECONNECT_OPTS, hls_opts
from utils.logger import logger

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
FRAME_TIMEOUT = 30.0
MAX_FRAME_BYTES = 8 * 1024 * 1024


class LiveThumbnailStream:
    """Один непрерывный ffmpeg-процесс на один URL. on_frame(jpeg_bytes)
    вызывается из фонового потока на каждый декодированный кадр."""

    def __init__(self, url: str, headers: Optional[dict], fps: float,
                 on_frame: Callable[[bytes], None], width: int = 320,
                 start_offset: float = 0, duration_limit: Optional[float] = None,
                 on_timed_frame=None, keyframes_only: bool = False):
        self.url = url
        self.headers = headers
        self.fps = fps
        self.width = width
        self.on_frame = on_frame
        self.on_timed_frame = on_timed_frame
        if keyframes_only and on_timed_frame is not None:
            raise ValueError('Опорные кадры не подходят для покадровой шкалы архива')
        self.keyframes_only = keyframes_only
        self._worker = None
        self.start_offset = start_offset
        self.duration_limit = duration_limit
        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self.completed = False
        self._stop_event = threading.Event()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def stop(self):
        self._running = False
        self._stop_event.set()
        proc = self._process
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def wait_stopped(self, timeout=5):
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=timeout)
        return self._worker is None or not self._worker.is_alive()

    def _watch_stall(self, proc, last_frame, finished):
        """A network reconnect can otherwise keep read() blocked indefinitely."""
        timeout = max(90.0, FRAME_TIMEOUT) if self.keyframes_only else FRAME_TIMEOUT
        while not finished.wait(1.0):
            if self._stop_event.is_set() or time.monotonic() - last_frame[0] >= timeout:
                if not self._stop_event.is_set():
                    logger.warning("LiveThumbnailStream: нет кадров %.0fс, перезапускаем предпросмотр", timeout)
                try:
                    proc.terminate()
                    if not finished.wait(2.0):
                        proc.kill()
                except OSError:
                    pass
                return

    @staticmethod
    def _read_errors(proc, errors):
        # Drain continuously, retaining only a bounded tail; a full stderr
        # pipe must never stop the video decoder.
        try:
            for line in proc.stderr:
                errors.append(line.decode(errors='replace').strip()[:1000])
        except (OSError, ValueError):
            pass

    def _run(self):
        if self.headers:
            header_str = ''.join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        else:
            header_str = f"User-Agent: {DEFAULT_UA}\r\n"

        cmd = [
            'ffmpeg', '-y', '-loglevel', 'error',
            *RECONNECT_OPTS, *hls_opts(self.url),
            '-headers', header_str,
            # Без явных лимитов ffmpeg по умолчанию пробует набрать до 5МБ/5с
            # перед стартом декодирования — на медленной сети это само по себе
            # съедало большую часть таймаута первого кадра в PreviewPanel.
            '-analyzeduration', '2000000',
            '-probesize', '1000000',
            # Без -re ffmpeg читает HLS настолько быстро, насколько позволяет
            # сеть/диск (в разы быстрее реального времени) — кадры сыпались
            # бы залпом, а не в темпе настоящего эфира.
            '-re',
            *(['-skip_frame', 'nokey'] if self.keyframes_only else []),
            '-i', self.url,
            *(['-ss', str(self.start_offset)] if self.start_offset else []),
            *(['-t', str(self.duration_limit)] if self.duration_limit else []),
            '-an',
            '-vf', (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{1/self.fps})',scale={self.width}:-1"
                    if self.keyframes_only else f'fps={self.fps},scale={self.width}:-1'),
            *(['-fps_mode', 'vfr'] if self.keyframes_only else []),
            '-q:v', '7',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            'pipe:1',
        ]
        retry_delay = 1.0
        while self._running:
            proc = None
            finished = threading.Event()
            errors = deque(maxlen=5)
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._process = proc
                last_frame = [time.monotonic()]
                threading.Thread(target=self._watch_stall, args=(proc, last_frame, finished), daemon=True).start()
                threading.Thread(target=self._read_errors, args=(proc, errors), daemon=True).start()
                buffer = bytearray()
                frame_number = 0
                while self._running:
                    chunk = proc.stdout.read1(8192)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    if len(buffer) > MAX_FRAME_BYTES:
                        raise ValueError("MJPEG frame exceeds 8 MiB")
                    while True:
                        start = buffer.find(b'\xff\xd8')
                        if start == -1:
                            buffer.clear()
                            break
                        end = buffer.find(b'\xff\xd9', start + 2)
                        if end == -1:
                            if start > 0:
                                del buffer[:start]
                            break
                        frame = bytes(buffer[start:end + 2])
                        del buffer[:end + 2]
                        if self._running:
                            last_frame[0] = time.monotonic()
                            retry_delay = 1.0
                            if self.on_timed_frame is not None:
                                self.on_timed_frame(frame, frame_number / self.fps)
                            else:
                                self.on_frame(frame)
                            frame_number += 1
            except Exception:
                if self._running:
                    logger.exception("LiveThumbnailStream: ошибка предпросмотра")
            finally:
                if proc is not None:
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                            proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=2)
                        except Exception:
                            pass
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe is not None:
                            pipe.close()
                    if self.duration_limit and proc.returncode == 0:
                        self.completed = True
                        self._running = False
                    if self._running:
                        logger.warning("LiveThumbnailStream: декодер завершился (код %s), повтор через %.1fс: %s",
                                       proc.returncode, retry_delay, '; '.join(errors) or 'без вывода')
                finished.set()
                self._process = None
            if self._running:
                self._stop_event.wait(retry_delay)
                retry_delay = min(10.0, retry_delay * 2)
