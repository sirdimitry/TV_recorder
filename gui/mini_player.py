# gui/mini_player.py
import tkinter as tk
from tkinter import messagebox
import subprocess
import shutil
import threading
from utils.config import Config
from utils.logger import logger
from core.m3u_parser import M3UParser
from core.stream_resolver import resolve_variant_url
from core.link_resolver import resolve_link


class MiniPlayer(tk.Toplevel):
    """Невидимый диспетчер ffplay с поддержкой спец. заголовков"""

    _active_players = {}  # name -> list[subprocess.Popen] (1 обычно, 2 при пайпе ffmpeg->ffplay)

    # -fs у ffplay уходит в НАСТОЯЩИЙ macOS-fullscreen — на весь физический
    # экран, без рамки окна и без простого способа выйти, если поток
    # подвиснет (не переключишься на другое окно, Dock/меню-бар скрыты).
    # Один такой завис уже перекрывал всю работу пользователю. Вместо
    # этого для "большого" предпросмотра просто открываем ffplay крупным
    # обычным окном — та же логика, что и с окном браузера (см.
    # browser_capture.py) — просто больше, а не fullscreen.
    LARGE_WIDTH = 1600
    LARGE_HEIGHT = 900

    def __init__(self, parent, channel_name: str, stream_url: str,
                 large: bool = False, resolve_via_ytdlp: bool = False):
        super().__init__(parent)
        self.withdraw()

        self.channel_name = channel_name
        self.stream_url = stream_url
        self.large = large
        self.resolve_via_ytdlp = resolve_via_ytdlp

        # TOGGLE: если уже играет - убиваем и открываем заново
        if channel_name in self._active_players:
            procs = self._active_players.pop(channel_name)
            if any(p.poll() is None for p in procs):
                logger.info(f"MiniPlayer: Перезапуск потока '{channel_name}'")
                for p in procs:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                for p in procs:
                    try:
                        p.wait(timeout=3)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass
                self.destroy()
                return

        self._play_stream()
        self.destroy()

    def _play_stream(self):
        if not shutil.which('ffplay'):
            messagebox.showerror("Ошибка", "Для предпросмотра нужен ffmpeg.\nУстановите: brew install ffmpeg")
            return

        # Выбор варианта качества / разбор ссылки через yt-dlp делает сетевой
        # запрос — уводим его вместе с запуском ffplay в фоновый поток, чтобы
        # клик по превью не подвешивал интерфейс (MiniPlayer не показывает
        # окно, поэтому это безопасно).
        threading.Thread(target=self._launch_ffplay, daemon=True).start()

    def _launch_ffplay(self):
        try:
            audio_url = None

            if self.resolve_via_ytdlp:
                info = resolve_link(self.stream_url)
                if not info.ok:
                    self.master.after(0, lambda: messagebox.showerror(
                        "Ошибка", f"Не удалось открыть «{self.channel_name}»:\n{info.error}"))
                    return
                stream_url = info.video_url
                audio_url = info.audio_url
                # Некоторые CDN (например VK) подписывают ссылку под тот
                # самый User-Agent, которым её запросил yt-dlp, и отвечают
                # HTTP 400 на любой другой.
                if info.headers:
                    headers = ''.join(f"{k}: {v}\r\n" for k, v in info.headers.items())
                else:
                    headers = "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)\r\n"
            else:
                ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                extra_headers = ""
                referer = None
                if self.channel_name in M3UParser.SPECIAL_HEADERS:
                    h = M3UParser.SPECIAL_HEADERS[self.channel_name]
                    if 'Referer' in h:
                        referer = h['Referer']
                        extra_headers += f"Referer: {h['Referer']}\r\n"
                    if 'Origin' in h:
                        extra_headers += f"Origin: {h['Origin']}\r\n"
                headers = f"User-Agent: {ua}\r\n{extra_headers}"
                # 720p/3-5 Мбит вместо того, что ffplay выберет сам из
                # мастер-плейлиста (обычно самый тяжёлый вариант).
                stream_url = resolve_variant_url(self.stream_url, user_agent=ua, referer=referer)

            ffplay_cmd = [
                'ffplay',
                '-autoexit',
                '-window_title', f"TV Recorder: {self.channel_name}",
                '-loglevel', 'error',
                '-infbuf',
                '-framedrop',
                '-sync', 'audio',
            ]
            if self.large:
                ffplay_cmd += ['-x', str(self.LARGE_WIDTH), '-y', str(self.LARGE_HEIGHT)]

            if audio_url:
                # ffplay умеет играть только один вход, а видео и звук здесь —
                # раздельные дорожки (типично для YouTube на 720p+). Мультиплекс
                # делает ffmpeg на лету и стримит результат в ffplay через pipe.
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-headers', headers, '-i', stream_url,
                    '-headers', headers, '-i', audio_url,
                    '-map', '0:v:0', '-map', '1:a:0',
                    '-c', 'copy',
                    '-f', 'matroska', 'pipe:1',
                ]
                ffplay_cmd += ['-i', '-']

                ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                ffplay_proc = subprocess.Popen(ffplay_cmd, stdin=ffmpeg_proc.stdout, stderr=subprocess.DEVNULL)
                ffmpeg_proc.stdout.close()  # ffplay держит свою копию — иначе ffmpeg не получит SIGPIPE при закрытии плеера
                procs = [ffmpeg_proc, ffplay_proc]
            else:
                ffplay_cmd += ['-headers', headers,
                                '-rw_timeout', '10000000',
                                '-probesize', '500000',
                                '-analyzeduration', '1000000',
                                '-err_detect', 'ignore_err',
                                stream_url]
                procs = [subprocess.Popen(ffplay_cmd)]

            self._active_players[self.channel_name] = procs
            logger.info(f"MiniPlayer: поток '{self.channel_name}' открыт в ffplay "
                        f"(PID: {[p.pid for p in procs]})")

        except Exception as e:
            logger.error(f"MiniPlayer ошибка запуска: {e}")
            # self (Toplevel) уничтожается сразу после запуска этого потока,
            # поэтому диалог показываем через ещё живой родительский root.
            self.master.after(0, lambda: messagebox.showerror(
                "Ошибка", f"Не удалось запустить предпросмотр:\n{e}"))
