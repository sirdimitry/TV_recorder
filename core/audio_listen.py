# core/audio_listen.py
"""Прослушивание звука уже идущей записи/превью — отдельный от записи путь,
никак её не трогающий (см. core/recorder.py: -c copy пишет файл сам по
себе, это чтение того же URL параллельно).

Раньше здесь просто запускался ffplay как чёрный ящик — если пользователь
слышал тишину, было невозможно отличить "поток реально без звука/сеть
подвела" от "звук декодируется нормально, но не долетает до колонок"
(например, если системным выводом по умолчанию назначено виртуальное
устройство вроде BlackHole — оно нужно для записи звука с браузера, см.
core/recorder.py: find_loopback_audio_index, но тогда обычные приложения
тоже тихо играют в него, а не в колонки). Теперь звук САМИ декодируем в
сырой PCM через ffmpeg, качаем оттуда реальный уровень (это и есть
доказательство, что аудио-сэмплы реально идут — не просто что ffplay не
упал) и уже этот PCM отдаём ffplay на воспроизведение через stdin."""
import array
import math
import subprocess
import threading
import time
from typing import Callable, Optional

from core.stream_resolver import RECONNECT_OPTS, hls_opts
from utils.logger import logger

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
SAMPLE_RATE = 44100
CHUNK_BYTES = 4096  # 16-bit mono -> 2048 сэмплов, ~46мс за чтение — достаточно отзывчиво для индикатора уровня
NO_AUDIO_TIMEOUT = 30.0  # живой HLS-старт (реконнект + буферизация первого сегмента) на некоторых CDN реально
# занимает 15-45+с — замерено напрямую: свежее TCP-соединение на ряде источников (cdn.ntv.ru и др.) регулярно
# сразу рвётся ("error=End of file") и ffmpeg тратит один-два полных цикла реконнекта (0,1,3,7с) прежде чем
# получится — это сетевая особенность, не баг декодера, но именно поэтому таймаут должен быть щедрым


