# gui/schedule_panel.py
import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Dict, Callable, Optional
from datetime import datetime, timedelta
from core.storage import Storage
from utils.config import Config
from utils.logger import logger


class SchedulePanel(ttk.Frame):
    """Панель управления расписанием записей"""
    
    def __init__(self, parent, on_schedule_changed: Optional[Callable] = None):
        super().__init__(parent)
        self.colors = Config.COLORS
        self.storage = Storage()
        self.on_schedule_changed = on_schedule_changed
        
        self._setup_ui()
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
        
        # Время
        time_frame = ttk.Frame(form_frame)
        time_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(time_frame, text="С:").pack(side='left')
        self.start_time = ttk.Entry(time_frame, width=8)
        self.start_time.pack(side='left', padx=5)
        
        ttk.Label(time_frame, text="До:").pack(side='left', padx=(10, 0))
        self.end_time = ttk.Entry(time_frame, width=8)
        self.end_time.pack(side='left', padx=5)
        
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
        """Устанавливает текущее время + 5 минут"""
        now = datetime.now()
        start = now + timedelta(minutes=5)
        end = start + timedelta(hours=1)
        
        self.start_time.delete(0, 'end')
        self.start_time.insert(0, start.strftime("%H:%M"))
        
        self.end_time.delete(0, 'end')
        self.end_time.insert(0, end.strftime("%H:%M"))
        
        # Выбираем сегодняшний день недели
        today_idx = now.weekday()  # 0=Пн, 6=Вс
        for idx, var in self.day_vars.items():
            var.set(idx == today_idx)
    
    def _add_schedule(self):
        channel_name = self.channel_combo.get()
        start = self.start_time.get().strip()
        end = self.end_time.get().strip()
        
        if not channel_name or not start or not end:
            messagebox.showwarning("Внимание", "Заполните все поля")
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
        
        if not channel_name or not start or not end:
            messagebox.showwarning("Внимание", "Заполните все поля")
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
        self.start_time.delete(0, 'end')
        self.start_time.insert(0, item.get('start_time', ''))
        self.end_time.delete(0, 'end')
        self.end_time.insert(0, item.get('end_time', ''))
        
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
    
    def _clear_form(self):
        """Очищает форму и сбрасывает кнопки"""
        self.channel_combo.set('')
        self.start_time.delete(0, 'end')
        self.end_time.delete(0, 'end')
        for var in self.day_vars.values():
            var.set(False)
        
        self._clear_form_buttons()
        self._set_current_time()
    
    def _clear_form_buttons(self):
        self.btn_add.config(state='normal')
        self.btn_update.config(state='disabled')
        self.btn_delete_form.config(state='disabled')