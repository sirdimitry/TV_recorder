# gui/app_window.py
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, Optional
import time
import threading
import json
from datetime import datetime, timedelta
from queue import Empty, Queue

import customtkinter as ctk

from gui.browser_link_list import BrowserLinkList
from gui.channel_list import ChannelList
from gui.link_list import LinkList
from gui.preview_panel import PreviewPanel
from gui.schedule_panel import SchedulePanel, TimeEntry
from gui.status_bar import StatusBar
from gui.recording_panel import RecordingPanel
from core.link_resolver import resolve_link, guess_type
from core.storage import Storage
from core.scheduler import RecordingScheduler
from core.recorder import Recorder
from utils.config import Config
from utils.icons import get_icon
from utils.vpn_manager import VPNManager
from utils.network_monitor import NetworkMonitor
from utils.logger import logger

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _make_clipboard_action(virtual_event: str):
    def handler(event):
        event.widget.event_generate(virtual_event)
        return 'break'
    return handler


# Tk на macOS матчит Cmd+C/V/X/A по СИМВОЛУ, который даёт нажатая клавиша,
# а не по её физическому положению. При активной русской раскладке (ЙЦУКЕН)
# те же клавиши дают кириллицу (V→М, C→С, X→Ч, A→Ф), и родные биндинги
# просто не срабатывают — при этом обычный ввод текста не страдает, отсюда
# и путаница "вставить не могу, а руками написать могу". Дублируем те же
# действия на кириллические варианты, ничего не убирая: на английской
# раскладке эти биндинги на кириллические клавиши никогда не сработают,
# так что двойной вставки не будет.
_CYRILLIC_CLIPBOARD_KEYSYMS = {
    '<<Paste>>': ('Cyrillic_em', 'Cyrillic_EM'),
    '<<Copy>>': ('Cyrillic_es', 'Cyrillic_ES'),
    '<<Cut>>': ('Cyrillic_che', 'Cyrillic_CHE'),
    '<<SelectAll>>': ('Cyrillic_ef', 'Cyrillic_EF'),
}


def bind_cyrillic_layout_shortcuts(widget):
    for virtual_event, keysyms in _CYRILLIC_CLIPBOARD_KEYSYMS.items():
        action = _make_clipboard_action(virtual_event)
        for keysym in keysyms:
            widget.bind(f'<Command-{keysym}>', action)


class SplashScreen(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        c = Config.COLORS
        self.title("TV Recorder")
        self.geometry("460x340")
        self.resizable(False, False)
        self.attributes('-topmost', True)
        self.configure(fg_color=c['bg_primary'])

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width() - 460) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 340) // 2
        if px < 0: px = 100
        if py < 0: py = 100
        self.geometry(f"+{px}+{py}")

        ctk.CTkLabel(self, image=get_icon('tv', c['accent'], 52), text="").pack(pady=(36, 10))
        ctk.CTkLabel(self, text="TV Recorder", font=ctk.CTkFont(size=22, weight='bold'),
                     text_color=c['text_primary']).pack()
        ctk.CTkLabel(self, text=f"v{Config.APP_VERSION}", font=ctk.CTkFont(size=11),
                     text_color=c['text_secondary']).pack(pady=(0, 22))

        self.progress = ctk.CTkProgressBar(self, mode='indeterminate', width=300,
                                            progress_color=c['accent'], fg_color=c['bg_tertiary'])
        self.progress.pack(pady=6)
        self.progress.start()

        self.log_box = ctk.CTkTextbox(self, width=380, height=100, corner_radius=8,
                                       fg_color=c['bg_secondary'], text_color=c['text_secondary'],
                                       font=('Menlo', 10))
        self.log_box.pack(pady=(18, 24), padx=30, fill='both', expand=True)
        self.log_box.configure(state='disabled')

        self._log("Инициализация приложения...")

    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state='normal')
        self.log_box.insert('end', f"[{timestamp}] {message}\n")
        self.log_box.see('end')
        self.log_box.configure(state='disabled')
        self.update_idletasks()

    def close(self):
        self.progress.stop()
        self.destroy()


