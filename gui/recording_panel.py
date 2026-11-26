# gui/recording_panel.py
import os
import platform
import subprocess
import threading
import unicodedata
from pathlib import Path
from gui.inline_messages import messagebox
from typing import Dict, Optional

import customtkinter as ctk
from PIL import Image

from core.link_resolver import resolve_link
from core.recorder import Recorder, RecordingTask
from core.storage import Storage
from gui.download_list import _elide_text
from utils.config import Config
from utils.icons import get_icon
from utils.logger import logger


from gui.recording_presenter import STATE_ACCENT, task_state, card_model, visible_tasks, summary_label


class RecordingPanel(ctk.CTkFrame):
    """Панель активных записей: карточки с таймером и статусом."""

    def __init__(self, parent, recorder: Recorder):
        super().__init__(parent, fg_color='transparent')
        self.colors = Config.COLORS
        self.recorder = recorder
        self.storage = Storage()

        self.task_widgets: Dict[str, dict] = {}
        self.empty_label: Optional[ctk.CTkLabel] = None
        self._refresh_requested = threading.Event()
        self._connecting_channels = set()
        self._view = 'list'
        self._view_generation = 0
        self._filter = 'Все'
        self.notice_dismiss_callback = None
        # Общий инстанс шрифта имени — измеряется тем же инстансом, которым
        # реально рисуется текст (см. _elide_text/_update_name_width), для
        # пиксель-в-пиксель точной обрезки под реальную ширину строки.
        self._name_font = ctk.CTkFont(size=12, weight='bold')

        self._setup_ui()
        self.refresh()

        self.recorder.set_ui_callback(self._refresh_requested.set)
        self._refresh_job = self.after(100, self._poll_refresh)

    def _poll_refresh(self):
        self._refresh_job = self.after(100, self._poll_refresh)
        if self._refresh_requested.is_set():
            self._refresh_requested.clear()
            self.refresh()

    def destroy(self):
        self.recorder.remove_ui_callback(self._refresh_requested.set)
        self.after_cancel(self._refresh_job)
        super().destroy()

    def set_channel_connecting(self, name, connecting):
        if connecting:
            self._connecting_channels.add(name)
            self.show_recordings()
        else:
            self._connecting_channels.discard(name)
            self.refresh()

    def show_recordings(self):
        self._filter = 'Все'
        self.filter_picker.set(self._filter)
        self._show_list()

    def _open_monitor(self):
        from gui.recording_monitor import RecordingMonitorWindow
        RecordingMonitorWindow.show(self.winfo_toplevel(), self.recorder)

    def _show_list(self):
        self._view_generation += 1
        self._view = 'list'
        self.list_page.tkraise()
        self.filter_bar.pack(fill='x', padx=12, pady=(0, 6), before=self.body)
        self.refresh()

    def _open_history(self):
        from gui.recording_history import STATUS_LABELS, date_label
        self._view_generation += 1
        generation = self._view_generation
        self._view = 'history'
        self.filter_bar.pack_forget()
        self.history_page.tkraise()
        for child in self.history_frame.winfo_children():
            child.destroy()
        toolbar = ctk.CTkFrame(self.history_frame, fg_color='transparent')
        toolbar.pack(fill='x', pady=6)
        ctk.CTkButton(toolbar, text='К записям', width=100, command=self._show_list).pack(side='left')
        ctk.CTkButton(toolbar, text='Обновить', width=90, command=self._open_history).pack(side='right')
        content = ctk.CTkFrame(self.history_frame, fg_color='transparent')
        content.pack(fill='both', expand=True)
        ctk.CTkLabel(content, text='Загрузка истории…').pack(pady=20)
        def read():
            try:
                records = self.recorder.history.list_records() if self.recorder.history else []
                error = '' if self.recorder.history else 'История недоступна в этом сеансе'
            except Exception as exc:
                records, error = [], str(exc)
            def apply():
                if generation != self._view_generation or not self.winfo_exists():
                    return
                for child in content.winfo_children():
                    child.destroy()
                if not records:
                    ctk.CTkLabel(content, text=error or 'История пока пуста').pack(pady=20)
                for record in records:
                    session = record['session']
                    name = session.get('name', 'Запись')
                    name = name[:45]+'…' if len(name)>46 else name
                    label = (name+'\n'+date_label(session.get('started_at'))+' · '
                             +STATUS_LABELS.get(session.get('state'), session.get('state', '—')))
                    ctk.CTkButton(content, text=label, height=52, anchor='w',
                                  command=lambda item=record: self._show_record(item, self._open_history)).pack(fill='x', pady=3)
            if not self.recorder._closing:
                self.after(0, apply)
        threading.Thread(target=read, daemon=True, name='RecordingHistoryRead').start()

    def _show_record(self, record, back):
        from gui.recording_history import format_record
        self._view_generation += 1
        self._view = 'details'
        self.filter_bar.pack_forget()
        for child in self.details_frame.winfo_children():
            child.destroy()
        ctk.CTkButton(self.details_frame, text='Назад', width=90, command=back).pack(anchor='w', padx=8, pady=8)
        box = ctk.CTkTextbox(self.details_frame, wrap='word', fg_color=self.colors['bg_secondary'])
        box.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        box.insert('1.0', format_record(record))
        box.configure(state='disabled')
        self.details_frame.tkraise()

    def _show_task_details(self, task):
        self._view_generation += 1
        generation = self._view_generation
        fallback = dict(session=dict(name=task.channel_name, state=task_state(task),
            output_path=task.output_path, expected_seconds=task.expected_seconds,
            actual_seconds=task.actual_seconds, missing_seconds=task.missing_seconds,
            retries=task.retry_count, pauses=task.pause_count, parts=list(task.parts),
            error=task.error_message or task.storage_warning or task.storage_error), events=[])
        def read():
            try:
                record = self.recorder.history.read(task.task_id) if self.recorder.history else None
            except Exception:
                record = None
            def apply():
                if generation == self._view_generation and self.winfo_exists():
                    self._show_record(record or fallback, self._show_list)
            if not self.recorder._closing:
                self.after(0, apply)
        threading.Thread(target=read, daemon=True, name='RecordingDetailsRead').start()

    def _dismiss_notice(self):
        self.notice.pack_forget()
        if self.notice_dismiss_callback:
            self.notice_dismiss_callback()

    def show_notice(self, message, severity='yellow'):
        self.notice_text.configure(text=message, text_color=self.colors.get(severity, self.colors['yellow']))
        self.notice.pack(fill='x', padx=12, pady=(0, 6), before=self.body)

    def _set_filter(self, selection):
        self._filter = selection
        self.refresh()

    def _setup_ui(self):
        c = self.colors
        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=12, pady=(10, 4))
        ctk.CTkLabel(header, text='ЗАПИСИ', font=ctk.CTkFont(size=12, weight='bold'),
                     text_color=c['text_secondary']).pack(side='left')
        ctk.CTkButton(header, text='История', width=74, height=28, command=self._open_history).pack(side='right', padx=(5, 0))
        ctk.CTkButton(header, text='Монитор', width=82, height=28, command=self._open_monitor).pack(side='right')
        self.count_label = ctk.CTkLabel(self, text='', anchor='w', font=ctk.CTkFont(size=10), text_color=c['text_muted'])
        self.count_label.pack(fill='x', padx=12, pady=(0, 6))
        self.notice = ctk.CTkFrame(self, fg_color=c['bg_tertiary'], corner_radius=8)
        self.notice.grid_columnconfigure(0, weight=1)
        self.notice_text = ctk.CTkLabel(self.notice, text='', wraplength=360, justify='left', anchor='w', font=ctk.CTkFont(size=11))
        self.notice_text.grid(row=0, column=0, sticky='ew', padx=10, pady=8)
        ctk.CTkButton(self.notice, text='×', width=26, height=26, command=self._dismiss_notice).grid(row=0, column=1, padx=5)
        self.notice.bind('<Configure>', lambda e: self.notice_text.configure(wraplength=max(120, e.width-55)))
        self.filter_bar = ctk.CTkFrame(self, fg_color='transparent')
        self.filter_bar.pack(fill='x', padx=12, pady=(0, 6))
        self.filter_picker = ctk.CTkSegmentedButton(self.filter_bar, values=['Все', 'Активные', 'Требуют внимания'], command=self._set_filter)
        self.filter_picker.set('Все')
        self.filter_picker.pack(fill='x')
        self.body = ctk.CTkFrame(self, fg_color='transparent')
        self.body.pack(fill='both', expand=True, padx=6, pady=(0, 6))
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)
        self.list_page = ctk.CTkFrame(self.body, fg_color='transparent')
        self.history_page = ctk.CTkFrame(self.body, fg_color='transparent')
        self.list_frame = ctk.CTkScrollableFrame(self.list_page, fg_color='transparent')
        self.history_frame = ctk.CTkScrollableFrame(self.history_page, fg_color='transparent')
        self.list_frame.pack(fill='both', expand=True)
        self.history_frame.pack(fill='both', expand=True)
        self.details_frame = ctk.CTkFrame(self.body, fg_color='transparent')
        for view in (self.list_page, self.history_page, self.details_frame):
            view.grid(row=0, column=0, sticky='nsew')
        self.list_page.tkraise()

    def refresh(self):
        tasks = self.recorder.get_all_tasks()
        summary = summary_label(tasks)
        if self._connecting_channels:
            summary += f'  ·  Подключение: {len(self._connecting_channels)}'
        self.count_label.configure(text=summary)
        if self._view != 'list':
            return
        tasks = visible_tasks(tasks, self._filter)
        current_ids = {task.task_id for task in tasks}
        for tid in list(self.task_widgets):
            if tid not in current_ids:
                self.task_widgets.pop(tid)['row'].destroy()
        if not tasks and self.empty_label is None:
            self.empty_label = ctk.CTkLabel(self.list_frame, text='', text_color=self.colors['text_muted'])
            self.empty_label.pack(pady=20)
        elif tasks and self.empty_label is not None:
            self.empty_label.destroy()
            self.empty_label = None
        if self.empty_label is not None:
            self.empty_label.configure(text='Подключение к эфиру…' if self._connecting_channels
                                       else 'Здесь пока нет записей')
        for task in tasks:
            if task.task_id not in self.task_widgets:
                self._add_task_row(task)
            self._render_task(task, self.task_widgets[task.task_id])

    def _task_state(self, task):
        return task_state(task)

    def _render_task(self, task, widgets):
        model = card_model(task)
        fingerprint = (tuple(model.items()), task.format_elapsed_time(), task.format_clip_range() or task.format_recording_period())
        if widgets.get('fingerprint') == fingerprint:
            return
        widgets['fingerprint'] = fingerprint
        c = self.colors
        widgets['timer'].configure(text=fingerprint[1])
        widgets['period'].configure(text=fingerprint[2])
        widgets['result'].configure(text=model['label'], text_color=c[model['color']])
        widgets['accent_bar'].configure(fg_color=c[model['color']])
        widgets['metrics'].configure(text=model['metrics'])
        disk_color = c['text_muted'] if model['disk_color']=='muted' else c[model['disk_color']]
        widgets['disk'].configure(text=model['disk'], text_color=disk_color)
        widgets['message'].configure(text=model['reason'], text_color=c['red'] if model['disk_color']=='red' else c['yellow'])
        if model['reason']:
            widgets['message'].grid()
        else:
            widgets['message'].grid_remove()
        widgets['btn_pause'].configure(text='▶' if task.is_paused else 'Ⅱ', state='normal' if model['can_pause'] else 'disabled')
        widgets['btn_stop'].configure(state='normal' if model['can_stop'] else 'disabled')
        widgets['btn_del'].configure(state='normal' if model['can_remove'] else 'disabled')
        widgets['btn_open'].configure(state='normal' if model['can_open'] else 'disabled')
        if model['can_merge']:
            widgets['btn_merge'].grid()
        else:
            widgets['btn_merge'].grid_remove()
        self._update_name_width(task.task_id)

    def _icon_button(self, parent, icon_name, command, color=None):
        c = self.colors
        return ctk.CTkButton(parent, text="", image=get_icon(icon_name, color or c['text_secondary'], 18),
                              width=34, height=34, corner_radius=Config.RADIUS_SM, fg_color='transparent',
                              hover_color=c['bg_active'], command=command)

    ROW_HEIGHT = 56
    THUMB_SIZE = (58, 40)  # шире квадрата — снимок 16:9 обрезается меньше

    def _load_task_logo(self, channel_name: str, logo_lbl: ctk.CTkLabel):
        from utils.logo_cache import LogoCache

        def worker():
            channels = {c['name']: c for c in self.storage.get_channels()}
            channel = channels.get(channel_name)
            logo_url = channel.get('logo_url', '') if channel else ''

            if not logo_url:
                # Не нашли среди IPTV-каналов — это могла быть запись по
                # вручную добавленной ссылке; там для превью используем
                # уже разобранный через yt-dlp thumbnail, если он есть.
                links = {l['name']: l for l in self.storage.get_links()}
                link = links.get(channel_name)
                if link:
                    info = resolve_link(link.get('url', ''), allow_tass_browser=False,
                                        allow_browser_sniff=False)
                    if info.ok:
                        logo_url = info.thumbnail

            image = None
            if logo_url:
                cache = LogoCache()
                logo_path = cache.get_logo_path(channel_name, logo_url)
                if logo_path:
                    try:
                        image = self._make_thumb_image(Image.open(logo_path))
                    except Exception as e:
                        logger.debug(f"RecordingPanel: ошибка отображения логотипа {channel_name}: {e}")

            def apply():
                if not logo_lbl.winfo_exists():
                    return
                if image is not None:
                    logo_lbl.configure(image=image)
                    logo_lbl._logo_ref = image
                else:
                    logo_lbl.configure(image=get_icon('tv', self.colors['text_muted'], 16))

            self.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    def _make_thumb_image(self, pil_img: Image.Image) -> "ctk.CTkImage":
        """Вписывает картинку в область THUMB_SIZE с обрезкой по центру
        (а не сжатием) — так живой кадр 16:9 меньше искажается."""
        w, h = self.THUMB_SIZE
        pil_img = pil_img.convert('RGBA')
        src_ratio = pil_img.width / pil_img.height
        dst_ratio = w / h
        if src_ratio > dst_ratio:
            new_height = h
            new_width = max(w, int(h * src_ratio))
        else:
            new_width = w
            new_height = max(h, int(w / src_ratio))
        resized = pil_img.resize((new_width, new_height), Image.LANCZOS)
        left = (new_width - w) // 2
        top = (new_height - h) // 2
        cropped = resized.crop((left, top, left + w, top + h))
        return ctk.CTkImage(light_image=cropped, dark_image=cropped, size=(w, h))

    def _add_task_row(self, task):
        c = self.colors
        row = ctk.CTkFrame(self.list_frame, fg_color=c['bg_secondary'], corner_radius=10)
        row.pack(fill='x', pady=4, padx=2)
        row.grid_columnconfigure(2, weight=1)
        accent = ctk.CTkFrame(row, width=3, fg_color=c['accent'], corner_radius=2)
        accent.grid(row=0, column=0, rowspan=5, sticky='ns', padx=(0, 8), pady=8)
        logo = ctk.CTkLabel(row, text='', width=48, height=34, image=get_icon('tv', c['text_muted'], 20))
        logo.grid(row=0, column=1, rowspan=2, sticky='nw', padx=(0, 8), pady=10)
        self._load_task_logo(task.channel_name, logo)
        title = ctk.CTkFrame(row, fg_color='transparent')
        title.grid(row=0, column=2, sticky='ew', pady=(8, 0))
        title.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(title, text=task.channel_name, font=self._name_font, anchor='w', width=1)
        name.grid(row=0, column=0, sticky='ew')
        timer = ctk.CTkLabel(title, text='', font=('Menlo', 11), width=68, anchor='e', text_color=c['accent'])
        timer.grid(row=0, column=1, padx=(6, 0))
        status = ctk.CTkLabel(row, text='', font=ctk.CTkFont(size=11, weight='bold'), anchor='w')
        status.grid(row=1, column=2, sticky='ew', pady=(0, 3))
        status.configure(cursor='hand2')
        status.bind('<Button-1>', lambda event, item=task: self._show_task_details(item))
        actions = ctk.CTkFrame(row, fg_color='transparent')
        actions.grid(row=0, column=3, rowspan=3, sticky='ne', padx=8, pady=8)
        def button(text, command, r, col, icon=None):
            item = ctk.CTkButton(actions, text=text, image=get_icon(icon, c['text_secondary'], 14) if icon else None,
                                width=28, height=26, corner_radius=6, fg_color=c['bg_tertiary'],
                                hover_color=c['bg_hover'], command=command)
            item.grid(row=r, column=col, padx=1, pady=1)
            return item
        pause = button('Ⅱ', lambda: self._toggle_pause(task), 0, 0)
        stop = button('', lambda: self._stop_task(task), 0, 1, 'stop')
        delete = button('×', lambda: self._remove_task(task), 0, 2)
        folder = button('', lambda: self._open_file(task), 1, 0, 'folder')
        button('i', lambda: self._show_task_details(task), 1, 1)
        merge = ctk.CTkButton(actions, text='Объединить', width=88, height=26,
                              command=lambda: self._merge_parts(task))
        merge.grid(row=2, column=0, columnspan=3, pady=(4, 0))
        metrics = ctk.CTkLabel(row, text='', font=ctk.CTkFont(size=10), anchor='w', justify='left', text_color=c['text_secondary'])
        metrics.grid(row=2, column=2, sticky='ew', pady=(1, 2))
        period = ctk.CTkLabel(row, text='', font=ctk.CTkFont(size=10), anchor='w', text_color=c['text_muted'])
        period.grid(row=3, column=2, columnspan=2, sticky='ew', pady=(0, 2), padx=(0, 8))
        disk = ctk.CTkLabel(row, text='', font=ctk.CTkFont(size=10), anchor='w')
        disk.grid(row=4, column=2, columnspan=2, sticky='ew', pady=(0, 5), padx=(0, 8))
        message = ctk.CTkLabel(row, text='', font=ctk.CTkFont(size=10), anchor='w', justify='left', wraplength=300)
        message.grid(row=5, column=1, columnspan=3, sticky='ew', padx=(0, 10), pady=(0, 8))
        self.task_widgets[task.task_id] = dict(row=row, title=title, name_lbl=name, full_name=task.channel_name,
            logo_lbl=logo, timer=timer, result=status, accent_bar=accent, actions=actions,
            metrics=metrics, period=period, disk=disk, message=message,
            btn_pause=pause, btn_stop=stop, btn_del=delete, btn_open=folder, btn_merge=merge)
        row.bind('<Configure>', lambda event, tid=task.task_id: self._update_name_width(tid))

    def _update_name_width(self, task_id):
        widgets = self.task_widgets.get(task_id)
        if not widgets:
            return
        width = widgets['title'].winfo_width()
        row_width = widgets['row'].winfo_width()
        if width > 1:
            available = max(20, width-80)
            widgets['name_lbl'].configure(text=_elide_text(self._name_font, widgets['full_name'], available))
            widgets['metrics'].configure(wraplength=max(80, width))
        widgets['message'].configure(wraplength=max(120, row_width-35))

    def _reveal_new_row(self, row: ctk.CTkFrame):
        """Прокручивает список к только что появившейся строке и на секунду
        подсвечивает её — чтобы было заметно, что запись правда началась."""
        try:
            self.list_frame._parent_canvas.update_idletasks()
            self.list_frame._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

        c = self.colors
        row.configure(fg_color=c['accent'])
        self.after(700, lambda: row.winfo_exists() and row.configure(fg_color=c['bg_secondary']))

    def _toggle_pause(self, task: RecordingTask):
        self.recorder.pause_recording(task.task_id)

    def _stop_task(self, task: RecordingTask):
        self.recorder.stop_recording(task.task_id)

    def _merge_parts(self, task):
        def worker():
            try:
                result = self.recorder.merge_recording_parts(task.task_id)
            except Exception as error:
                logger.exception('Не удалось объединить части записи')
                text = str(error)
                if not self.recorder._closing:
                    task.merge_error = text
                    self.after(0, lambda message=text: self.show_notice(message, 'red'))
            else:
                if not self.recorder._closing:
                    self.after(0, lambda: self.show_notice('Единый файл сохранён. Исходные части оставлены.', 'green'))
        threading.Thread(target=worker, daemon=False, name='RecordingPartsMerge').start()

    def _open_file(self, task: RecordingTask):
        try:
            file_path = self._find_recording_file(task.output_path)
            if not file_path:
                messagebox.showerror(
                    "File not found",
                    "The recording file is not available. This recording may have failed."
                )
                logger.error(f"RecordingPanel: file not found: {task.output_path}")
                return
            if platform.system() == 'Darwin':
                subprocess.Popen(['open', '-R', str(file_path)])
            elif platform.system() == 'Windows':
                subprocess.Popen(['explorer', '/select,', os.path.normpath(str(file_path))])
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(str(file_path))])
            logger.info(f"RecordingPanel: file revealed: {file_path}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку с файлом:\n{e}")

    @staticmethod
    def _find_recording_file(output_path: str):
        """Handles macOS Unicode filename normalisation when revealing old files."""
        path = Path(output_path)
        if path.is_file():
            return path
        if not path.parent.is_dir():
            return None
        target_name = unicodedata.normalize('NFC', path.name)
        for candidate in path.parent.iterdir():
            if unicodedata.normalize('NFC', candidate.name) == target_name and candidate.is_file():
                return candidate
        return None

    def _remove_task(self, task: RecordingTask):
        self.recorder.remove_task(task.task_id)
