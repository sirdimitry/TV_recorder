# gui/app_window.py
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, Optional
import time
import threading
import json
from queue import Empty, Queue

import customtkinter as ctk

from gui.channel_list import ChannelList
from gui.download_dialog import show_add_download_dialog
from gui.download_list import DownloadList
from gui.link_list import LinkList
from gui.preview_panel import PreviewPanel
from gui.schedule_panel import SchedulePanel, TimeEntry
from gui.status_bar import StatusBar
from gui.recording_panel import RecordingPanel
from gui.tab_strip import TabStrip
from core.link_resolver import resolve_link, guess_type
from core.storage import Storage
from core.scheduler import RecordingScheduler
from core.recorder import Recorder
from core.downloader import Downloader
from core.notifier import Notifier
from utils.config import Config
from utils.icons import get_icon
from utils.vpn_manager import VPNManager
from utils.network_monitor import NetworkMonitor
from utils.tk_helpers import bind_cyrillic_layout_shortcuts
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


def _format_mmss(total_seconds: float) -> str:
    """Секунды -> "мм:сс" — позиция/длительность внутри самого ролика, не
    время на часах. Минуты не ограничены двумя цифрами (см. TimeEntry в
    gui/schedule_panel.py — виджет сам разрешает вводить больше цифр,
    трёхчасовой ролик — это "200:44", а не переполнение)."""
    total = round(total_seconds)
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


def _format_human_duration(total_seconds: float) -> str:
    """Секунды -> "6 мин 13 сек" — для итогового уведомления по завершении
    записи (не путать с _format_mmss — та обозначает позицию в ролике)."""
    total = round(total_seconds)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if hours or minutes:
        parts.append(f"{minutes} мин")
    parts.append(f"{seconds} сек")
    return " ".join(parts)


def _parse_mmss_seconds(text: str) -> Optional[int]:
    """"мм:сс" -> секунды, либо None если пусто/невалидно. Секунды должны
    быть 0-59 (минуты не ограничены — см. _format_mmss)."""
    if not text:
        return None
    parts = text.split(':')
    if len(parts) != 2:
        return None
    try:
        minutes, seconds = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if seconds >= 60 or minutes < 0 or seconds < 0:
        return None
    return minutes * 60 + seconds