class AppWindow:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("TV Recorder")
        self.root.geometry("1150x720")
        self.root.minsize(920, 620)

        self.colors = Config.COLORS
        self.storage = Storage()
        self.recorder = Recorder()
        self.scheduler = RecordingScheduler(recorder=self.recorder)
        self._network_results = Queue(maxsize=1)
        self._network_monitor_running = False

        self.root.configure(fg_color=self.colors['bg_primary'])

        self.splash = SplashScreen(self.root)
        self.root.withdraw()

        self._apply_ttk_theme()
        self._setup_ui()
        self.scheduler.set_status_callback(self.schedule_panel.update_run_status)

        threading.Thread(target=self._initialize_app, daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _initialize_app(self):
        try:
            self.splash._log("Проверка конфигурации...")
            Config.init_dirs()
            time.sleep(0.3)

            self.splash._log("Синхронизация списка каналов...")
            self._sync_channels()
            time.sleep(0.3)

            self.splash._log("Загрузка расписания и логотипов...")
            self.root.after(0, self._refresh_data)
            time.sleep(0.5)

            self.splash._log("Запуск планировщика...")
            self.scheduler.start()
            time.sleep(0.3)

            self.splash._log("Проверка сетевого подключения...")
            self._start_background_checks()
            time.sleep(0.5)

            self.splash._log("Готово! Запуск интерфейса...")
            time.sleep(0.5)

            self.root.after(0, self._show_main_window)

        except Exception as e:
            self.splash._log(f"ОШИБКА: {e}")
            logger.error(f"Ошибка инициализации: {e}", exc_info=True)

    def _sync_channels(self):
        """Синхронизирует список каналов с живым плейлистом.

        Живой плейлист — источник истины и может обновлять уже сохранённые
        каналы (URL потока/логотипа мог поменяться). Локальный
        default_channels.json — это статичный аварийный резерв на случай,
        когда сеть недоступна; он используется только чтобы добавить каналы,
        которых ещё нет, и никогда не перезаписывает уже сохранённые —
        иначе временный сетевой сбой затирал бы рабочие ссылки устаревшими.
        """
        from core.m3u_parser import M3UParser

        parser = M3UParser()
        online_channels = parser.fetch_and_parse()
        is_live = bool(online_channels)

        if not is_live:
            self.splash._log("⚠️ Не удалось загрузить онлайн-плейлист.")
            defaults_path = Config.BASE_DIR / "data" / "default_channels.json"
            if defaults_path.exists():
                with open(defaults_path, 'r', encoding='utf-8') as f:
                    online_channels = json.load(f)
                self.splash._log("📂 Используем локальную базу каналов (только для новых каналов)")
            else:
                return

        current = self.storage.get_channels()
        current_map = {ch['name']: ch for ch in current}

        added = 0
        updated = 0

        for ch in online_channels:
            if ch['name'] not in current_map:
                self.storage.save_channel(ch)
                added += 1
            elif is_live:
                existing = current_map[ch['name']]
                if existing.get('url') != ch['url'] or existing.get('logo_url') != ch['logo_url']:
                    self.storage.save_channel(ch)
                    updated += 1

        msg = f"✅ Добавлено: {added}, Обновлено: {updated}"
        self.splash._log(msg)

    def _show_main_window(self):
        self.splash.close()
        self.root.deiconify()
        self.root.focus_force()

    def _apply_ttk_theme(self):
        """Стилизует остаточные ttk-виджеты (Treeview, PanedWindow, Scrollbar),
        для которых у CustomTkinter нет собственных аналогов."""
        style = ttk.Style()
        style.theme_use('clam')
        c = self.colors

        style.configure('.', background=c['bg_primary'], foreground=c['text_primary'], font=('Helvetica', 10))
        style.configure('TFrame', background=c['bg_primary'])
        style.configure('TPanedwindow', background=c['bg_primary'])
        style.configure('Sash', background=c['bg_primary'], sashthickness=6, gripcount=0)

        style.configure('Treeview', background=c['bg_secondary'], foreground=c['text_primary'],
                        fieldbackground=c['bg_secondary'], borderwidth=0, rowheight=26)
        style.configure('Treeview.Heading', background=c['bg_tertiary'], foreground=c['text_secondary'],
                        borderwidth=0, relief='flat', font=('Helvetica', 10, 'bold'))
        style.map('Treeview.Heading', background=[('active', c['bg_tertiary'])])
        style.map('Treeview', background=[('selected', c['bg_active'])],
                  foreground=[('selected', c['text_primary'])])
        style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])

        style.configure('Vertical.TScrollbar', background=c['bg_tertiary'], troughcolor=c['bg_primary'],
                        borderwidth=0, arrowsize=12)
        style.map('Vertical.TScrollbar', background=[('active', c['bg_hover'])])

        self.root.configure(bg=c['bg_primary'])

    def _setup_ui(self):
        c = self.colors

        # === МЕНЮ БАР ===
        menubar = tk.Menu(self.root)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Settings…", command=self._show_settings)
        app_menu.add_command(label="О программе", command=self._show_about)
        app_menu.add_separator()
        app_menu.add_command(label="Выход", command=self._on_close)
        menubar.add_cascade(label="TV Recorder", menu=app_menu)

        # Меню Edit: без него на macOS `root.config(menu=...)` полностью
        # заменяет системное меню приложения, а вместе с ним — и скрытую
        # в нём стандартную маршрутизацию Cmd+C/V/X/A в текстовые поля.
        # Физическое нажатие Cmd+V без этого меню в принципе не доходит
        # до виджета (хотя обычный ввод текста продолжает работать).
        def edit_action(virtual_event):
            widget = self.root.focus_get()
            if widget is not None:
                widget.event_generate(virtual_event)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Cut", accelerator="Cmd+X", command=lambda: edit_action('<<Cut>>'))
        edit_menu.add_command(label="Copy", accelerator="Cmd+C", command=lambda: edit_action('<<Copy>>'))
        edit_menu.add_command(label="Paste", accelerator="Cmd+V", command=lambda: edit_action('<<Paste>>'))
        edit_menu.add_separator()
        edit_menu.add_command(label="Select All", accelerator="Cmd+A", command=lambda: edit_action('<<SelectAll>>'))
        menubar.add_cascade(label="Edit", menu=edit_menu)

        self.root.config(menu=menubar)

        # === ТУЛБАР ===
        toolbar = ctk.CTkFrame(self.root, fg_color='transparent')
        toolbar.pack(fill='x', padx=16, pady=(14, 8))

        title_frame = ctk.CTkFrame(toolbar, fg_color='transparent')
        title_frame.pack(side='left')
        ctk.CTkLabel(title_frame, image=get_icon('tv', c['accent'], 22), text="").pack(side='left', padx=(0, 8))
        ctk.CTkLabel(title_frame, text="TV Recorder", font=ctk.CTkFont(size=18, weight='bold'),
                     text_color=c['text_primary']).pack(side='left')

        self.btn_toolbar_check = ctk.CTkButton(
            toolbar, text="Проверить все", image=get_icon('refresh', c['text_primary'], 16),
            compound='left', width=140, height=34, corner_radius=Config.RADIUS_SM,
            fg_color=c['bg_tertiary'], hover_color=c['bg_hover'], text_color=c['text_primary'],
            command=self._toolbar_check_all)
        self.btn_toolbar_check.pack(side='right', padx=(6, 0))

        # === ОСНОВНАЯ ОБЛАСТЬ: 2 КОЛОНКИ ===
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=14, pady=(0, 8))

        left_frame = ctk.CTkFrame(paned, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)

        self.preview_panel = PreviewPanel(left_frame)
        self.preview_panel.pack(fill='x', padx=4, pady=(4, 0))

        self.left_tabview = ctk.CTkTabview(
            left_frame, fg_color='transparent', corner_radius=Config.RADIUS,
            segmented_button_fg_color=c['bg_tertiary'], segmented_button_selected_color=c['accent'],
            segmented_button_selected_hover_color=c['accent_hover'], segmented_button_unselected_color=c['bg_tertiary'],
            segmented_button_unselected_hover_color=c['bg_hover'], text_color=c['text_primary'],
            command=self._on_left_tab_changed)
        self.left_tabview.pack(fill='both', expand=True, padx=4, pady=(4, 0))

        tab_channels = self.left_tabview.add("Каналы")
        tab_links = self.left_tabview.add("Мои ссылки")
        tab_browser = self.left_tabview.add("Браузер")

        self.channel_list = ChannelList(
            tab_channels,
            recorder=self.recorder,
            on_select=self._on_channel_select,
            on_edit=self._edit_channel_dialog,
            on_record=self._record_channel_now,
            on_delete=self._delete_channel,
            on_preview=self._show_channel_preview,
            on_add=self._add_channel_dialog,
        )
        self.channel_list.pack(fill='both', expand=True)

        self.link_list = LinkList(
            tab_links,
            recorder=self.recorder,
            on_select=self._on_channel_select,
            on_edit=self._edit_link_dialog,
            on_record=self._record_link_now,
            on_delete=self._delete_link,
            on_add=self._add_link_dialog,
            on_preview=self._show_channel_preview,
        )
        self.link_list.pack(fill='both', expand=True)

        self.browser_link_list = BrowserLinkList(
            tab_browser,
            recorder=self.recorder,
            on_select=self._on_channel_select,
            on_edit=self._edit_browser_link_dialog,
            on_record=self._record_browser_link_now,
            on_delete=self._delete_browser_link,
            on_add=self._add_browser_link_dialog,
        )
        self.browser_link_list.pack(fill='both', expand=True)

        paned.add(left_frame, weight=1)

        right_paned = ttk.PanedWindow(paned, orient='vertical')

        schedule_container = ctk.CTkFrame(right_paned, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        self.schedule_panel = SchedulePanel(schedule_container, on_schedule_changed=self._on_schedule_changed,
                                             on_record_now=self._record_from_schedule_item)
        self.schedule_panel.pack(fill='both', expand=True)
        right_paned.add(schedule_container, weight=1)

        recordings_container = ctk.CTkFrame(right_paned, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        self.recording_panel = RecordingPanel(recordings_container, self.recorder)
        self.recording_panel.pack(fill='both', expand=True)
        right_paned.add(recordings_container, weight=1)

        paned.add(right_paned, weight=1)

        self.status_bar = StatusBar(self.root)
        self.status_bar.pack(fill='x', side='bottom')

    def _refresh_data(self):
        self.channel_list.load_channels(self.storage.get_channels())
        self.link_list.load_links(self.storage.get_links())
        self.browser_link_list.load_links(self.storage.get_browser_links())
        self.schedule_panel.refresh()

    def _check_all_channels(self):
        self.channel_list.check_all()

    def _toolbar_check_all(self):
        tab = self.left_tabview.get()
        if tab == "Мои ссылки":
            self.link_list.load_links(self.storage.get_links())
        elif tab == "Браузер":
            self.browser_link_list.load_links(self.storage.get_browser_links())
        else:
            self.channel_list.check_all()

    def _on_left_tab_changed(self):
        tab = self.left_tabview.get()
        self.btn_toolbar_check.configure(text="Обновить статус" if tab == "Мои ссылки" else "Проверить все")

    def _on_channel_select(self, name: str):
        logger.info(f"Выбран канал: {name}")

    def _show_channel_preview(self, name: str, url: str, headers: Optional[Dict] = None):
        self.preview_panel.show(name, url, headers)

    def _on_schedule_changed(self):
        self.scheduler.reload_schedules()

    def _record_channel_now(self, name: str, channel: Dict):
        """Мгновенная запись выбранного канала по кнопке записи у канала"""
        output = str(self.recorder.build_output_path(name))

        def start():
            # start_recording теперь ходит в сеть (выбор варианта качества),
            # поэтому запускаем его вне главного потока, чтобы не подвесить
            # интерфейс по клику на кнопку записи. recorder.set_ui_callback
            # уже надёжно обновляет панель записей из любого потока.
            task_id = self.recorder.start_recording(
                name, channel['url'], output, source="manual",
                on_complete=self._on_record_complete
            )
            if task_id:
                logger.info(f"Начата мгновенная запись: {name} (task: {task_id})")

        threading.Thread(target=start, daemon=True).start()

    def _record_link_now(self, name: str, link: Dict):
        """Мгновенная запись вручную добавленной ссылки: сперва разбираем её
        через yt-dlp (страница -> прямой поток), потом как обычно."""
        output = str(self.recorder.build_output_path(name))

        def start():
            info = resolve_link(link.get('url', ''))
            if not info.ok:
                logger.error(f"Не удалось разобрать ссылку '{name}': {info.error}")
                self.root.after(0, lambda: messagebox.showerror(
                    "Ошибка", f"Не удалось получить поток «{name}»:\n{info.error}"))
                return
            task_id = self.recorder.start_recording(
                name, info.video_url, output, source="manual",
                on_complete=self._on_record_complete, audio_url=info.audio_url,
                extra_headers=info.headers,
            )
            if task_id:
                logger.info(f"Начата мгновенная запись ссылки: {name} (task: {task_id})")

        threading.Thread(target=start, daemon=True).start()

    def _record_browser_link_now(self, name: str, link: Dict):
        """Мгновенная запись ссылки из вкладки "Браузер": открывает окно-браузер
        и параллельно пишет экран (core/screen_capture.py) — для сайтов, чью
        прямую ссылку на поток получить не удалось."""
        output = str(self.recorder.build_output_path(name))
        open_url = link.get('player_url') or link.get('url', '')

        def start():
            task_id = self.recorder.start_browser_recording(
                name, open_url, output, source="manual",
                on_complete=self._on_record_complete,
            )
            if task_id:
                logger.info(f"Начата запись экрана (браузер): {name} (task: {task_id})")
            else:
                self.root.after(0, lambda: messagebox.showerror(
                    "Ошибка", f"Не удалось начать запись экрана «{name}».\nПроверьте лог."))

        threading.Thread(target=start, daemon=True).start()

    def _record_from_schedule_item(self, source_type: str, name: str, target: Dict):
        """Кнопка "Сейчас" в планировщике — запись выбранной строки не по
        времени, а сразу, вручную."""
        if source_type == 'link':
            self._record_link_now(name, target)
        elif source_type == 'browser':
            self._record_browser_link_now(name, target)
        else:
            self._record_channel_now(name, target)

    def _on_record_complete(self, success: bool, channel_name: str, output_path: str, ended_early: bool = False):
        if success and ended_early:
            logger.warning(f"Запись завершена раньше срока (источник закончился сам): {channel_name} → {output_path}")
        elif success:
            logger.info(f"Запись завершена: {channel_name} → {output_path}")
        else:
            logger.error(f"Ошибка записи: {channel_name}")

    def _delete_channel(self, name: str):
        if messagebox.askyesno("Удалить канал", f"Удалить канал «{name}» из списка?\nЭто не затронет уже сделанные записи.",
                                parent=self.root):
            self.storage.delete_channel(name)
            self._refresh_data()

    def _delete_link(self, name: str):
        if messagebox.askyesno("Удалить ссылку", f"Удалить «{name}» из списка?\nЭто не затронет уже сделанные записи.",
                                parent=self.root):
            self.storage.delete_link(name)
            self._refresh_data()

    def _delete_browser_link(self, name: str):
        if messagebox.askyesno("Удалить ссылку", f"Удалить «{name}» из списка?\nЭто не затронет уже сделанные записи.",
                                parent=self.root):
            self.storage.delete_browser_link(name)
            self._refresh_data()

    def _create_dialog(self, title: str, geo: str) -> ctk.CTkToplevel:
        c = self.colors
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(title)
        dialog.geometry(geo)
        dialog.transient(self.root)
        dialog.configure(fg_color=c['bg_secondary'])
        dialog.after(100, dialog.grab_set)
        return dialog

    def _add_channel_dialog(self):
        c = self.colors
        dialog = self._create_dialog("Добавить канал", "460x400")
        fields = {}

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(1, weight=1)

        labels = ["Название:", "URL потока:", "Логотип (URL):", "Тип:", "VPN:"]
        keys = ['name', 'url', 'logo', 'type', 'vpn']
        types = ['entry', 'entry', 'entry', 'option', 'option']
        values = ['', '', '', 'iptv', 'Не важно']
        options = [None, None, None, ['iptv', 'youtube', 'vk', 'rutube', 'rtmp'],
                   ['Не важно', 'Требуется', 'Запрещен']]

        for i, (label, key, t, val, opts) in enumerate(zip(labels, keys, types, values, options)):
            ctk.CTkLabel(body, text=label, text_color=c['text_secondary']).grid(
                row=i, column=0, padx=(0, 12), pady=8, sticky='w')
            if t == 'entry':
                fields[key] = ctk.CTkEntry(body, height=32, corner_radius=Config.RADIUS_SM,
                                            fg_color=c['bg_primary'], border_color=c['border'],
                                            text_color=c['text_primary'])
                fields[key].grid(row=i, column=1, pady=8, sticky='ew')
                bind_cyrillic_layout_shortcuts(fields[key])
            elif t == 'option':
                var = tk.StringVar(value=val)
                fields[key] = var
                ctk.CTkOptionMenu(body, values=opts, variable=var, height=32,
                                  corner_radius=Config.RADIUS_SM, fg_color=c['bg_primary'],
                                  button_color=c['bg_tertiary'], button_hover_color=c['bg_hover'],
                                  text_color=c['text_primary']).grid(row=i, column=1, pady=8, sticky='ew')

        def save():
            vpn_map = {'Не важно': None, 'Требуется': True, 'Запрещен': False}
            channel = {
                'name': fields['name'].get().strip(),
                'url': fields['url'].get().strip(),
                'logo_url': fields['logo'].get().strip(),
                'type': fields['type'].get(),
                'vpn_required': vpn_map.get(fields['vpn'].get()),
                'alt_urls': []
            }
            if channel['name'] and channel['url']:
                self.storage.save_channel(channel)
                self._refresh_data()
                dialog.destroy()
            else:
                messagebox.showwarning("Внимание", "Заполните название и URL", parent=dialog)

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=len(labels), column=0, columnspan=2, pady=(16, 0), sticky='ew')

    def _edit_channel_dialog(self, name: str, channel: Dict):
        c = self.colors
        dialog = self._create_dialog(f"Редактировать: {name}", "460x340")
        fields = {}

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(1, weight=1)

        labels = ["Название:", "URL потока:", "Логотип (URL):", "Тип:"]
        keys = ['name', 'url', 'logo', 'type']
        vals = [channel.get('name', ''), channel.get('url', ''), channel.get('logo_url', ''), channel.get('type', 'iptv')]

        for i, (label, key, val) in enumerate(zip(labels, keys, vals)):
            ctk.CTkLabel(body, text=label, text_color=c['text_secondary']).grid(
                row=i, column=0, padx=(0, 12), pady=8, sticky='w')
            if key == 'type':
                var = tk.StringVar(value=val)
                fields[key] = var
                ctk.CTkOptionMenu(body, values=['iptv', 'youtube', 'vk', 'rutube', 'rtmp'], variable=var,
                                  height=32, corner_radius=Config.RADIUS_SM, fg_color=c['bg_primary'],
                                  button_color=c['bg_tertiary'], button_hover_color=c['bg_hover'],
                                  text_color=c['text_primary']).grid(row=i, column=1, pady=8, sticky='ew')
            else:
                fields[key] = ctk.CTkEntry(body, height=32, corner_radius=Config.RADIUS_SM,
                                            fg_color=c['bg_primary'], border_color=c['border'],
                                            text_color=c['text_primary'])
                fields[key].insert(0, val)
                fields[key].grid(row=i, column=1, pady=8, sticky='ew')
                bind_cyrillic_layout_shortcuts(fields[key])

        def save():
            updated = {
                'name': fields['name'].get().strip(),
                'url': fields['url'].get().strip(),
                'logo_url': fields['logo'].get().strip(),
                'type': fields['type'].get(),
                'vpn_required': channel.get('vpn_required'),
                'alt_urls': channel.get('alt_urls', [])
            }
            if updated['name'] and updated['url']:
                self.storage.save_channel(updated)
                self._refresh_data()
                dialog.destroy()
            else:
                messagebox.showwarning("Внимание", "Заполните название и URL", parent=dialog)

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=len(labels), column=0, columnspan=2, pady=(16, 0), sticky='ew')

    LINK_TYPE_OPTIONS = ['Авто', 'youtube', 'vk', 'rutube', 'twitch', '1tv', 'other']

    def _add_link_dialog(self):
        c = self.colors
        dialog = self._create_dialog("Добавить ссылку", "480x430")
        fields = {}

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Ссылка:", text_color=c['text_secondary']).grid(
            row=0, column=0, padx=(0, 12), pady=8, sticky='w')
        url_var = tk.StringVar()
        fields['url'] = ctk.CTkEntry(body, textvariable=url_var, height=32, corner_radius=Config.RADIUS_SM,
                                      placeholder_text="https://www.youtube.com/watch?v=…",
                                      fg_color=c['bg_primary'], border_color=c['border'],
                                      text_color=c['text_primary'])
        fields['url'].grid(row=0, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(fields['url'])

        ctk.CTkLabel(body, text="Название:", text_color=c['text_secondary']).grid(
            row=1, column=0, padx=(0, 12), pady=8, sticky='w')
        name_var = tk.StringVar()
        fields['name'] = ctk.CTkEntry(body, textvariable=name_var, height=32, corner_radius=Config.RADIUS_SM,
                                       placeholder_text="Определится по ссылке автоматически",
                                       fg_color=c['bg_primary'], border_color=c['border'],
                                       text_color=c['text_primary'])
        fields['name'].grid(row=1, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(fields['name'])

        ctk.CTkLabel(body, text="Тип:", text_color=c['text_secondary']).grid(
            row=2, column=0, padx=(0, 12), pady=8, sticky='w')
        type_var = tk.StringVar(value='Авто')
        ctk.CTkOptionMenu(body, values=self.LINK_TYPE_OPTIONS, variable=type_var, height=32,
                          corner_radius=Config.RADIUS_SM, fg_color=c['bg_primary'],
                          button_color=c['bg_tertiary'], button_hover_color=c['bg_hover'],
                          text_color=c['text_primary']).grid(row=2, column=1, pady=8, sticky='ew')

        hint = ctk.CTkLabel(body, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'],
                             justify='left', wraplength=430)
        hint.grid(row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))

        # --- Запись сразу по расписанию: дата (всегда сегодня) + окно
        # времени, авторасчёт которого — из фактической длительности
        # ролика, как только ссылка разберётся. Для эфира (без известной
        # длительности) — запасной вариант +60 минут от старта.
        schedule_var = tk.BooleanVar(value=True)
        schedule_check = ctk.CTkCheckBox(body, text="Сразу запланировать запись", variable=schedule_var,
                                          fg_color=c['accent'], hover_color=c['accent_hover'],
                                          text_color=c['text_primary'], border_color=c['border'],
                                          command=lambda: toggle_schedule_fields())
        schedule_check.grid(row=4, column=0, columnspan=2, sticky='w', pady=(10, 4))

        today = datetime.now()
        day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        today_weekday = today.weekday()
        ctk.CTkLabel(body, text=f"Дата: сегодня, {day_names[today_weekday]} {today:%d.%m.%Y}",
                     text_color=c['text_secondary']).grid(row=5, column=0, columnspan=2, sticky='w', pady=2)

        time_frame = ctk.CTkFrame(body, fg_color='transparent')
        time_frame.grid(row=6, column=0, columnspan=2, sticky='w', pady=4)
        ctk.CTkLabel(time_frame, text="С:", text_color=c['text_secondary']).pack(side='left')
        start_entry = TimeEntry(time_frame, width=64, height=30, corner_radius=Config.RADIUS_SM,
                                 fg_color=c['bg_primary'], border_color=c['border'], text_color=c['text_primary'])
        start_entry.pack(side='left', padx=6)
        ctk.CTkLabel(time_frame, text="До:", text_color=c['text_secondary']).pack(side='left', padx=(10, 0))
        end_entry = TimeEntry(time_frame, width=64, height=30, corner_radius=Config.RADIUS_SM,
                               fg_color=c['bg_primary'], border_color=c['border'], text_color=c['text_primary'])
        end_entry.pack(side='left', padx=6)
        start_entry.set_time(today.strftime('%H:%M'))
        end_entry.set_time(today.strftime('%H:%M'))

        def toggle_schedule_fields():
            state = 'normal' if schedule_var.get() else 'disabled'
            start_entry.configure(state=state)
            end_entry.configure(state=state)

        # Название вводили руками — не перетирать его автоопределением,
        # даже если оно ещё выполняется в фоне и придёт позже.
        name_touched = {'value': False}
        fields['name'].bind('<Key>', lambda e: name_touched.__setitem__('value', True))

        detect_generation = {'id': 0}
        debounce = {'after_id': None}

        def on_url_change(*_):
            if debounce['after_id']:
                dialog.after_cancel(debounce['after_id'])
            # Не дёргаем yt-dlp на каждое нажатие при ручном наборе ссылки —
            # ждём короткую паузу после последнего изменения поля.
            debounce['after_id'] = dialog.after(600, start_detection)

        def start_detection():
            url = url_var.get().strip()
            if not url:
                hint.configure(text="")
                return
            # Тип — сразу и без сети, просто по домену.
            guessed = guess_type(url)
            if guessed != 'other':
                type_var.set(guessed)

            detect_generation['id'] += 1
            my_generation = detect_generation['id']
            hint.configure(text="Определяем название, тип и длительность…")

            def resolve_async():
                info = resolve_link(url)

                def apply():
                    # Пока резолвили — ссылку могли уже поменять на другую.
                    if my_generation != detect_generation['id']:
                        return
                    if not info.ok:
                        # Прямую ссылку получить не удалось — ссылка всё
                        # равно сохранится в "Мои ссылки" (не переносим
                        # автоматически в "Браузер"), просто пометится как
                        # недоступная в списке. Если понадобится режим
                        # браузера — можно добавить ту же ссылку отдельно
                        # через вкладку "Браузер".
                        if not name_touched['value'] and info.title:
                            name_var.set(info.title)
                        start_now = datetime.now()
                        start_entry.set_time(start_now.strftime('%H:%M'))
                        end_entry.set_time((start_now + timedelta(minutes=60)).strftime('%H:%M'))
                        hint.configure(text=f"Не удалось получить прямую ссылку: {info.error}\n"
                                             f"Ссылка всё равно сохранится, но помечена как недоступная.")
                        return
                    if not name_touched['value'] and info.title:
                        name_var.set(info.title)

                    # Окно записи — от текущего момента (начала) и до конца
                    # ролика по его фактической длительности; для эфира без
                    # известной длительности — запасной час.
                    start_now = datetime.now()
                    start_entry.set_time(start_now.strftime('%H:%M'))
                    if info.duration:
                        end_dt = start_now + timedelta(seconds=info.duration)
                        minutes = int(info.duration // 60)
                        hint.configure(text=f"Определено: запись, длительность ~{minutes} мин")
                    else:
                        end_dt = start_now + timedelta(minutes=60)
                        hint.configure(text="Определено: прямой эфир (длительность неизвестна — окно +60 мин, поправьте при необходимости)")
                    end_entry.set_time(end_dt.strftime('%H:%M'))

                self.root.after(0, apply)

            threading.Thread(target=resolve_async, daemon=True).start()

        url_var.trace_add('write', on_url_change)

        def save():
            url = url_var.get().strip()
            name = name_var.get().strip()
            if not url:
                messagebox.showwarning("Внимание", "Вставьте ссылку", parent=dialog)
                return
            link_type = type_var.get()
            if link_type == 'Авто':
                link_type = guess_type(url)

            do_schedule = schedule_var.get()
            start = start_entry.get().strip()
            end = end_entry.get().strip()
            if do_schedule:
                if not self._valid_time_range(start, end, dialog):
                    return

            def finish(display_name: str):
                # Сохраняем в "Мои ссылки" независимо от того, удалось ли
                # получить прямой поток — неудачные просто помечаются
                # недоступными в списке (см. LinkList._resolve_row). Если
                # нужен режим браузера — та же ссылка добавляется отдельно
                # через вкладку "Браузер".
                self.storage.save_link({'name': display_name, 'url': url, 'type': link_type})
                if do_schedule:
                    self.storage.add_schedule_item({
                        'channel_name': display_name,
                        'source_type': 'link',
                        'start_time': start,
                        'end_time': end,
                        'days': [today_weekday],
                        'enabled': True,
                    })
                    self.root.after(0, self.scheduler.reload_schedules)
                self.root.after(0, self._refresh_data)

            if name:
                finish(name)
                dialog.destroy()
            else:
                # Название не задано и автоопределение ещё не подоспело —
                # пробуем сами, не блокируя интерфейс диалогом ожидания.
                dialog.destroy()

                def resolve_name():
                    info = resolve_link(url)
                    finish(info.title if info.ok and info.title else url)

                threading.Thread(target=resolve_name, daemon=True).start()

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=7, column=0, columnspan=2, pady=(16, 0), sticky='ew')

    @staticmethod
    def _valid_time_range(start: str, end: str, parent) -> bool:
        try:
            datetime.strptime(start, '%H:%M')
            datetime.strptime(end, '%H:%M')
        except ValueError:
            messagebox.showwarning("Внимание", "Введите время в формате ЧЧ:ММ, например 09:30", parent=parent)
            return False
        if start == end:
            messagebox.showwarning(
                "Внимание",
                "Измените время окончания: одинаковое время означало бы запись на 24 часа.",
                parent=parent)
            return False
        return True

    def _edit_link_dialog(self, name: str, link: Dict):
        c = self.colors
        dialog = self._create_dialog(f"Редактировать: {name}", "480x260")
        fields = {}

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Название:", text_color=c['text_secondary']).grid(
            row=0, column=0, padx=(0, 12), pady=8, sticky='w')
        fields['name'] = ctk.CTkEntry(body, height=32, corner_radius=Config.RADIUS_SM,
                                       fg_color=c['bg_primary'], border_color=c['border'],
                                       text_color=c['text_primary'])
        fields['name'].insert(0, link.get('name', ''))
        fields['name'].grid(row=0, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(fields['name'])

        ctk.CTkLabel(body, text="Ссылка:", text_color=c['text_secondary']).grid(
            row=1, column=0, padx=(0, 12), pady=8, sticky='w')
        fields['url'] = ctk.CTkEntry(body, height=32, corner_radius=Config.RADIUS_SM,
                                      fg_color=c['bg_primary'], border_color=c['border'],
                                      text_color=c['text_primary'])
        fields['url'].insert(0, link.get('url', ''))
        fields['url'].grid(row=1, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(fields['url'])

        ctk.CTkLabel(body, text="Тип:", text_color=c['text_secondary']).grid(
            row=2, column=0, padx=(0, 12), pady=8, sticky='w')
        type_var = tk.StringVar(value=link.get('type', 'other'))
        ctk.CTkOptionMenu(body, values=self.LINK_TYPE_OPTIONS[1:], variable=type_var, height=32,
                          corner_radius=Config.RADIUS_SM, fg_color=c['bg_primary'],
                          button_color=c['bg_tertiary'], button_hover_color=c['bg_hover'],
                          text_color=c['text_primary']).grid(row=2, column=1, pady=8, sticky='ew')

        def save():
            updated = {
                'name': fields['name'].get().strip(),
                'url': fields['url'].get().strip(),
                'type': type_var.get(),
            }
            if updated['name'] and updated['url']:
                if updated['name'] != name:
                    self.storage.delete_link(name)
                self.storage.save_link(updated)
                self._refresh_data()
                dialog.destroy()
            else:
                messagebox.showwarning("Внимание", "Заполните название и ссылку", parent=dialog)

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=3, column=0, columnspan=2, pady=(16, 0), sticky='ew')

    def _add_browser_link_dialog(self):
        """Ссылка для вкладки "Браузер": сайт, чью прямую ссылку на поток
        получить не удалось — при записи откроется окно-браузер, fullscreen
        в плеере включает сам пользователь, пишется экран."""
        c = self.colors
        dialog = self._create_dialog("Добавить (браузер)", "480x380")

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Ссылка:", text_color=c['text_secondary']).grid(
            row=0, column=0, padx=(0, 12), pady=8, sticky='w')
        url_var = tk.StringVar()
        url_entry = ctk.CTkEntry(body, textvariable=url_var, height=32, corner_radius=Config.RADIUS_SM,
                                  placeholder_text="https://…", fg_color=c['bg_primary'],
                                  border_color=c['border'], text_color=c['text_primary'])
        url_entry.grid(row=0, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(url_entry)

        ctk.CTkLabel(body, text="Название:", text_color=c['text_secondary']).grid(
            row=1, column=0, padx=(0, 12), pady=8, sticky='w')
        name_var = tk.StringVar()
        name_entry = ctk.CTkEntry(body, textvariable=name_var, height=32, corner_radius=Config.RADIUS_SM,
                                   placeholder_text="Например: ОТР — прямой эфир", fg_color=c['bg_primary'],
                                   border_color=c['border'], text_color=c['text_primary'])
        name_entry.grid(row=1, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(name_entry)

        ctk.CTkLabel(body, text="При записи откроется окно-браузер с этой страницей — fullscreen в плеере "
                                 "включаете сами, экран запишется автоматически.",
                     font=ctk.CTkFont(size=10), text_color=c['text_muted'], wraplength=430, justify='left'
                     ).grid(row=2, column=0, columnspan=2, sticky='w', pady=(4, 8))

        schedule_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(body, text="Сразу запланировать запись", variable=schedule_var,
                         fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['text_primary'],
                         border_color=c['border'], command=lambda: toggle_schedule_fields()
                         ).grid(row=3, column=0, columnspan=2, sticky='w', pady=(6, 4))

        today = datetime.now()
        day_names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
        today_weekday = today.weekday()
        ctk.CTkLabel(body, text=f"Дата: сегодня, {day_names[today_weekday]} {today:%d.%m.%Y}",
                     text_color=c['text_secondary']).grid(row=4, column=0, columnspan=2, sticky='w', pady=2)

        time_frame = ctk.CTkFrame(body, fg_color='transparent')
        time_frame.grid(row=5, column=0, columnspan=2, sticky='w', pady=4)
        ctk.CTkLabel(time_frame, text="С:", text_color=c['text_secondary']).pack(side='left')
        start_entry = TimeEntry(time_frame, width=64, height=30, corner_radius=Config.RADIUS_SM,
                                 fg_color=c['bg_primary'], border_color=c['border'], text_color=c['text_primary'])
        start_entry.pack(side='left', padx=6)
        ctk.CTkLabel(time_frame, text="До:", text_color=c['text_secondary']).pack(side='left', padx=(10, 0))
        end_entry = TimeEntry(time_frame, width=64, height=30, corner_radius=Config.RADIUS_SM,
                               fg_color=c['bg_primary'], border_color=c['border'], text_color=c['text_primary'])
        end_entry.pack(side='left', padx=6)
        start_entry.set_time(today.strftime('%H:%M'))
        end_entry.set_time((today + timedelta(minutes=60)).strftime('%H:%M'))

        def toggle_schedule_fields():
            state = 'normal' if schedule_var.get() else 'disabled'
            start_entry.configure(state=state)
            end_entry.configure(state=state)

        def save():
            url = url_var.get().strip()
            name = name_var.get().strip() or url
            if not url:
                messagebox.showwarning("Внимание", "Вставьте ссылку", parent=dialog)
                return

            do_schedule = schedule_var.get()
            start = start_entry.get().strip()
            end = end_entry.get().strip()
            if do_schedule and not self._valid_time_range(start, end, dialog):
                return

            self.storage.save_browser_link({'name': name, 'url': url})
            if do_schedule:
                self.storage.add_schedule_item({
                    'channel_name': name,
                    'source_type': 'browser',
                    'start_time': start,
                    'end_time': end,
                    'days': [today_weekday],
                    'enabled': True,
                })
                self.scheduler.reload_schedules()
            self._refresh_data()
            dialog.destroy()

            # Некоторые страницы не рисуют видео в нашем встроенном браузере
            # (WKWebView), но публикуют og:video — прямую ссылку на сам
            # плеер, которая обычно открывается нормально. Ищем её в фоне
            # и, если найдётся, дописываем в уже сохранённую ссылку.
            def enrich_player_url():
                info = resolve_link(url)
                if info.player_url:
                    self.storage.save_browser_link({'name': name, 'url': url, 'player_url': info.player_url})
                    self.root.after(0, self._refresh_data)

            threading.Thread(target=enrich_player_url, daemon=True).start()

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=6, column=0, columnspan=2, pady=(16, 0), sticky='ew')

    def _edit_browser_link_dialog(self, name: str, link: Dict):
        c = self.colors
        dialog = self._create_dialog(f"Редактировать: {name}", "480x220")

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Название:", text_color=c['text_secondary']).grid(
            row=0, column=0, padx=(0, 12), pady=8, sticky='w')
        name_entry = ctk.CTkEntry(body, height=32, corner_radius=Config.RADIUS_SM,
                                   fg_color=c['bg_primary'], border_color=c['border'],
                                   text_color=c['text_primary'])
        name_entry.insert(0, link.get('name', ''))
        name_entry.grid(row=0, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(name_entry)

        ctk.CTkLabel(body, text="Ссылка:", text_color=c['text_secondary']).grid(
            row=1, column=0, padx=(0, 12), pady=8, sticky='w')
        url_entry = ctk.CTkEntry(body, height=32, corner_radius=Config.RADIUS_SM,
                                  fg_color=c['bg_primary'], border_color=c['border'],
                                  text_color=c['text_primary'])
        url_entry.insert(0, link.get('url', ''))
        url_entry.grid(row=1, column=1, pady=8, sticky='ew')
        bind_cyrillic_layout_shortcuts(url_entry)

        def save():
            new_name = name_entry.get().strip()
            new_url = url_entry.get().strip()
            if not (new_name and new_url):
                messagebox.showwarning("Внимание", "Заполните название и ссылку", parent=dialog)
                return

            url_changed = new_url != link.get('url', '')
            updated = {'name': new_name, 'url': new_url}
            if not url_changed and link.get('player_url'):
                updated['player_url'] = link['player_url']
            if new_name != name:
                self.storage.delete_browser_link(name)
            self.storage.save_browser_link(updated)
            self._refresh_data()
            dialog.destroy()

            if url_changed:
                # Ссылка сменилась — старый player_url (если был) больше не
                # актуален, ищем заново в фоне, как и при добавлении.
                def enrich_player_url():
                    info = resolve_link(new_url)
                    if info.player_url:
                        self.storage.save_browser_link({'name': new_name, 'url': new_url, 'player_url': info.player_url})
                        self.root.after(0, self._refresh_data)

                threading.Thread(target=enrich_player_url, daemon=True).start()

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=2, column=0, columnspan=2, pady=(16, 0), sticky='ew')

    def _show_settings(self):
        c = self.colors
        dialog = self._create_dialog("Settings", "620x220")
        directory = tk.StringVar(value=str(Config.get_recordings_dir()))

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(0, weight=1)

        ctk.CTkLabel(body, text="Recording folder:", text_color=c['text_secondary']).pack(anchor='w')

        entry = ctk.CTkEntry(body, textvariable=directory, height=32, corner_radius=Config.RADIUS_SM,
                              fg_color=c['bg_primary'], border_color=c['border'],
                              text_color=c['text_primary'], state='readonly')
        entry.pack(fill='x', pady=(8, 12))

        def choose_folder():
            selected = filedialog.askdirectory(parent=dialog, initialdir=directory.get())
            if selected:
                directory.set(selected)

        def save():
            try:
                Config.set_recordings_dir(directory.get())
                logger.info(f"Recording folder changed to: {Config.get_recordings_dir()}")
                dialog.destroy()
            except OSError as error:
                messagebox.showerror("Settings", f"Could not use this folder:\n{error}", parent=dialog)

        btn_row = ctk.CTkFrame(body, fg_color='transparent')
        btn_row.pack(fill='x', pady=(4, 0))
        ctk.CTkButton(btn_row, text="Choose…", command=choose_folder, height=32, width=110,
                      corner_radius=Config.RADIUS_SM, fg_color=c['bg_tertiary'], hover_color=c['bg_hover'],
                      text_color=c['text_primary']).pack(side='left')
        ctk.CTkButton(btn_row, text="Save", command=save, height=32, width=90,
                      corner_radius=Config.RADIUS_SM, fg_color=c['accent'], hover_color=c['accent_hover'],
                      text_color=c['accent_text']).pack(side='right')

    def _show_about(self):
        c = self.colors
        dialog = self._create_dialog("О программе", "500x380")

        content = f"""TV Recorder v{Config.APP_VERSION}

Desktop application for previewing and recording TV streams.

Credits and data sources:
• IPTVru (github.com/smolnp/IPTVru)
  provides the playlist currently used to discover channels.
• FFmpeg provides stream recording and preview capabilities.
• Some channel logo URLs come from the playlist and Wikimedia Commons.

TV Recorder is not affiliated with channels or source providers.
Built for macOS."""

        ctk.CTkLabel(dialog, text=content, font=ctk.CTkFont(size=12), text_color=c['text_primary'],
                     justify='left').pack(pady=28, padx=28)

        ctk.CTkButton(dialog, text="Закрыть", command=dialog.destroy, height=32, width=110,
                      corner_radius=Config.RADIUS_SM, fg_color=c['bg_tertiary'], hover_color=c['bg_hover'],
                      text_color=c['text_primary']).pack(pady=10)

    def _start_background_checks(self):
        """Обновляет статус сети каждые пять секунд, не замедляя окно."""
        if self._network_monitor_running:
            return
        self._network_monitor_running = True

        def monitor():
            while self._network_monitor_running:
                result = (VPNManager.is_vpn_active(), NetworkMonitor.is_internet_available())
                while not self._network_results.empty():
                    try:
                        self._network_results.get_nowait()
                    except Empty:
                        break
                self._network_results.put(result)
                time.sleep(5)

        def refresh_status_bar():
            try:
                vpn_active, internet_available = self._network_results.get_nowait()
                self.status_bar.update_vpn_status(vpn_active)
                self.status_bar.update_net_status(internet_available)
            except Empty:
                pass
            if self._network_monitor_running:
                self.root.after(500, refresh_status_bar)

        threading.Thread(target=monitor, daemon=True).start()
        self.root.after(0, refresh_status_bar)

    def _on_close(self):
        self._network_monitor_running = False
        self.preview_panel.stop()
        self.scheduler.stop()
        self.recorder.stop_all()
        self.root.destroy()

    def run(self):
        logger.info("Приложение запущено")
        self.root.mainloop()
