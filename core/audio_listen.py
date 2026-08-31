# core/audio_listen.py
"""Прослушивание звука уже идущей записи прямо из окна мониторинга.

Отдельный ffplay-процесс поверх ТОГО ЖЕ URL, который Recorder параллельно
и независимо продолжает писать в файл (-c copy). Никак не связан с
процессом записи — не читает и не пишет в её файл, не трогает task.process.
Остановка/сбой прослушивания не может повлиять на саму запись, и наоборот."""
import subprocess
from typing import Optional

from core.stream_resolver import RECONNECT_OPTS, hls_opts

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


class AudioListener:
    """Один ffplay-процесс на один URL — только звук, без окна плеера."""

    def __init__(self, url: str, headers: Optional[dict] = None):
        self.url = url
        self.headers = headers
        self._process: Optional[subprocess.Popen] = None

    def start(self):
        if self.headers:
            header_str = ''.join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        else:
            header_str = f"User-Agent: {DEFAULT_UA}\r\n"

        cmd = [
            'ffplay', '-nodisp', '-autoexit', '-loglevel', 'error',
            '-vn',
            *RECONNECT_OPTS, *hls_opts(self.url),
            '-headers', header_str,
            '-i', self.url,
        ]
        try:
            self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self._process = None

    def stop(self):
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
