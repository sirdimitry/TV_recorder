# gui/recording_panel.py
import os
import platform
import subprocess
import threading
import unicodedata
from pathlib import Path
from tkinter import messagebox
from typing import Dict, Optional

import customtkinter as ctk
from PIL import Image

from core.recorder import Recorder, RecordingTask
from core.storage import Storage
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

STATE_ACCENT = {
    'recording': 'red',
    'paused': 'yellow',
    'finalizing': 'yellow',
    'completed': 'green',
    'failed': 'red',
}


class RecordingPanel(ctk.CTkFrame):
    """Панель активных записей: карточки с таймером и статусом."""

    def __init__(self, parent, recorder: Recorder):
        super().__init__(parent, fg_color='transparent')
        self.colors = Config.COLORS
        self.recorder = recorder
        self.storage = Storage()

        self.recorder.set_ui_callback(self._schedule_refresh)

        self.task_widgets: Dict[str, dict] = {}
        self.empty_label: Optional[ctk.CTkLabel] = None

        self._setup_ui()
        self.refresh()

    def _schedule_refresh(self):
        self.after(0, self._update_timers_only)

    def _setup_ui(self):
        c = self.colors
        header_frame = ctk.CTkFrame(self, fg_color='transparent')
        header_frame.pack(fill='x', padx=14, pady=(12, 6))

        ctk.CTkLabel(header_frame, text="АКТИВНЫЕ ЗАПИСИ", font=ctk.CTkFont(size=12, weight='bold'),
                     text_color=c['text_secondary']).pack(side='left')

        self.count_label = ctk.CTkLabel(header_frame, text="(0)", font=ctk.CTkFont(size=11),
                                         text_color=c['text_muted'])
        self.count_label.pack(side='left', padx=4)

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color='transparent')
        self.list_frame.pack(fill='both', expand=True, padx=6, pady=(0, 6))

    def refresh(self):
        current_ids = {t.task_id for t in self.recorder.get_all_tasks()}

        for tid in list(self.task_widgets.keys()):
            if tid not in current_ids:
                self.task_widgets[tid]['row'].destroy()
                del self.task_widgets[tid]

        tasks = self.recorder.get_all_tasks()
        self.count_label.configure(text=f"({len(tasks)})")

        if not tasks:
            if self.empty_label is None:
                self.empty_label = ctk.CTkLabel(self.list_frame, text="Нет активных записей",
                                                 font=ctk.CTkFont(size=11), text_color=self.colors['text_muted'])
                self.empty_label.pack(pady=18)
        elif self.empty_label is not None:
            self.empty_label.destroy()
            self.empty_label = None

        for task in tasks:
            if task.task_id not in self.task_widgets:
                self._add_task_row(task)

    def _task_state(self, task: RecordingTask) -> str:
        if task.is_paused:
            return 'paused'
        if task.is_recording:
            return 'recording'
        if task.success is True:
            return 'completed'
        if task.success is False:
            return 'failed'
        return 'finalizing'

    def _update_timers_only(self):
        tasks = {t.task_id: t for t in self.recorder.get_all_tasks()}

        current_ids = set(tasks.keys())
        widget_ids = set(self.task_widgets.keys())

        if current_ids != widget_ids:
            self.refresh()
            return

        for tid, widgets in self.task_widgets.items():
            task = tasks.get(tid)
            if not task:
                continue

            widgets['timer'].configure(text=task.format_elapsed_time())
            widgets['period'].configure(text=task.format_recording_period())

            state = self._task_state(task)
            accent = self.colors[STATE_ACCENT[state]]
            widgets['accent_bar'].configure(fg_color=accent)

            state_text = {
                'paused': 'Paused', 'recording': 'Recording',
                'completed': 'Completed', 'failed': 'Failed', 'finalizing': 'Finalizing',
            }[state]
            widgets['result'].configure(text=state_text,
                                         text_color=self.colors['text_secondary'] if state == 'recording' else accent)

            widgets['status_icon'].configure(image=get_icon(
                {'paused': 'pause', 'recording': 'record', 'completed': 'record',
                 'failed': 'close', 'finalizing': 'record'}[state], accent, 12))

            pause_icon = 'play' if task.is_paused else 'pause'
            widgets['btn_pause'].configure(image=get_icon(pause_icon, self.colors['text_primary'], 12))
            button_state = 'normal' if task.is_recording else 'disabled'
            widgets['btn_pause'].configure(state=button_state)
            widgets['btn_stop'].configure(state=button_state)

            if not task.is_recording and 'btn_open' not in widgets:
                btn_open = self._icon_button(widgets['actions'], 'folder', lambda t=task: self._open_file(t))
                btn_open.pack(side='left', padx=1, before=widgets['btn_del'])
                widgets['btn_open'] = btn_open

    def _icon_button(self, parent, icon_name, command, color=None):
        c = self.colors
        return ctk.CTkButton(parent, text="", image=get_icon(icon_name, color or c['text_secondary'], 14),
                              width=30, height=30, corner_radius=Config.RADIUS_SM, fg_color='transparent',
                              hover_color=c['bg_active'], command=command)

    ROW_HEIGHT = 48
    LOGO_SIZE = 30

    def _load_task_logo(self, channel_name: str, logo_lbl: ctk.CTkLabel):
        from utils.logo_cache import LogoCache

        def worker():
            channels = {c['name']: c for c in self.storage.get_channels()}
            channel = channels.get(channel_name)
            logo_url = channel.get('logo_url', '') if channel else ''

            image = None
            if logo_url:
                cache = LogoCache()
                logo_path = cache.get_logo_path(channel_name, logo_url)
                if logo_path:
                    try:
                        pil_img = Image.open(logo_path).convert('RGBA')
                        pil_img.thumbnail((self.LOGO_SIZE, self.LOGO_SIZE), Image.LANCZOS)
                        canvas = Image.new('RGBA', (self.LOGO_SIZE, self.LOGO_SIZE), (0, 0, 0, 0))
                        offset = ((self.LOGO_SIZE - pil_img.width) // 2, (self.LOGO_SIZE - pil_img.height) // 2)
                        canvas.paste(pil_img, offset, pil_img)
                        image = ctk.CTkImage(light_image=canvas, dark_image=canvas,
                                              size=(self.LOGO_SIZE, self.LOGO_SIZE))
                    except Exception as e:
                        logger.debug(f"RecordingPanel: ошибка отображения логотипа {channel_name}: {e}")

            def apply():
                if not logo_lbl.winfo_exists():
                    return
                if image is not None:
                    logo_lbl.configure(image=image)
                    logo_lbl._logo_ref = image
                else:
                    logo_lbl.configure(image=get_icon('tv', self.colors['text_muted'], 16))

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _add_task_row(self, task: RecordingTask):
        c = self.colors
        row = ctk.CTkFrame(self.list_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM,
                            height=self.ROW_HEIGHT)
        row.pack(fill='x', pady=3, padx=2)
        row.pack_propagate(False)

        accent_bar = ctk.CTkFrame(row, fg_color=c['red'], width=4, corner_radius=2)
        accent_bar.pack(side='left', fill='y', padx=(0, 8), pady=6)

        logo_lbl = ctk.CTkLabel(row, text="", width=self.LOGO_SIZE, height=self.LOGO_SIZE,
                                 corner_radius=6, fg_color=c['bg_tertiary'])
        logo_lbl.pack(side='left', padx=(2, 8), pady=6)
        self._load_task_logo(task.channel_name, logo_lbl)

        source_icon = 'bolt' if task.source == 'manual' else 'calendar'
        source_lbl = ctk.CTkLabel(row, text="", image=get_icon(source_icon, c['text_muted'], 10))
        source_lbl.place(in_=logo_lbl, relx=1.0, rely=1.0, x=-1, y=-1, anchor='se')

        status_icon = ctk.CTkLabel(row, text="", image=get_icon('record', c['red'], 12))
        status_icon.pack(side='left', padx=(0, 6), pady=8)

        name_lbl = ctk.CTkLabel(row, text=task.channel_name, font=ctk.CTkFont(size=12, weight='bold'),
                                 text_color=c['text_primary'])
        name_lbl.pack(side='left', padx=(0, 10), pady=8)

        timer_lbl = ctk.CTkLabel(row, text=task.format_elapsed_time(), font=('Menlo', 12),
                                  text_color=c['accent'], width=64, anchor='w')
        timer_lbl.pack(side='left', padx=(0, 10), pady=8)

        period_lbl = ctk.CTkLabel(row, text=task.format_recording_period(), font=ctk.CTkFont(size=11),
                                   text_color=c['text_secondary'])
        period_lbl.pack(side='left', padx=(0, 8), pady=8)

        result_lbl = ctk.CTkLabel(row, text="Recording", font=ctk.CTkFont(size=11),
                                   text_color=c['text_secondary'])
        result_lbl.pack(side='left', pady=8)

        actions = ctk.CTkFrame(row, fg_color='transparent')
        actions.pack(side='right', padx=6, pady=6)

        btn_del = self._icon_button(actions, 'close', lambda t=task: self._remove_task(t))
        btn_del.pack(side='right', padx=1)

        btn_stop = self._icon_button(actions, 'stop', lambda t=task: self._stop_task(t))
        btn_stop.pack(side='right', padx=1)

        btn_pause = self._icon_button(actions, 'pause', lambda t=task: self._toggle_pause(t))
        btn_pause.pack(side='right', padx=1)

        self.task_widgets[task.task_id] = {
            'row': row,
            'accent_bar': accent_bar,
            'status_icon': status_icon,
            'timer': timer_lbl,
            'period': period_lbl,
            'result': result_lbl,
            'actions': actions,
            'btn_pause': btn_pause,
            'btn_stop': btn_stop,
            'btn_del': btn_del,
        }

    def _toggle_pause(self, task: RecordingTask):
        self.recorder.pause_recording(task.task_id)

    def _stop_task(self, task: RecordingTask):
        self.recorder.stop_recording(task.task_id)

    def _open_file(self, task: RecordingTask):
        try:
            file_path = self._find_recording_file(task.output_path)
            if not file_path:
                messagebox.showerror(
                    "File not found",
                    "The recording file is not available. This recording may have failed."
                )
                logger.error(f"RecordingPanel: file not found: {task.output_path}")
                return
            if platform.system() == 'Darwin':
                subprocess.Popen(['open', '-R', str(file_path)])
            elif platform.system() == 'Windows':
                subprocess.Popen(['explorer', '/select,', os.path.normpath(str(file_path))])
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(str(file_path))])
            logger.info(f"RecordingPanel: file revealed: {file_path}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку с файлом:\n{e}")

    @staticmethod
    def _find_recording_file(output_path: str):
        """Handles macOS Unicode filename normalisation when revealing old files."""
        path = Path(output_path)
        if path.is_file():
            return path
        if not path.parent.is_dir():
            return None
        target_name = unicodedata.normalize('NFC', path.name)
        for candidate in path.parent.iterdir():
            if unicodedata.normalize('NFC', candidate.name) == target_name and candidate.is_file():
                return candidate
        return None

    def _remove_task(self, task: RecordingTask):
        self.recorder.remove_task(task.task_id)
