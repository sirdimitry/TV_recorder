# gui/schedule_panel.py
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Dict, List, Optional
from datetime import datetime, timedelta

import customtkinter as ctk

from core.link_resolver import resolve_link
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
        # Последние 2 введённые цифры — минуты/секунды (второй сегмент),
        # всё, что перед ними — часы/минуты (первый сегмент), без
        # ограничения в 2 цифры: полю нужно показывать и "10:05" (время на
        # часах), и "200:44" (позиция в 3-часовом ролике для "Мои ссылки" —
        # см. gui/app_window.py: _add_link_dialog). Раньше первый сегмент
        # был жёстко зафиксирован в 2 цифры ("93:0" при вводе "930"), из-за
        # чего длинные позиции физически нельзя было ввести.
        digits = ''.join(char for char in raw_value if char.isdigit())[:6]
        if len(digits) <= 2:
            formatted = digits
        else:
            formatted = f"{digits[:-2]}:{digits[-2:]}"
        if formatted != raw_value:
            self._formatting = True
            self.value.set(formatted)
            self._formatting = False
            new_cursor_position = digits_before_cursor
            if len(digits) > 2 and digits_before_cursor > len(digits) - 2:
                new_cursor_position += 1
            self.after_idle(lambda: self.icursor(min(new_cursor_position, len(formatted))))

    def set_time(self, value: str):
        self.value.set(value)


