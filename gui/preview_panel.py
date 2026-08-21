# gui/preview_panel.py
"""Встроенная (НЕ всплывающая) панель live-превью канала: снимок кадра,
обновляемый раз в несколько секунд, прямо в макете окна приложения.

Не всплывающее окно — раньше превью открывалось как отдельный
CTkToplevel(overrideredirect=True), но безрамочные окна на macOS
периодически не отрисовываются нормально (известная особенность Tk/Aqua).
Постоянная панель в layout снимает эту проблему полностью."""
import io
import threading
import time
from typing import Optional

import customtkinter as ctk
from PIL import Image

from core.snapshot import grab_snapshot
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

REFRESH_MS = 5000
IMG_SIZE = (260, 146)


class PreviewPanel(ctk.CTkFrame):
    def __init__(self, parent):
        c = Config.COLORS
        super().__init__(parent, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM)
        self._running = True
        self._generation = 0
        self._url: Optional[str] = None
        self._headers: Optional[dict] = None

        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=10, pady=(8, 4))
        self.name_lbl = ctk.CTkLabel(header, text="Превью", font=ctk.CTkFont(size=12, weight='bold'),
                                      text_color=c['text_primary'])
        self.name_lbl.pack(side='left')
        self.status_lbl = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'])
        self.status_lbl.pack(side='right')

        self.image_lbl = ctk.CTkLabel(
            self, text="Клик по логотипу канала\nпокажет, что там сейчас",
            width=IMG_SIZE[0], height=IMG_SIZE[1], corner_radius=8,
            fg_color=c['bg_tertiary'], text_color=c['text_muted'],
            font=ctk.CTkFont(size=11), justify='center')
        self.image_lbl.pack(padx=10, pady=(0, 10))

    def show(self, name: str, url: str, headers: Optional[dict] = None):
        self._generation += 1
        self.name_lbl.configure(text=name)
        self.status_lbl.configure(text="Загрузка…")
        self.image_lbl.configure(text="", image=get_icon('tv', Config.COLORS['text_muted'], 32))
        self._url = url
        self._headers = headers
        self._loop(self._generation)

    def _loop(self, generation: int):
        if not self._running or generation != self._generation:
            return

        def worker():
            jpeg_bytes = grab_snapshot(self._url, self._headers)

            def apply():
                if not self._running or generation != self._generation:
                    return
                if jpeg_bytes:
                    try:
                        pil = Image.open(io.BytesIO(jpeg_bytes)).convert('RGB')
                        pil.thumbnail(IMG_SIZE, Image.LANCZOS)
                        canvas = Image.new('RGB', IMG_SIZE, Config.COLORS['bg_tertiary'])
                        offset = ((IMG_SIZE[0] - pil.width) // 2, (IMG_SIZE[1] - pil.height) // 2)
                        canvas.paste(pil, offset)
                        img = ctk.CTkImage(light_image=canvas, dark_image=canvas, size=IMG_SIZE)
                        self.image_lbl.configure(image=img, text="")
                        self.image_lbl._img_ref = img
                        self.status_lbl.configure(text=f"Обновлено {time.strftime('%H:%M:%S')}")
                    except Exception as e:
                        logger.debug(f"PreviewPanel: ошибка кадра: {e}")
                        self.status_lbl.configure(text="Ошибка кадра")
                else:
                    self.status_lbl.configure(text="Нет сигнала")
                if self._running:
                    self.after(REFRESH_MS, lambda: self._loop(generation))

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self._running = False
