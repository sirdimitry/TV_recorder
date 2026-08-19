# gui/app_window.py
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Dict
import time
import threading
import json
from datetime import datetime
from gui.channel_list import ChannelList
from gui.schedule_panel import SchedulePanel
from gui.status_bar import StatusBar
from gui.recording_panel import RecordingPanel
from core.storage import Storage
from core.scheduler import RecordingScheduler
from core.recorder import Recorder
from utils.config import Config
from utils.vpn_manager import VPNManager
from utils.logger import logger


class SplashScreen(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("TV Recorder - Загрузка")
        self.geometry("500x350")
        self.resizable(False, False)
        self.attributes('-topmost', True)
        
        c = Config.COLORS
        self.configure(bg=c['bg_primary'])
        
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width() - 500) // 2
        py = parent.winfo_y() + (parent.winfo_height() - 350) // 2
        if px < 0: px = 100
        if py < 0: py = 100
        self.geometry(f"+{px}+{py}")
        
        logo_label = ttk.Label(self, text="📺", font=('Arial', 60), background=c['bg_primary'])
        logo_label.pack(pady=(40, 10))
        
        title_label = ttk.Label(self, text="TV Recorder", font=('Inter', 24, 'bold'), 
                               background=c['bg_primary'], foreground=c['text_primary'])
        title_label.pack()
        
        version_label = ttk.Label(self, text="v1.0.0 Beta", font=('Inter', 10),
                                 background=c['bg_primary'], foreground=c['text_secondary'])
        version_label.pack(pady=(0, 30))
        
        self.progress = ttk.Progressbar(self, mode='indeterminate', length=300)
        self.progress.pack(pady=10)
        self.progress.start(10)
        
        log_frame = ttk.Frame(self)
        log_frame.pack(fill='both', expand=True, padx=20, pady=(20, 30))
        
        self.log_text = tk.Text(log_frame, height=6, width=50, bg=c['bg_secondary'], 
                               fg=c['text_secondary'], font=('Menlo', 9), bd=0, highlightthickness=0)
        self.log_text.pack(fill='both', expand=True)
        
        self._log("Инициализация приложения...")
    
    def _log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert('end', f"[{timestamp}] {message}\n")
        self.log_text.see('end')
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
        self.root = tk.Tk()
        self.root.title("TV Recorder")
        self.root.geometry("1100x700")
        self.root.minsize(900, 600)
        
        self.colors = Config.COLORS
        self.storage = Storage()
        self.scheduler = RecordingScheduler()
        self.recorder = Recorder()
        
        self.splash = SplashScreen(self.root)
        self.root.withdraw()
        
        self._apply_theme()
        self._setup_ui()
        
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
        from core.m3u_parser import M3UParser
        
        parser = M3UParser()
        online_channels = parser.fetch_and_parse()
        
        if not online_channels:
            self.splash._log("⚠️ Не удалось загрузить онлайн-плейлист.")
            defaults_path = Config.BASE_DIR / "data" / "default_channels.json"
            if defaults_path.exists():
                with open(defaults_path, 'r', encoding='utf-8') as f:
                    online_channels = json.load(f)
                self.splash._log("📂 Используем локальную базу каналов")
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
            else:
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
    
    def _apply_theme(self):
        style = ttk.Style()
        style.theme_use('clam')
        c = self.colors
        
        style.configure('.', background=c['bg_primary'], foreground=c['text_primary'], font=('Inter', 10))
        style.configure('TFrame', background=c['bg_primary'])
        style.configure('Status.TFrame', background=c['bg_tertiary'])
        style.configure('TLabel', background=c['bg_primary'], foreground=c['text_primary'])
        style.configure('TLabelframe', background=c['bg_primary'], foreground=c['accent'])
        style.configure('TLabelframe.Label', background=c['bg_primary'], foreground=c['accent'])
        style.configure('TButton', background=c['bg_tertiary'], foreground=c['text_primary'], padding=(10, 5))
        style.map('TButton', background=[('active', c['accent'])])
        style.configure('Treeview', background=c['bg_secondary'], foreground=c['text_primary'], fieldbackground=c['bg_secondary'], borderwidth=0)
        style.configure('Treeview.Heading', background=c['bg_tertiary'], foreground=c['text_primary'])
        
        style.configure('TEntry', fieldbackground=c['bg_secondary'], background=c['bg_secondary'], 
                        foreground=c['text_primary'], insertcolor=c['text_primary'], 
                        selectbackground=c['accent'], selectforeground='white')
        style.configure('TCombobox', fieldbackground=c['bg_secondary'], background=c['bg_secondary'], 
                        foreground=c['text_primary'], arrowcolor=c['text_primary'])
        style.map('TCombobox', fieldbackground=[('readonly', c['bg_secondary'])])

        style.configure('Dialog.TFrame', background=c['bg_secondary'])
        style.configure('Dialog.TLabel', background=c['bg_secondary'], foreground=c['text_primary'])
        style.configure('Dialog.TEntry', 
                        fieldbackground=c['bg_primary'], background=c['bg_primary'], 
                        foreground='#ffffff', insertcolor='#ffffff', 
                        selectbackground=c['accent'], selectforeground='#ffffff')
        style.configure('Dialog.TCombobox', 
                        fieldbackground=c['bg_primary'], background=c['bg_primary'], 
                        foreground='#ffffff', arrowcolor='#ffffff')
        style.map('Dialog.TCombobox', fieldbackground=[('readonly', c['bg_primary'])])
        
        self.root.configure(bg=c['bg_primary'])

    def _setup_ui(self):
        # === МЕНЮ БАР ===
        menubar = tk.Menu(self.root)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="О программе", command=self._show_about)
        app_menu.add_separator()
        app_menu.add_command(label="Выход", command=self._on_close)
        menubar.add_cascade(label="TV Recorder", menu=app_menu)
        self.root.config(menu=menubar)
        
        # === ТУЛБАР (без кнопки быстрой записи) ===
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill='x', padx=10, pady=(10, 5))
        
        ttk.Label(toolbar, text=" TV Recorder", font=('Inter', 16, 'bold')).pack(side='left')
        
        ttk.Button(toolbar, text="Проверить все", command=self._check_all_channels).pack(side='right', padx=3)
        ttk.Button(toolbar, text="Добавить канал", command=self._add_channel_dialog).pack(side='right', padx=3)
        
        # === ОСНОВНАЯ ОБЛАСТЬ: 2 КОЛОНКИ ===
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Левая колонка: Каналы
        left_frame = ttk.Frame(paned)
        self.channel_list = ChannelList(
            left_frame, 
            on_select=self._on_channel_select, 
            on_edit=self._edit_channel_dialog,
            on_record=self._record_channel_now
        )
        self.channel_list.pack(fill='both', expand=True)
        paned.add(left_frame, weight=1)
        
        # Правая колонка: Расписание + Активные записи
        right_frame = ttk.Frame(paned)
        
        # Верхняя часть: Расписание
        schedule_container = ttk.Frame(right_frame)
        schedule_container.pack(fill='both', expand=True, pady=(0, 5))
        self.schedule_panel = SchedulePanel(schedule_container, on_schedule_changed=self._on_schedule_changed)
        self.schedule_panel.pack(fill='both', expand=True)
        
        # Нижняя часть: Активные записи
        recordings_container = ttk.Frame(right_frame)
        recordings_container.pack(fill='both', expand=True)
        self.recording_panel = RecordingPanel(recordings_container, self.recorder)
        self.recording_panel.pack(fill='both', expand=True)
        
        paned.add(right_frame, weight=1)
        
        # Статус бар
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
        """Мгновенная запись выбранного канала по кнопке ⏺ у канала"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = f"{Config.RECORDINGS_DIR}/{name}_{timestamp}.mp4"
        
        task_id = self.recorder.start_recording(
            name, channel['url'], output, source="manual",
            on_complete=self._on_record_complete
        )
        
        if task_id:
            logger.info(f"Начата мгновенная запись: {name} (task: {task_id})")
            # Принудительное обновление панели записей
            self.recording_panel.refresh()
    
    def _on_record_complete(self, success: bool, channel_name: str, output_path: str):
        if success:
            logger.info(f"Запись завершена: {channel_name} → {output_path}")
        else:
            logger.error(f"Ошибка записи: {channel_name}")
    
    def _create_dialog(self, title: str, geo: str) -> tk.Toplevel:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry(geo)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=self.colors['bg_secondary'])
        return dialog
    
    def _add_channel_dialog(self):
        dialog = self._create_dialog("Добавить канал", "450x360")
        fields = {}
        
        labels = ["Название:", "URL потока:", "Логотип (URL):", "Тип:", "VPN:"]
        keys = ['name', 'url', 'logo', 'type', 'vpn']
        types = ['entry', 'entry', 'entry', 'combo', 'combo']
        values = ['', '', '', 'iptv', 'Не важно']
        combos = [None, None, None, ['iptv', 'youtube', 'vk', 'rutube', 'rtmp'], ['Не важно', 'Требуется', 'Запрещен']]
        
        for i, (label, key, t, val, opts) in enumerate(zip(labels, keys, types, values, combos)):
            ttk.Label(dialog, text=label, style='Dialog.TLabel').grid(row=i, column=0, padx=15, pady=10, sticky='w')
            if t == 'entry':
                fields[key] = ttk.Entry(dialog, width=35, style='Dialog.TEntry')
                fields[key].grid(row=i, column=1, padx=10, pady=10, sticky='ew')
                bind_macos_shortcuts(fields[key])
            elif t == 'combo':
                fields[key] = ttk.Combobox(dialog, values=opts, state='readonly', width=32, style='Dialog.TCombobox')
                fields[key].set(val)
                fields[key].grid(row=i, column=1, padx=10, pady=10, sticky='ew')
        
        dialog.columnconfigure(1, weight=1)
        
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
        
        ttk.Button(dialog, text="Сохранить", command=save, style='TButton').grid(row=len(labels), column=0, columnspan=2, pady=20)
    
    def _edit_channel_dialog(self, name: str, channel: Dict):
        dialog = self._create_dialog(f"Редактировать: {name}", "450x320")
        fields = {}
        
        labels = ["Название:", "URL потока:", "Логотип (URL):", "Тип:"]
        keys = ['name', 'url', 'logo', 'type']
        vals = [channel.get('name',''), channel.get('url',''), channel.get('logo_url',''), channel.get('type','iptv')]
        
        for i, (label, key, val) in enumerate(zip(labels, keys, vals)):
            ttk.Label(dialog, text=label, style='Dialog.TLabel').grid(row=i, column=0, padx=15, pady=10, sticky='w')
            fields[key] = ttk.Entry(dialog, width=35, style='Dialog.TEntry')
            fields[key].insert(0, val)
            fields[key].grid(row=i, column=1, padx=10, pady=10, sticky='ew')
            bind_macos_shortcuts(fields[key])
            
        dialog.columnconfigure(1, weight=1)
        
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
                
        ttk.Button(dialog, text="Сохранить", command=save, style='TButton').grid(row=len(labels), column=0, columnspan=2, pady=20)
    
    def _show_about(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("О программе")
        dialog.geometry("400x300")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        
        c = self.colors
        dialog.configure(bg=c['bg_primary'])
        
        content = """TV Recorder v1.0.0 Beta

Приложение для записи и просмотра 
федеральных телеканалов России.

Источники данных:
• Плейлисты и логотипы предоставлены 
  проектом IPTVru 
  (github.com/smolnp/IPTVru)
  Лицензия: MIT

Разработано для macOS"""
        
        label = ttk.Label(dialog, text=content, font=('Inter', 10), 
                         background=c['bg_primary'], foreground=c['text_primary'],
                         justify='left')
        label.pack(pady=30, padx=30)
        
        ttk.Button(dialog, text="Закрыть", command=dialog.destroy).pack(pady=10)
    
    def _start_background_checks(self):
        def check():
            self.status_bar.update_vpn_status(VPNManager.is_vpn_active())
            try:
                import requests; requests.get('https://google.com', timeout=3)
                self.status_bar.update_net_status(True)
            except: self.status_bar.update_net_status(False)
            self.root.after(10000, check)
        check()
    
    def _on_close(self):
        self.scheduler.stop()
        self.recorder.stop_all()
        self.root.destroy()
    
    def run(self):
        logger.info("Приложение запущено")
        self.root.mainloop()