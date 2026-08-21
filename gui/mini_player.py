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


class MiniPlayer(tk.Toplevel):
    """Невидимый диспетчер ffplay с поддержкой спец. заголовков"""
    
    _active_players = {}
    
    def __init__(self, parent, channel_name: str, stream_url: str):
        super().__init__(parent)
        self.withdraw()
        
        self.channel_name = channel_name
        self.stream_url = stream_url
        
        # TOGGLE: если уже играет - убиваем и открываем заново
        if channel_name in self._active_players:
            proc = self._active_players[channel_name]
            if proc.poll() is None:
                logger.info(f"MiniPlayer: Перезапуск потока '{channel_name}'")
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except:
                    proc.kill()
                del self._active_players[channel_name]
                self.destroy()
                return
        
        self._play_stream()
        self.destroy()

    def _play_stream(self):
        if not shutil.which('ffplay'):
            messagebox.showerror("Ошибка", "Для предпросмотра нужен ffmpeg.\nУстановите: brew install ffmpeg")
            return

        # Выбор варианта качества делает сетевой запрос — уводим его вместе
        # с запуском ffplay в фоновый поток, чтобы клик по превью не подвешивал
        # интерфейс (MiniPlayer не показывает окно, поэтому это безопасно).
        threading.Thread(target=self._launch_ffplay, daemon=True).start()

    def _launch_ffplay(self):
        try:
            # Базовый User-Agent
            ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

            # Спец. заголовки для конкретных каналов
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

            cmd = [
                'ffplay',
                '-autoexit',
                '-window_title', f"TV Recorder: {self.channel_name}",
                '-loglevel', 'error',
                '-headers', headers,
                # Таймаут подключения, чтобы намертво зависший источник
                # (например нерабочий CDN) не держал окно предпросмотра
                # открытым бесконечно, а быстро сообщал об ошибке.
                '-rw_timeout', '10000000',
                # Небольшие probesize/analyzeduration — предпросмотру live-ТВ
                # не нужно ждать 10 МБ/10 секунд данных, чтобы определить
                # формат; это и было причиной долгого старта.
                '-probesize', '500000',
                '-analyzeduration', '1000000',
                '-err_detect', 'ignore_err',
                '-infbuf',
                '-framedrop',
                '-sync', 'audio',
                stream_url
            ]

            proc = subprocess.Popen(cmd)
            self._active_players[self.channel_name] = proc
            logger.info(f"MiniPlayer: поток '{self.channel_name}' открыт в ffplay (PID: {proc.pid})")
            
        except Exception as e:
            logger.error(f"MiniPlayer ошибка запуска: {e}")
            # self (Toplevel) уничтожается сразу после запуска этого потока,
            # поэтому диалог показываем через ещё живой родительский root.
            self.master.after(0, lambda: messagebox.showerror(
                "Ошибка", f"Не удалось запустить предпросмотр:\n{e}"))