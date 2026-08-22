# gui/channel_list.py
import threading
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

import customtkinter as ctk
from PIL import Image

from core.checker import StreamChecker, StreamStatus
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

STATUS_COLOR_MAP = {
    StreamStatus.GREEN: 'green',
    StreamStatus.YELLOW: 'yellow',
    StreamStatus.RED: 'red',
}


class ChannelList(ctk.CTkFrame):
    """Список каналов: карточки с логотипом, индикатором статуса и действиями."""

    LOGO_SIZE = 38
    ROW_HEIGHT = 60

    def __init__(self, parent, recorder=None, on_select: Optional[Callable] = None,
                 on_edit: Optional[Callable] = None, on_record: Optional[Callable] = None,
                 on_delete: Optional[Callable] = None, on_preview: Optional[Callable] = None,
                 on_add: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.root = parent.winfo_toplevel()
        self.colors = Config.COLORS
        self.recorder = recorder
        self.on_select = on_select
        self.on_edit = on_edit
        self.on_record = on_record
        self.on_delete = on_delete
        self.on_preview = on_preview
        self.on_add = on_add
        self.checker = StreamChecker()
        self.channel_widgets: Dict[str, dict] = {}

        self._setup_ui()

        if self.recorder:
            self.recorder.set_ui_callback(self._on_recorder_update)

    def _setup_ui(self):
        c = self.colors
        header_row = ctk.CTkFrame(self, fg_color='transparent')
        header_row.pack(fill='x', padx=14, pady=(12, 6))
        header = ctk.CTkLabel(header_row, text="КАНАЛЫ", font=ctk.CTkFont(size=12, weight='bold'),
                               text_color=c['text_secondary'])
        header.pack(side='left', anchor='w')
        if self.on_add:
            ctk.CTkButton(header_row, text="", image=get_icon('plus', c['accent_text'], 14), width=26, height=26,
                          corner_radius=Config.RADIUS_SM, fg_color=c['accent'], hover_color=c['accent_hover'],
                          command=self.on_add).pack(side='right')

        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color='transparent')
        self.scroll_frame.pack(fill='both', expand=True, padx=6, pady=(0, 6))

    def load_channels(self, channels: List[Dict]):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self.channel_widgets.clear()

        for channel in channels:
            self._add_channel_row(channel)

        self._refresh_record_buttons()

    def _add_channel_row(self, channel: Dict):
        c = self.colors
        name = channel.get('name', 'Unknown')
        source_type = channel.get('type', 'iptv')

        row = ctk.CTkFrame(self.scroll_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM,
                           height=self.ROW_HEIGHT)
        row.pack(fill='x', padx=4, pady=3)
        row.grid_propagate(False)

        def on_enter(_e=None):
            row.configure(fg_color=c['bg_hover'])

        def on_leave(_e=None):
            row.configure(fg_color=c['bg_secondary'])

        # Логотип
        logo_label = ctk.CTkLabel(row, text="", width=self.LOGO_SIZE, height=self.LOGO_SIZE,
                                   corner_radius=8, fg_color=c['bg_tertiary'])
        logo_label.grid(row=0, column=0, padx=(10, 10), pady=8, sticky='ns')
        logo_label.bind('<Button-1>', lambda e, ch=channel: self._open_preview(ch))

        # Индикатор статуса поверх логотипа
        status_dot = ctk.CTkLabel(row, text="", image=get_icon('record', c['text_muted'], 10))
        status_dot.place(in_=logo_label, relx=1.0, rely=1.0, x=-2, y=-2, anchor='se')

        # Информация
        info_frame = ctk.CTkFrame(row, fg_color='transparent')
        info_frame.grid(row=0, column=1, sticky='nsew', pady=8)
        row.columnconfigure(1, weight=1)

        label_name = ctk.CTkLabel(info_frame, text=name, font=ctk.CTkFont(size=13, weight='bold'),
                                   text_color=c['text_primary'], anchor='w')
        label_name.pack(fill='x', anchor='w')

        badge = ctk.CTkLabel(info_frame, text=source_type.upper(), font=ctk.CTkFont(size=9, weight='bold'),
                              text_color=c['text_secondary'], fg_color=c['bg_tertiary'],
                              corner_radius=5, width=1, height=16)
        badge.pack(anchor='w', pady=(3, 0), ipadx=4)

        # Кнопки действий
        actions = ctk.CTkFrame(row, fg_color='transparent')
        actions.grid(row=0, column=2, padx=(4, 8), pady=8)

        def icon_btn(parent, icon_name, color, tooltip_cmd):
            return ctk.CTkButton(parent, text="", image=get_icon(icon_name, color, 18), width=34, height=34,
                                  corner_radius=Config.RADIUS_SM, fg_color='transparent',
                                  hover_color=c['bg_active'], command=tooltip_cmd)

        btn_record = icon_btn(actions, 'record', c['red'], lambda n=name, ch=channel: self._on_record(n, ch))
        btn_record.pack(side='left', padx=1)

        btn_edit = icon_btn(actions, 'edit', c['text_secondary'], lambda n=name, ch=channel: self._on_edit(n, ch))
        btn_edit.pack(side='left', padx=1)

        btn_delete = icon_btn(actions, 'trash', c['text_secondary'], lambda n=name: self._on_delete(n))
        btn_delete.pack(side='left', padx=1)

        self.channel_widgets[name] = {
            'row': row,
            'logo_label': logo_label,
            'status_dot': status_dot,
            'btn_record': btn_record,
            'channel': channel,
        }

        for widget in (row, info_frame, label_name, badge):
            widget.bind('<Button-1>', lambda e, n=name: self._on_click(n))
            widget.bind('<Enter>', on_enter)
            widget.bind('<Leave>', on_leave)
        row.bind('<Enter>', on_enter)
        row.bind('<Leave>', on_leave)

        self._load_logo(name, channel.get('logo_url', ''), logo_label)

    def _load_logo(self, name: str, logo_url: str, logo_label: ctk.CTkLabel):
        from utils.logo_cache import LogoCache

        def worker():
            cache = LogoCache()
            logo_path = cache.get_logo_path(name, logo_url)
            image = None
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
                    logger.debug(f"Ошибка отображения логотипа {name}: {e}")

            def apply():
                if name not in self.channel_widgets:
                    return
                if image is not None:
                    logo_label.configure(image=image)
                    logo_label._logo_ref = image
                else:
                    logo_label.configure(image=get_icon('tv', self.colors['text_muted'], 20))

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _on_click(self, name: str):
        if self.on_select: self.on_select(name)

    def _on_edit(self, name: str, channel: Dict):
        if self.on_edit: self.on_edit(name, channel)

    def _on_delete(self, name: str):
        if self.on_delete: self.on_delete(name)

    def _on_record(self, name: str, channel: Dict):
        if self.recorder:
            task_id = self.recorder.find_active_task_id(name)
            if task_id:
                self.recorder.stop_recording(task_id)
                return
        if self.on_record:
            self.on_record(name, channel)
        else:
            logger.info(f"Запрос записи канала: {name}")

    def _on_recorder_update(self):
        self.after(0, self._refresh_record_buttons)

    def _refresh_record_buttons(self):
        if not self.recorder:
            return
        c = self.colors
        active_names = {t.channel_name for t in self.recorder.get_all_tasks() if t.is_recording}
        for name, widgets in self.channel_widgets.items():
            btn = widgets['btn_record']
            if name in active_names:
                btn.configure(image=get_icon('stop', c['red'], 18), fg_color=c['bg_active'])
            else:
                btn.configure(image=get_icon('record', c['red'], 18), fg_color='transparent')

    def _open_preview(self, channel: Dict):
        from core.recorder import Recorder

        name = channel.get('name', 'Unknown')
        url = channel.get('url', '')

        if not url:
            messagebox.showwarning("Внимание", f"У канала '{name}' нет URL потока")
            return

        headers_info = Recorder.CHANNEL_HEADERS.get(name, {})
        headers = None
        if headers_info:
            headers = {}
            if headers_info.get('ua'):
                headers['User-Agent'] = headers_info['ua']
            if headers_info.get('ref'):
                headers['Referer'] = headers_info['ref']
                headers['Origin'] = headers_info['ref']

        logger.info(f"Открыт предпросмотр: {name}")
        if self.on_preview:
            self.on_preview(name, url, headers)

    def _check_single(self, name: str, status_dot: ctk.CTkLabel, channel: Dict):
        status_dot.configure(image=get_icon('record', self.colors['yellow'], 10))

        def do_check():
            status, msg = self.checker.check(channel)
            color = self.colors.get(STATUS_COLOR_MAP.get(status, ''), self.colors['text_muted'])

            def apply():
                if name in self.channel_widgets:
                    status_dot.configure(image=get_icon('record', color, 10))
            self.after(0, apply)
            logger.info(f"Проверка {name}: {status.value} — {msg}")

        threading.Thread(target=do_check, daemon=True).start()

    def check_all(self):
        for name, widgets in self.channel_widgets.items():
            self._check_single(name, widgets['status_dot'], widgets['channel'])

    def update_indicator(self, name: str, status: StreamStatus):
        if name in self.channel_widgets:
            color = self.colors.get(STATUS_COLOR_MAP.get(status, ''), self.colors['text_muted'])
            self.channel_widgets[name]['status_dot'].configure(image=get_icon('record', color, 10))
