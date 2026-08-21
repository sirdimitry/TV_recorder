# gui/schedule_panel.py
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Dict, List, Optional
from datetime import datetime

import customtkinter as ctk

from core.storage import Storage
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger


class TimeEntry(ctk.CTkEntry):
    """Поле времени: пользователь вводит только цифры, двоеточие добавляется само."""

    def __init__(self, parent, **kwargs):
        self.value = tk.StringVar()
        self._formatting = False
        super().__init__(parent, textvariable=self.value, **kwargs)
        self.value.trace_add('write', self._format_value)

    def _format_value(self, *_):
        if self._formatting:
            return
        raw_value = self.value.get()
        try:
            cursor_position = self.index(tk.INSERT)
        except tk.TclError:
            cursor_position = len(raw_value)
        digits_before_cursor = sum(char.isdigit() for char in raw_value[:cursor_position])
        digits = ''.join(char for char in raw_value if char.isdigit())[:4]
        if len(digits) <= 2:
            formatted = digits
        else:
            formatted = f"{digits[:2]}:{digits[2:]}"
        if formatted != raw_value:
            self._formatting = True
            self.value.set(formatted)
            self._formatting = False
            new_cursor_position = digits_before_cursor
            if digits_before_cursor > 2:
                new_cursor_position += 1
            self.after_idle(lambda: self.icursor(min(new_cursor_position, len(formatted))))

    def set_time(self, value: str):
        self.value.set(value)


