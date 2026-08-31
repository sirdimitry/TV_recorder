# gui/preview_panel.py
"""Встроенная (НЕ всплывающая) панель почти-реалтайм превью канала —
непрерывный низкочастотный видеопоток, а не снимок раз в несколько секунд,
прямо в макете окна приложения.

Не всплывающее окно — раньше превью открывалось как отдельный
CTkToplevel(overrideredirect=True), но безрамочные окна на macOS
периодически не отрисовываются нормально (известная особенность Tk/Aqua).
Постоянная панель в layout снимает эту проблему полностью."""
from typing import Optional

import customtkinter as ctk

from core.live_stream import LiveThumbnailStream
from core.snapshot import to_ctk_image
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

FPS = 15  # один канал в разделе просмотра — можно себе позволить почти-видео
IMG_SIZE = (260, 146)


class PreviewPanel(ctk.CTkFrame):
    def __init__(self, parent):
        c = Config.COLORS
        super().__init__(parent, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM)
        self._running = True
        self._stream: Optional[LiveThumbnailStream] = None
        self._frame_count = 0
        self._generation = 0
        self._preview_name = ''

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
        self._stop_stream()
        self._generation += 1
        generation = self._generation
        self._preview_name = name
        self.name_lbl.configure(text=name)
        self.status_lbl.configure(text="Подключение…")
        self.image_lbl.configure(text="", image=get_icon('tv', Config.COLORS['text_muted'], 32))
        self._frame_count = 0

        self._stream = LiveThumbnailStream(url, headers, fps=FPS, on_frame=lambda jb: self._on_frame(jb, generation),
                                            width=IMG_SIZE[0] * 2)
        self._stream.start()
        # 8с исторически давали ложные "Нет сигнала" на медленных, но рабочих
        # потоках (та же проблема уже ловили в core/snapshot.py — там таймаут
        # пришлось поднять с 6 до 10с). Тут поток ещё и идёт в -re темпе
        # реального эфира, так что запас нужен больше.
        self.after(15000, lambda: self._check_connected(generation))

    def _check_connected(self, generation: int):
        if generation == self._generation and self._frame_count == 0:
            self.status_lbl.configure(text="Нет сигнала")
            # Раньше это было видно только глазами на самой панели — в логе
            # оставалась только строка "Открыт предпросмотр: ..." из
            # ChannelList/LinkList, и по нему было не отличить, реально ли
            # показалось видео или превью тихо умерло (например, ffmpeg не
            # смог открыть поток вообще — см. историю с DASH-потоком
            # "Первый канал"). Пишем в лог сам факт неудачи, раз кнопки для
            # этого нет.
            logger.warning(f"PreviewPanel: '{self._preview_name}' — нет сигнала за 15с (кадров не получено)")

    def _on_frame(self, jpeg_bytes: bytes, generation: int):
        if self._running:
            self.after(0, lambda: self._display(jpeg_bytes, generation))

    def _display(self, jpeg_bytes: bytes, generation: int):
        if not self._running or generation != self._generation or not self.winfo_exists():
            return
        try:
            img = to_ctk_image(jpeg_bytes, IMG_SIZE)
        except Exception as e:
            logger.debug(f"PreviewPanel: ошибка кадра: {e}")
            return
        self.image_lbl.configure(image=img, text="")
        self.image_lbl._img_ref = img
        self._frame_count += 1
        # Не дёргаем текстовый лейбл на каждый кадр — просто держим отметку "в эфире".
        if self._frame_count == 1:
            self.status_lbl.configure(text="В эфире")
            logger.info(f"PreviewPanel: '{self._preview_name}' — превью в эфире (получен первый кадр)")

    def _stop_stream(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream = None

    def stop(self):
        self._running = False
        self._stop_stream()
