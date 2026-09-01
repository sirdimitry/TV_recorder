# core/audio_listen.py
"""Прослушивание звука уже идущей записи/превью — отдельный от записи путь,
никак её не трогающий (см. core/recorder.py: -c copy пишет файл сам по
себе, это чтение того же URL параллельно).

Было два варианта: (1) один ffplay-процесс читает поток напрямую и играет
сам, (2) ffmpeg декодирует в PCM -> Python перекладывает в ffplay ->
считает уровень звука для индикатора. Второй давал полоску уровня, но
ценой трёх процессов на каждое прослушивание вместо одного и лишней сетевой
нагрузки (тот же поток фактически читался дважды на пути через Python).
По решению по итогам живого тестирования вернулись к варианту (1) —
тот же приём, что уже проверен в gui/mini_player.py для "Мои ссылки".
Если понадобится индикатор уровня обратно — рабочая PCM-версия есть в
git-истории этого файла (коммит "Rework AudioListener to decode+meter
PCM instead of a black-box ffplay")."""
import subprocess
import threading
from typing import Callable, Optional

from core.stream_resolver import RECONNECT_OPTS, hls_opts
from utils.logger import logger

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


class AudioListener:
    """Один ffplay-процесс на один URL — только звук, без окна плеера."""

    def __init__(self, url: str, headers: Optional[dict] = None, label: str = '',
                 on_level: Optional[Callable[[float], None]] = None):
        # on_level из более сложной PCM-версии больше не используется —
        # параметр оставлен только чтобы не трогать вызовы в
        # gui/preview_panel.py и gui/recording_monitor.py.
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
        """Раньше stderr шёл в DEVNULL — если звук для канала не заводился,
        в логе не оставалось никакого следа. Пишем в лог только настоящий
        сбой — не наш собственный stop() (тогда _stopping уже True)."""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            stderr = proc.stderr.read()
        except Exception:
            stderr = b''
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if self._stopping:
            return
        if proc.returncode != 0:
            tail = (stderr or b'').decode(errors='ignore').strip().splitlines()
            tail_msg = '; '.join(tail[-5:]) if tail else '(без вывода)'
            logger.warning(f"AudioListener: прослушивание '{self.label}' прервалось "
                            f"(код {proc.returncode}): {tail_msg}")

    def stop(self):
        # Не ждём здесь завершения процесса синхронно — stop() дёргается
        # прямо из клика в Tk-потоке (переключение между плитками/каналами),
        # блокировка на wait() подвесила бы интерфейс. Реально дожидается и
        # вычитывает процесс фоновый _watch()-поток, запущенный в start().
        self._stopping = True
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
