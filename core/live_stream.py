# core/live_stream.py
"""Непрерывный низкочастотный MJPEG-захват из потока через ffmpeg — вместо
разового кадра раз в несколько секунд отдаёт кадры с заданным fps, пока
поток открыт. Не декодирует полноценное видео и не тянет звук — просто
просит ffmpeg отдавать JPEG-кадры с нужной частотой и разбирает их из
байтового потока по маркерам SOI/EOI (0xFFD8...0xFFD9)."""
import subprocess
import threading
from typing import Callable, Optional

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


class LiveThumbnailStream:
    """Один непрерывный ffmpeg-процесс на один URL. on_frame(jpeg_bytes)
    вызывается из фонового потока на каждый декодированный кадр."""

    def __init__(self, url: str, headers: Optional[dict], fps: float,
                 on_frame: Callable[[bytes], None], width: int = 320):
        self.url = url
        self.headers = headers
        self.fps = fps
        self.width = width
        self.on_frame = on_frame
        self._process: Optional[subprocess.Popen] = None
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._running = False
        proc = self._process
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _run(self):
        if self.headers:
            header_str = ''.join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        else:
            header_str = f"User-Agent: {DEFAULT_UA}\r\n"

        cmd = [
            'ffmpeg', '-y',
            '-allowed_extensions', 'ALL',
            '-headers', header_str,
            # Без -re ffmpeg читает HLS настолько быстро, насколько позволяет
            # сеть/диск (в разы быстрее реального времени) — кадры сыпались
            # бы залпом, а не в темпе настоящего эфира.
            '-re',
            '-i', self.url,
            '-an',
            '-r', str(self.fps),
            '-vf', f'scale={self.width}:-1',
            '-q:v', '7',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            'pipe:1',
        ]
        try:
            self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            return

        buffer = bytearray()
        try:
            while self._running:
                chunk = self._process.stdout.read(8192)
                if not chunk:
                    break
                buffer.extend(chunk)
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
                        self.on_frame(frame)
        except Exception:
            pass
        finally:
            proc = self._process
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