def _rms(chunk: bytes) -> float:
    """Без audioop (deprecated/убран в 3.13+) — обычный RMS по int16-сэмплам."""
    n = len(chunk) - len(chunk) % 2
    if n <= 0:
        return 0.0
    samples = array.array('h')
    samples.frombytes(chunk[:n])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class AudioListener:
    """decode (ffmpeg -> сырой PCM) -> play (ffplay читает PCM из stdin).
    Разделение специально ради on_level: без него было бы не отличить
    "декодер молчит" от "звук играет, просто тихо/не туда"."""

    def __init__(self, url: str, headers: Optional[dict] = None, label: str = '',
                 on_level: Optional[Callable[[float], None]] = None):
        self.url = url
        self.headers = headers
        self.label = label or url
        self.on_level = on_level
        self._decode_proc: Optional[subprocess.Popen] = None
        self._play_proc: Optional[subprocess.Popen] = None
        self._stopping = False
        self._got_audio = False

    def start(self):
        if self.headers:
            header_str = ''.join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        else:
            header_str = f"User-Agent: {DEFAULT_UA}\r\n"

        decode_cmd = [
            'ffmpeg', '-y', '-loglevel', 'error',
            '-analyzeduration', '2000000', '-probesize', '1000000',
            *RECONNECT_OPTS, *hls_opts(self.url),
            '-headers', header_str, '-i', self.url,
            '-vn', '-f', 's16le', '-acodec', 'pcm_s16le', '-ar', str(SAMPLE_RATE), '-ac', '1',
            'pipe:1',
        ]
        play_cmd = [
            # ffplay, в отличие от ffmpeg, не принимает '-ac' для сырого
            # PCM на вход (ffmpeg 7.1: "Option not found") — там нужен
            # именно приватный параметр демуксера '-ch_layout'.
            'ffplay', '-nodisp', '-autoexit', '-loglevel', 'error',
            '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ch_layout', 'mono', '-i', '-',
        ]
        try:
            self._decode_proc = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._play_proc = subprocess.Popen(play_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            logger.warning(f"AudioListener: не удалось запустить ffmpeg/ffplay для '{self.label}': {e}")
            self._decode_proc = None
            self._play_proc = None
            return

        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._watch_decode_errors, daemon=True).start()
        threading.Thread(target=self._watch_play_errors, daemon=True).start()
        threading.Thread(target=self._watch_no_audio, daemon=True).start()

    def _pump(self):
        """Качает PCM из ffmpeg и тут же скармливает его ffplay — заодно
        единственное место, где реально видно, дошли ли хоть какие-то
        аудио-байты вообще."""
        proc, player = self._decode_proc, self._play_proc
        if proc is None or player is None:
            return
        try:
            while True:
                chunk = proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                if not self._got_audio:
                    self._got_audio = True
                    logger.info(f"AudioListener: '{self.label}' — пошли аудио-сэмплы, звук реально декодируется")
                if player.stdin:
                    try:
                        player.stdin.write(chunk)
                        player.stdin.flush()
                    except (BrokenPipeError, OSError):
                        if not self._stopping:
                            logger.warning(f"AudioListener: '{self.label}' — плеер (ffplay) закрылся раньше потока, "
                                            f"воспроизведение прервалось")
                        break
                if self.on_level:
                    try:
                        self.on_level(min(1.0, _rms(chunk) / 8000.0))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if player.stdin:
                try:
                    player.stdin.close()
                except Exception:
                    pass
            if self.on_level:
                try:
                    self.on_level(0.0)
                except Exception:
                    pass

    def _watch_no_audio(self):
        """Если за NO_AUDIO_TIMEOUT секунд не пришло вообще ни одного
        сэмпла — раньше это было видно только по тишине в колонках, теперь
        хотя бы явно написано в лог, что декодер тоже ничего не получил
        (а не просто "не долетело до динамиков")."""
        time.sleep(NO_AUDIO_TIMEOUT)
        if not self._stopping and not self._got_audio and self._decode_proc is not None:
            logger.warning(f"AudioListener: '{self.label}' — за {NO_AUDIO_TIMEOUT:.0f}с не пришло ни одного "
                            f"аудио-сэмпла (см. предупреждения ffmpeg выше, если есть)")

    def _watch_decode_errors(self):
        # ВАЖНО: только stderr — stdout этого процесса уже читает _pump()
        # в своём потоке, второй читатель того же pipe устроил бы гонку.
        proc = self._decode_proc
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
        if proc.returncode not in (0, None):
            tail = (stderr or b'').decode(errors='ignore').strip().splitlines()
            tail_msg = '; '.join(tail[-5:]) if tail else '(без вывода)'
            logger.warning(f"AudioListener: декодер для '{self.label}' завершился с ошибкой "
                            f"(код {proc.returncode}): {tail_msg}")

    def _watch_play_errors(self):
        # ffplay здесь играет PCM из своего stdin — если он не смог открыть
        # звуковое устройство (например, оно занято/недоступно), сам процесс
        # decode этого не узнает и продолжит слать байты в никуда. Именно
        # в stderr плеера будет реальный ответ на "звук пошёл ли?".
        proc = self._play_proc
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
        if proc.returncode not in (0, None):
            tail = (stderr or b'').decode(errors='ignore').strip().splitlines()
            tail_msg = '; '.join(tail[-5:]) if tail else '(без вывода)'
            logger.warning(f"AudioListener: воспроизведение для '{self.label}' завершилось с ошибкой "
                            f"(код {proc.returncode}): {tail_msg}")

    def stop(self):
        # Не ждём здесь завершения процессов синхронно — stop() дёргается
        # прямо из клика в Tk-потоке, а блокировка на wait() подвесила бы
        # интерфейс. Фоновые потоки выше сами вычитывают и реапят обоих
        # детей после terminate().
        self._stopping = True
        for proc in (self._decode_proc, self._play_proc):
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._decode_proc = None
        self._play_proc = None
