# gui/preview_popup.py
"""Компактное встроенное превью канала: периодический снимок кадра вместо
отдельного окна ffplay. Не запускает чужой процесс с собственным окном —
всё рисуется внутри нашего приложения, обновляется раз в несколько секунд."""
import io
import threading
import time

import customtkinter as ctk
from PIL import Image

from core.snapshot import grab_snapshot
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

REFRESH_MS = 5000
IMG_SIZE = (320, 180)


class PreviewPopup(ctk.CTkToplevel):
    """Один попап на всё приложение: повторный клик по другому каналу
    переиспользует уже открытое окно вместо того, чтобы плодить новые."""

    _instance = None

    @classmethod
    def show(cls, root, name: str, url: str, headers: dict | None = None):
        if not url:
            return
        if cls._instance is not None and cls._instance.winfo_exists():
            cls._instance._switch_to(name, url, headers)
            cls._instance.lift()
            return
        cls._instance = PreviewPopup(root, name, url, headers)

    def __init__(self, root, name: str, url: str, headers: dict | None):
        super().__init__(root)
        self._root_win = root
        self._running = True
        self._generation = 0

        self.overrideredirect(True)
        self.attributes('-topmost', True)
        c = Config.COLORS

        border = ctk.CTkFrame(self, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS,
                               border_width=1, border_color=c['border'])
        border.pack(fill='both', expand=True)

        header = ctk.CTkFrame(border, fg_color='transparent')
        header.pack(fill='x', padx=10, pady=(8, 4))
        self.name_lbl = ctk.CTkLabel(header, text=name, font=ctk.CTkFont(size=12, weight='bold'),
                                      text_color=c['text_primary'])
        self.name_lbl.pack(side='left')
        ctk.CTkButton(header, text="", image=get_icon('close', c['text_secondary'], 12), width=22, height=22,
                      corner_radius=Config.RADIUS_SM, fg_color='transparent', hover_color=c['bg_active'],
                      command=self._close).pack(side='right')

        self.image_lbl = ctk.CTkLabel(border, text="", width=IMG_SIZE[0], height=IMG_SIZE[1],
                                       corner_radius=8, fg_color=c['bg_tertiary'],
                                       image=get_icon('tv', c['text_muted'], 32))
        self.image_lbl.pack(padx=10, pady=(0, 4))

        self.status_lbl = ctk.CTkLabel(border, text="Загрузка…", font=ctk.CTkFont(size=10),
                                        text_color=c['text_muted'])
        self.status_lbl.pack(pady=(0, 8))

        self._position()
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._switch_to(name, url, headers)

    def _position(self):
        self.update_idletasks()
        rx, ry = self._root_win.winfo_rootx(), self._root_win.winfo_rooty()
        rw, rh = self._root_win.winfo_width(), self._root_win.winfo_height()
        w, h = IMG_SIZE[0] + 40, IMG_SIZE[1] + 92
        x = rx + rw - w - 24
        y = ry + rh - h - 48
        self.geometry(f"{w}x{h}+{max(x, rx)}+{max(y, ry)}")

    def _switch_to(self, name: str, url: str, headers: dict | None):
        self._generation += 1
        self.name_lbl.configure(text=name)
        self.status_lbl.configure(text="Загрузка…")
        self._url = url
        self._headers = headers
        logger.info(f"PreviewPopup: превью «{name}»")
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
                        self.image_lbl.configure(image=img)
                        self.image_lbl._img_ref = img
                        self.status_lbl.configure(text=f"Обновлено {time.strftime('%H:%M:%S')}")
                    except Exception as e:
                        logger.debug(f"PreviewPopup: ошибка кадра: {e}")
                        self.status_lbl.configure(text="Ошибка кадра")
                else:
                    self.status_lbl.configure(text="Нет сигнала")
                if self._running:
                    self.after(REFRESH_MS, lambda: self._loop(generation))

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _close(self):
        self._running = False
        PreviewPopup._instance = None
        self.destroy()
