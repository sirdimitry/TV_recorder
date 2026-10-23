# gui/preview_panel.py
"""Встроенная (НЕ всплывающая) панель почти-реалтайм превью канала —
непрерывный низкочастотный видеопоток, а не снимок раз в несколько секунд,
прямо в макете окна приложения.

Не всплывающее окно — раньше превью открывалось как отдельный
CTkToplevel(overrideredirect=True), но безрамочные окна на macOS
периодически не отрисовываются нормально (известная особенность Tk/Aqua).
Постоянная панель в layout снимает эту проблему полностью."""
from typing import Optional
import threading
import time

import customtkinter as ctk

from core.audio_listen import AudioListener
from core.live_stream import LiveThumbnailStream
from core.snapshot import to_ctk_image
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

FPS = 15  # один канал в разделе просмотра — можно себе позволить почти-видео
IMG_SIZE = (260, 146)


class PreviewPanel(ctk.CTkFrame):
    def __init__(self, parent, *, image_size=IMG_SIZE, contain_image=False):
        c = Config.COLORS
        super().__init__(parent, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM)
        self._image_size = image_size
        self._contain_image = contain_image
        self._running = True
        self._stream: Optional[LiveThumbnailStream] = None
        self._frame_count = 0
        self._generation = 0
        self._frame_lock = threading.Lock()
        self._pending_frame = None
        self._last_frame_at = 0.0
        self._stalled = False
        self._preview_name = ''
        self.playing_label = 'В эфире'
        self._url = ''
        self._headers: Optional[dict] = None
        self._audio_url: Optional[str] = None
        self._start_offset = 0
        self._duration_limit = None
        self.playback_seconds = None
        # Прослушивание — отдельный ffplay-процесс поверх того же URL, что и
        # превью, никак не связанный с самой записью (см. core/audio_listen.py,
        # та же логика уже проверена в gui/recording_monitor.py).
        self._listener: Optional[AudioListener] = None
        self._listening = False
        # Хочет ли пользователь слышать звук ВООБЩЕ, а не только для текущего
        # канала — переживает переключение на другой канал/ссылку (show()),
        # чтобы звук "следовал" за картинкой без повторного клика на каждую
        # смену канала (раньше show() всегда останавливал прослушивание).
        self._want_audio = False

        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=10, pady=(8, 4))
        self.name_lbl = ctk.CTkLabel(header, text="Превью", font=ctk.CTkFont(size=12, weight='bold'),
                                      text_color=c['text_primary'])
        self.name_lbl.pack(side='left')
        # Просто индикатор "звук сейчас идёт" — НЕ кнопка, включать/выключать
        # звук нужно кликом по самой картинке (см. image_lbl ниже). Спрятан,
        # пока прослушивание не включено.
        self.listen_icon = ctk.CTkLabel(header, text="", image=get_icon('volume', c['accent'], 14))
        self.status_lbl = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'])
        self.status_lbl.pack(side='right', padx=(0, 6))

        self.image_lbl = ctk.CTkLabel(
            self, text="Клик по логотипу канала\nпокажет, что там сейчас",
            width=image_size[0], height=image_size[1], corner_radius=8,
            fg_color=c['bg_tertiary'], text_color=c['text_muted'],
            font=ctk.CTkFont(size=11), justify='center')
        self.image_lbl.pack(padx=10, pady=(0, 6))
        # Клик по самой картинке — послушать звук того, что сейчас показано
        # (видео и так молча крутится в превью — тут именно про звук).
        self.image_lbl.configure(cursor='pointinghand')
        self.image_lbl.bind('<Button-1>', lambda e: self._toggle_listen())

        # Простой статус прослушивания — без полоски уровня звука (была,
        # но убрали ради простоты/надёжности: три процесса на канал вместо
        # одного, поток фактически читался дважды. Рабочая PCM-версия с
        # индикатором осталась в git-истории, если понадобится вернуть).
        self.audio_status_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=9), text_color=c['text_muted'])
        self.audio_status_lbl.pack(padx=10, pady=(0, 8), anchor='w')
        self._poll_id = self.after(round(1000 / FPS), self._poll_frame)

    def show(self, name: str, url: str, headers: Optional[dict] = None, audio_url: Optional[str] = None,
             start_offset: float = 0, duration_limit: Optional[float] = None):
        self._stop_stream()
        self._stop_listening()
        with self._frame_lock:
            self._generation += 1
            generation = self._generation
            self._pending_frame = None
        self._last_frame_at = time.monotonic()
        self._stalled = False
        self._preview_name = name
        self._url = url
        self._headers = headers
        self._audio_url = audio_url
        self._start_offset = start_offset
        self._duration_limit = duration_limit
        self.playback_seconds = None
        self.name_lbl.configure(text=name)
        self.status_lbl.configure(text="Подключение…")
        self.image_lbl.configure(text="", image=get_icon('tv', Config.COLORS['text_muted'], 32))
        self._frame_count = 0

        self._stream = LiveThumbnailStream(url, headers, fps=FPS, on_frame=lambda jb: self._on_frame(jb, generation),
                                            width=self._image_size[0] * 2, start_offset=start_offset,
                                            duration_limit=duration_limit,
                                            on_timed_frame=(lambda jb, seconds: self._on_frame(jb, generation, seconds))
                                            if duration_limit is not None else None)
        self._stream.start()
        # 8с исторически давали ложные "Нет сигнала" на медленных, но рабочих
        # потоках (та же проблема уже ловили в core/snapshot.py — там таймаут
        # пришлось поднять с 6 до 10с). Тут поток ещё и идёт в -re темпе
        # реального эфира, так что запас нужен больше.
        self.after(15000, lambda: self._check_connected(generation))

        if self._want_audio:
            # Звук уже был включён на предыдущем канале — продолжаем слушать
            # СРАЗУ на новом, без повторного клика по картинке (раньше любое
            # переключение канала звук молча гасило).
            self._start_listening()

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

    def _on_frame(self, jpeg_bytes: bytes, generation: int, seconds=None):
        # No Tk calls from the decoder thread. Keep only the newest frame:
        # a busy/hidden window must not accumulate callbacks or hold up ffmpeg.
        with self._frame_lock:
            if self._running and generation == self._generation:
                self._pending_frame = ((jpeg_bytes, generation, seconds) if seconds is not None
                                       else (jpeg_bytes, generation))

    def _poll_frame(self):
        if not self._running:
            return
        with self._frame_lock:
            frame = self._pending_frame
            self._pending_frame = None
        if frame is not None:
            self._display(*frame)
        elif self._stream is not None and self._stream.completed:
            self.status_lbl.configure(text='Просмотр завершён')
            self._stop_listening()
            self._stream = None
            self._frame_count = 0
        elif self._frame_count and time.monotonic() - self._last_frame_at > 15 and not self._stalled:
            self._stalled = True
            self.status_lbl.configure(text="Переподключение…")
            logger.warning(f"PreviewPanel: '{self._preview_name}' — кадры не поступают 15с")
        self._poll_id = self.after(round(1000 / FPS), self._poll_frame)

    def _display(self, jpeg_bytes: bytes, generation: int, seconds=None):
        if not self._running or generation != self._generation or not self.winfo_exists():
            return
        try:
            img = to_ctk_image(jpeg_bytes, self._image_size, crop=not self._contain_image)
        except Exception as e:
            logger.debug(f"PreviewPanel: ошибка кадра: {e}")
            return
        self.image_lbl.configure(image=img, text="")
        self.image_lbl._img_ref = img
        self._frame_count += 1
        if seconds is not None:
            self.playback_seconds = seconds
        self._last_frame_at = time.monotonic()
        # Не дёргаем текстовый лейбл на каждый кадр — просто держим отметку "в эфире".
        if self._frame_count == 1 or self._stalled:
            self._stalled = False
            self.status_lbl.configure(text=self.playing_label)
            logger.info(f"PreviewPanel: '{self._preview_name}' — превью в эфире (получен первый кадр)")

    def _stop_stream(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream = None

    def _toggle_listen(self):
        """Единственный способ включить/выключить звук — клик по картинке.
        Явное намерение пользователя запоминаем в _want_audio, чтобы оно
        пережило последующие переключения канала (см. show())."""
        if not self._url:
            return
        if self._listening:
            self._want_audio = False
            self._stop_listening()
        else:
            self._want_audio = True
            self._start_listening()

    def _start_listening(self):
        self._stop_listening()  # на случай, если что-то уже играло — не запускать второй ffplay поверх первого
        # audio_url — для каналов, у которых видео и звук на CDN лежат
        # раздельными HLS-рендициями (см. 'Россия 24'/'Россия К' в
        # core/m3u_parser.py) — в self._url тогда только видео, слушать
        # там нечего.
        listen_url = self._audio_url or self._url
        position = (self.playback_seconds or 0) if self._duration_limit is not None else 0
        self._listener = AudioListener(listen_url, self._headers, label=self._preview_name,
                                       start_offset=self._start_offset + position,
                                       duration_limit=max(.1, self._duration_limit-position)
                                       if self._duration_limit is not None else None)
        self._listener.start()
        self._listening = True
        self.listen_icon.pack(side='right', padx=(0, 6), before=self.status_lbl)
        self.audio_status_lbl.configure(text="Прослушивание — клик ещё раз, чтобы выключить",
                                         text_color=Config.COLORS['accent'])
        logger.info(f"PreviewPanel: включено прослушивание '{self._preview_name}'")

    def _stop_listening(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._listening = False
        self.listen_icon.pack_forget()
        self.audio_status_lbl.configure(text="")

    def stop(self):
        with self._frame_lock:
            self._running = False
            self._pending_frame = None
        self.after_cancel(self._poll_id)
        self._stop_stream()
        self._stop_listening()

    def clear(self, message='Выберите канал слева'):
        """Stop output without destroying the panel or its UI polling loop."""
        self._stop_stream()
        self._stop_listening()
        with self._frame_lock:
            self._generation += 1
            self._pending_frame = None
        self._url = ''
        self._frame_count = 0
        self.playback_seconds = None
        self.status_lbl.configure(text='')
        self.image_lbl.configure(image=None, text=message)

    def set_audio_enabled(self, enabled: bool):
        self._want_audio = bool(enabled)
        if enabled and self._url and not self._listening:
            self._start_listening()
        elif not enabled:
            self._stop_listening()
