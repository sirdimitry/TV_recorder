# gui/link_list.py
import threading
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

import customtkinter as ctk
from PIL import Image

from core.link_resolver import resolve_link
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
                 on_delete: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.root = parent.winfo_toplevel()
        self.colors = Config.COLORS
        self.recorder = recorder
        self.on_select = on_select
        self.on_edit = on_edit
        self.on_record = on_record
        self.on_delete = on_delete
        self.link_widgets: Dict[str, dict] = {}

        self._setup_ui()

        if self.recorder:
            self.recorder.set_ui_callback(self._on_recorder_update)

    def _setup_ui(self):
        c = self.colors
        header = ctk.CTkLabel(self, text="МОИ ССЫЛКИ", font=ctk.CTkFont(size=12, weight='bold'),
                               text_color=c['text_secondary'])
        header.pack(fill='x', padx=14, pady=(12, 6), anchor='w')

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

        label_name = ctk.CTkLabel(info_frame, text=name, font=ctk.CTkFont(size=13, weight='bold'),
                                   text_color=c['text_primary'], anchor='w')
        label_name.pack(fill='x', anchor='w')

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
        }

        for widget in (row, info_frame, label_name, badge_row):
            widget.bind('<Button-1>', lambda e, n=name: self._on_click(n))
            widget.bind('<Enter>', on_enter)
            widget.bind('<Leave>', on_leave)
        row.bind('<Enter>', on_enter)
        row.bind('<Leave>', on_leave)

        self._resolve_row(name, link, thumb_label, live_dot, status_badge)

    def _resolve_row(self, name: str, link: Dict, thumb_label, live_dot, status_badge):
        """Тянет превью и live/VOD статус через yt-dlp в фоне, не блокируя UI."""
        if link.get('capture_mode') == 'browser':
            # Прямую ссылку на поток для таких сайтов получить в принципе
            # нельзя (иначе бы не понадобился режим браузера) — обычная
            # resolve_link-проверка здесь только впустую сходит в сеть и
            # покажет вводящее в заблуждение "НЕДОСТУПНО".
            c = self.colors
            live_dot.configure(image=get_icon('tv', c['text_muted'], 10))
            status_badge.configure(text="БРАУЗЕР", text_color=c['text_secondary'])
            return

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
                    live_dot.configure(image=get_icon('record', c['text_muted'], 10))
                    status_badge.configure(text="НЕДОСТУПНО", text_color=c['text_muted'])

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _on_click(self, name: str):
        if self.on_select: self.on_select(name)

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

        if link.get('capture_mode') == 'browser':
            # Прямой поток для таких ссылок недоступен (иначе не нужен был
            # бы режим браузера) — MiniPlayer/ffplay здесь просто упадёт с
            # той же ошибкой. Открываем то же окно-браузер, что и при записи,
            # только без захвата экрана — чисто посмотреть.
            import subprocess
            import sys
            from pathlib import Path
            browser_script = Path(__file__).resolve().parent / 'browser_capture.py'
            subprocess.Popen([sys.executable, str(browser_script), url, name])
            logger.info(f"Открыто окно-браузер (просмотр без записи): {name}")
            return

        from gui.mini_player import MiniPlayer
        logger.info(f"Открыт полноэкранный предпросмотр: {name}")
        MiniPlayer(self.root, name, url, fullscreen=True, resolve_via_ytdlp=True)
