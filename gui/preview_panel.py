# gui/preview_panel.py
"""Встроенная (НЕ всплывающая) панель почти-реалтайм превью канала —
непрерывный низкочастотный видеопоток, а не снимок раз в несколько секунд,
прямо в макете окна приложения.

Не всплывающее окно — раньше превью открывалось как отдельный
CTkToplevel(overrideredirect=True), но безрамочные окна на macOS
периодически не отрисовываются нормально (известная особенность Tk/Aqua).
Постоянная панель в layout снимает эту проблему полностью."""
import time
from typing import Optional

import customtkinter as ctk

from core.audio_listen import AudioListener, NO_AUDIO_TIMEOUT
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
        self._url = ''
        self._headers: Optional[dict] = None
        self._audio_url: Optional[str] = None
        # Прослушивание — отдельный ffplay-процесс поверх того же URL, что и
        # превью, никак не связанный с самой записью (см. core/audio_listen.py,
        # та же логика уже проверена в gui/recording_monitor.py).
        self._listener: Optional[AudioListener] = None
        self._listening = False
        self._muted = False
        # Уровень звука (0..1) — пишется из фонового потока AudioListener
        # (см. on_level), читается таймером _tick_level в Tk-потоке. Простое
        # чтение/запись float в CPython атомарно, отдельная синхронизация
        # тут не нужна — это просто индикатор, не критичное состояние.
        self._level = 0.0
        self._level_display = 0.0
        self._listen_start = 0.0
        self._audio_status_shown = ''

        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=10, pady=(8, 4))
        self.name_lbl = ctk.CTkLabel(header, text="Превью", font=ctk.CTkFont(size=12, weight='bold'),
                                      text_color=c['text_primary'])
        self.name_lbl.pack(side='left')
        self.mute_btn = ctk.CTkButton(
            header, text="", width=22, height=22, corner_radius=Config.RADIUS_SM,
            fg_color='transparent', hover_color=c['bg_hover'],
            image=get_icon('volume', c['text_secondary'], 14), command=self._toggle_mute)
        self.mute_btn.pack(side='right')
        self.status_lbl = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'])
        self.status_lbl.pack(side='right', padx=(0, 6))

        self.image_lbl = ctk.CTkLabel(
            self, text="Клик по логотипу канала\nпокажет, что там сейчас",
            width=IMG_SIZE[0], height=IMG_SIZE[1], corner_radius=8,
            fg_color=c['bg_tertiary'], text_color=c['text_muted'],
            font=ctk.CTkFont(size=11), justify='center')
        self.image_lbl.pack(padx=10, pady=(0, 4))
        # Клик по самой картинке — послушать звук того, что сейчас показано
        # (видео и так молча крутится в превью — тут именно про звук).
        self.image_lbl.configure(cursor='pointinghand')
        self.image_lbl.bind('<Button-1>', lambda e: self._toggle_listen())

        # Тонкая полоска уровня звука — единственный способ УВИДЕТЬ, что
        # звук реально идёт, а не просто предположить это по тому, что
        # ffplay не упал (что раньше и было единственным сигналом).
        self.level_bar = ctk.CTkProgressBar(
            self, height=4, corner_radius=2, width=IMG_SIZE[0],
            fg_color=c['bg_tertiary'], progress_color=c['accent'])
        self.level_bar.set(0)
        self.level_bar.pack(padx=10, pady=(0, 2))
        # Без этого клик по видео не давал вообще НИКАКОЙ обратной связи, пока
        # либо не пойдёт первый звук, либо (на медленном/нестабильном потоке —
        # реально бывает 15-20+с на некоторых CDN) не истечёт таймаут в логе.
        # Пользователю это неотличимо от "ничего не произошло" — теперь статус
        # виден сразу же по клику, а не только постфактум в логе.
        self.audio_status_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=9), text_color=c['text_muted'])
        self.audio_status_lbl.pack(padx=10, pady=(0, 8), anchor='w')

    def show(self, name: str, url: str, headers: Optional[dict] = None, audio_url: Optional[str] = None):
        self._stop_stream()
        self._stop_listening()
        self._generation += 1
        generation = self._generation
        self._preview_name = name
        self._url = url
        self._headers = headers
        self._audio_url = audio_url
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

    def _toggle_listen(self):
        if not self._url:
            return
        if self._listening:
            self._stop_listening()
            return
        self._stop_listening()
        # audio_url — для каналов, у которых видео и звук на CDN лежат
        # раздельными HLS-рендициями (см. 'Россия 24'/'Россия К' в
        # core/m3u_parser.py) — в self._url тогда только видео, слушать
        # там нечего.
        listen_url = self._audio_url or self._url
        self._listener = AudioListener(listen_url, self._headers, label=self._preview_name,
                                        on_level=self._on_level_raw)
        self._listener.start()
        self._listening = True
        self._listen_start = time.monotonic()
        self._muted = False  # клик — явное намерение услышать звук, снимаем mute
        self.mute_btn.configure(image=get_icon('volume', Config.COLORS['text_secondary'], 14))
        self.audio_status_lbl.configure(text="Звук: подключение…", text_color=Config.COLORS['text_muted'])
        logger.info(f"PreviewPanel: включено прослушивание '{self._preview_name}'")
        self._tick_level()

    def _stop_listening(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._listening = False
        self._level = 0.0
        self._audio_status_shown = ''
        self.audio_status_lbl.configure(text="")

    def _on_level_raw(self, level: float):
        # Вызывается из фонового потока AudioListener — просто копим
        # значение, читает и сглаживает его таймер _tick_level в Tk-потоке.
        self._level = level

    def _tick_level(self):
        c = Config.COLORS
        if not self.winfo_exists():
            return
        if not self._listening:
            self.level_bar.set(0)
            return
        # Экспоненциальное сглаживание — иначе полоска дёргается рывками
        # на каждый обрывок PCM вместо плавного VU-metering.
        self._level_display += (self._level - self._level_display) * 0.5
        self.level_bar.set(self._level_display)

        got_audio = bool(self._listener and self._listener._got_audio)
        elapsed = time.monotonic() - self._listen_start
        if got_audio:
            new_status = "Звук: идёт" if self._level_display > 0.02 else "Звук: идёт (тихо)"
            color = c['accent']
        elif elapsed > NO_AUDIO_TIMEOUT:
            new_status = "Звук: не пришёл (см. лог)"
            color = c['red']
        else:
            new_status = "Звук: подключение…"
            color = c['text_muted']
        if new_status != self._audio_status_shown:
            self._audio_status_shown = new_status
            self.audio_status_lbl.configure(text=new_status, text_color=color)

        self.after(80, self._tick_level)

    def _toggle_mute(self):
        c = Config.COLORS
        self._muted = not self._muted
        if self._muted:
            self._stop_listening()
            self.mute_btn.configure(image=get_icon('volume_off', c['red'], 14))
        else:
            self.mute_btn.configure(image=get_icon('volume', c['text_secondary'], 14))

    def stop(self):
        self._running = False
        self._stop_stream()
        self._stop_listening()
