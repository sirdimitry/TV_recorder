# gui/recording_monitor.py
"""Отдельное окно-монитор: сетка живых миниатюр всех активных записей —
для случаев, когда одновременно пишется много каналов (условно 1-16) и
хочется видеть общую картину сразу, а не листать список по одной строке.

Обычное окно с рамкой (НЕ overrideredirect) — тот баг был именно в
безрамочных Toplevel на macOS, обычные окна отрисовываются нормально.
Кадры не захватывает сам — читает то, что Recorder и так собирает
централизованно на каждую активную задачу (core/recorder.py), поэтому
открытие монитора не удваивает нагрузку поверх панели записей."""
import customtkinter as ctk

from core.recorder import Recorder, RecordingTask
from core.snapshot import to_ctk_image
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

TILE_IMG_SIZE = (220, 124)
POLL_MS = 1000


class RecordingMonitorWindow(ctk.CTkToplevel):
    """Одно окно на всё приложение — повторный вызов просто поднимает его."""

    _instance = None

    @classmethod
    def show(cls, root, recorder: Recorder):
        if cls._instance is not None and cls._instance.winfo_exists():
            cls._instance.lift()
            cls._instance.focus_force()
            return
        cls._instance = RecordingMonitorWindow(root, recorder)

    def __init__(self, root, recorder: Recorder):
        super().__init__(root)
        self.recorder = recorder
        self._running = True
        self.tile_widgets = {}

        c = Config.COLORS
        self.title("Мониторинг записей")
        self.geometry("900x600")
        self.minsize(420, 300)
        self.configure(fg_color=c['bg_primary'])

        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=16, pady=(14, 6))
        ctk.CTkLabel(header, text="Мониторинг активных записей", font=ctk.CTkFont(size=16, weight='bold'),
                     text_color=c['text_primary']).pack(side='left')
        self.count_lbl = ctk.CTkLabel(header, text="", font=ctk.CTkFont(size=12), text_color=c['text_secondary'])
        self.count_lbl.pack(side='left', padx=8)

        self.grid_frame = ctk.CTkScrollableFrame(self, fg_color='transparent')
        self.grid_frame.pack(fill='both', expand=True, padx=12, pady=(0, 12))

        self.recorder.set_ui_callback(self._on_recorder_update)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._rebuild()
        self._poll()

    def _on_recorder_update(self):
        if self._running:
            self.after(0, self._maybe_rebuild)

    def _maybe_rebuild(self):
        if not self._running:
            return
        current_ids = {t.task_id for t in self.recorder.get_all_tasks()}
        if current_ids != set(self.tile_widgets.keys()):
            self._rebuild()

    def _rebuild(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.tile_widgets.clear()

        tasks = self.recorder.get_all_tasks()
        self.count_lbl.configure(text=f"({len(tasks)})")

        if not tasks:
            ctk.CTkLabel(self.grid_frame, text="Нет активных записей", font=ctk.CTkFont(size=12),
                         text_color=Config.COLORS['text_muted']).pack(pady=30)
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
        tile = ctk.CTkFrame(self.grid_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM)
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
        timer_lbl.pack(side='right')

        self.tile_widgets[task.task_id] = {
            'tile': tile, 'img_lbl': img_lbl, 'name_lbl': name_lbl, 'timer_lbl': timer_lbl,
            'snapshot_seq': 0,
        }

    def _poll(self):
        if not self._running:
            return
        tasks = {t.task_id: t for t in self.recorder.get_all_tasks()}
        for tid, widgets in self.tile_widgets.items():
            task = tasks.get(tid)
            if not task:
                continue
            widgets['timer_lbl'].configure(text=task.format_elapsed_time())
            if task.last_snapshot and task.snapshot_seq != widgets['snapshot_seq']:
                try:
                    img = to_ctk_image(task.last_snapshot, TILE_IMG_SIZE)
                    widgets['img_lbl'].configure(image=img)
                    widgets['img_lbl']._img_ref = img
                    widgets['snapshot_seq'] = task.snapshot_seq
                except Exception as e:
                    logger.debug(f"RecordingMonitor: ошибка снимка: {e}")
        self.after(POLL_MS, self._poll)

    def _close(self):
        self._running = False
        self.recorder.remove_ui_callback(self._on_recorder_update)
        RecordingMonitorWindow._instance = None
        self.destroy()
