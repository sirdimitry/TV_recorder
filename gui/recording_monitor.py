# gui/recording_monitor.py
"""Монитор: сетка живых миниатюр всех активных записей —
для случаев, когда одновременно пишется много каналов (условно 1-16) и
хочется видеть общую картину сразу, а не листать список по одной строке.

Монитор миниатюр открывается отдельным окном.
Кадры не захватывает сам — читает то, что Recorder и так собирает
централизованно на каждую активную задачу (core/recorder.py), поэтому
открытие монитора не удваивает нагрузку поверх панели записей."""
from typing import Optional

import customtkinter as ctk

from core.audio_listen import AudioListener
from core.recorder import Recorder, RecordingTask
from core.snapshot import to_ctk_image
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

TILE_IMG_SIZE = (220, 124)
POLL_MS = 250
MODE_LABELS = {'Экономичный': 'economy', 'Обычный': 'normal', 'Выключен': 'off'}


class RecordingMonitorWindow(ctk.CTkFrame):
    """Окно живых миниатюр активных записей."""

    _instance = None

    @classmethod
    def show(cls, root, recorder: Recorder):
        if cls._instance is not None and cls._instance.winfo_exists():
            cls._instance.master.deiconify()
            cls._instance.master.lift()
            return
        window = ctk.CTkToplevel(root)
        window.title('TV Recorder — монитор записей')
        window.geometry('820x520')
        window.minsize(520, 320)
        cls._instance = cls(window, recorder)
        monitor = cls._instance
        monitor.pack(fill='both', expand=True)

        def close():
            monitor._close()
            window.destroy()

        window.protocol('WM_DELETE_WINDOW', close)

    @classmethod
    def close_if_open(cls):
        if cls._instance is not None and cls._instance.winfo_exists():
            window = cls._instance.master
            cls._instance._close()
            window.destroy()

    def __init__(self, root, recorder: Recorder):
        super().__init__(root)
        self.recorder = recorder
        self._running = True
        self._visible = True
        self._owns_snapshots = False
        self._refresh_pending = False
        self._poll_id = None
        self.tile_widgets = {}
        # Прослушивание — отдельный ffplay-процесс поверх того же URL, что и
        # запись (см. core/audio_listen.py). НЕ трогает саму запись никак —
        # если этот процесс упадёт или мы его убьём, на файле записи это
        # не отражается вообще.
        self._listener: Optional[AudioListener] = None
        self._listening_task_id: Optional[str] = None
        self._muted = False

        c = Config.COLORS
        self.configure(fg_color=c['bg_primary'])

        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text="Мониторинг активных записей", font=ctk.CTkFont(size=16, weight='bold'),
                     text_color=c['text_primary']).pack(side='left')
        self.count_lbl = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=12), text_color=c['text_secondary'])
        self.count_lbl.pack(side='left', padx=8)
        self.mute_btn = ctk.CTkButton(
            header, text="", width=30, height=30, corner_radius=Config.RADIUS_SM,
            fg_color=c['bg_tertiary'], hover_color=c['bg_hover'],
            image=get_icon('volume', c['text_secondary'], 16), command=self._toggle_mute)
        self.mute_btn.pack(side='right')
        self.mode_picker = ctk.CTkOptionMenu(header, values=list(MODE_LABELS), width=140,
                                            command=self._set_mode)
        self.mode_picker.set(next(label for label, mode in MODE_LABELS.items()
                                  if mode == self.recorder.snapshot_mode))
        self.mode_picker.pack(side='right', padx=8)


        self.grid_frame = ctk.CTkScrollableFrame(self, fg_color='transparent')
        self.grid_frame.pack(fill='both', expand=True, padx=12, pady=(0, 12))

        self.recorder.set_ui_callback(self._on_recorder_update)
        self.recorder.acquire_snapshot_consumer()
        self._owns_snapshots = True
        self.bind('<Map>', self._mapped, add='+')
        self.bind('<Unmap>', self._unmapped, add='+')

        self._rebuild()
        self._poll()

    def _set_mode(self, label):
        self.recorder.set_snapshot_mode(MODE_LABELS[label])
        if MODE_LABELS[label] == 'off':
            self._clear_images()

    def _mapped(self, event):
        if event.widget is self:
            self._set_visible(True)

    def _unmapped(self, event):
        if event.widget is self:
            self._set_visible(False)

    def _clear_images(self, message='Миниатюры выключены'):
        for widgets in self.tile_widgets.values():
            widgets['img_lbl'].configure(image=None, text=message)
            widgets['img_lbl']._img_ref = None
            widgets['snapshot_seq'] = -1

    def _set_visible(self, visible):
        self._visible = visible and self._running
        if self._visible and not self._owns_snapshots:
            self.recorder.acquire_snapshot_consumer()
            self._owns_snapshots = True
            self._maybe_rebuild()
            if self.recorder.snapshot_mode != 'off':
                self._clear_images('Подключение…')
        elif not self._visible and self._owns_snapshots:
            self.recorder.release_snapshot_consumer()
            self._owns_snapshots = False
            self._stop_listening()
            self._clear_images()

    def _on_recorder_update(self):
        self._refresh_pending = True

    def _monitor_tasks(self):
        return [task for task in self.recorder.get_all_tasks() if task.is_recording or task.recovering]

    def _maybe_rebuild(self):
        self._refresh_pending = False
        if not self._running or not self._visible:
            return
        current_ids = {t.task_id for t in self._monitor_tasks()}
        if current_ids != set(self.tile_widgets.keys()):
            self._rebuild()

    def _rebuild(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.tile_widgets.clear()

        tasks = self._monitor_tasks()
        self.count_lbl.configure(text=f"({len(tasks)})")

        if self._listening_task_id and self._listening_task_id not in {t.task_id for t in tasks}:
            # Слушали именно эту запись, а её уже нет в списке (закончилась/
            # остановлена) — ffplay-процессу больше нечего играть.
            self._stop_listening()

        if not tasks:
            ctk.CTkLabel(self.grid_frame, text="Нет активных записей", font=ctk.CTkFont(size=12),
                         text_color=Config.COLORS['text_muted']).grid(row=0, column=0, pady=30)
            return

        columns = self._columns_for(len(tasks))
        for col in range(columns):
            self.grid_frame.columnconfigure(col, weight=1)

        for i, task in enumerate(tasks):
            row, col = divmod(i, columns)
            self._add_tile(task, row, col)

    @staticmethod
    def _columns_for(count: int) -> int:
        if count <= 2:
            return max(count, 1)
        if count <= 4:
            return 2
        if count <= 9:
            return 3
        return 4

    def _add_tile(self, task: RecordingTask, row: int, col: int):
        c = Config.COLORS
        listening = task.task_id == self._listening_task_id
        # Прослушивание берёт звук напрямую из task.stream_url — для записи
        # экрана браузера (is_screen_capture) там лежит адрес СТРАНИЦЫ, а не
        # медиапотока, ffplay на нём ничего не сыграет. Такие плитки просто
        # не кликабельны для звука, без ложной кнопки.
        listenable = not task.is_screen_capture
        tile = ctk.CTkFrame(self.grid_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM,
                             border_width=2, border_color=c['accent'] if listening else c['bg_secondary'])
        tile.grid(row=row, column=col, padx=6, pady=6, sticky='nsew')

        img_lbl = ctk.CTkLabel(tile, text="", width=TILE_IMG_SIZE[0], height=TILE_IMG_SIZE[1],
                                corner_radius=8, fg_color=c['bg_tertiary'],
                                image=get_icon('tv', c['text_muted'], 32))
        img_lbl.pack(padx=8, pady=(8, 4))

        info = ctk.CTkFrame(tile, fg_color='transparent')
        info.pack(fill='x', padx=8, pady=(0, 8))
        source_icon = 'bolt' if task.source == 'manual' else 'calendar'
        ctk.CTkLabel(info, text="", image=get_icon(source_icon, c['text_muted'], 12)).pack(side='left', padx=(0, 4))
        name_lbl = ctk.CTkLabel(info, text=task.channel_name, font=ctk.CTkFont(size=12, weight='bold'),
                                 text_color=c['text_primary'], anchor='w')
        name_lbl.pack(side='left', fill='x', expand=True)
        timer_lbl = ctk.CTkLabel(info, text=task.format_elapsed_time(), font=('Menlo', 11),
                                  text_color=c['accent'])
        timer_lbl.pack(side='right', padx=(6, 0))
        sound_color = c['accent'] if listening else c['text_muted']
        sound_lbl = ctk.CTkLabel(info, text="", image=get_icon('volume', sound_color, 14))
        sound_lbl.pack(side='right')

        if listenable:
            for w in (tile, img_lbl, sound_lbl):
                w.configure(cursor='pointinghand')
                w.bind('<Button-1>', lambda e, tid=task.task_id: self._toggle_listen(tid))

        self.tile_widgets[task.task_id] = {
            'tile': tile, 'img_lbl': img_lbl, 'name_lbl': name_lbl, 'timer_lbl': timer_lbl,
            'sound_lbl': sound_lbl, 'snapshot_seq': 0,
        }

    def _toggle_listen(self, task_id: str):
        """Клик по плитке — послушать звук ИМЕННО этой записи. Отдельный
        ffplay поверх того же URL, что и запись (см. core/audio_listen.py) —
        сама запись (task.process, файл на диске) этим не затрагивается
        никак: слушаем параллельно, не читая и не трогая файл записи."""
        if task_id == self._listening_task_id:
            self._stop_listening()
            return
        task = next((t for t in self.recorder.get_all_tasks() if t.task_id == task_id), None)
        if task is None or task.is_screen_capture:
            return
        self._stop_listening()
        # Если видео и звук у источника — раздельные рендиции (см.
        # 'Россия 24'/'Россия К'), звук лежит в task.audio_url — в
        # task.stream_url там только видео, слушать там нечего.
        listen_url = task.audio_url or task.stream_url
        self._listener = AudioListener(listen_url, task.headers, label=task.channel_name)
        self._listener.start()
        self._listening_task_id = task_id
        self._muted = False  # клик по плитке — явное намерение услышать звук, снимаем mute
        self.mute_btn.configure(image=get_icon('volume', Config.COLORS['text_secondary'], 16))
        logger.info(f"RecordingMonitor: включено прослушивание '{task.channel_name}'")
        self._refresh_listen_indicators()

    def _stop_listening(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._listening_task_id is not None:
            logger.info("RecordingMonitor: прослушивание остановлено")
        self._listening_task_id = None
        self._refresh_listen_indicators()

    def _refresh_listen_indicators(self):
        c = Config.COLORS
        for tid, widgets in self.tile_widgets.items():
            listening = tid == self._listening_task_id
            widgets['tile'].configure(border_color=c['accent'] if listening else c['bg_secondary'])
            widgets['sound_lbl'].configure(image=get_icon('volume', c['accent'] if listening else c['text_muted'], 14))

    def _toggle_mute(self):
        """Общий выключатель прослушивания — на запись никак не влияет,
        только глушит/останавливает то, что сейчас играет в этом окне."""
        c = Config.COLORS
        self._muted = not self._muted
        if self._muted:
            self._stop_listening()
            self.mute_btn.configure(image=get_icon('volume_off', c['red'], 16))
            logger.info("RecordingMonitor: прослушивание выключено (mute)")
        else:
            self.mute_btn.configure(image=get_icon('volume', c['text_secondary'], 16))

    def _poll(self):
        if not self._running:
            return
        if not self._visible:
            self._poll_id = self.after(1000, self._poll)
            return
        if self._refresh_pending:
            self._maybe_rebuild()
        profile = self.recorder.get_snapshot_profile()
        tasks = {t.task_id: t for t in self._monitor_tasks()}
        for tid, widgets in self.tile_widgets.items():
            task = tasks.get(tid)
            if not task:
                continue
            widgets['timer_lbl'].configure(text=task.format_elapsed_time())
            if task.last_snapshot and task.snapshot_seq != widgets['snapshot_seq']:
                try:
                    img = to_ctk_image(task.last_snapshot, TILE_IMG_SIZE)
                    widgets['img_lbl'].configure(image=img, text='')
                    widgets['img_lbl']._img_ref = img
                    widgets['snapshot_seq'] = task.snapshot_seq
                except Exception as e:
                    logger.debug(f"RecordingMonitor: ошибка снимка: {e}")
        delay = max(POLL_MS, min(1000, round(1000/profile.fps))) if profile else 1000
        self._poll_id = self.after(delay, self._poll)

    def _close(self):
        if not self._running:
            return
        self._running = False
        self._set_visible(False)
        self._stop_listening()
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
        self.recorder.remove_ui_callback(self._on_recorder_update)
        RecordingMonitorWindow._instance = None
        self.destroy()
