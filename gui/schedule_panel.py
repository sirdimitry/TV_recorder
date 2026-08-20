# gui/schedule_panel.py
import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Dict, Callable, Optional
from datetime import datetime, timedelta
from core.storage import Storage
from utils.config import Config
from utils.logger import logger


class TimeEntry(ttk.Entry):
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
            # Не переносим курсор в конец: так можно заменить только минуты.
            new_cursor_position = digits_before_cursor
            if digits_before_cursor > 2:
                new_cursor_position += 1
            self.after_idle(lambda: self.icursor(min(new_cursor_position, len(formatted))))

    def set_time(self, value: str):
        self.value.set(value)


class SchedulePanel(ttk.Frame):
    """Панель управления расписанием записей"""
    
    def __init__(self, parent, on_schedule_changed: Optional[Callable] = None):
        super().__init__(parent)
        self.colors = Config.COLORS
        self.storage = Storage()
        self.on_schedule_changed = on_schedule_changed
        
        self._setup_ui()
        self._set_current_time()
        self.refresh()
    
    def _setup_ui(self):
        # Заголовок
        header = ttk.Label(self, text="РАСПИСАНИЕ", font=('Inter', 11, 'bold'))
        header.pack(fill='x', padx=10, pady=(10, 5))
        
        # Форма добавления/редактирования
        form_frame = ttk.LabelFrame(self, text="Новая запись")
        form_frame.pack(fill='x', padx=10, pady=5)
        
        # Канал
        channel_frame = ttk.Frame(form_frame)
        channel_frame.pack(fill='x', padx=10, pady=5)
        ttk.Label(channel_frame, text="Канал:").pack(side='left')
        self.channel_combo = ttk.Combobox(channel_frame, state='readonly', width=30)
        self.channel_combo.pack(side='left', padx=5, fill='x', expand=True)
        self.channel_combo.bind('<MouseWheel>', self._scroll_channel_selection)
        self.channel_combo.bind('<Button-4>', lambda event: self._scroll_channel_selection(event, -1))
        self.channel_combo.bind('<Button-5>', lambda event: self._scroll_channel_selection(event, 1))
        
        # Время
        time_frame = ttk.Frame(form_frame)
        time_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(time_frame, text="С:").pack(side='left')
        self.start_time = TimeEntry(time_frame, width=8)
        self.start_time.pack(side='left', padx=5)
        
        ttk.Label(time_frame, text="До:").pack(side='left', padx=(10, 0))
        self.end_time = TimeEntry(time_frame, width=8)
        self.end_time.pack(side='left', padx=5)

        self.date_label = ttk.Label(time_frame, foreground=self.colors['text_secondary'])
        self.date_label.pack(side='left', padx=(12, 0))
        
        # Дни недели
        days_frame = ttk.Frame(form_frame)
        days_frame.pack(fill='x', padx=10, pady=5)
        ttk.Label(days_frame, text="Дни:").pack(side='left')
        
        self.day_vars = {}
        day_names = [('Пн', 0), ('Вт', 1), ('Ср', 2), ('Чт', 3), ('Пт', 4), ('Сб', 5), ('Вс', 6)]
        for name, idx in day_names:
            var = tk.BooleanVar()
            self.day_vars[idx] = var
            ttk.Checkbutton(days_frame, text=name, variable=var).pack(side='left', padx=2)
        
        # Кнопки формы
        btn_frame = ttk.Frame(form_frame)
        btn_frame.pack(fill='x', padx=10, pady=10)
        
        self.btn_add = ttk.Button(btn_frame, text="+ Добавить", command=self._add_schedule)
        self.btn_add.pack(side='left', padx=2)
        
        self.btn_update = ttk.Button(btn_frame, text="↻ Обновить", command=self._update_schedule, state='disabled')
        self.btn_update.pack(side='left', padx=2)
        
        self.btn_delete_form = ttk.Button(btn_frame, text="🗑 Удалить", command=self._delete_from_form, state='disabled')
        self.btn_delete_form.pack(side='left', padx=2)
        
        self.btn_clear_form = ttk.Button(btn_frame, text="Очистить", command=self._clear_form)
        self.btn_clear_form.pack(side='right', padx=2)
        
        # Таблица расписания
        table_frame = ttk.Frame(self)
        table_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        columns = ('channel', 'time', 'days', 'active')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', height=10)
        
        self.tree.heading('channel', text='Канал')
        self.tree.heading('time', text='Время')
        self.tree.heading('days', text='Дни')
        self.tree.heading('active', text='Активно')
        
        self.tree.column('channel', width=150)
        self.tree.column('time', width=120)
        self.tree.column('days', width=150)
        self.tree.column('active', width=80)
        
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        # Обработчики событий таблицы
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Double-1>', self._on_tree_double_click)
    
    def refresh(self):
        """Обновляет таблицу и комбобокс каналов"""
        # Обновляем комбобокс каналов
        channels = self.storage.get_channels()
        self.channel_combo['values'] = [ch['name'] for ch in channels]
        
        # Обновляем таблицу
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        schedule = self.storage.get_schedule()
        day_names_short = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        
        for i, item in enumerate(schedule):
            # БЕЗОПАСНОЕ ПРЕОБРАЗОВАНИЕ ДНЕЙ (исправление ошибки TypeError)
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
            active_str = "✅" if item.get('enabled', True) else "❌"
            
            self.tree.insert('', 'end', iid=i, values=(
                item.get('channel_name', ''),
                f"{item.get('start_time', '')} — {item.get('end_time', '')}",
                days_str,
                active_str
            ))
    
    def _set_current_time(self):
        """Подставляет системные дату, время и сегодняшний день недели."""
        now = datetime.now()
        start = now
        end = start

        self.start_time.set_time(start.strftime("%H:%M"))
        self.end_time.set_time(end.strftime("%H:%M"))

        day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        self.date_label.config(text=f"Сегодня: {day_names[now.weekday()]}, {now:%d.%m.%Y}")
        
        # Выбираем сегодняшний день недели
        today_idx = now.weekday()  # 0=Пн, 6=Вс
        for idx, var in self.day_vars.items():
            var.set(idx == today_idx)

    def _scroll_channel_selection(self, event, direction=None):
        """Меняет канал колёсиком мыши или жестом двумя пальцами."""
        if not self.channel_combo['values']:
            return 'break'
        if direction is None:
            direction = -1 if event.delta > 0 else 1
        current = self.channel_combo.current()
        if current < 0:
            current = 0 if direction > 0 else len(self.channel_combo['values']) - 1
        else:
            current = max(0, min(len(self.channel_combo['values']) - 1, current + direction))
        self.channel_combo.current(current)
        return 'break'
    
    def _add_schedule(self):
        channel_name = self.channel_combo.get()
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
        """Обновляет выбранную запись"""
        selected = self.tree.selection()
        if not selected:
            return
        
        index = int(selected[0])
        channel_name = self.channel_combo.get()
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
        
        self.storage.update_schedule_item(index, item)
        self.refresh()
        self._clear_form()
        
        if self.on_schedule_changed:
            self.on_schedule_changed()
        
        logger.info(f"Расписание обновлено: индекс {index}")
    
    def _delete_from_form(self):
        """Удаляет выбранную запись из формы"""
        selected = self.tree.selection()
        if not selected:
            return
        
        index = int(selected[0])
        self.storage.delete_schedule_item(index)
        self.refresh()
        self._clear_form()
        
        if self.on_schedule_changed:
            self.on_schedule_changed()
        
        logger.info(f"Расписание удалено: индекс {index}")
    
    def _on_tree_select(self, event):
        """При выборе записи заполняет форму"""
        selected = self.tree.selection()
        if not selected:
            self._clear_form_buttons()
            return
        
        index = int(selected[0])
        schedule = self.storage.get_schedule()
        if index >= len(schedule):
            return
        
        item = schedule[index]
        
        # Заполняем форму
        self.channel_combo.set(item.get('channel_name', ''))
        self.start_time.set_time(item.get('start_time', ''))
        self.end_time.set_time(item.get('end_time', ''))
        
        for idx, var in self.day_vars.items():
            # Безопасная проверка дня недели при загрузке из файла
            try:
                val = int(idx) if isinstance(idx, str) else idx
                var.set(val in [int(d) for d in item.get('days', [])])
            except (ValueError, TypeError):
                var.set(False)
        
        # Включаем кнопки обновления и удаления
        self.btn_add.config(state='disabled')
        self.btn_update.config(state='normal')
        self.btn_delete_form.config(state='normal')
    
    def _on_tree_double_click(self, event):
        """Двойной клик = редактирование"""
        self._on_tree_select(event)

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
        """Очищает форму и сбрасывает кнопки"""
        self.channel_combo.set('')
        self.start_time.set_time('')
        self.end_time.set_time('')
        for var in self.day_vars.values():
            var.set(False)
        
        self._clear_form_buttons()
        self._set_current_time()
    
    def _clear_form_buttons(self):
        self.btn_add.config(state='normal')
        self.btn_update.config(state='disabled')
        self.btn_delete_form.config(state='disabled')
