# core/audio_listen.py
"""Прослушивание звука уже идущей записи прямо из окна мониторинга.

Отдельный ffplay-процесс поверх ТОГО ЖЕ URL, который Recorder параллельно
и независимо продолжает писать в файл (-c copy). Никак не связан с
процессом записи — не читает и не пишет в её файл, не трогает task.process.
Остановка/сбой прослушивания не может повлиять на саму запись, и наоборот."""
import subprocess
import threading
from typing import Optional

from core.stream_resolver import RECONNECT_OPTS, hls_opts
from utils.logger import logger

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


class AudioListener:
    """Один ffplay-процесс на один URL — только звук, без окна плеера."""

    def __init__(self, url: str, headers: Optional[dict] = None, label: str = ''):
        self.url = url
        self.headers = headers
        self.label = label or url
        self._process: Optional[subprocess.Popen] = None
        self._stopping = False

    def start(self):
        if self.headers:
            header_str = ''.join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        else:
            header_str = f"User-Agent: {DEFAULT_UA}\r\n"

        cmd = [
            'ffplay', '-nodisp', '-autoexit', '-loglevel', 'error',
            '-vn',
            # Дефолтные лимиты пробы (до 5МБ/5с) на медленных сетях сами
            # съедают заметную часть той короткой паузы, что пользователь
            # реально готов ждать звука после клика — та же правка, что
            # уже помогла с первым кадром превью (core/live_stream.py).
            '-analyzeduration', '2000000',
            '-probesize', '1000000',
            *RECONNECT_OPTS, *hls_opts(self.url),
            '-headers', header_str,
            '-i', self.url,
        ]
        try:
            self._process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            logger.warning(f"AudioListener: не удалось запустить ffplay для '{self.label}': {e}")
            self._process = None
            return
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        """Раньше stderr шёл в DEVNULL — если звук для канала не заводился
        (например, у него в этом URL вообще нет аудио-дорожки), в логе не
        оставалось никакого следа, и разобраться, что пошло не так, было
        нечем. Пишем в лог только настоящий сбой — не наш собственный
        stop() (тогда _stopping уже True и process явно убит нами)."""
        proc = self._process
        if proc is None:
            return
        try:
            _, stderr = proc.communicate()
        except Exception:
            return
        if self._stopping:
            return
        if proc.returncode != 0:
            tail = (stderr or b'').decode(errors='ignore').strip().splitlines()
            tail_msg = '; '.join(tail[-5:]) if tail else '(без вывода)'
            logger.warning(f"AudioListener: прослушивание '{self.label}' прервалось "
                            f"(код {proc.returncode}): {tail_msg}")

    def stop(self):
        # Не ждём здесь завершения процесса синхронно — stop() дёргается
        # прямо из клика в Tk-потоке (переключение между плитками), и
        # блокировка на wait() подвесила бы интерфейс на каждый клик.
        # Реально дожидается и вычитывает процесс фоновый _watch()-поток,
        # запущенный в start() — он и предотвращает зомби-процессы.
        self._stopping = True
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