class SchedulePanel(ctk.CTkFrame):
    """Панель управления расписанием записей"""

    ACTIVE_COLUMN = '#4'

    def __init__(self, parent, on_schedule_changed: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.colors = Config.COLORS
        self.storage = Storage()
        self.on_schedule_changed = on_schedule_changed
        self._channel_names: List[str] = []
        self.run_status: Dict[int, str] = {}

        self._setup_ui()
        self._set_current_time()
        self.refresh()

    def _setup_ui(self):
        c = self.colors

        header = ctk.CTkLabel(self, text="РАСПИСАНИЕ", font=ctk.CTkFont(size=12, weight='bold'),
                               text_color=c['text_secondary'])
        header.pack(fill='x', padx=14, pady=(12, 6), anchor='w')

        form_frame = ctk.CTkFrame(self, fg_color=c['bg_tertiary'], corner_radius=Config.RADIUS_SM)
        form_frame.pack(fill='x', padx=10, pady=(0, 8))

        ctk.CTkLabel(form_frame, text="Новая запись", font=ctk.CTkFont(size=11, weight='bold'),
                     text_color=c['text_secondary']).pack(anchor='w', padx=12, pady=(10, 4))

        # Канал
        channel_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        channel_frame.pack(fill='x', padx=12, pady=4)
        ctk.CTkLabel(channel_frame, text="Канал:", text_color=c['text_secondary'], width=50, anchor='w').pack(side='left')

        self.channel_var = tk.StringVar(value='')
        self.channel_combo = ctk.CTkOptionMenu(channel_frame, values=[''], variable=self.channel_var,
                                                height=30, corner_radius=Config.RADIUS_SM,
                                                fg_color=c['bg_secondary'], button_color=c['bg_secondary'],
                                                button_hover_color=c['bg_hover'], text_color=c['text_primary'])
        self.channel_combo.pack(side='left', padx=6, fill='x', expand=True)
        self.channel_combo.bind('<MouseWheel>', self._scroll_channel_selection)
        self.channel_combo.bind('<Button-4>', lambda event: self._scroll_channel_selection(event, -1))
        self.channel_combo.bind('<Button-5>', lambda event: self._scroll_channel_selection(event, 1))

        # Время
        time_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        time_frame.pack(fill='x', padx=12, pady=4)

        ctk.CTkLabel(time_frame, text="С:", text_color=c['text_secondary']).pack(side='left')
        self.start_time = TimeEntry(time_frame, width=64, height=30, corner_radius=Config.RADIUS_SM,
                                     fg_color=c['bg_secondary'], border_color=c['border'],
                                     text_color=c['text_primary'])
        self.start_time.pack(side='left', padx=6)

        ctk.CTkLabel(time_frame, text="До:", text_color=c['text_secondary']).pack(side='left', padx=(10, 0))
        self.end_time = TimeEntry(time_frame, width=64, height=30, corner_radius=Config.RADIUS_SM,
                                   fg_color=c['bg_secondary'], border_color=c['border'],
                                   text_color=c['text_primary'])
        self.end_time.pack(side='left', padx=6)

        self.date_label = ctk.CTkLabel(time_frame, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'])
        self.date_label.pack(side='left', padx=(12, 0))

        # Дни недели
        days_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        days_frame.pack(fill='x', padx=12, pady=6)
        ctk.CTkLabel(days_frame, text="Дни:", text_color=c['text_secondary'], width=50, anchor='w').pack(side='left')

        self.day_vars = {}
        day_names = [('Пн', 0), ('Вт', 1), ('Ср', 2), ('Чт', 3), ('Пт', 4), ('Сб', 5), ('Вс', 6)]
        for name, idx in day_names:
            var = tk.BooleanVar()
            self.day_vars[idx] = var
            ctk.CTkCheckBox(days_frame, text=name, variable=var, width=20, checkbox_width=18, checkbox_height=18,
                             font=ctk.CTkFont(size=11), fg_color=c['accent'], hover_color=c['accent_hover'],
                             text_color=c['text_primary'], border_color=c['border']).pack(side='left', padx=3)

        # Кнопки формы
        btn_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        btn_frame.pack(fill='x', padx=12, pady=(6, 12))

        self.btn_add = ctk.CTkButton(btn_frame, text="Добавить", image=get_icon('plus', c['accent_text'], 12),
                                      compound='left', height=30, corner_radius=Config.RADIUS_SM,
                                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text'],
                                      command=self._add_schedule)
        self.btn_add.pack(side='left', padx=(0, 4))

        self.btn_update = ctk.CTkButton(btn_frame, text="Обновить", image=get_icon('refresh', c['text_primary'], 12),
                                         compound='left', height=30, corner_radius=Config.RADIUS_SM,
                                         fg_color=c['bg_secondary'], hover_color=c['bg_hover'],
                                         text_color=c['text_primary'], state='disabled',
                                         command=self._update_schedule)
        self.btn_update.pack(side='left', padx=4)

        self.btn_delete_form = ctk.CTkButton(btn_frame, text="Удалить", image=get_icon('trash', c['text_primary'], 12),
                                              compound='left', height=30, corner_radius=Config.RADIUS_SM,
                                              fg_color=c['bg_secondary'], hover_color=c['bg_hover'],
                                              text_color=c['text_primary'], state='disabled',
                                              command=self._delete_from_form)
        self.btn_delete_form.pack(side='left', padx=4)

        self.btn_clear_form = ctk.CTkButton(btn_frame, text="Очистить", height=30, corner_radius=Config.RADIUS_SM,
                                             fg_color='transparent', hover_color=c['bg_hover'],
                                             text_color=c['text_secondary'], command=self._clear_form)
        self.btn_clear_form.pack(side='right')

        # Таблица расписания
        table_frame = ctk.CTkFrame(self, fg_color='transparent')
        table_frame.pack(fill='both', expand=True, padx=10, pady=(0, 8))

        columns = ('channel', 'time', 'days', 'active', 'status')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', height=8)

        self.tree.heading('channel', text='Канал')
        self.tree.heading('time', text='Время')
        self.tree.heading('days', text='Дни')
        self.tree.heading('active', text='Активно')
        self.tree.heading('status', text='Статус')

        self.tree.column('channel', width=140)
        self.tree.column('time', width=110)
        self.tree.column('days', width=140)
        self.tree.column('active', width=70, anchor='center')
        self.tree.column('status', width=110, anchor='center')

        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Button-1>', self._on_tree_click, add='+')

    def refresh(self):
        """Обновляет таблицу и список каналов"""
        channels = self.storage.get_channels()
        self._channel_names = [ch['name'] for ch in channels]
        self.channel_combo.configure(values=self._channel_names or [''])
        if self.channel_var.get() not in self._channel_names:
            self.channel_var.set(self._channel_names[0] if self._channel_names else '')

        for item in self.tree.get_children():
            self.tree.delete(item)

        schedule = self.storage.get_schedule()
        day_names_short = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

        for i, item in enumerate(schedule):
            raw_days = item.get('days', [])
            safe_days = []
            for d in raw_days:
                try:
                    val = int(d)
                    if 0 <= val < 7:
                        safe_days.append(val)
                except (ValueError, TypeError):
                    continue

            days_str = ', '.join([day_names_short[d] for d in safe_days])
            active_str = "✓" if item.get('enabled', True) else "—"

            self.tree.insert('', 'end', iid=i, values=(
                item.get('channel_name', ''),
                f"{item.get('start_time', '')} — {item.get('end_time', '')}",
                days_str,
                active_str,
                self._format_status(self.run_status.get(i))
            ))

    @staticmethod
    def _format_status(status: Optional[str]) -> str:
        return {
            'checking': '… проверка',
            'recording': '● идёт',
            'completed': '✓ готово',
            'failed': '✕ ошибка',
        }.get(status, '—')

    def update_run_status(self, index: int, status: str):
        """Вызывается планировщиком (в т.ч. из фонового потока APScheduler)
        при смене статуса конкретной строки: 'checking' / 'recording' /
        'completed' / 'failed'. Обновляет только одну ячейку, не всю таблицу."""
        self.after(0, lambda: self._apply_run_status(index, status))

    def _apply_run_status(self, index: int, status: str):
        self.run_status[index] = status
        item_id = str(index)
        if self.tree.exists(item_id):
            values = list(self.tree.item(item_id, 'values'))
            if len(values) >= 5:
                values[4] = self._format_status(status)
                self.tree.item(item_id, values=values)

    def _reindex_run_status_after_delete(self, deleted_index: int):
        """Сдвигает сохранённые статусы после удаления строки по индексу."""
        shifted: Dict[int, str] = {}
        for idx, status in self.run_status.items():
            if idx < deleted_index:
                shifted[idx] = status
            elif idx > deleted_index:
                shifted[idx - 1] = status
        self.run_status = shifted

    def _set_current_time(self):
        """Подставляет системные дату, время и сегодняшний день недели."""
        now = datetime.now()
        self.start_time.set_time(now.strftime("%H:%M"))
        self.end_time.set_time(now.strftime("%H:%M"))

        day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        self.date_label.configure(text=f"Сегодня: {day_names[now.weekday()]}, {now:%d.%m.%Y}")

        today_idx = now.weekday()
        for idx, var in self.day_vars.items():
            var.set(idx == today_idx)

    def _scroll_channel_selection(self, event, direction=None):
        """Меняет канал колёсиком мыши или жестом двумя пальцами."""
        if not self._channel_names:
            return 'break'
        if direction is None:
            direction = -1 if event.delta > 0 else 1
        current = self.channel_var.get()
        try:
            idx = self._channel_names.index(current)
        except ValueError:
            idx = 0 if direction > 0 else len(self._channel_names) - 1
        else:
            idx = max(0, min(len(self._channel_names) - 1, idx + direction))
        self.channel_var.set(self._channel_names[idx])
        return 'break'

    def _add_schedule(self):
        channel_name = self.channel_var.get()
        start = self.start_time.get().strip()
        end = self.end_time.get().strip()

        if not self._valid_form(channel_name, start, end):
            return

        days = [idx for idx, var in self.day_vars.items() if var.get()]
        if not days:
            messagebox.showwarning("Внимание", "Выберите хотя бы один день")
            return

        item = {
            'channel_name': channel_name,
            'start_time': start,
            'end_time': end,
            'days': days,
            'enabled': True
        }

        self.storage.add_schedule_item(item)
        self.refresh()
        self._clear_form()

        if self.on_schedule_changed:
            self.on_schedule_changed()

        logger.info(f"Расписание добавлено: {channel_name} {start}-{end}")

    def _update_schedule(self):
        selected = self.tree.selection()
        if not selected:
            return

        index = int(selected[0])
        channel_name = self.channel_var.get()
        start = self.start_time.get().strip()
        end = self.end_time.get().strip()

        if not self._valid_form(channel_name, start, end):
            return

        days = [idx for idx, var in self.day_vars.items() if var.get()]

        item = {
            'channel_name': channel_name,
            'start_time': start,
            'end_time': end,
            'days': days,
            'enabled': True
        }

        self.run_status.pop(index, None)
        self.storage.update_schedule_item(index, item)
        self.refresh()
        self._clear_form()

        if self.on_schedule_changed:
            self.on_schedule_changed()

        logger.info(f"Расписание обновлено: индекс {index}")

    def _delete_from_form(self):
        selected = self.tree.selection()
        if not selected:
            return

        index = int(selected[0])
        self._reindex_run_status_after_delete(index)
        self.storage.delete_schedule_item(index)
        self.refresh()
        self._clear_form()

        if self.on_schedule_changed:
            self.on_schedule_changed()

        logger.info(f"Расписание удалено: индекс {index}")

    def _on_tree_click(self, event):
        """Клик по колонке 'Активно' переключает запись без входа в режим редактирования."""
        region = self.tree.identify('region', event.x, event.y)
        if region != 'cell':
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if col == self.ACTIVE_COLUMN and row:
            index = int(row)
            self.storage.toggle_schedule_item(index)
            self.refresh()
            if self.on_schedule_changed:
                self.on_schedule_changed()
            logger.info(f"Расписание переключено: индекс {index}")
            return 'break'

    def _on_tree_select(self, event):
        selected = self.tree.selection()
        if not selected:
            self._clear_form_buttons()
            return

        index = int(selected[0])
        schedule = self.storage.get_schedule()
        if index >= len(schedule):
            return

        item = schedule[index]

        self.channel_var.set(item.get('channel_name', ''))
        self.start_time.set_time(item.get('start_time', ''))
        self.end_time.set_time(item.get('end_time', ''))

        for idx, var in self.day_vars.items():
            try:
                val = int(idx) if isinstance(idx, str) else idx
                var.set(val in [int(d) for d in item.get('days', [])])
            except (ValueError, TypeError):
                var.set(False)

        self.btn_add.configure(state='disabled')
        self.btn_update.configure(state='normal')
        self.btn_delete_form.configure(state='normal')

    @staticmethod
    def _is_valid_time(value: str) -> bool:
        try:
            datetime.strptime(value, '%H:%M')
            return True
        except ValueError:
            return False

    def _valid_form(self, channel_name: str, start: str, end: str) -> bool:
        if not channel_name or not start or not end:
            messagebox.showwarning("Внимание", "Заполните все поля")
            return False
        if not self._is_valid_time(start) or not self._is_valid_time(end):
            messagebox.showwarning("Внимание", "Введите время в формате ЧЧ:ММ, например 09:30")
            return False
        if start == end:
            messagebox.showwarning(
                "Внимание",
                "Измените время окончания: одинаковое время означало бы запись на 24 часа."
            )
            return False
        return True

    def _clear_form(self):
        self.channel_var.set(self._channel_names[0] if self._channel_names else '')
        self.start_time.set_time('')
        self.end_time.set_time('')
        for var in self.day_vars.values():
            var.set(False)

        self._clear_form_buttons()
        self._set_current_time()

    def _clear_form_buttons(self):
        self.btn_add.configure(state='normal')
        self.btn_update.configure(state='disabled')
        self.btn_delete_form.configure(state='disabled')
