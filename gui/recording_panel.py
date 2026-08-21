# gui/recording_panel.py
import tkinter as tk
from tkinter import ttk, messagebox
import os
import subprocess
import platform
import unicodedata
from typing import Optional, Dict
from core.recorder import Recorder, RecordingTask
from utils.config import Config
from utils.logger import logger


class RecordingPanel(ttk.Frame):
    """Компактная панель активных записей без мигания"""
    
    def __init__(self, parent, recorder: Recorder):
        super().__init__(parent)
        self.colors = Config.COLORS
        self.recorder = recorder
        
        # Привязываем callback через специальный метод рекордера
        self.recorder.set_ui_callback(self._schedule_refresh)
        
        self.task_widgets: Dict[str, dict] = {}
        
        self._setup_ui()
        self.refresh()
    
    def _schedule_refresh(self):
        """Планирует обновление UI в главном потоке"""
        self.after(0, self._update_timers_only)
    
    def _setup_ui(self):
        header_frame = ttk.Frame(self)
        header_frame.pack(fill='x', padx=5, pady=(5, 2))
        
        ttk.Label(header_frame, text="АКТИВНЫЕ ЗАПИСИ", 
                 font=('Inter', 10, 'bold')).pack(side='left')
        
        self.count_label = ttk.Label(header_frame, text="(0)", 
                                     font=('Inter', 9),
                                     foreground=self.colors['text_secondary'])
        self.count_label.pack(side='left', padx=4)
        
        self.list_frame = ttk.Frame(self)
        self.list_frame.pack(fill='both', expand=True, padx=5, pady=2)
    
    def refresh(self):
        """Полное обновление структуры (при добавлении/удалении)"""
        current_ids = {t.task_id for t in self.recorder.get_all_tasks()}
        
        # Удаляем виджеты отсутствующих задач
        for tid in list(self.task_widgets.keys()):
            if tid not in current_ids:
                self.task_widgets[tid]['row'].destroy()
                del self.task_widgets[tid]
        
        tasks = self.recorder.get_all_tasks()
        self.count_label.config(text=f"({len(tasks)})")
        
        # Управление заглушкой
        has_placeholder = any(
            isinstance(w, ttk.Label) and w.cget('text') == "Нет активных записей"
            for w in self.list_frame.winfo_children()
        )
        
        if not tasks and not self.task_widgets:
            if not has_placeholder:
                empty_label = ttk.Label(self.list_frame, text="Нет активных записей",
                                       font=('Inter', 9),
                                       foreground=self.colors['text_secondary'])
                empty_label.pack(pady=15)
        elif has_placeholder:
            for w in self.list_frame.winfo_children():
                if isinstance(w, ttk.Label) and w.cget('text') == "Нет активных записей":
                    w.destroy()
        
        # Добавляем новые задачи
        for task in tasks:
            if task.task_id not in self.task_widgets:
                self._add_task_row(task)
    
    def _update_timers_only(self):
        """Обновляет только динамические данные без пересоздания виджетов"""
        tasks = {t.task_id: t for t in self.recorder.get_all_tasks()}
        
        # Сначала проверяем, нужны ли структурные изменения
        current_ids = set(tasks.keys())
        widget_ids = set(self.task_widgets.keys())
        
        if current_ids != widget_ids:
            self.refresh()
            return
        
        # Обновляем существующие виджеты
        for tid, widgets in self.task_widgets.items():
            task = tasks.get(tid)
            if not task:
                continue
            
            widgets['timer'].config(text=task.format_elapsed_time())
            widgets['period'].config(text=task.format_recording_period())
            
            if task.is_paused:
                widgets['status'].config(text="⏸", foreground='#fbbf24')
                widgets['result'].config(text="Paused", foreground='#fbbf24')
            elif task.is_recording:
                widgets['status'].config(text="●", foreground='#ff4444')
                widgets['result'].config(text="Recording", foreground=self.colors['text_secondary'])
            elif task.success is True:
                widgets['status'].config(text="✓", foreground='#a6e3a1')
                widgets['result'].config(text="Completed", foreground='#a6e3a1')
            elif task.success is False:
                widgets['status'].config(text="✕", foreground=self.colors['red'])
                widgets['result'].config(text="Failed", foreground=self.colors['red'])
            else:
                widgets['status'].config(text="…", foreground=self.colors['yellow'])
                widgets['result'].config(text="Finalizing", foreground=self.colors['yellow'])
            
            pause_text = "▶" if task.is_paused else "⏸"
            widgets['btn_pause'].config(text=pause_text)
            button_state = 'normal' if task.is_recording else 'disabled'
            widgets['btn_pause'].config(state=button_state)
            widgets['btn_stop'].config(state=button_state)
            
            # Кнопка открытия файла для завершенных
            if not task.is_recording and 'btn_open' not in widgets:
                btn_open = ttk.Button(widgets['row'], text="📂", width=2,
                                     command=lambda t=task: self._open_file(t))
                btn_open.pack(side='right', padx=1, before=widgets['btn_del'])
                widgets['btn_open'] = btn_open
    
    def _add_task_row(self, task: RecordingTask):
        row = ttk.Frame(self.list_frame)
        row.pack(fill='x', pady=1)
        
        status_lbl = ttk.Label(row, text="●", foreground='#ff4444', font=('Arial', 10))
        status_lbl.pack(side='left', padx=(0, 4))
        
        # Мигание индикатора
        if task.is_recording and not task.is_paused:
            def blink():
                if task.task_id not in self.task_widgets:
                    return
                t = self.recorder.tasks.get(task.task_id)
                if not t or not t.is_recording or t.is_paused:
                    return
                current = status_lbl.cget('foreground')
                new_color = '#550000' if current == '#ff4444' else '#ff4444'
                status_lbl.config(foreground=new_color)
                row.after(500, blink)
            blink()
        
        source_icon = "⚡" if task.source == "manual" else "📅"
        name_lbl = ttk.Label(row, text=f"{source_icon} {task.channel_name}",
                            font=('Inter', 9, 'bold'))
        name_lbl.pack(side='left', padx=(0, 8))
        
        timer_lbl = ttk.Label(row, text=task.format_elapsed_time(),
                             font=('Menlo', 10),
                             foreground=self.colors['accent'],
                             width=8)
        timer_lbl.pack(side='left', padx=(0, 8))

        period_lbl = ttk.Label(row, text=task.format_recording_period(),
                               font=('Inter', 9), foreground=self.colors['text_secondary'])
        period_lbl.pack(side='left', padx=(0, 5))

        result_lbl = ttk.Label(row, text="Recording", font=('Inter', 9),
                               foreground=self.colors['text_secondary'])
        result_lbl.pack(side='left')
        
        btn_pause = ttk.Button(row, text="⏸", width=2,
                              command=lambda t=task: self._toggle_pause(t))
        btn_pause.pack(side='right', padx=1)
        
        btn_stop = ttk.Button(row, text="⏹", width=2,
                             command=lambda t=task: self._stop_task(t))
        btn_stop.pack(side='right', padx=1)
        
        btn_del = ttk.Button(row, text="✕", width=2,
                            command=lambda t=task: self._remove_task(t))
        btn_del.pack(side='right', padx=1)
        
        self.task_widgets[task.task_id] = {
            'row': row,
            'status': status_lbl,
            'timer': timer_lbl,
            'period': period_lbl,
            'result': result_lbl,
            'btn_pause': btn_pause,
            'btn_stop': btn_stop,
            'btn_del': btn_del
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
                # -R открывает Finder и выделяет файл, не запуская проигрывание.
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
