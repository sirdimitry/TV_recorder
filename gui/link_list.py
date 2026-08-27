# gui/link_list.py
import threading
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

import customtkinter as ctk
from PIL import Image

from core.link_resolver import resolve_link
from gui.download_list import _elide_text
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

TYPE_LABELS = {
    'youtube': 'YOUTUBE', 'vk': 'VK', 'rutube': 'RUTUBE',
    'twitch': 'TWITCH', '1tv': '1TV', 'other': 'ССЫЛКА',
}


class LinkList(ctk.CTkFrame):
    """Список вручную добавленных ссылок (YouTube/VK/RuTube/Twitch/страницы
    с эфиром или готовой записью) — карточки с превью, статусом live/VOD
    и теми же действиями, что и у обычных каналов."""

    LOGO_SIZE = 38
    ROW_HEIGHT = 60

    def __init__(self, parent, recorder=None, on_select: Optional[Callable] = None,
                 on_edit: Optional[Callable] = None, on_record: Optional[Callable] = None,
                 on_delete: Optional[Callable] = None, on_add: Optional[Callable] = None,
                 on_preview: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.root = parent.winfo_toplevel()
        self.colors = Config.COLORS
        self.recorder = recorder
        self.on_select = on_select
        self.on_edit = on_edit
        self.on_record = on_record
        self.on_delete = on_delete
        self.on_add = on_add
        self.on_preview = on_preview
        self.link_widgets: Dict[str, dict] = {}
        # Общий инстанс шрифта, которым реально измеряется и рисуется текст —
        # см. _elide_text/_update_row_width (тот же приём, что и в
        # gui/download_list.py: без общего инстанса измерение было бы
        # приблизительным, а не пиксель-в-пиксель точным).
        self._name_font = ctk.CTkFont(size=13, weight='bold')

        self._setup_ui()

        if self.recorder:
            self.recorder.set_ui_callback(self._on_recorder_update)

    def _setup_ui(self):
        c = self.colors
        header_row = ctk.CTkFrame(self, fg_color='transparent')
        header_row.pack(fill='x', padx=14, pady=(12, 6))
        if self.on_add:
            ctk.CTkButton(header_row, text="", image=get_icon('plus', c['accent_text'], 14), width=26, height=26,
                          corner_radius=Config.RADIUS_SM, fg_color=c['accent'], hover_color=c['accent_hover'],
                          command=self.on_add).pack(side='right')

        hint = ctk.CTkLabel(self, text="Прямой эфир или готовая запись с YouTube, VK, RuTube, Twitch и т.п.",
                             font=ctk.CTkFont(size=10), text_color=c['text_muted'], wraplength=280, justify='left')
        hint.pack(fill='x', padx=14, pady=(0, 8), anchor='w')

        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color='transparent')
        self.scroll_frame.pack(fill='both', expand=True, padx=6, pady=(0, 6))

    def load_links(self, links: List[Dict]):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self.link_widgets.clear()

        if not links:
            ctk.CTkLabel(self.scroll_frame, text="Пока нет ни одной ссылки",
                         font=ctk.CTkFont(size=11), text_color=self.colors['text_muted']).pack(pady=18)
            return

        for link in links:
            self._add_link_row(link)

        self._refresh_record_buttons()

    def _add_link_row(self, link: Dict):
        c = self.colors
        name = link.get('name', 'Unknown')
        link_type = link.get('type', 'other')

        row = ctk.CTkFrame(self.scroll_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM,
                           height=self.ROW_HEIGHT)
        row.pack(fill='x', padx=4, pady=3)
        row.grid_propagate(False)

        def on_enter(_e=None):
            row.configure(fg_color=c['bg_hover'])

        def on_leave(_e=None):
            row.configure(fg_color=c['bg_secondary'])

        thumb_label = ctk.CTkLabel(row, text="", width=self.LOGO_SIZE, height=self.LOGO_SIZE,
                                    corner_radius=8, fg_color=c['bg_tertiary'],
                                    image=get_icon('tv', c['text_muted'], 20))
        thumb_label.grid(row=0, column=0, padx=(10, 10), pady=8, sticky='ns')
        thumb_label.bind('<Button-1>', lambda e, l=link: self._open_preview(l))

        live_dot = ctk.CTkLabel(row, text="", image=get_icon('record', c['text_muted'], 10))
        live_dot.place(in_=thumb_label, relx=1.0, rely=1.0, x=-2, y=-2, anchor='se')

        info_frame = ctk.CTkFrame(row, fg_color='transparent')
        info_frame.grid(row=0, column=1, sticky='nsew', pady=8)
        row.columnconfigure(1, weight=1)

        label_name = ctk.CTkLabel(info_frame, text=name, font=self._name_font,
                                   text_color=c['text_primary'], anchor='w')
        label_name.pack(fill='x', anchor='w')
        # Без обрезки по реальной ширине длинное название просто вылезало
        # за пределы узкой боковой панели (или отображалось Tk обрубленным
        # без многоточия) — та же проблема и то же решение, что уже
        # починили в gui/download_list.py (_elide_text/_update_row_widths).
        info_frame.bind('<Configure>', lambda e, n=name: self._update_row_width(n, e.width))

        badge_row = ctk.CTkFrame(info_frame, fg_color='transparent')
        badge_row.pack(anchor='w', pady=(3, 0))

        badge = ctk.CTkLabel(badge_row, text=TYPE_LABELS.get(link_type, 'ССЫЛКА'),
                              font=ctk.CTkFont(size=9, weight='bold'), text_color=c['text_secondary'],
                              fg_color=c['bg_tertiary'], corner_radius=5, width=1, height=16)
        badge.pack(side='left', ipadx=4)

        status_badge = ctk.CTkLabel(badge_row, text="", font=ctk.CTkFont(size=9, weight='bold'),
                                     text_color=c['text_muted'], width=1, height=16)
        status_badge.pack(side='left', padx=(6, 0))

        actions = ctk.CTkFrame(row, fg_color='transparent')
        actions.grid(row=0, column=2, padx=(4, 8), pady=8)

        def icon_btn(parent, icon_name, color, command):
            return ctk.CTkButton(parent, text="", image=get_icon(icon_name, color, 18), width=34, height=34,
                                  corner_radius=Config.RADIUS_SM, fg_color='transparent',
                                  hover_color=c['bg_active'], command=command)

        btn_record = icon_btn(actions, 'record', c['red'], lambda n=name, l=link: self._on_record(n, l))
        btn_record.pack(side='left', padx=1)

        btn_edit = icon_btn(actions, 'edit', c['text_secondary'], lambda n=name, l=link: self._on_edit(n, l))
        btn_edit.pack(side='left', padx=1)

        btn_delete = icon_btn(actions, 'trash', c['text_secondary'], lambda n=name: self._on_delete(n))
        btn_delete.pack(side='left', padx=1)

        self.link_widgets[name] = {
            'row': row, 'thumb_label': thumb_label, 'live_dot': live_dot,
            'status_badge': status_badge, 'btn_record': btn_record, 'link': link,
            'label_name': label_name, 'full_name': name,
        }

        for widget in (row, info_frame, label_name, badge_row):
            widget.bind('<Button-1>', lambda e, n=name: self._on_click(n))
            widget.bind('<Enter>', on_enter)
            widget.bind('<Leave>', on_leave)
        row.bind('<Enter>', on_enter)
        row.bind('<Leave>', on_leave)

        self._resolve_row(name, link, thumb_label, live_dot, status_badge)

    def _update_row_width(self, name: str, available_width: int):
        widgets = self.link_widgets.get(name)
        if not widgets or available_width <= 1:
            return
        widgets['label_name'].configure(
            text=_elide_text(self._name_font, widgets['full_name'], available_width))

    def _resolve_row(self, name: str, link: Dict, thumb_label, live_dot, status_badge):
        """Тянет превью и live/VOD статус через yt-dlp в фоне, не блокируя UI."""
        def worker():
            info = resolve_link(link.get('url', ''))
            image = None
            if info.ok and info.thumbnail:
                try:
                    from utils.logo_cache import LogoCache
                    cache = LogoCache()
                    thumb_path = cache.get_logo_path(name, info.thumbnail)
                    if thumb_path:
                        pil_img = Image.open(thumb_path).convert('RGBA')
                        pil_img.thumbnail((self.LOGO_SIZE, self.LOGO_SIZE), Image.LANCZOS)
                        canvas = Image.new('RGBA', (self.LOGO_SIZE, self.LOGO_SIZE), (0, 0, 0, 0))
                        offset = ((self.LOGO_SIZE - pil_img.width) // 2, (self.LOGO_SIZE - pil_img.height) // 2)
                        canvas.paste(pil_img, offset, pil_img)
                        image = ctk.CTkImage(light_image=canvas, dark_image=canvas,
                                              size=(self.LOGO_SIZE, self.LOGO_SIZE))
                except Exception as e:
                    logger.debug(f"LinkList: ошибка превью {name}: {e}")

            def apply():
                if name not in self.link_widgets:
                    return
                # Кэшируем — по клику на строку показываем встроенное превью
                # (PreviewPanel) без повторного resolve_link на каждый клик.
                self.link_widgets[name]['resolved_info'] = info
                c = self.colors
                if image is not None:
                    thumb_label.configure(image=image)
                    thumb_label._logo_ref = image
                if info.ok:
                    dot_color = c['red'] if info.is_live else c['green']
                    live_dot.configure(image=get_icon('record', dot_color, 10))
                    status_badge.configure(text="В ЭФИРЕ" if info.is_live else "ЗАПИСЬ",
                                           text_color=c['red'] if info.is_live else c['text_secondary'])
                else:
                    # Прямой поток не нашёлся автоматически — но это больше
                    # не тупик: запись всё равно можно начать, просто она
                    # пойдёт через видимое окно браузера с захватом экрана
                    # вместо копирования потока (см. AppWindow._record_link_now).
                    # Раньше для этого нужно было вручную дублировать ссылку
                    # во вкладку "Браузер" — теперь это происходит само.
                    live_dot.configure(image=get_icon('record', c['accent'], 10))
                    status_badge.configure(text="ЧЕРЕЗ БРАУЗЕР", text_color=c['accent'])

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _on_click(self, name: str):
        if self.on_select:
            self.on_select(name)
        if self.on_preview:
            widgets = self.link_widgets.get(name)
            info = widgets.get('resolved_info') if widgets else None
            if info and info.ok and info.video_url:
                self.on_preview(name, info.video_url, info.headers)

    def _on_edit(self, name: str, link: Dict):
        if self.on_edit: self.on_edit(name, link)

    def _on_delete(self, name: str):
        if self.on_delete: self.on_delete(name)

    def _on_record(self, name: str, link: Dict):
        if self.recorder:
            task_id = self.recorder.find_active_task_id(name)
            if task_id:
                self.recorder.stop_recording(task_id)
                return
        if self.on_record:
            self.on_record(name, link)
        else:
            logger.info(f"Запрос записи ссылки: {name}")

    def set_row_resolving(self, name: str, resolving: bool):
        """Пока идёт resolve_link() перед стартом записи (может занимать до
        минуты на тяжёлых сайтах — otr-online.ru, tass.ru и т.п.), без этого
        индикатора непонятно, работает ли приложение или зависло: строка
        просто молчала до появления записи или окна с ошибкой. Показываем
        "ИЩЕМ ПОТОК…" на бейдже статуса и блокируем повторный клик по
        записи, пока не определится результат (прямой поток / браузер /
        ошибка) — см. gui/app_window.py:_record_link_now."""
        widgets = self.link_widgets.get(name)
        if not widgets:
            return
        badge = widgets['status_badge']
        c = self.colors
        if resolving:
            if '_prev_badge' not in widgets:
                widgets['_prev_badge'] = (badge.cget('text'), badge.cget('text_color'))
            badge.configure(text="ИЩЕМ ПОТОК…", text_color=c['accent'])
            widgets['btn_record'].configure(state='disabled')
        else:
            widgets['btn_record'].configure(state='normal')
            prev = widgets.pop('_prev_badge', None)
            if prev:
                badge.configure(text=prev[0], text_color=prev[1])

    def _on_recorder_update(self):
        self.after(0, self._refresh_record_buttons)

    def _refresh_record_buttons(self):
        if not self.recorder:
            return
        c = self.colors
        active_names = {t.channel_name for t in self.recorder.get_all_tasks() if t.is_recording}
        for name, widgets in self.link_widgets.items():
            btn = widgets['btn_record']
            if name in active_names:
                btn.configure(image=get_icon('stop', c['red'], 18), fg_color=c['bg_active'])
            else:
                btn.configure(image=get_icon('record', c['red'], 18), fg_color='transparent')

    def _open_preview(self, link: Dict):
        name = link.get('name', 'Unknown')
        url = link.get('url', '')

        if not url:
            messagebox.showwarning("Внимание", f"У «{name}» нет ссылки")
            return

        from gui.mini_player import MiniPlayer
        logger.info(f"Открыт крупный предпросмотр: {name}")
        MiniPlayer(self.root, name, url, large=True, resolve_via_ytdlp=True)
