# gui/download_list.py
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import customtkinter as ctk
from PIL import Image

from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger

def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ''
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_speed(bps: Optional[float]) -> str:
    if not bps or bps <= 0:
        return ''
    units = ['Б/с', 'КБ/с', 'МБ/с', 'ГБ/с']
    value = float(bps)
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}"


def _format_eta(seconds: Optional[float]) -> str:
    if seconds is None or seconds <= 0:
        return ''
    return "осталось ~" + _format_duration(seconds)


class DownloadList(ctk.CTkFrame):
    """Список загрузок ("Загрузки") — присланная ссылка ищется универсально
    (core/link_resolver.py: yt-dlp -> HTML-скрейп -> sniff через встроенный
    браузер), находится прямой поток (при необходимости — видео и звук
    раздельно) и копируется в один готовый файл (core/downloader.py),
    без перекодирования — тот же принцип, что уже работает в "Мои ссылки"."""

    LOGO_SIZE = 44
    ROW_HEIGHT = 64
    NAME_MAX = 50

    def __init__(self, parent, downloader=None, storage=None, on_add: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.root = parent.winfo_toplevel()
        self.colors = Config.COLORS
        self.downloader = downloader
        self.storage = storage
        self.on_add = on_add
        self.row_widgets: Dict[str, dict] = {}

        self._setup_ui()

        if self.downloader:
            self.downloader.set_ui_callback(self._on_downloader_update)

    def _setup_ui(self):
        c = self.colors
        header_row = ctk.CTkFrame(self, fg_color='transparent')
        header_row.pack(fill='x', padx=14, pady=(12, 6))
        header = ctk.CTkLabel(header_row, text="ЗАГРУЗКИ", font=ctk.CTkFont(size=12, weight='bold'),
                               text_color=c['text_secondary'])
        header.pack(side='left', anchor='w')
        if self.on_add:
            ctk.CTkButton(header_row, text="", image=get_icon('plus', c['accent_text'], 14), width=26, height=26,
                          corner_radius=Config.RADIUS_SM, fg_color=c['accent'], hover_color=c['accent_hover'],
                          command=self.on_add).pack(side='right')

        hint = ctk.CTkLabel(
            self, text="Пришлите ссылку — найдём в ней видео (при необходимости отдельно "
                       "звук) и сохраним одним файлом.",
            font=ctk.CTkFont(size=10), text_color=c['text_muted'], wraplength=280, justify='left')
        hint.pack(fill='x', padx=14, pady=(0, 8), anchor='w')

        self.scroll_frame = ctk.CTkScrollableFrame(self, fg_color='transparent')
        self.scroll_frame.pack(fill='both', expand=True, padx=6, pady=(0, 6))

    def load_downloads(self, items: List[Dict]):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self.row_widgets.clear()

        # 'downloading' в данных с диска — это либо реально идущая сейчас
        # задача (тогда в self.downloader есть живая DownloadTask с тем же
        # id — эта функция может звlater перевызываться, пока загрузка ещё
        # идёт, и её статус трогать нельзя), либо хвост от прошлого запуска
        # приложения, который прервали закрытием — тогда живой задачи с
        # таким id уже нет и не будет, значит это тихая ошибка, а не
        # "качается" навечно.
        live_ids = {t.task_id for t in self.downloader.get_all_downloads()} if self.downloader else set()
        for item in items:
            if item.get('status') == 'downloading' and item.get('id') not in live_ids:
                item['status'] = 'error'
                item['error_message'] = item.get('error_message') or 'Прервано закрытием приложения'

        if not items:
            ctk.CTkLabel(self.scroll_frame, text="Пока нет ни одной загрузки",
                         font=ctk.CTkFont(size=11), text_color=self.colors['text_muted']).pack(pady=18)
            return

        for item in items:
            self._add_row(item)

    def _add_row(self, item: Dict):
        c = self.colors
        download_id = item.get('id', '')
        name = item.get('name') or item.get('url', 'Unknown')
        display_name = name if len(name) <= self.NAME_MAX else name[:self.NAME_MAX] + '…'

        row = ctk.CTkFrame(self.scroll_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM,
                           height=self.ROW_HEIGHT)
        row.pack(fill='x', padx=4, pady=3)
        row.grid_propagate(False)

        thumb_label = ctk.CTkLabel(row, text="", width=self.LOGO_SIZE, height=self.LOGO_SIZE,
                                    corner_radius=8, fg_color=c['bg_tertiary'],
                                    image=get_icon('download', c['text_muted'], 22))
        thumb_label.grid(row=0, column=0, padx=(10, 10), pady=8, sticky='ns')

        info_frame = ctk.CTkFrame(row, fg_color='transparent')
        info_frame.grid(row=0, column=1, sticky='nsew', pady=8)
        row.columnconfigure(1, weight=1)

        label_name = ctk.CTkLabel(info_frame, text=display_name, font=ctk.CTkFont(size=13, weight='bold'),
                                   text_color=c['text_primary'], anchor='w')
        label_name.pack(fill='x', anchor='w')

        badge_row = ctk.CTkFrame(info_frame, fg_color='transparent')
        badge_row.pack(anchor='w', pady=(3, 0))

        duration_label = _format_duration(item.get('duration'))
        type_text = "MP4" + (f" · {duration_label}" if duration_label else "")
        badge = ctk.CTkLabel(badge_row, text=type_text,
                              font=ctk.CTkFont(size=9, weight='bold'), text_color=c['text_secondary'],
                              fg_color=c['bg_tertiary'], corner_radius=5, width=1, height=16)
        badge.pack(side='left', ipadx=4)

        # Текстовый статус ("ГОТОВО" и т.п.) не влезал в узкую колонку и
        # обрезался — точка тем же цветом занимает в разы меньше места и
        # достаточно понятна сама по себе (зелёный/красный и так читаются).
        status_dot = ctk.CTkLabel(badge_row, text="", image=get_icon('record', c['text_muted'], 10))
        status_dot.pack(side='left', padx=(6, 0))

        progress_bar = ctk.CTkProgressBar(info_frame, height=4, corner_radius=2,
                                           fg_color=c['bg_tertiary'], progress_color=c['accent'])
        progress_bar.set(0)

        # Отдельная строка под полоской — процент/скорость/ETA. Пустой
        # текст, пока не качается, но всегда упакована вместе с
        # progress_bar (см. _apply_status) — держим её в том же
        # прижатом-к-полоске месте, а не после error_label.
        progress_label = ctk.CTkLabel(info_frame, text="", font=ctk.CTkFont(size=9),
                                       text_color=c['text_muted'], anchor='w')

        error_label = ctk.CTkLabel(info_frame, text="", font=ctk.CTkFont(size=9), text_color=c['red'],
                                    anchor='w', wraplength=220, justify='left')
        error_label.pack(fill='x', anchor='w')

        actions = ctk.CTkFrame(row, fg_color='transparent')
        actions.grid(row=0, column=2, padx=(4, 8), pady=8)

        def icon_btn(parent, icon_name, color, command):
            return ctk.CTkButton(parent, text="", image=get_icon(icon_name, color, 18), width=34, height=34,
                                  corner_radius=Config.RADIUS_SM, fg_color='transparent',
                                  hover_color=c['bg_active'], command=command)

        # Смотрят в self.row_widgets[download_id]['item'] в момент клика, а
        # не замыкают текущий item — иначе после обновления статуса
        # (_refresh_from_downloader кладёт новый dict) кнопки продолжали бы
        # работать со старыми данными (например, с ещё пустым output_path).
        btn_folder = icon_btn(actions, 'folder', c['text_secondary'],
                               lambda d=download_id: self._open_folder(d))
        btn_folder.pack(side='left', padx=1)

        btn_cancel = icon_btn(actions, 'stop', c['red'], lambda d=download_id: self._on_cancel(d))
        btn_cancel.pack(side='left', padx=1)

        btn_delete = icon_btn(actions, 'trash', c['text_secondary'], lambda d=download_id: self._on_delete(d))
        btn_delete.pack(side='left', padx=1)

        self.row_widgets[download_id] = {
            'row': row, 'thumb_label': thumb_label, 'label_name': label_name, 'badge': badge,
            'status_dot': status_dot, 'error_label': error_label, 'progress_bar': progress_bar,
            'progress_label': progress_label,
            'btn_folder': btn_folder, 'btn_cancel': btn_cancel, 'item': item,
            'thumbnail_loaded': False, 'progress_shown': False,
        }
        self._apply_status(download_id, item)

    def _apply_status(self, download_id: str, item: Dict):
        widgets = self.row_widgets.get(download_id)
        if not widgets:
            return
        c = self.colors
        widgets['item'] = item

        name = item.get('name') or item.get('url', 'Unknown')
        display_name = name if len(name) <= self.NAME_MAX else name[:self.NAME_MAX] + '…'
        widgets['label_name'].configure(text=display_name)

        duration_label = _format_duration(item.get('duration'))
        widgets['badge'].configure(text="MP4" + (f" · {duration_label}" if duration_label else ""))

        status = item.get('status', 'resolving')
        colors_by_status = {
            'resolving': c['text_muted'], 'downloading': c['accent'],
            'done': c['green'], 'error': c['red'], 'canceled': c['text_muted'],
        }
        dot_color = colors_by_status.get(status, c['text_muted'])
        widgets['status_dot'].configure(image=get_icon('record', dot_color, 10))
        error_message = item.get('error_message') if status == 'error' else ''
        widgets['error_label'].configure(text=error_message or '')

        # Полоска прогресса — с самого начала статуса 'downloading', даже
        # пока процент ещё не из чего посчитать (медленный источник вроде
        # 1tv.ru может по минуте не отдавать ffmpeg вообще ничего, пока
        # тянется самый первый сегмент — раньше это выглядело так, будто
        # ничего не происходит вообще; теперь хотя бы "подключение…" сразу
        # показывает, что задача жива, а не зависла).
        progress = item.get('progress')
        show_progress = status == 'downloading'
        if show_progress:
            if progress is not None:
                widgets['progress_bar'].set(max(0.0, min(1.0, progress / 100)))
                parts = [f"{progress:.0f}%"]
            else:
                widgets['progress_bar'].set(0)
                parts = ["подключение…"]
            speed_text = _format_speed(item.get('speed_bps'))
            if speed_text:
                parts.append(speed_text)
            eta_text = _format_eta(item.get('eta_seconds'))
            if eta_text:
                parts.append(eta_text)
            widgets['progress_label'].configure(text=" · ".join(parts))
            if not widgets['progress_shown']:
                widgets['progress_bar'].pack(fill='x', anchor='w', pady=(4, 0))
                widgets['progress_label'].pack(fill='x', anchor='w', pady=(1, 0))
                widgets['progress_shown'] = True
        elif widgets['progress_shown']:
            widgets['progress_bar'].pack_forget()
            widgets['progress_label'].pack_forget()
            widgets['progress_shown'] = False

        widgets['btn_folder'].configure(state='normal' if status == 'done' else 'disabled')
        widgets['btn_cancel'].configure(
            state='normal' if status in ('resolving', 'downloading') else 'disabled')

        # Превью появляется только после resolve — при создании строки
        # (status='resolving') его ещё нет, а на этот момент вызывается
        # только этот метод (не _load_thumbnail отдельно), поэтому ловим
        # его тут же, как только thumbnail станет непустым.
        if item.get('thumbnail') and not widgets['thumbnail_loaded']:
            widgets['thumbnail_loaded'] = True
            self._load_thumbnail(download_id, item)

    def _load_thumbnail(self, download_id: str, item: Dict):
        thumbnail_url = item.get('thumbnail')
        if not thumbnail_url:
            return

        def worker():
            try:
                from utils.logo_cache import LogoCache
                cache = LogoCache()
                thumb_path = cache.get_logo_path(download_id, thumbnail_url)
                if not thumb_path:
                    return
                pil_img = Image.open(thumb_path).convert('RGBA')
                pil_img.thumbnail((self.LOGO_SIZE, self.LOGO_SIZE), Image.LANCZOS)
                canvas = Image.new('RGBA', (self.LOGO_SIZE, self.LOGO_SIZE), (0, 0, 0, 0))
                offset = ((self.LOGO_SIZE - pil_img.width) // 2, (self.LOGO_SIZE - pil_img.height) // 2)
                canvas.paste(pil_img, offset, pil_img)
                image = ctk.CTkImage(light_image=canvas, dark_image=canvas,
                                      size=(self.LOGO_SIZE, self.LOGO_SIZE))
            except Exception as e:
                logger.debug(f"DownloadList: ошибка превью {download_id}: {e}")
                return

            def apply():
                widgets = self.row_widgets.get(download_id)
                if widgets:
                    widgets['thumb_label'].configure(image=image)
            self.after(0, apply)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _open_folder(self, download_id: str):
        item = self.row_widgets.get(download_id, {}).get('item', {})
        output_path = item.get('output_path')
        if not output_path or not Path(output_path).exists():
            return
        if sys.platform == 'darwin':
            subprocess.run(['open', '-R', output_path])
        else:
            subprocess.run(['open', str(Path(output_path).parent)])

    def _on_cancel(self, download_id: str):
        # cancel_download() сам зовёт _notify_ui() -> _refresh_from_downloader,
        # который и обновит строку, и сохранит новый статус через storage —
        # отдельно дублировать это здесь незачем.
        if self.downloader:
            self.downloader.cancel_download(download_id)

    def _on_delete(self, download_id: str):
        # "Удалить" убирает строку из списка — файл уже готовой загрузки не
        # трогаем (cancel_download() удалил бы его, но это для НЕЗАВЕРШЁННОЙ
        # закачки — здесь только чистим саму задачу/историю).
        item = self.row_widgets.get(download_id, {}).get('item', {})
        if self.downloader:
            if item.get('status') in ('resolving', 'downloading'):
                self.downloader.cancel_download(download_id)
            self.downloader.remove_task(download_id)
        if self.storage:
            self.storage.delete_download(download_id)
        widgets = self.row_widgets.pop(download_id, None)
        if widgets:
            widgets['row'].destroy()

    def _on_downloader_update(self):
        self.after(0, self._refresh_from_downloader)

    def _refresh_from_downloader(self):
        if not self.downloader:
            return
        for task in self.downloader.get_all_downloads():
            item = task.to_dict()
            if self.storage:
                self.storage.save_download(item)
            if task.task_id in self.row_widgets:
                # Название/хронометраж известны только после resolve —
                # _apply_status обновляет текст строки на месте, не
                # перестраивая её целиком.
                self._apply_status(task.task_id, item)
            else:
                self._add_row(item)