class SchedulePanel(ctk.CTkFrame):
    """Панель управления расписанием записей"""

    ACTIVE_COLUMN = '#5'

    def __init__(self, parent, on_schedule_changed: Optional[Callable] = None,
                 on_record_now: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.colors = Config.COLORS
        self.storage = Storage()
        self.on_schedule_changed = on_schedule_changed
        self.on_record_now = on_record_now
        self._channel_names: List[str] = []
        self._link_names: List[str] = []
        self._duration_detect_generation = 0
        self.run_status: Dict[int, str] = {}

        self._setup_ui()
        self._apply_time_defaults()
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

        # Источник: канал из списка IPTV или своя ссылка
        source_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        source_frame.pack(fill='x', padx=12, pady=(4, 0))
        ctk.CTkLabel(source_frame, text="Источник:", text_color=c['text_secondary'], width=50, anchor='w').pack(side='left')

        self.source_type_var = tk.StringVar(value='channel')
        self.source_segmented = ctk.CTkSegmentedButton(
            source_frame, values=['Канал', 'Ссылка'],
            command=self._on_source_type_changed,
            fg_color=c['bg_secondary'], selected_color=c['accent'], selected_hover_color=c['accent_hover'],
            unselected_color=c['bg_secondary'], unselected_hover_color=c['bg_hover'],
            text_color=c['text_primary'], text_color_disabled=c['text_muted'], height=28)
        self.source_segmented.set('Канал')
        self.source_segmented.pack(side='left', padx=6, fill='x', expand=True)

        # Канал / ссылка
        channel_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        channel_frame.pack(fill='x', padx=12, pady=4)
        self.channel_label = ctk.CTkLabel(channel_frame, text="Канал:", text_color=c['text_secondary'], width=50, anchor='w')
        self.channel_label.pack(side='left')

        self.channel_var = tk.StringVar(value='')
        self.channel_combo = ctk.CTkOptionMenu(channel_frame, values=[''], variable=self.channel_var,
                                                height=30, corner_radius=Config.RADIUS_SM,
                                                fg_color=c['bg_secondary'], button_color=c['bg_secondary'],
                                                button_hover_color=c['bg_hover'], text_color=c['text_primary'],
                                                command=lambda _v: self._maybe_detect_link_duration())
        self.channel_combo.pack(side='left', padx=6, fill='x', expand=True)
        self.channel_combo.bind('<MouseWheel>', self._scroll_channel_selection)
        self.channel_combo.bind('<Button-4>', lambda event: self._scroll_channel_selection(event, -1))
        self.channel_combo.bind('<Button-5>', lambda event: self._scroll_channel_selection(event, 1))

        # Время
        time_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        time_frame.pack(fill='x', padx=12, pady=4)

        ctk.CTkLabel(time_frame, text="С:", text_color=c['text_secondary']).pack(side='left')
        self.start_time = TimeEntry(time_frame, width=76, height=30, corner_radius=Config.RADIUS_SM,
                                     fg_color=c['bg_secondary'], border_color=c['border'],
                                     text_color=c['text_primary'])
        self.start_time.pack(side='left', padx=6)

        ctk.CTkLabel(time_frame, text="До:", text_color=c['text_secondary']).pack(side='left', padx=(10, 0))
        self.end_time = TimeEntry(time_frame, width=76, height=30, corner_radius=Config.RADIUS_SM,
                                   fg_color=c['bg_secondary'], border_color=c['border'],
                                   text_color=c['text_primary'])
        self.end_time.pack(side='left', padx=6)

        self.date_label = ctk.CTkLabel(time_frame, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'])
        self.date_label.pack(side='left', padx=(12, 0))

        # Для ссылки/браузера — статус автоопределения хронометража
        # (скрыт для канала, там это не нужно).
        self.duration_hint = ctk.CTkLabel(form_frame, text="", font=ctk.CTkFont(size=10),
                                           text_color=c['text_muted'], anchor='w', justify='left')
        self.duration_hint.pack(fill='x', padx=12, pady=(0, 2))

        # Дни недели — только для каналов (у ссылки/браузера нет еженедельного
        # эфира, это разовая запись по хронометражу; скрывается в _on_source_type_changed)
        self.days_frame = ctk.CTkFrame(form_frame, fg_color='transparent')
        self.days_frame.pack(fill='x', padx=12, pady=6)
        ctk.CTkLabel(self.days_frame, text="Дни:", text_color=c['text_secondary'], width=50, anchor='w').pack(side='left')

        self.day_vars = {}
        day_names = [('Пн', 0), ('Вт', 1), ('Ср', 2), ('Чт', 3), ('Пт', 4), ('Сб', 5), ('Вс', 6)]
        for name, idx in day_names:
            var = tk.BooleanVar()
            self.day_vars[idx] = var
            ctk.CTkCheckBox(self.days_frame, text=name, variable=var, width=20, checkbox_width=18, checkbox_height=18,
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

        # Записать выбранную строку прямо сейчас, не дожидаясь её времени в
        # расписании.
        self.btn_record_now = ctk.CTkButton(btn_frame, text="Сейчас", image=get_icon('record', c['red'], 12),
                                             compound='left', height=30, corner_radius=Config.RADIUS_SM,
                                             fg_color=c['bg_secondary'], hover_color=c['bg_hover'],
                                             text_color=c['text_primary'], state='disabled',
                                             command=self._record_selected_now)
        self.btn_record_now.pack(side='left', padx=4)

        self.btn_clear_form = ctk.CTkButton(btn_frame, text="Очистить", height=30, corner_radius=Config.RADIUS_SM,
                                             fg_color='transparent', hover_color=c['bg_hover'],
                                             text_color=c['text_secondary'], command=self._clear_form)
        self.btn_clear_form.pack(side='right')

        # Таблица расписания
        table_frame = ctk.CTkFrame(self, fg_color='transparent')
        table_frame.pack(fill='both', expand=True, padx=10, pady=(0, 8))

        columns = ('channel', 'source', 'time', 'days', 'active', 'status')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', height=8)

        self.tree.heading('channel', text='Канал / ссылка')
        self.tree.heading('source', text='Тип')
        self.tree.heading('time', text='Время')
        self.tree.heading('days', text='Дни')
        self.tree.heading('active', text='Активно')
        self.tree.heading('status', text='Статус')

        self.tree.column('channel', width=140)
        self.tree.column('source', width=70, anchor='center')
        self.tree.column('time', width=140)
        self.tree.column('days', width=90)
        self.tree.column('active', width=70, anchor='center')
        self.tree.column('status', width=100, anchor='center')

        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Button-1>', self._on_tree_click, add='+')

    LABEL_TO_TYPE = {'Канал': 'channel', 'Ссылка': 'link'}
    TYPE_TO_LABEL = {'channel': 'Канал', 'link': 'Ссылка'}

    def _names_for(self, source_type: str) -> List[str]:
        if source_type == 'link':
            return self._link_names
        return self._channel_names

    def _on_source_type_changed(self, label: str):
        source_type = self.LABEL_TO_TYPE.get(label, 'channel')
        self.source_type_var.set(source_type)
        self.channel_label.configure(text="Канал:" if source_type == 'channel' else "Ссылка:")
        self._refresh_source_dropdown()
        self._apply_time_defaults()
        self._maybe_detect_link_duration()

    def _refresh_source_dropdown(self):
        names = self._names_for(self.source_type_var.get())
        self.channel_combo.configure(values=names or [''])
        if self.channel_var.get() not in names:
            self.channel_var.set(names[0] if names else '')

    def refresh(self):
        """Обновляет таблицу и списки источников"""
        self._channel_names = [ch['name'] for ch in self.storage.get_channels()]
        self._link_names = [l['name'] for l in self.storage.get_links()]
        self._refresh_source_dropdown()

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
            source_str = self.TYPE_TO_LABEL.get(item.get('source_type', 'channel'), 'Канал')

            self.tree.insert('', 'end', iid=i, values=(
                item.get('channel_name', ''),
                source_str,
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
            'ended_early': '⚠ раньше',
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
            if len(values) >= 6:
                values[5] = self._format_status(status)
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

    def _apply_time_defaults(self):
        """Каналу — текущее время и день недели (это эфир, идёт сейчас).
        Ссылке/браузеру — нет: это не эфир по расписанию, а разовая запись
        с чётким хронометражем, привязывать её к времени на компьютере
        только сбивает с толку. Время начала — 00:00, конец пользователь
        вводит сам исходя из длительности записи. День недели скрыт —
        internally берётся сегодняшний, раз запись разовая."""
        now = datetime.now()
        day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

        if self.source_type_var.get() == 'channel':
            self.days_frame.pack(fill='x', padx=12, pady=6)
            self.start_time.set_time(now.strftime("%H:%M"))
            self.end_time.set_time(now.strftime("%H:%M"))
            self.date_label.configure(text=f"Сегодня: {day_names[now.weekday()]}, {now:%d.%m.%Y}")
            today_idx = now.weekday()
            for idx, var in self.day_vars.items():
                var.set(idx == today_idx)
        else:
            self.days_frame.pack_forget()
            self.start_time.set_time("00:00")
            self.end_time.set_time("")
            self.date_label.configure(text=f"Сегодня: {day_names[now.weekday()]}, {now:%d.%m.%Y}")
            for var in self.day_vars.values():
                var.set(False)

    def _scroll_channel_selection(self, event, direction=None):
        """Меняет канал/ссылку колёсиком мыши или жестом двумя пальцами."""
        names = self._names_for(self.source_type_var.get())
        if not names:
            return 'break'
        if direction is None:
            direction = -1 if event.delta > 0 else 1
        current = self.channel_var.get()
        try:
            idx = names.index(current)
        except ValueError:
            idx = 0 if direction > 0 else len(names) - 1
        else:
            idx = max(0, min(len(names) - 1, idx + direction))
        self.channel_var.set(names[idx])
        self._maybe_detect_link_duration()
        return 'break'

    def _maybe_detect_link_duration(self):
        """Выбрана ссылка (не браузер, там нет резолвящегося потока) —
        подтягиваем её фактическую длительность через link_resolver и сами
        подставляем время окончания, вместо того чтобы пользователь гадал."""
        if self.source_type_var.get() != 'link':
            self.duration_hint.configure(text="")
            return
        name = self.channel_var.get()
        if not name:
            self.duration_hint.configure(text="")
            return
        link = next((l for l in self.storage.get_links() if l.get('name') == name), None)
        if not link or not link.get('url'):
            self.duration_hint.configure(text="")
            return

        self._duration_detect_generation += 1
        my_generation = self._duration_detect_generation
        self.duration_hint.configure(text="Определяем длительность…")

        def worker():
            info = resolve_link(link['url'])

            def apply():
                # Пока резолвили — выбор в форме мог уже поменяться.
                if my_generation != self._duration_detect_generation:
                    return
                if not info.ok:
                    self.duration_hint.configure(text=f"Не удалось определить длительность: {info.error}")
                    return
                try:
                    start_dt = datetime.strptime(self.start_time.get().strip(), '%H:%M')
                except ValueError:
                    start_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                if info.duration:
                    end_dt = start_dt + timedelta(seconds=info.duration)
                    minutes = int(info.duration // 60)
                    self.duration_hint.configure(text=f"Определено: длительность ~{minutes} мин")
                else:
                    end_dt = start_dt + timedelta(minutes=60)
                    self.duration_hint.configure(
                        text="Прямой эфир (длительность неизвестна — окно +60 мин, поправьте при необходимости)")
                self.end_time.set_time(end_dt.strftime('%H:%M'))

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _days_for_save(self) -> List[int]:
        """Для канала — что отмечено галочками. Для ссылки/браузера дни
        скрыты (разовая запись, не еженедельный эфир) — подставляем
        сегодняшний, как и в диалогах добавления ссылки."""
        if self.source_type_var.get() != 'channel':
            return [datetime.now().weekday()]
        return [idx for idx, var in self.day_vars.items() if var.get()]

    def _add_schedule(self):
        channel_name = self.channel_var.get()
        start = self.start_time.get().strip()
        end = self.end_time.get().strip()

        if not self._valid_form(channel_name, start, end):
            return

        days = self._days_for_save()
        if not days:
            messagebox.showwarning("Внимание", "Выберите хотя бы один день")
            return

        item = {
            'channel_name': channel_name,
            'source_type': self.source_type_var.get(),
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

        days = self._days_for_save()

        item = {
            'channel_name': channel_name,
            'source_type': self.source_type_var.get(),
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

        source_type = item.get('source_type', 'channel')
        self.source_segmented.set(self.TYPE_TO_LABEL.get(source_type, 'Канал'))
        self.source_type_var.set(source_type)
        self.channel_label.configure(text="Канал:" if source_type == 'channel' else "Ссылка:")
        self._refresh_source_dropdown()
        if source_type == 'channel':
            self.days_frame.pack(fill='x', padx=12, pady=6)
        else:
            self.days_frame.pack_forget()

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
        self.btn_record_now.configure(state='normal' if self.on_record_now else 'disabled')

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
        names = self._names_for(self.source_type_var.get())
        self.channel_var.set(names[0] if names else '')

        self._clear_form_buttons()
        self._apply_time_defaults()
        self._maybe_detect_link_duration()

    def _clear_form_buttons(self):
        self.btn_add.configure(state='normal')
        self.btn_update.configure(state='disabled')
        self.btn_delete_form.configure(state='disabled')
        self.btn_record_now.configure(state='disabled')

    def _record_selected_now(self):
        selected = self.tree.selection()
        if not selected or not self.on_record_now:
            return
        index = int(selected[0])
        schedule = self.storage.get_schedule()
        if index >= len(schedule):
            return
        item = schedule[index]
        source_type = item.get('source_type', 'channel')
        name = item.get('channel_name', '')

        if source_type == 'link':
            target = next((l for l in self.storage.get_links() if l.get('name') == name), None)
        else:
            target = next((ch for ch in self.storage.get_channels() if ch.get('name') == name), None)

        if not target:
            messagebox.showwarning("Внимание", f"«{name}» не найден(а) — возможно, был(а) удалён(а)")
            return

        self.on_record_now(source_type, name, target)
        logger.info(f"Запись выбранной строки расписания сейчас: {name} ({source_type})")
