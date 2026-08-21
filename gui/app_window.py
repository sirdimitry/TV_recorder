# gui/app_window.py
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict
import time
import threading
import json
from queue import Empty, Queue

import customtkinter as ctk

from gui.channel_list import ChannelList
from gui.schedule_panel import SchedulePanel
from gui.status_bar import StatusBar
from gui.recording_panel import RecordingPanel
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


def bind_macos_shortcuts(widget):
    widget.bind('<Command-a>', lambda e: widget.select_range(0, 'end'))

    def do_copy(e):
        try:
            text = widget.selection_get()
            widget.clipboard_clear()
            widget.clipboard_append(text)
        except tk.TclError: pass

    widget.bind('<Command-c>', do_copy)

    def do_cut(e):
        try:
            text = widget.selection_get()
            widget.clipboard_clear()
            widget.clipboard_append(text)
            widget.delete("sel.first", "sel.last")
        except tk.TclError: pass

    widget.bind('<Command-x>', do_cut)

    def do_paste(e):
        try:
            text = widget.clipboard_get()
            widget.insert("insert", text)
        except tk.TclError: pass

    widget.bind('<Command-v>', do_paste)


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
        self.root.config(menu=menubar)

        # === ТУЛБАР ===
        toolbar = ctk.CTkFrame(self.root, fg_color='transparent')
        toolbar.pack(fill='x', padx=16, pady=(14, 8))

        title_frame = ctk.CTkFrame(toolbar, fg_color='transparent')
        title_frame.pack(side='left')
        ctk.CTkLabel(title_frame, image=get_icon('tv', c['accent'], 22), text="").pack(side='left', padx=(0, 8))
        ctk.CTkLabel(title_frame, text="TV Recorder", font=ctk.CTkFont(size=18, weight='bold'),
                     text_color=c['text_primary']).pack(side='left')

        ctk.CTkButton(toolbar, text="Проверить все", image=get_icon('refresh', c['text_primary'], 14),
                      compound='left', width=140, height=32, corner_radius=Config.RADIUS_SM,
                      fg_color=c['bg_tertiary'], hover_color=c['bg_hover'], text_color=c['text_primary'],
                      command=self._check_all_channels).pack(side='right', padx=(6, 0))
        ctk.CTkButton(toolbar, text="Добавить канал", image=get_icon('plus', c['accent_text'], 14),
                      compound='left', width=150, height=32, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text'],
                      command=self._add_channel_dialog).pack(side='right', padx=(6, 0))

        # === ОСНОВНАЯ ОБЛАСТЬ: 2 КОЛОНКИ ===
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=14, pady=(0, 8))

        left_frame = ctk.CTkFrame(paned, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        self.channel_list = ChannelList(
            left_frame,
            on_select=self._on_channel_select,
            on_edit=self._edit_channel_dialog,
            on_record=self._record_channel_now,
            on_delete=self._delete_channel,
        )
        self.channel_list.pack(fill='both', expand=True)
        paned.add(left_frame, weight=1)

        right_frame = ttk.Frame(paned)

        schedule_container = ctk.CTkFrame(right_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        schedule_container.pack(fill='both', expand=True, pady=(0, 8))
        self.schedule_panel = SchedulePanel(schedule_container, on_schedule_changed=self._on_schedule_changed)
        self.schedule_panel.pack(fill='both', expand=True)

        recordings_container = ctk.CTkFrame(right_frame, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)
        recordings_container.pack(fill='both', expand=True)
        self.recording_panel = RecordingPanel(recordings_container, self.recorder)
        self.recording_panel.pack(fill='both', expand=True)

        paned.add(right_frame, weight=1)

        self.status_bar = StatusBar(self.root)
        self.status_bar.pack(fill='x', side='bottom')

    def _refresh_data(self):
        channels = self.storage.get_channels()
        self.channel_list.load_channels(channels)
        self.schedule_panel.refresh()

    def _check_all_channels(self):
        self.channel_list.check_all()

    def _on_channel_select(self, name: str):
        logger.info(f"Выбран канал: {name}")

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

    def _on_record_complete(self, success: bool, channel_name: str, output_path: str):
        if success:
            logger.info(f"Запись завершена: {channel_name} → {output_path}")
        else:
            logger.error(f"Ошибка записи: {channel_name}")

    def _delete_channel(self, name: str):
        if messagebox.askyesno("Удалить канал", f"Удалить канал «{name}» из списка?\nЭто не затронет уже сделанные записи.",
                                parent=self.root):
            self.storage.delete_channel(name)
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
                bind_macos_shortcuts(fields[key])
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
                bind_macos_shortcuts(fields[key])

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
        self.scheduler.stop()
        self.recorder.stop_all()
        self.root.destroy()

    def run(self):
        logger.info("Приложение запущено")
        self.root.mainloop()
