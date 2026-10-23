"""Channel-first archive browsing, a dated playback cursor and exact clip export."""
import subprocess
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty
from tkinter import ttk

import customtkinter as ctk

from core.archive_controller import ArchiveController, build_interval_request
from core.archive_catalog import ChannelFamily, group_channels, normalized
from core.tv_archive import ArchiveRequest, BASE_URL, MOSCOW, DEFAULT_UA
from gui.preview_panel import PreviewPanel
from utils.config import Config
from utils.tk_helpers import bind_cyrillic_layout_shortcuts

PROVIDERS = {'Забава / Wink': 'ngenix', 'ontvtime.ru': 'ontvtime'}
STATES = {'available': 'Архив', 'live_only': 'Эфир', 'unavailable': 'Недоступен',
          'unknown': 'Нет ответа'}
STAMP = '%d.%m.%Y %H:%M:%S'


def fit_preview_size(width, height):
    units = max(1, int(min(width / 16, height / 9)))
    return units * 16, units * 9


class ArchivePanel(ctk.CTkFrame):
    def __init__(self, parent):
        self.c = Config.COLORS
        super().__init__(parent, fg_color='transparent')
        self.controller = ArchiveController()
        self._running, self._active, self._busy = True, False, False
        self._loading = False
        self._selected = None
        self._saved_path = None
        self._results = dict(self.controller.cached)
        self._play_request = None
        self._seek_id = None
        self._editing = False
        self._dragging = False
        self._audio_after_seek = False
        self._counts = {}
        self._filter_id = None
        self._version_labels = {}
        self._families = ()
        self._family_by_slug = {}
        self._selected_family = None
        self._building_list = False
        self._preview_size = (288, 162)
        self._resize_id = None
        self._build()
        self._populate_channels()
        channels = self._channels()
        if channels:
            self._select(next((ch for ch in channels if ch.name == 'ТНТ'), channels[0]))
        self._poll_id = self.after(150, self._poll)

    @property
    def provider(self):
        return PROVIDERS[self.source.get()]

    def _channels(self):
        return self.controller.catalog[self.provider]

    def _label(self, parent, text, size=12, color='text_primary', weight='normal', **kwargs):
        return ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=size, weight=weight),
                           text_color=self.c[color], **kwargs)

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        kwargs.setdefault('height', 34)
        return ctk.CTkButton(parent, text=text, command=command,
                            corner_radius=Config.RADIUS_SM,
                            fg_color=self.c['accent' if primary else 'bg_tertiary'],
                            hover_color=self.c['accent_hover' if primary else 'bg_hover'],
                            text_color=self.c['accent_text' if primary else 'text_primary'], **kwargs)

    def _entry(self, parent, variable, **kwargs):
        entry = ctk.CTkEntry(parent, textvariable=variable, height=36,
                            fg_color=self.c['bg_primary'], border_color=self.c['border'], **kwargs)
        bind_cyrillic_layout_shortcuts(entry)
        return entry

    def _build(self):
        c = self.c
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)
        heading = ctk.CTkFrame(self, fg_color='transparent')
        heading.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        self._label(heading, 'Архив ТВ', size=20, weight='bold').pack(side='left')
        self._label(heading, 'Смотрите прошедший эфир и сохраняйте нужное', size=12,
                    color='text_secondary').pack(side='left', padx=16)
        self._button(heading, 'Источники', self._toggle_connection, width=100, height=30).pack(side='right')
        self.check_btn = self._button(heading, 'Обновить', self.refresh_channels, width=100, height=30)
        self.check_btn.pack(side='right', padx=8)
        self.source = tk.StringVar(value='Забава / Wink')
        ctk.CTkOptionMenu(heading, variable=self.source, values=list(PROVIDERS),
                         command=self._source_changed, width=150, height=30,
                         fg_color=c['bg_tertiary'], button_color=c['bg_hover'],
                         text_color=c['text_primary']).pack(side='right')
        self.connection = ctk.CTkFrame(self, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM)
        self.connection.columnconfigure(1, weight=1)
        self._label(self.connection, 'Добавить свой поток', weight='bold').grid(row=0, column=0, sticky='w', padx=12, pady=(8, 2))
        self._label(self.connection, 'Ссылка Wink / Ngenix (.m3u8)', color='text_secondary').grid(row=0, column=1, sticky='w', padx=8)
        self.connection_name, self.connection_url = tk.StringVar(), tk.StringVar()
        self._entry(self.connection, self.connection_name, width=170).grid(row=1, column=0, padx=12, pady=(0, 10))
        self._entry(self.connection, self.connection_url).grid(row=1, column=1, sticky='ew', padx=8, pady=(0, 10))
        self._button(self.connection, 'Добавить', self._connect, width=95).grid(row=1, column=2, padx=12, pady=(0, 10))

        sidebar = ctk.CTkFrame(self, width=258, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        sidebar.grid(row=2, column=0, sticky='nsew', padx=(0, 12))
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(4, weight=1)
        self._label(sidebar, 'Каналы', size=15, weight='bold').grid(row=0, column=0, sticky='w', padx=14, pady=(12, 4))
        self.search = tk.StringVar()
        search = self._entry(sidebar, self.search, placeholder_text='Найти канал или регион')
        search.grid(row=1, column=0, sticky='ew', padx=12, pady=(0, 8))
        self.group = tk.StringVar(value='Все регионы')
        self.group_menu = ctk.CTkOptionMenu(sidebar, variable=self.group, values=['Все регионы'],
                         command=self._region_changed, height=30,
                         fg_color=c['bg_tertiary'], button_color=c['bg_hover'], text_color=c['text_primary'])
        self.group_menu.grid(row=2, column=0, sticky='ew', padx=12, pady=(0, 6))
        self.availability = tk.StringVar(value='Все каналы')
        ctk.CTkOptionMenu(sidebar, variable=self.availability,
                         values=['Все каналы', 'С архивом', 'Только эфир', 'Недоступные'],
                         command=lambda _: self._filter(), height=30,
                         fg_color=c['bg_tertiary'], button_color=c['bg_hover'], text_color=c['text_primary']).grid(
                         row=3, column=0, sticky='ew', padx=12, pady=(0, 8))
        table = ctk.CTkFrame(sidebar, fg_color='transparent')
        table.grid(row=4, column=0, sticky='nsew', padx=10)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        style = ttk.Style()
        style.configure('Archive.Treeview', background=c['bg_secondary'], fieldbackground=c['bg_secondary'],
                        foreground=c['text_primary'], rowheight=30, borderwidth=0, font=('', 12))
        style.configure('Archive.Treeview.Heading', background=c['bg_tertiary'], foreground=c['text_secondary'], font=('', 10))
        style.map('Archive.Treeview', background=[('selected', c['bg_active'])], foreground=[('selected', c['text_primary'])])
        self.tree = ttk.Treeview(table, columns=('name', 'state'), show='headings',
                                 selectmode='browse', style='Archive.Treeview', takefocus=True)
        self.tree.heading('name', text='Канал', anchor='w')
        self.tree.heading('state', text='Доступно', anchor='w')
        self.tree.column('name', width=140, minwidth=100, stretch=True)
        self.tree.column('state', width=72, minwidth=65, stretch=False)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure('available', foreground=c['green'])
        self.tree.tag_configure('unavailable', foreground=c['text_secondary'])
        self.tree.tag_configure('live_only', foreground=c['yellow'])
        self.tree.bind('<<TreeviewSelect>>', self._tree_selected)
        self.empty_list = self._label(table, 'Ничего не найдено.\nИзмените поиск или фильтры.', color='text_secondary', justify='center')
        self.list_count = self._label(sidebar, '', size=11, color='text_secondary', anchor='w')
        self.list_count.grid(row=5, column=0, sticky='ew', padx=14, pady=(6, 0))
        self.summary = self._label(sidebar, '', size=10, color='text_secondary', anchor='w')
        self.summary.grid(row=6, column=0, sticky='ew', padx=14, pady=(0, 10))
        self.search.trace_add('write', lambda *_: self._queue_filter())

        content = ctk.CTkFrame(self, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        content.grid(row=2, column=1, sticky='nsew')
        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        title = ctk.CTkFrame(content, fg_color='transparent')
        title.grid(row=0, column=0, sticky='ew', padx=16, pady=(10, 4))
        title.columnconfigure(0, weight=1)
        self.channel_title = self._label(title, 'Выберите канал', size=20, weight='bold', anchor='w')
        self.channel_title.grid(row=0, column=0, sticky='w')
        self.variant = tk.StringVar(value='Основной')
        self.version_menu = ctk.CTkOptionMenu(title, variable=self.variant, values=['Основной'],
                        command=self._version_changed, height=30, width=240,
                        fg_color=c['bg_tertiary'], button_color=c['bg_hover'], text_color=c['text_primary'])
        self.version_menu.grid(row=0, column=1, sticky='e', padx=(12, 0))

        controls = ctk.CTkFrame(content, fg_color='transparent')
        controls.grid(row=1, column=0, sticky='ew', padx=16, pady=(0, 6))
        initial = datetime.now(MOSCOW).replace(microsecond=0)
        self.day = tk.StringVar(value=initial.strftime('%d.%m.%Y'))
        self.position = tk.StringVar(value=initial.strftime('%H:%M:%S'))
        self._label(controls, 'МСК', size=11, color='text_secondary').pack(side='left', padx=(0, 8))
        self.date_combo = ctk.CTkComboBox(controls, variable=self.day, values=self._recent_days(), height=32, width=140,
                        fg_color=c['bg_primary'], border_color=c['border'], button_color=c['bg_tertiary'],
                        command=lambda _: self._schedule_seek())
        self.date_combo.pack(side='left', padx=(0, 6))
        bind_cyrillic_layout_shortcuts(self.date_combo)
        entry = self._entry(controls, self.position, width=100)
        entry.configure(height=32)
        entry.pack(side='left', padx=(0, 8))
        entry.bind('<Return>', lambda _: self._seek_from_fields())
        self.watch_btn = self._button(controls, 'Смотреть архив', self._seek_from_fields, width=140, height=32)
        self.watch_btn.pack(side='left')
        self._button(controls, 'Сейчас', self._near_live, width=70, height=32).pack(side='right')

        player = ctk.CTkFrame(content, fg_color=c['bg_primary'], corner_radius=Config.RADIUS_SM)
        self.player = player
        player.grid(row=2, column=0, sticky='nsew', padx=12, pady=(0, 6))
        player.columnconfigure(0, weight=1)
        player.columnconfigure(1, weight=0)
        player.rowconfigure(0, weight=1)
        self.preview = PreviewPanel(player, image_size=(288, 162), contain_image=True)
        self.preview.playing_label = 'Архив'
        self.preview.name_lbl.master.pack_forget()
        self.preview.audio_status_lbl.pack_forget()
        self.preview.configure(width=1, height=1, fg_color=c['bg_primary'])
        self.preview.pack_propagate(False)
        self.preview.image_lbl.pack_configure(expand=True)
        self.preview.grid(row=0, column=0, sticky='nsew', padx=(6, 0), pady=6)
        self.preview.clear('Выберите канал для просмотра')
        details = ctk.CTkFrame(player, fg_color='transparent')
        details.grid(row=0, column=1, sticky='w', padx=12, pady=8)
        self._label(details, 'ВРЕМЯ В КАДРЕ · МСК', size=10, color='text_secondary').pack(anchor='w')
        self.clock = self._label(details, 'Подключение…', size=16, weight='bold', anchor='w')
        self.clock.pack(anchor='w', pady=(2, 4))
        self.channel_hint = self._label(details, '', size=11, color='text_secondary', justify='left', anchor='w', wraplength=235)
        self.channel_hint.pack(anchor='w', pady=(0, 6))

        nav = ctk.CTkFrame(content, fg_color='transparent')
        nav.grid(row=3, column=0, sticky='ew', padx=16, pady=(0, 2))
        nav.columnconfigure(1, weight=1)
        self._button(nav, '−30 с', lambda: self._skip(-30), width=60, height=28).grid(row=0, column=0, padx=(0, 8))
        self.timeline = ctk.CTkSlider(nav, from_=0, to=86399, command=self._scrub,
                        progress_color=c['accent'], button_color=c['accent'], height=16)
        self.timeline.grid(row=0, column=1, sticky='ew')
        self.timeline.bind('<ButtonPress-1>', lambda _: self._start_scrub(), add='+')
        self.timeline.bind('<ButtonRelease-1>', lambda _: self._finish_scrub(), add='+')
        self.timeline.bind('<Left>', lambda _: self._skip(-30))
        self.timeline.bind('<Right>', lambda _: self._skip(30))
        self._button(nav, '+30 с', lambda: self._skip(30), width=60, height=28).grid(row=0, column=2, padx=(8, 0))
        self.audio_btn = self._button(nav, 'Звук: выкл.', self._toggle_audio, width=100, height=28)
        self.audio_btn.grid(row=0, column=3, padx=(8, 0))
        self._button(nav, 'Стоп', self._stop_preview, width=60, height=28).grid(row=0, column=4, padx=(6, 0))
        self.message = self._label(content, 'Выбор канала включает только просмотр. Для записи задайте фрагмент ниже.',
                                  size=11, color='text_secondary', justify='left', anchor='w', wraplength=680)
        self.message.grid(row=4, column=0, sticky='ew', padx=16, pady=(0, 4))

        save = ctk.CTkFrame(content, fg_color=c['bg_primary'], corner_radius=Config.RADIUS_SM)
        save.grid(row=5, column=0, sticky='ew', padx=12, pady=(0, 12))
        save.columnconfigure(0, weight=1)
        save.columnconfigure(1, weight=1)
        save_header = ctk.CTkFrame(save, fg_color='transparent')
        save_header.grid(row=0, column=0, columnspan=2, sticky='ew', padx=12, pady=(8, 4))
        self._label(save_header, 'Фрагмент', size=14, weight='bold').pack(side='left')
        self.duration_label = self._label(save_header, 'Отметьте начало и конец', size=11, color='text_secondary')
        self.duration_label.pack(side='left', padx=12)
        self.save_btn = self._button(save_header, 'Сохранить MP4', self._download, primary=True, width=145, height=32)
        self.save_btn.pack(side='right')
        self.start, self.end = tk.StringVar(value=initial.strftime(STAMP)), tk.StringVar(value=initial.strftime(STAMP))
        for column, label, variable, callback in [(0, 'Начало · дата и время', self.start, self._mark_start),
                                                   (1, 'Конец · дата и время', self.end, self._mark_end)]:
            field = ctk.CTkFrame(save, fg_color='transparent')
            field.grid(row=1, column=column, sticky='ew', padx=(12, 6) if column == 0 else (6, 12))
            field.columnconfigure(0, weight=1)
            self._label(field, label, size=10, color='text_secondary').grid(row=0, column=0, columnspan=2, sticky='w')
            entry = self._entry(field, variable, width=160)
            entry.configure(height=32)
            entry.grid(row=1, column=0, sticky='ew')
            self._button(field, 'Из кадра', callback, width=75, height=32).grid(row=1, column=1, padx=(6, 0))
        output = ctk.CTkFrame(save, fg_color='transparent')
        output.grid(row=2, column=0, columnspan=2, sticky='ew', padx=12, pady=(4, 4))
        output.columnconfigure(0, weight=1)
        self.job_label = self._label(output, 'MP4 · до 4 часов · папка записей', size=10, color='text_secondary', anchor='w')
        self.job_label.grid(row=0, column=0, sticky='ew')
        self.cancel_btn = self._button(output, 'Отменить', self.controller.cancel, width=75, height=24, state='disabled')
        self.cancel_btn.grid(row=0, column=1, padx=(8, 0))
        self.open_btn = self._button(output, 'Показать файл', self._reveal, width=105, height=24, state='disabled')
        self.open_btn.grid(row=0, column=2, padx=(6, 0))
        self.progress_bar = ctk.CTkProgressBar(save, height=4, progress_color=c['accent'])
        self.progress_bar.grid(row=3, column=0, columnspan=2, sticky='ew', padx=12, pady=(0, 8))
        self.progress_bar.set(0)
        for variable in (self.start, self.end):
            variable.trace_add('write', lambda *_: self._validate_interval())
        self._validate_interval()
        self.bind('<Configure>', self._resize_workspace, add='+')
        player.bind('<Configure>', self._resize_workspace, add='+')
        self.preview.bind('<Configure>', self._resize_workspace, add='+')

    def _recent_days(self):
        now = datetime.now(MOSCOW)
        return [(now-timedelta(days=i)).strftime('%d.%m.%Y') for i in range(8)]

    def _populate_channels(self):
        channels = self._channels()
        self._by_slug = {ch.slug: ch for ch in channels}
        self._families = group_channels(channels)
        self._family_by_key = {family.key: family for family in self._families}
        self._family_by_slug = {ch.slug: family for family in self._families for ch in family.versions}
        groups = sorted({getattr(ch, 'group', '') for ch in channels} - {''})
        self.group_menu.configure(values=['Все регионы'] + groups)
        if self.group.get() not in ['Все регионы'] + groups:
            self.group.set('Все регионы')
        self._filter()
        self._update_summary()
        if self._selected:
            self._update_versions()

    def _queue_filter(self):
        if self._filter_id:
            self.after_cancel(self._filter_id)
        self._filter_id = self.after(180, self._filter)

    def _filter(self):
        if self._filter_id:
            self.after_cancel(self._filter_id)
            self._filter_id = None
        query = normalized(self.search.get().strip())
        region = self.group.get()
        state = {'С архивом': 'available', 'Только эфир': 'live_only', 'Недоступные': 'unavailable'}.get(self.availability.get())
        scroll = self.tree.yview()
        self._building_list = True
        self.tree.delete(*self.tree.get_children())
        self._visible_versions = {}
        for family in self._families:
            versions = tuple(ch for ch in family.versions
                if (region == 'Все регионы' or getattr(ch, 'group', '') == region)
                and (not state or self._results.get(ch.slug, {}).get('state') == state)
                and query in normalized(ch.name + ' ' + getattr(ch, 'group', '') + ' ' + family.name))
            if not versions:
                continue
            self._visible_versions[family.key] = versions
            family_state = family.state(self._results)
            self.tree.insert('', 'end', iid=family.key, values=(family.name, STATES.get(family_state, 'Не проверен')),
                             tags=(family_state,))
        if self._selected:
            family = self._family_by_slug.get(self._selected.slug)
            if family and self.tree.exists(family.key):
                self.tree.selection_set(family.key)
        if scroll:
            self.tree.yview_moveto(scroll[0])
        count = len(self._visible_versions)
        self.list_count.configure(text=f'{count} из {len(self._families)} каналов · версии внутри')
        if count:
            self.empty_list.place_forget()
        else:
            self.empty_list.place(relx=0.5, rely=0.45, anchor='center')
        self._building_list = False

    def _tree_selected(self, _event=None):
        if self._building_list:
            return
        selection = self.tree.selection()
        if not selection:
            return
        family = self._family_by_key.get(selection[0])
        current = self._family_by_slug.get(self._selected.slug) if self._selected else None
        if family and (current is None or current.key != family.key):
            versions = self._visible_versions.get(family.key, family.versions)
            chosen = family.preferred(self._results)
            if chosen not in versions:
                chosen = next((ch for ch in versions if self._results.get(ch.slug, {}).get('state') == 'available'), versions[0])
            self._select(chosen)

    def _update_versions(self):
        family = self._family_by_slug.get(self._selected.slug) if self._selected else None
        if family is None:
            return
        self._selected_family = family
        versions = family.choices(self.group.get())
        if self._selected not in versions:
            versions += (self._selected,)
        self._version_labels = ChannelFamily(family.key, family.name, versions).version_labels(self._results)
        self.version_menu.configure(values=list(self._version_labels))
        label = next(label for label, ch in self._version_labels.items() if ch.slug == self._selected.slug)
        self.variant.set(label)
        self.channel_title.configure(text=family.name)

    def _region_changed(self, _value=None):
        self._filter()
        family = self._family_by_slug.get(self._selected.slug) if self._selected else None
        if family:
            versions = family.choices(self.group.get())
            if versions and self._selected not in versions:
                choice = ChannelFamily(family.key, family.name, versions).preferred(self._results)
                self._select(choice, reset_time=False)
            else:
                self._update_versions()

    def _version_changed(self, label):
        channel = self._version_labels.get(label)
        if channel is not None and channel.slug != self._selected.slug:
            # Comparing regional/time-shift sources must preserve the selected
            # date and clip draft; only choosing a new channel resets them.
            self._select(channel, reset_time=False)

    def _select(self, channel, *, reset_time=True):
        selected_at = datetime.now(MOSCOW).replace(microsecond=0)
        self.preview.set_audio_enabled(False)
        self._audio_after_seek = False
        self.audio_btn.configure(text='Звук: выкл.')
        self._selected = channel
        if reset_time:
            self._reset_selection_time(selected_at)
        else:
            self._play_request = None
        self.channel_title.configure(text=channel.name)
        self._update_versions()
        self._selected_status()
        family = self._family_by_slug.get(channel.slug)
        if family and self.tree.exists(family.key):
            self.tree.selection_set(family.key)
            self.tree.see(family.key)
        self._validate_interval()
        if self._active:
            self._seek_from_fields()

    def _reset_selection_time(self, selected_at):
        if self._seek_id:
            self.after_cancel(self._seek_id)
            self._seek_id = None
        preview_at = selected_at - timedelta(seconds=30)
        self._editing = True
        try:
            self.day.set(preview_at.strftime('%d.%m.%Y'))
            self.position.set(preview_at.strftime('%H:%M:%S'))
            self.start.set(selected_at.strftime(STAMP))
            self.end.set(selected_at.strftime(STAMP))
        finally:
            self._editing = False
        self._play_request = None
        self._validate_interval()
        self.timeline.set(preview_at.hour*3600 + preview_at.minute*60 + preview_at.second)

    def _near_live(self):
        self._seek_to(datetime.now(MOSCOW)-timedelta(seconds=30))

    def _toggle_audio(self):
        enabled = not self.preview._want_audio
        self.preview.set_audio_enabled(enabled)
        self.audio_btn.configure(text='Звук: вкл.' if enabled else 'Звук: выкл.')

    def _stop_preview(self):
        self.controller.stop_preview()
        self.preview.set_audio_enabled(False)
        self.preview.clear('Просмотр остановлен')
        self._play_request = None
        self._loading = False
        self.watch_btn.configure(text='Смотреть архив', state='normal')
        self.clock.configure(text='Просмотр остановлен')
        self.audio_btn.configure(text='Звук: выкл.')
        self.message.configure(text='Выберите дату и нажмите «Смотреть архив», чтобы продолжить.',
                               text_color=self.c['text_secondary'])

    def _validate_interval(self):
        if self._editing:
            return
        try:
            request = build_interval_request(self._selected, self.start.get(), self.end.get())
            seconds = int(request.duration)
            duration = f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'
            self.duration_label.configure(text=duration, text_color=self.c['text_primary'])
            valid = self._selected is not None
        except ValueError:
            self.duration_label.configure(text='Задайте начало и конец', text_color=self.c['text_secondary'])
            valid = False
        self.save_btn.configure(state='normal' if valid and not self._busy else 'disabled')

    def _resize_workspace(self, _event=None):
        if self._resize_id is None:
            self._resize_id = self.after(50, self._apply_workspace_size)

    def _apply_workspace_size(self):
        self._resize_id = None
        # Fit the actual flexible preview cell, with no fixed maximum size.
        # Keep a single decoder and scale the currently displayed image too.
        size = fit_preview_size(max(16, self.preview.winfo_width()-20),
                                max(9, self.preview.winfo_height()-12))
        if size != self._preview_size:
            self._preview_size = size
            self.preview._image_size = size
            self.preview.image_lbl.configure(width=size[0], height=size[1])
            current = getattr(self.preview.image_lbl, '_img_ref', None)
            if current is not None:
                current.configure(size=size)
        if self.player.winfo_height() < 140:
            self.channel_hint.pack_forget()
        elif not self.channel_hint.winfo_manager():
            self.channel_hint.pack(anchor='w', pady=(0, 6))

    def _selected_status(self):
        if self._selected is None:
            return
        result = self._results.get(self._selected.slug, {})
        state = result.get('state', 'unknown')
        hints = {'available': 'Архив доступен. Выберите дату и время.',
                 'live_only': 'Архив не подтверждён. Попробуйте другую версию.',
                 'unavailable': 'Версия недоступна. Выберите другую в меню сверху.',
                 'unknown': 'Доступность уточняется при подключении.'}
        self.channel_hint.configure(text=hints.get(state, hints['unknown']))
        dates = result.get('dates', [])
        if self.provider == 'ontvtime' and dates:
            values = [datetime.fromisoformat(day).strftime('%d.%m.%Y') for day in reversed(dates)]
            self.date_combo.configure(values=values)
            if self.day.get() not in values:
                self.day.set(values[0])
        elif self.provider == 'ngenix':
            self.date_combo.configure(values=self._recent_days())

    def _source_changed(self, _value=None):
        self.deactivate()
        self._selected = None
        self.search.set('')
        self.group.set('Все регионы')
        self.availability.set('Все каналы')
        self._populate_channels()
        channels = self._channels()
        if channels:
            self._select(channels[0])
        self.activate()
        self.refresh_channels()

    def _schedule_seek(self):
        if self._editing or not self._active:
            return
        if self._seek_id:
            self.after_cancel(self._seek_id)
        self._seek_id = self.after(900, self._seek_from_fields)

    def _seek_from_fields(self):
        if self._seek_id:
            self.after_cancel(self._seek_id)
            self._seek_id = None
        if not self._active or self._selected is None:
            return
        try:
            value = self.position.get().strip()
            point = datetime.strptime(self.day.get().strip() + ' ' + value,
                                      '%d.%m.%Y %H:%M:%S' if value.count(':') == 2 else '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW)
            self._play_at(point)
        except ValueError as error:
            self.message.configure(text=str(error) if str(error).startswith('Выберите') else
                                   'Введите дату ДД.ММ.ГГГГ и время ЧЧ:ММ:СС.', text_color=self.c['yellow'])

    def _play_at(self, point):
        limit = datetime.now(MOSCOW) - timedelta(seconds=25)
        if point > limit:
            raise ValueError('Выберите прошедшее время: архив появляется с небольшой задержкой после эфира.')
        self._audio_after_seek = self.preview._want_audio
        end = min(point+timedelta(minutes=5), limit)
        if (end-point).total_seconds() < 1:
            raise ValueError('Выберите время хотя бы на полминуты раньше эфира.')
        self.preview.clear('Подключение к архиву…')
        self._play_request = None
        self.clock.configure(text='Подключение…')
        self._loading = True
        self.watch_btn.configure(text='Подключение…', state='disabled')
        self.message.configure(text=f'Открываем {point:%d.%m.%Y %H:%M:%S} · МСК…', text_color=self.c['text_secondary'])
        self.controller.preview(ArchiveRequest(self._selected, point, end))

    def _seek_to(self, point):
        point = min(point, datetime.now(MOSCOW)-timedelta(seconds=30))
        self._editing = True
        self.day.set(point.strftime('%d.%m.%Y'))
        self.position.set(point.strftime('%H:%M:%S'))
        self._editing = False
        self._seek_from_fields()

    def current_position(self):
        if self._play_request is None or self.preview.playback_seconds is None:
            return None
        return self._play_request.start + timedelta(seconds=self.preview.playback_seconds)

    def _skip(self, seconds):
        point = self.current_position()
        if point is None:
            try:
                point = datetime.strptime(self.day.get()+' '+self.position.get(), STAMP).replace(tzinfo=MOSCOW)
            except ValueError:
                return
        self._seek_to(point+timedelta(seconds=seconds))

    def _start_scrub(self):
        self._dragging = True

    def _scrub(self, seconds):
        self._dragging = True
        try:
            day = datetime.strptime(self.day.get(), '%d.%m.%Y').replace(tzinfo=MOSCOW)
        except ValueError:
            return
        point = min(day+timedelta(seconds=int(seconds)), datetime.now(MOSCOW)-timedelta(seconds=30))
        self.clock.configure(text=f'{point:%d.%m.%Y %H:%M:%S} · МСК')
        self._editing = True
        self.position.set(point.strftime('%H:%M:%S'))
        self._editing = False

    def _finish_scrub(self):
        if self._dragging:
            self._dragging = False
            self._seek_from_fields()

    def _mark_start(self):
        point = self.current_position()
        if point:
            self.start.set(point.strftime(STAMP))
        else:
            self.message.configure(text='Дождитесь первого кадра, чтобы поставить метку.', text_color=self.c['yellow'])

    def _mark_end(self):
        point = self.current_position()
        if point:
            self.end.set(point.strftime(STAMP))
        else:
            self.message.configure(text='Дождитесь первого кадра, чтобы поставить метку.', text_color=self.c['yellow'])

    def _download(self):
        if self._selected is None:
            return
        try:
            request = build_interval_request(self._selected, self.start.get(), self.end.get())
        except ValueError as error:
            self.job_label.configure(text=str(error), text_color=self.c['yellow'])
            return
        if self.controller.download(request, Config.get_recordings_dir()):
            self._busy = True
            self.save_btn.configure(state='disabled')
            self.cancel_btn.configure(state='normal')
            self.progress_bar.set(0)
            self.job_label.configure(text=f'Сохраняем {request.channel.name} · {request.duration / 60:.1f} мин',
                                     text_color=self.c['text_secondary'])

    def _toggle_connection(self):
        if self.connection.winfo_ismapped():
            self.connection.grid_remove()
        else:
            self.connection.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, 10))

    def _connect(self):
        try:
            channel = self.controller.add_stream(self.connection_name.get(), self.connection_url.get().strip())
        except ValueError as error:
            self.message.configure(text=str(error), text_color=self.c['yellow'])
            return
        self.source.set('Забава / Wink')
        self.search.set('')
        self.group.set('Все регионы')
        self.availability.set('Все каналы')
        self._populate_channels()
        self._select(channel)
        self.connection.grid_remove()

    def refresh_channels(self):
        if self.controller.refresh(self.provider):
            self._counts[self.provider] = 0
            self._update_summary()

    def _update_summary(self):
        available = sum(family.state(self._results) == 'available' for family in self._families)
        checking = self.provider in self.controller._checking
        self.summary.configure(text=(f'Архив у {available} · проверяем версии…'
                                     if checking else f'Архив доступен у {available} каналов'))
        self.check_btn.configure(state='disabled' if checking else 'normal',
                                 text='Проверяем…' if checking else 'Обновить')

    def _poll(self):
        if not self._running:
            return
        # A large catalog must not monopolise Tk's event loop.
        dirty = False
        try:
            for _ in range(40):
                kind, key, value = self.controller.events.get_nowait()
                if kind == 'catalog':
                    if key == self.provider:
                        self._populate_channels()
                elif kind == 'channel':
                    self._results[key] = value
                    provider = 'ngenix' if key.startswith('stream_') else 'ontvtime'
                    self._counts[provider] = self._counts.get(provider, 0)+1
                    family = self._family_by_slug.get(key)
                    if family and self.tree.exists(family.key):
                        state = family.state(self._results)
                        self.tree.set(family.key, 'state', STATES.get(state, 'Не проверен'))
                        self.tree.item(family.key, tags=(state,))
                    dirty = True
                    if family and self._selected and self._family_by_slug.get(self._selected.slug) == family:
                        self._update_versions()
                    if self._selected and key == self._selected.slug:
                        self._selected_status()
                elif kind == 'checked':
                    dirty = True
                elif kind == 'preview' and key == self.controller._preview_generation and self._active:
                    self._loading = False
                    self.watch_btn.configure(text='Смотреть архив', state='normal')
                    self._play_request = value.request
                    headers = getattr(value, 'headers', {'User-Agent': DEFAULT_UA, 'Referer': value.page_url, 'Origin': BASE_URL})
                    self.preview.show(value.request.channel.name, value.manifest_url, headers,
                                      start_offset=value.offset, duration_limit=value.request.duration)
                    self.preview.set_audio_enabled(self._audio_after_seek)
                    self.message.configure(text='Просмотр архива · «Из кадра» ставит начало и конец фрагмента. Сохранение — отдельной кнопкой.',
                                           text_color=self.c['text_secondary'])
                elif kind == 'preview_error' and key == self.controller._preview_generation:
                    self._loading = False
                    self.watch_btn.configure(text='Смотреть архив', state='normal')
                    self.preview.clear('Архив не подключён')
                    self.clock.configure(text='Нет кадров')
                    self.message.configure(text=value, text_color=self.c['yellow'])
                elif kind in {'saved', 'canceled', 'download_error'}:
                    self._busy = False
                    self.save_btn.configure(state='normal')
                    self.cancel_btn.configure(state='disabled')
                    if kind == 'saved':
                        self._saved_path = value
                        self.progress_bar.set(1)
                        self.job_label.configure(text='Готово · фрагмент сохранён в папку записей', text_color=self.c['green'])
                        self.open_btn.configure(state='normal')
                    else:
                        self.job_label.configure(text=value[:100], text_color=self.c['yellow'])
                    self._validate_interval()
        except Empty:
            pass
        if dirty:
            self._update_summary()
            if self.availability.get() != 'Все каналы' and self._filter_id is None:
                self._filter_id = self.after(400, self._filter)
        audio_label = 'Звук: вкл.' if self.preview._want_audio else 'Звук: выкл.'
        if self.audio_btn.cget('text') != audio_label:
            self.audio_btn.configure(text=audio_label)
        point = self.current_position()
        if point and not self._dragging and self._seek_id is None:
            self.clock.configure(text=f'{point:%d.%m.%Y %H:%M:%S} · МСК')
            self.timeline.set(point.hour*3600+point.minute*60+point.second)
            if (self._active and not self._loading and self.preview._stream is None
                    and self._play_request.end < datetime.now(MOSCOW)-timedelta(seconds=30)):
                self._seek_to(self._play_request.end)
        if self._busy:
            progress = self.controller.progress
            try:
                fraction = float(progress.percent.rstrip('%')) / 100
            except ValueError:
                fraction = 0
            self.progress_bar.set(fraction)
            self.job_label.configure(text=f'{progress.stage} · {progress.percent}', text_color=self.c['text_secondary'])
        self._poll_id = self.after(150, self._poll)

    def _reveal(self):
        if self._saved_path and Path(self._saved_path).exists():
            subprocess.Popen(['open', '-R', self._saved_path])

    def activate(self):
        if not self._active:
            self._active = True
            self._seek_from_fields()

    def deactivate(self):
        self._active = False
        if self._seek_id:
            self.after_cancel(self._seek_id)
            self._seek_id = None
        self.controller.stop_preview()
        self.preview.set_audio_enabled(False)
        self.preview.clear()
        self._play_request = None
        self._loading = False
        self.watch_btn.configure(text='Смотреть архив', state='normal')

    def begin_shutdown(self):
        self._running = False
        self.deactivate()
        self.after_cancel(self._poll_id)
        if self._filter_id:
            self.after_cancel(self._filter_id)
            self._filter_id = None
        if self._resize_id:
            self.after_cancel(self._resize_id)
            self._resize_id = None
        self.preview.stop()
        self.controller.cancel()