class AppWindow:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("TV Recorder")
        self.root.geometry("1150x720")
        self.root.minsize(920, 620)

        self.colors = Config.COLORS
        self.storage = Storage()
        self.recorder = Recorder()
        self.downloader = Downloader()
        self.scheduler = RecordingScheduler(recorder=self.recorder)
        self.notifier = Notifier()
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
                if (existing.get('url') != ch['url'] or existing.get('logo_url') != ch['logo_url']
                        or existing.get('audio_url') != ch.get('audio_url')):
                    self.storage.save_channel(ch)
                    updated += 1

        # MANUAL_FIXES с audio_url (см. 'Россия 24'/'Россия К' — видео и звук
        # у них раздельные HLS-рендиции) применяем НАПРЯМУЮ к уже сохранённым
        # каналам, а не только через цикл выше — тот зависит от того, попал
        # ли именно этот канал в federal_channels ЭТОГО конкретного онлайн-
        # плейлиста (список из внешнего источника, порядок и состав которого
        # не гарантирован), и на практике 'Россия К' туда не всегда попадает.
        # Без этого прохода однажды пропущенный канал так и остаётся без
        # звука до случайного попадания в подходящий фетч.
        for name, fix in M3UParser.MANUAL_FIXES.items():
            audio_fix = fix.get('audio_url')
            if not audio_fix:
                continue
            existing = current_map.get(name)
            if existing and existing.get('audio_url') != audio_fix:
                existing['audio_url'] = audio_fix
                self.storage.save_channel(existing)
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
        self.paned = ttk.PanedWindow(self.root, orient='horizontal')
        self.paned.pack(fill='both', expand=True, padx=14, pady=(0, 8))
        paned = self.paned

        left_frame = ctk.CTkFrame(paned, fg_color=c['bg_secondary'], corner_radius=Config.RADIUS)

        self.preview_panel = PreviewPanel(left_frame)
        self.preview_panel.pack(fill='x', padx=4, pady=(4, 0))

        # Иконки вместо текстовых вкладок — встроенный CTkTabview этого не
        # умеет (его CTkSegmentedButton без слота под картинку), поэтому
        # свой компактный переключатель (gui/tab_strip.py) + страницы,
        # уложенные в одну ячейку grid и переключаемые через .tkraise() —
        # обычный tkinter-приём для "вкладок" без стороннего виджета.
        self.tab_strip = TabStrip(
            left_frame,
            tabs=[
                ('channels', 'Каналы', 'tv'),
                ('links', 'Мои ссылки', 'link'),
                ('downloads', 'Загрузки', 'download'),
            ],
            command=self._on_left_tab_changed, colors=c)
        self.tab_strip.pack(fill='x', padx=4, pady=(8, 6))

        self.tab_pages = ctk.CTkFrame(left_frame, fg_color='transparent')
        self.tab_pages.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        self.tab_pages.grid_rowconfigure(0, weight=1)
        self.tab_pages.grid_columnconfigure(0, weight=1)

        self.channel_list = ChannelList(
            self.tab_pages,
            recorder=self.recorder,
            on_select=lambda name: self._on_source_select('channel', name),
            on_edit=self._edit_channel_dialog,
            on_record=self._record_channel_now,
            on_delete=self._delete_channel,
            on_preview=self._show_channel_preview,
            on_add=self._add_channel_dialog,
        )
        self.channel_list.grid(row=0, column=0, sticky='nsew')

        self.link_list = LinkList(
            self.tab_pages,
            recorder=self.recorder,
            on_select=lambda name: self._on_source_select('link', name),
            on_edit=self._edit_link_dialog,
            on_record=self._record_link_now,
            on_delete=self._delete_link,
            on_add=self._add_link_dialog,
            on_preview=self._show_channel_preview,
        )
        self.link_list.grid(row=0, column=0, sticky='nsew')

        self.download_list = DownloadList(
            self.tab_pages,
            downloader=self.downloader,
            storage=self.storage,
            on_add=self._add_download_dialog,
        )
        self.download_list.grid(row=0, column=0, sticky='nsew')

        self._tab_pages = {
            'channels': self.channel_list,
            'links': self.link_list,
            'downloads': self.download_list,
        }
        self.channel_list.tkraise()

        paned.add(left_frame, weight=1)

        self.right_paned = ttk.PanedWindow(paned, orient='vertical')
        right_paned = self.right_paned

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
        self.download_list.load_downloads(self.storage.get_downloads())
        self.schedule_panel.refresh()

    def _add_download_dialog(self):
        show_add_download_dialog(self.root, self.colors, self.storage, self.downloader,
                                  on_added=lambda: self.tab_strip.set('downloads'))

    def _check_all_channels(self):
        self.channel_list.check_all()

    def _toolbar_check_all(self):
        key = self.tab_strip.get()
        if key == 'links':
            self.link_list.load_links(self.storage.get_links())
        else:
            self.channel_list.check_all()

    def _on_left_tab_changed(self, key: str):
        page = self._tab_pages.get(key)
        if page is not None:
            page.tkraise()
        self.btn_toolbar_check.configure(text="Обновить статус" if key == 'links' else "Проверить все")

        # "Загрузки" архитектурно не связаны ни с планировщиком, ни с
        # активными записями (core/downloader.py не пересекается с
        # core/scheduler.py/core/recorder.py) — на этой вкладке правая
        # панель просто отъедала бы ширину у строк загрузок без всякой
        # пользы. На Каналах и Ссылках планировщик реально работает — оба
        # равноправные source_type в core/scheduler.py, панель там уместна
        # и остаётся.
        right_visible = str(self.right_paned) in self.paned.panes()
        if key == 'downloads' and right_visible:
            self.paned.forget(self.right_paned)
        elif key != 'downloads' and not right_visible:
            self.paned.add(self.right_paned, weight=1)

    def _on_source_select(self, source_type: str, name: str):
        # Клик по строке в списке слева (канал или ссылка) синхронизирует
        # форму планировщика справа — остаётся только ввести время (см.
        # SchedulePanel.preselect_source). Раньше клик по списку ничего не
        # делал для планировщика, канал/ссылку приходилось выбирать заново
        # вручную в его собственном выпадающем списке.
        logger.info(f"Выбран {'канал' if source_type == 'channel' else 'ссылка'}: {name}")
        self.schedule_panel.preselect_source(source_type, name)

    def _show_channel_preview(self, name: str, url: str, headers: Optional[Dict] = None,
                               audio_url: Optional[str] = None):
        self.preview_panel.show(name, url, headers, audio_url)

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
                on_complete=self._on_record_complete,
                audio_url=channel.get('audio_url')
            )
            if task_id:
                logger.info(f"Начата мгновенная запись: {name} (task: {task_id})")

        threading.Thread(target=start, daemon=True).start()

    def _record_link_now(self, name: str, link: Dict, stop_after: Optional[float] = None,
                          seek_seconds: Optional[float] = None):
        """Мгновенная запись вручную добавленной ссылки: сперва разбираем её
        через yt-dlp/HTML-скрейп/скрытый браузер-снифф (core/link_resolver.py)
        в поисках прямого потока — если находится, копируем его как обычно.
        Если нет (link_resolver уже перепробовал всё автоматическое) —
        последний рубеж: настоящее видимое окно браузера и запись самого
        экрана под ним (core/screen_capture.py), тем же способом, что раньше
        требовал отдельно добавлять ссылку во вкладку "Браузер" — теперь это
        происходит само, без второй ручной попытки.
        stop_after — секунды, через сколько остановить запись самим (длина
        фрагмента из диалога добавления, а не время по часам); None — не
        останавливать, ждать ручной остановки.
        seek_seconds — позиция в САМОМ ролике, с которой начать (поле "С:"),
        а не задержка перед стартом — работает только для прямого потока
        (core/recorder.py передаёт её ffmpeg как -ss перед -i); запись через
        браузер (screen-capture фолбэк) не умеет перематывать открывшуюся
        страницу и просто пишет с того места, с которого сама начнёт
        воспроизведение."""
        output = str(self.recorder.build_output_path(name))
        clip_end_seconds = (seek_seconds or 0) + stop_after if stop_after else None
        clip_range_text = None
        if seek_seconds:
            end_label = _format_mmss(clip_end_seconds) if clip_end_seconds else '…'
            clip_range_text = f"{_format_mmss(seek_seconds)}–{end_label}"

        # Скорость ускоренного воспроизведения для резервной записи экрана
        # (когда не помог ни прямой поток, ни sniff — см. ниже) — реальное
        # экранное время короче содержимого ролика в это же число раз,
        # ffmpeg потом растягивает файл обратно (core/screen_capture.py:
        # build_timestretch_cmd). Смысла ускорять короткие фрагменты нет
        # (запас на открытие окна/старт плеера тот же, а качество только
        # хуже) — включаем только когда реально стоит недёшево прождать:
        # цель — уложиться примерно в 75 секунд экранного времени, но не
        # больше x8 (на такой скорости звук/картинка у части плееров уже
        # разваливаются).
        browser_speed_factor = None
        if stop_after and stop_after > 90:
            browser_speed_factor = min(8, max(1, round(stop_after / 75)))
            if browser_speed_factor <= 1:
                browser_speed_factor = None

        def start():
            record_started = time.time()
            used_browser_fallback = False
            used_speed_factor = None
            # Пока resolve_link() (ниже) ищет прямой поток, строка ссылки
            # молчит — на тяжёлых сайтах (otr-online.ru, tass.ru) это может
            # занять до минуты, и непонятно, работает приложение или зависло.
            # Снимаем индикатор в finally — то есть как только определится
            # итог (прямая запись, браузер или ошибка), а не раньше.
            self.root.after(0, lambda: self.link_list.set_row_resolving(name, True))

            def on_task_complete(success, channel_name, output_path, ended_early=False):
                # Раньше по завершении мгновенной записи ссылки не было
                # вообще никакой видимой пользователю обратной связи (только
                # лог) — планировщик (core/scheduler.py) шлёт уведомления
                # для расписания, а этот, ручной, путь — нет. По просьбе:
                # итог (сколько реально писали, и какой фрагмент ролика,
                # если был seek) должен быть виден, а не только в логе.
                self._on_record_complete(success, channel_name, output_path, ended_early)
                elapsed = time.time() - record_started
                if success:
                    body = f"Длительность записи: {_format_human_duration(elapsed)}"
                    if clip_range_text and not used_browser_fallback:
                        body = f"Фрагмент {clip_range_text}\n{body}"
                    if used_speed_factor:
                        body += f"\nЗапись экрана x{used_speed_factor:.0f} (резервный способ)"
                    self.notifier.send("✅ Запись завершена", f"{channel_name}\n{body}")
                else:
                    self.notifier.send("❌ Ошибка записи", channel_name)

            try:
                info = resolve_link(link.get('url', ''))
                if info.ok:
                    task_id = self.recorder.start_recording(
                        name, info.video_url, output, source="manual",
                        on_complete=on_task_complete, audio_url=info.audio_url,
                        extra_headers=info.headers, seek_seconds=seek_seconds,
                        clip_end_seconds=clip_end_seconds, duration_limit_seconds=stop_after,
                    )
                    if task_id:
                        logger.info(f"Начата мгновенная запись ссылки: {name} (task: {task_id})")
                elif info.skip_browser_fallback:
                    # Источник заведомо не отдаст ничего полезного и через
                    # screen-capture (например tass.ru — тот же антибот
                    # экран, что и на прямом запросе, см.
                    # core/link_resolver.py:_resolve_tass) — показываем
                    # причину как есть, вместо того чтобы тратить время на
                    # открытие браузерного окна ради заведомо пустой записи.
                    logger.warning(f"'{name}': {info.error}")
                    self.root.after(0, lambda: messagebox.showerror(
                        "Не удалось записать", f"«{name}»:\n\n{info.error}", parent=self.root))
                    return
                else:
                    logger.warning(f"Прямой поток для '{name}' не найден ({info.error}) — пробуем через браузер")
                    used_browser_fallback = True
                    if seek_seconds:
                        logger.warning(f"Позиция С:{seek_seconds:.0f}с для '{name}' не будет учтена — "
                                        f"запись через браузер не умеет перематывать ролик")
                    open_url = info.player_url or link.get('player_url') or link.get('url', '')
                    task_id = self.recorder.start_browser_recording(
                        name, open_url, output, source="manual",
                        on_complete=on_task_complete, speed_factor=browser_speed_factor,
                    )
                    if task_id:
                        # start_browser_recording уже дождался (синхронно)
                        # подтверждения от страницы, что ускорение реально
                        # применилось (см. core/recorder.py:
                        # _wait_for_speed_confirmation) — если видео не
                        # нашлось (например, чужой iframe), task.speed_factor
                        # там уже сброшен в None. Берём АКТУАЛЬНОЕ значение
                        # оттуда, а не свою исходную заявку browser_speed_factor —
                        # иначе таймер остановки ниже посчитал бы неправильную
                        # (заниженную в browser_speed_factor раз) паузу для
                        # записи, которая на самом деле идёт в обычном темпе.
                        confirmed_task = self.recorder.tasks.get(task_id)
                        used_speed_factor = confirmed_task.speed_factor if confirmed_task else None
                        logger.info(f"Начата запись экрана (браузер) для ссылки: {name} (task: {task_id})"
                                    + (f", ускорено x{used_speed_factor:.0f}" if used_speed_factor else ""))
                        if seek_seconds:
                            self.root.after(0, lambda: messagebox.showwarning(
                                "Внимание",
                                f"Прямую ссылку на поток для «{name}» получить не удалось — запись пошла через "
                                f"окно браузера.\nПеремотка на {_format_mmss(seek_seconds)} в этом режиме не "
                                f"поддерживается — пишется с той позиции, с которой сама начнёт воспроизведение "
                                f"страница.", parent=self.root))
                    else:
                        self.root.after(0, lambda: messagebox.showerror(
                            "Ошибка", f"Не удалось начать запись «{name}» ни напрямую, ни через браузер.\nПроверьте лог."))
                        return
                if task_id and stop_after:
                    # На ускоренном воспроизведении экранное время короче
                    # содержимого в browser_speed_factor раз — ждать полный
                    # stop_after по часам означало бы записать в
                    # browser_speed_factor раз БОЛЬШЕ содержимого, чем просили.
                    wall_clock_stop = stop_after / used_speed_factor if used_speed_factor else stop_after
                    timer = threading.Timer(wall_clock_stop, self.recorder.stop_recording, args=[task_id])
                    timer.daemon = True
                    timer.start()
            finally:
                self.root.after(0, lambda: self.link_list.set_row_resolving(name, False))

        threading.Thread(target=start, daemon=True).start()

    def _record_from_schedule_item(self, source_type: str, name: str, target: Dict):
        """Кнопка "Сейчас" в планировщике — запись выбранной строки не по
        времени, а сразу, вручную."""
        if source_type == 'link':
            self._record_link_now(name, target)
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

        hint_frame = ctk.CTkFrame(body, fg_color='transparent')
        hint_frame.grid(row=3, column=0, columnspan=2, sticky='w', pady=(4, 0))
        hint = ctk.CTkLabel(hint_frame, text="", font=ctk.CTkFont(size=10), text_color=c['text_muted'],
                             justify='left', wraplength=430)
        hint.pack(anchor='w')
        # Прямую ссылку на поток получить не всегда возможно (видео
        # рисуется JS-ом на странице, сайт отдаёт стаб анти-бот системе и
        # т.п.) — тут ничего не "доразвить", HTTP-запросом такое в
        # принципе не пройти. Единственный рабочий вариант для таких
        # сайтов — режим "Браузер" (открывается настоящий движок и
        # пишется экран), поэтому вместо тихого "недоступно" сразу
        # предлагаем переключиться, не заставляя вбивать ссылку заново.
        # --- Запись сразу по хронометражу ролика: это не расписание по
        # часам (ссылка не привязана к календарной дате/времени эфира) —
        # "С:"/"До:" здесь означают положение в САМОМ ролике в формате
        # мм:сс (0:00 — его начало), а не время на часах. "С:" — реальный
        # seek (ffmpeg -ss, см. core/recorder.py), не просто вычитается для
        # длины записи. По умолчанию — весь ролик целиком (0:00 до его
        # настоящей длительности, как только она определится); можно
        # указать вручную и "С:", и более короткое "До:", чтобы вырезать
        # конкретный фрагмент. При включении запись стартует сразу же, без
        # ожидания какого-либо расписания.
        schedule_var = tk.BooleanVar(value=True)
        schedule_check = ctk.CTkCheckBox(body, text="Начать запись сразу же", variable=schedule_var,
                                          fg_color=c['accent'], hover_color=c['accent_hover'],
                                          text_color=c['text_primary'], border_color=c['border'],
                                          command=lambda: toggle_schedule_fields())
        schedule_check.grid(row=4, column=0, columnspan=2, sticky='w', pady=(10, 4))

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
        ctk.CTkLabel(time_frame, text="мм:сс — позиция в ролике", font=ctk.CTkFont(size=10),
                     text_color=c['text_muted']).pack(side='left', padx=(10, 0))
        start_entry.set_time('0:00')
        end_entry.set_time('')

        def toggle_schedule_fields():
            state = 'normal' if schedule_var.get() else 'disabled'
            start_entry.configure(state=state)
            end_entry.configure(state=state)

        # Название вводили руками — не перетирать его автоопределением,
        # даже если оно ещё выполняется в фоне и придёт позже.
        name_touched = {'value': False}
        fields['name'].bind('<Key>', lambda e: name_touched.__setitem__('value', True))

        # TimeEntry показывает "До:" только с точностью до минуты, а
        # реальная длительность может быть, например, 83.968 сек — если по
        # округлённому до минуты значению ставить таймер остановки, запись
        # обрежется на десятки секунд раньше конца ролика. Поэтому точную
        # длительность храним отдельно и используем её при сохранении, если
        # пользователь сам не трогал поле "До:".
        end_touched = {'value': False}
        end_entry.bind('<Key>', lambda e: end_touched.__setitem__('value', True))
        resolved_duration = {'value': None}

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
                        # Прямую ссылку получить не удалось — обычно потому,
                        # что видео рисуется JS-ом или сайт отдаёт анти-бот
                        # заглушку, а это HTTP-запросом принципиально не
                        # обойти. Ссылка всё равно сохранится и запись всё
                        # равно сработает — просто вместо копирования потока
                        # автоматически откроется настоящее окно браузера и
                        # запишется сам экран под ним (см. _record_link_now).
                        if not name_touched['value'] and info.title:
                            name_var.set(info.title)
                        resolved_duration['value'] = None
                        if not end_touched['value']:
                            end_entry.set_time('')
                        hint.configure(text=f"Не удалось получить прямую ссылку: {info.error}\n"
                                             f"Запись всё равно сработает — автоматически через окно браузера.")
                        return
                    if not name_touched['value'] and info.title:
                        name_var.set(info.title)

                    # "До:" — фактическая длительность самого ролика (не
                    # время на часах): по умолчанию пишем его целиком. Для
                    # эфира без известной длительности оставляем поле
                    # пустым — пусть человек сам решит, сколько писать,
                    # а не гадаем случайным числом.
                    if info.duration:
                        resolved_duration['value'] = info.duration
                        if not end_touched['value']:
                            end_entry.set_time(_format_mmss(info.duration))
                        minutes = int(info.duration // 60)
                        hint.configure(text=f"Определено: длительность ролика ~{minutes} мин")
                    else:
                        resolved_duration['value'] = None
                        if not end_touched['value']:
                            end_entry.set_time('')
                        hint.configure(text="Определено: прямой эфир (длительность неизвестна — "
                                             "укажите «До:» сами, либо оставьте пустым, чтобы писать "
                                             "до ручной остановки)")

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

            start_seconds = _parse_mmss_seconds(start) or 0
            # Реальный seek в сам ролик (ffmpeg -ss перед -i, см.
            # core/recorder.py) — раньше "С:" только вычиталось из "До:" для
            # длины записи, а сама запись всегда стартовала с начала ролика.
            seek_seconds = float(start_seconds) if do_schedule and start_seconds > 0 else None

            duration_seconds = None
            if do_schedule and end:
                # Поле "До:" — если пользователь его не трогал, используем
                # точную длительность ролика (секунды) как верхнюю границу
                # таймера остановки; сама запись всё равно завершится раньше
                # по концу потока, если seek_seconds > 0.
                if not end_touched['value'] and resolved_duration['value']:
                    duration_seconds = resolved_duration['value']
                else:
                    end_seconds = _parse_mmss_seconds(end)
                    if end_seconds is None:
                        messagebox.showwarning(
                            "Внимание", "«До:» должно быть в формате мм:сс (позиция в ролике), "
                                         "либо оставьте поле пустым, чтобы писать до ручной остановки", parent=dialog)
                        return
                    duration_seconds = end_seconds - start_seconds
                    if duration_seconds <= 0:
                        messagebox.showwarning(
                            "Внимание", "«До:» должно быть позже «С:»", parent=dialog)
                        return

            def finish(display_name: str):
                # Сохраняем в "Мои ссылки" независимо от того, удалось ли
                # получить прямой поток — неудачные просто помечаются как
                # идущие через браузер в списке (см. LinkList._resolve_row);
                # запись сама разберётся с этим при старте (см. _record_link_now).
                link = {'name': display_name, 'url': url, 'type': link_type}
                self.storage.save_link(link)
                self.root.after(0, self._refresh_data)
                if do_schedule:
                    # Не расписание по часам — ссылка стартует записью
                    # сразу же, "До:" (если задано) лишь ограничивает её
                    # длительность, "С:" — позиция начала в самом ролике.
                    self.root.after(0, lambda: self._record_link_now(
                        display_name, link, stop_after=duration_seconds, seek_seconds=seek_seconds))

            # Сохраняем СРАЗУ, даже если название ещё не определилось (тогда
            # временно используем саму ссылку как имя) — раньше при пустом
            # названии диалог закрывался мгновенно, а сама запись в "Мои
            # ссылки" появлялась только после ОТДЕЛЬНОГО повторного
            # resolve_link в фоне, который для некоторых сайтов (например
            # otr-online.ru) может идти почти минуту — с точки зрения
            # пользователя ссылка будто "пропадала в никуда" без всякой
            # обратной связи. Теперь строка появляется в списке мгновенно,
            # а название уточняется на месте (переименованием), когда
            # (и если) домоется настоящее — так же, как уже устроено в
            # "Загрузки" (строка сразу же в статусе "resolving").
            initial_name = name or url
            finish(initial_name)
            dialog.destroy()

            if not name:
                def resolve_and_rename():
                    info = resolve_link(url)
                    if info.ok and info.title and info.title != initial_name:
                        self.storage.delete_link(initial_name)
                        self.storage.save_link({'name': info.title, 'url': url, 'type': link_type})
                        self.root.after(0, self._refresh_data)

                threading.Thread(target=resolve_and_rename, daemon=True).start()

        ctk.CTkButton(body, text="Сохранить", command=save, height=36, corner_radius=Config.RADIUS_SM,
                      fg_color=c['accent'], hover_color=c['accent_hover'], text_color=c['accent_text']
                      ).grid(row=6, column=0, columnspan=2, pady=(16, 0), sticky='ew')

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

    def _show_settings(self):
        c = self.colors
        dialog = self._create_dialog("Settings", "620x320")
        recordings_dir = tk.StringVar(value=str(Config.get_recordings_dir()))
        downloads_dir = tk.StringVar(value=str(Config.get_downloads_dir()))

        body = ctk.CTkFrame(dialog, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=20, pady=20)
        body.columnconfigure(0, weight=1)

        def folder_row(label_text: str, variable: tk.StringVar):
            ctk.CTkLabel(body, text=label_text, text_color=c['text_secondary']).pack(anchor='w')
            entry = ctk.CTkEntry(body, textvariable=variable, height=32, corner_radius=Config.RADIUS_SM,
                                  fg_color=c['bg_primary'], border_color=c['border'],
                                  text_color=c['text_primary'], state='readonly')
            entry.pack(fill='x', pady=(8, 4))

            def choose_folder():
                selected = filedialog.askdirectory(parent=dialog, initialdir=variable.get())
                if selected:
                    variable.set(selected)

            ctk.CTkButton(body, text="Choose…", command=choose_folder, height=32, width=110,
                          corner_radius=Config.RADIUS_SM, fg_color=c['bg_tertiary'], hover_color=c['bg_hover'],
                          text_color=c['text_primary']).pack(anchor='w', pady=(0, 16))

        folder_row("Recording folder:", recordings_dir)
        folder_row("Downloads folder:", downloads_dir)

        def save():
            try:
                Config.set_recordings_dir(recordings_dir.get())
                Config.set_downloads_dir(downloads_dir.get())
                logger.info(f"Recording folder changed to: {Config.get_recordings_dir()}")
                logger.info(f"Downloads folder changed to: {Config.get_downloads_dir()}")
                dialog.destroy()
            except OSError as error:
                messagebox.showerror("Settings", f"Could not use this folder:\n{error}", parent=dialog)

        ctk.CTkButton(body, text="Save", command=save, height=32, width=90,
                      corner_radius=Config.RADIUS_SM, fg_color=c['accent'], hover_color=c['accent_hover'],
                      text_color=c['accent_text']).pack(anchor='e')

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
