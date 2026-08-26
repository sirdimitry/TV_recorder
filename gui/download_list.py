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


def _format_size(num_bytes: Optional[int]) -> str:
    if not num_bytes or num_bytes <= 0:
        return ''
    units = ['Б', 'КБ', 'МБ', 'ГБ']
    value = float(num_bytes)
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}"


def _elide_text(font, text: str, max_width: int) -> str:
    """Обрезает text с многоточием, чтобы уместиться в max_width пикселей
    заданного шрифта — в отличие от обрезки по числу символов (было:
    NAME_MAX), учитывает реальную ширину букв и реальную ширину строки,
    поэтому остаётся корректным при любом размере окна. Только имя ролика
    имеет право сокращаться (см. _update_row_widths) — вся индикация
    статуса/прогресса всегда занимает своё место первой, имени достаётся
    то, что осталось."""
    if max_width <= 0 or font.measure(text) <= max_width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + '…') <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + '…') if lo > 0 else '…'


class DownloadList(ctk.CTkFrame):
    """Список загрузок ("Загрузки") — присланная ссылка ищется универсально
    (core/link_resolver.py: yt-dlp -> HTML-скрейп -> sniff через встроенный
    браузер), находится прямой поток (при необходимости — видео и звук
    раздельно) и копируется в один готовый файл (core/downloader.py),
    без перекодирования — тот же принцип, что уже работает в "Мои ссылки"."""

    LOGO_SIZE = 44
    # Высота карточки — фиксированная (grid_propagate(False) ниже), поэтому
    # должна вмещать самое насыщенное состояние с запасом: имя + бейдж/точка
    # статуса + тонкая полоска прогресса с процентом под ней. 64px (старое
    # значение) реально хватало только на имя+бейдж — полоска и подпись
    # прогресса рисовались уже НИЖЕ видимой границы карточки и были
    # обрезаны grid_propagate начисто (подтверждено замером виджетов:
    # progress_bar оказывался на y=91 при высоте строки 64).
    ROW_HEIGHT = 86

    def __init__(self, parent, downloader=None, storage=None, on_add: Optional[Callable] = None):
        super().__init__(parent, fg_color='transparent')
        self.root = parent.winfo_toplevel()
        self.colors = Config.COLORS
        self.downloader = downloader
        self.storage = storage
        self.on_add = on_add
        self.row_widgets: Dict[str, dict] = {}
        self._empty_label: Optional[ctk.CTkLabel] = None
        # Общие шрифты для имени и текста ошибки — измеряются тем же
        # инстансом, которым рисуется текст (см. _elide_text/
        # _update_row_widths), чтобы обрезка по ширине была
        # пиксель-в-пиксель точной, а не приблизительной.
        self._name_font = ctk.CTkFont(size=13, weight='bold')
        self._error_font = ctk.CTkFont(size=9)

        self._setup_ui()

        if self.downloader:
            self.downloader.set_ui_callback(self._on_downloader_update)

    def _setup_ui(self):
        c = self.colors
        header_row = ctk.CTkFrame(self, fg_color='transparent')
        header_row.pack(fill='x', padx=14, pady=(12, 6))
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
        self._empty_label = None

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
            self._show_empty_placeholder()
            return

        for item in items:
            self._add_row(item)

    def _show_empty_placeholder(self):
        if self._empty_label is not None and self._empty_label.winfo_exists():
            return
        self._empty_label = ctk.CTkLabel(self.scroll_frame, text="Пока нет ни одной загрузки",
                                          font=ctk.CTkFont(size=11), text_color=self.colors['text_muted'])
        self._empty_label.pack(pady=18)

    def _clear_empty_placeholder(self):
        # Раньше "Пока нет ни одной загрузки" ставился только в
        # load_downloads([]) и НИКОГДА не убирался, когда первая же
        # загрузка добавлялась поверх уже нарисованного списка через
        # _refresh_from_downloader() -> _add_row() (а не через повторный
        # load_downloads) — плашка "пусто" так и оставалась висеть над
        # настоящей строкой загрузки.
        if self._empty_label is not None:
            if self._empty_label.winfo_exists():
                self._empty_label.destroy()
            self._empty_label = None

    def _add_row(self, item: Dict):
        c = self.colors
        self._clear_empty_placeholder()
        download_id = item.get('id', '')
        name = item.get('name') or item.get('url', 'Unknown')

        row = ctk.CTkFrame(self.scroll_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS_SM,
                           height=self.ROW_HEIGHT)
        row.pack(fill='x', padx=4, pady=3)
        row.grid_propagate(False)
        row.columnconfigure(1, weight=1)
        # Строка 0 — основное содержимое (иконка/имя/бейдж/кнопки), тянется;
        # строка 1 — тонкая полоска прогресса, прижатая к самому НИЗУ карточки
        # (весь смысл "снизу ссылки" из формулировки задачи) фиксированной
        # высоты, не зависящей от содержимого строки 0.
        row.grid_rowconfigure(0, weight=1)
        row.grid_rowconfigure(1, weight=0)

        thumb_label = ctk.CTkLabel(row, text="", width=self.LOGO_SIZE, height=self.LOGO_SIZE,
                                    corner_radius=8, fg_color=c['bg_tertiary'],
                                    image=get_icon('download', c['text_muted'], 22))
        thumb_label.grid(row=0, column=0, padx=(10, 10), pady=(10, 4), sticky='n')

        info_frame = ctk.CTkFrame(row, fg_color='transparent')
        info_frame.grid(row=0, column=1, sticky='nsew', pady=(10, 0))

        label_name = ctk.CTkLabel(info_frame, text=name, font=self._name_font,
                                   text_color=c['text_primary'], anchor='w')
        label_name.pack(fill='x', anchor='w')
        # Единственное, что здесь имеет право сокращаться под размер окна —
        # см. _elide_text/_update_row_widths: бейдж/точка статуса/полоска
        # прогресса всегда рисуются целиком, а имени достаётся, что
        # осталось от реальной ширины карточки на момент показа.
        info_frame.bind('<Configure>', lambda e, d=download_id: self._update_row_widths(d, e.width))

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

        # error_label пакуется только когда есть реальный текст ошибки (см.
        # _apply_status) — раньше была упакована всегда (даже пустая) и
        # тихо съедала место, которого потом не хватало полоске прогресса.
        # Без wraplength: сообщения об ошибке часто содержат длинный URL без
        # пробелов, а Tk перено́сит текст только по пробелам — сплошная
        # строка без них просто вылезает за пределы карточки вбок, вместо
        # переноса (это и было на скриншоте с webcaster.pro-ссылкой).
        # Вместо переноса — обрезаем по реальной ширине, как и имя ролика.
        error_label = ctk.CTkLabel(info_frame, text="", font=self._error_font, text_color=c['red'], anchor='w')

        # Тонкая полоска прогресса во всю ширину карточки (columnspan=3, а
        # не только под именем) — заполняется слева направо через
        # CTkProgressBar.set(). Процент/скорость/ETA — одной строкой
        # прижаты к правому краю той же полосы ("в углу"), а не отдельной
        # растущей строкой снизу, которая раньше и вылезала за пределы
        # фиксированной высоты карточки.
        progress_strip = ctk.CTkFrame(row, fg_color='transparent')
        progress_strip.columnconfigure(0, weight=1)

        progress_bar = ctk.CTkProgressBar(progress_strip, height=4, corner_radius=2,
                                           fg_color=c['bg_tertiary'], progress_color=c['accent'])
        progress_bar.set(0)
        progress_bar.grid(row=0, column=0, sticky='ew')

        progress_label = ctk.CTkLabel(progress_strip, text="", font=ctk.CTkFont(size=9),
                                       text_color=c['text_muted'], anchor='e')
        progress_label.grid(row=1, column=0, sticky='e', pady=(1, 0))

        actions = ctk.CTkFrame(row, fg_color='transparent')
        actions.grid(row=0, column=2, padx=(4, 8), pady=(8, 0))

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
            'progress_label': progress_label, 'progress_strip': progress_strip,
            'btn_folder': btn_folder, 'btn_cancel': btn_cancel, 'item': item,
            'thumbnail_loaded': False, 'progress_shown': False, 'error_shown': False,
            'full_name': name, 'full_error': '', 'bar_mode': 'determinate',
        }
        self._apply_status(download_id, item)
        self._update_row_widths(download_id, info_frame.winfo_width())

    def _update_row_widths(self, download_id: str, available_width: int):
        """Обрезает имя (всегда) и текст ошибки (когда она показана) под
        текущую реальную ширину карточки — единственное, что здесь может
        сокращаться; бейдж/точка статуса/полоска прогресса ширину не
        уступают (см. _elide_text)."""
        widgets = self.row_widgets.get(download_id)
        if not widgets or available_width <= 1:
            return
        widgets['label_name'].configure(
            text=_elide_text(self._name_font, widgets['full_name'], available_width))
        if widgets['error_shown']:
            widgets['error_label'].configure(
                text=_elide_text(self._error_font, widgets['full_error'], available_width))

    def _apply_status(self, download_id: str, item: Dict):
        widgets = self.row_widgets.get(download_id)
        if not widgets:
            return
        c = self.colors
        widgets['item'] = item

        name = item.get('name') or item.get('url', 'Unknown')
        if name != widgets.get('full_name'):
            widgets['full_name'] = name
            self._update_row_widths(download_id, widgets['label_name'].master.winfo_width())

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
        if error_message:
            widgets['full_error'] = error_message
            info_width = widgets['label_name'].master.winfo_width()
            widgets['error_label'].configure(text=_elide_text(self._error_font, error_message, info_width))
            if not widgets['error_shown']:
                widgets['error_label'].pack(fill='x', anchor='w', pady=(2, 0))
                widgets['error_shown'] = True
        elif widgets['error_shown']:
            widgets['error_label'].pack_forget()
            widgets['error_shown'] = False

        # Полоска прогресса — с самого начала статуса 'downloading', даже
        # пока процент ещё не из чего посчитать (медленный источник вроде
        # 1tv.ru может по минуте не отдавать ffmpeg вообще ничего, пока
        # тянется самый первый сегмент — раньше это выглядело так, будто
        # ничего не происходит вообще; теперь хотя бы "подключение…" сразу
        # показывает, что задача жива, а не зависла).
        #
        # Процент считается только когда известна duration ролика (см.
        # core/downloader.py: _watch_progress) — у части источников (обычный
        # HTML-скрейп без метаданных, некоторые встроенные плееры) duration
        # никогда не появляется, хотя байты реально льются (speed_bps есть).
        # В этом случае процент навсегда останется None — полоска на 0%
        # выглядела бы намертво зависшей, хотя скачивание идёт. Вместо этого
        # переключаем полоску в неопределённый (бегущий) режим и показываем
        # реальный счётчик скачанных байт вместо статичного "подключение…".
        progress = item.get('progress')
        show_progress = status == 'downloading'
        if show_progress:
            downloaded = item.get('downloaded_bytes')
            if progress is not None:
                if widgets['bar_mode'] != 'determinate':
                    widgets['progress_bar'].stop()
                    widgets['progress_bar'].configure(mode='determinate')
                    widgets['bar_mode'] = 'determinate'
                widgets['progress_bar'].set(max(0.0, min(1.0, progress / 100)))
                parts = [f"{progress:.0f}%"]
            elif downloaded:
                if widgets['bar_mode'] != 'indeterminate':
                    widgets['progress_bar'].configure(mode='indeterminate')
                    widgets['progress_bar'].start()
                    widgets['bar_mode'] = 'indeterminate'
                parts = [_format_size(downloaded)]
            else:
                if widgets['bar_mode'] != 'determinate':
                    widgets['progress_bar'].stop()
                    widgets['progress_bar'].configure(mode='determinate')
                    widgets['bar_mode'] = 'determinate'
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
                widgets['progress_strip'].grid(row=1, column=0, columnspan=3, sticky='sew', padx=10, pady=(0, 6))
                widgets['progress_shown'] = True
        elif widgets['progress_shown']:
            if widgets['bar_mode'] != 'determinate':
                widgets['progress_bar'].stop()
                widgets['progress_bar'].configure(mode='determinate')
                widgets['bar_mode'] = 'determinate'
            widgets['progress_strip'].grid_forget()
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
