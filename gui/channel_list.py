# gui/channel_list.py
import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Dict, Callable, Optional
from core.checker import StreamChecker, StreamStatus
from utils.config import Config
from utils.logger import logger


class ChannelList(ttk.Frame):
    """Список каналов с логотипами, индикаторами и кнопкой записи"""
    
    def __init__(self, parent, on_select: Optional[Callable] = None, 
                 on_edit: Optional[Callable] = None, on_record: Optional[Callable] = None):
        super().__init__(parent)
        self.root = parent.winfo_toplevel()
        self.colors = Config.COLORS
        self.on_select = on_select
        self.on_edit = on_edit
        self.on_record = on_record  # Callback для кнопки записи
        self.checker = StreamChecker()
        self.channel_widgets: Dict[str, dict] = {}
        
        self._setup_ui()
    
    def _setup_ui(self):
        header = ttk.Label(self, text="КАНАЛЫ", font=('Inter', 11, 'bold'))
        header.pack(fill='x', padx=10, pady=(10, 5))
        
        canvas = tk.Canvas(self, bg=self.colors['bg_secondary'], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        self.scroll_frame = ttk.Frame(canvas)
        
        self.scroll_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=self.scroll_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-int(e.delta/120), 'units'))
    
    def load_channels(self, channels: List[Dict]):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self.channel_widgets.clear()
        
        for channel in channels:
            self._add_channel_row(channel)
    
    def _add_channel_row(self, channel: Dict):
        name = channel.get('name', 'Unknown')
        source_type = channel.get('type', 'iptv')
        
        row = ttk.Frame(self.scroll_frame)
        row.pack(fill='x', padx=5, pady=3)
        
        # Логотип
        logo_canvas = tk.Canvas(row, width=40, height=40, 
                               bg=self.colors['bg_tertiary'], highlightthickness=0)
        logo_canvas.pack(side='left', padx=(5, 8), pady=2)
        logo_canvas.bind('<Button-1>', lambda e, ch=channel: self._open_preview(ch))
        
        # Индикатор статуса
        status_indicator = tk.Canvas(row, width=12, height=12, 
                                    bg=self.colors['bg_secondary'], highlightthickness=0)
        status_indicator.place(x=35, y=35)
        status_indicator.create_oval(2, 2, 10, 10, fill='#555555', outline='#555555', tags='dot')
        
        # Информация
        info_frame = ttk.Frame(row)
        info_frame.pack(side='left', fill='x', expand=True, pady=4)
        
        type_badge = f"[{source_type.upper()}]"
        label_name = ttk.Label(info_frame, text=name, font=('Inter', 10, 'bold'))
        label_name.pack(anchor='w')
        label_type = ttk.Label(info_frame, text=type_badge, font=('Inter', 8), 
                              foreground=self.colors['text_secondary'])
        label_type.pack(anchor='w')
        
        # КНОПКА ЗАПИСИ (⏺)
        btn_record = ttk.Button(row, text="⏺", width=3,
                               command=lambda n=name, ch=channel: self._on_record(n, ch))
        btn_record.pack(side='right', padx=2, pady=2)
        
        # Кнопка редактирования
        btn_edit = ttk.Button(row, text="️", width=3,
                             command=lambda n=name, ch=channel: self._on_edit(n, ch))
        btn_edit.pack(side='right', padx=2, pady=2)
        
        # Кнопка проверки
        btn_check = ttk.Button(row, text="⟳", width=3,
                              command=lambda n=name, ind=status_indicator, ch=channel: 
                                  self._check_single(n, ind, ch))
        btn_check.pack(side='right', padx=2, pady=2)
        
        self.channel_widgets[name] = {
            'logo_canvas': logo_canvas,
            'status_indicator': status_indicator,
            'row': row,
            'channel': channel
        }
        
        # Загрузка логотипа
        from utils.logo_cache import LogoCache
        cache = LogoCache()
        logo_path = cache.get_logo_path(name, channel.get('logo_url', ''))
        if logo_path:
            try:
                img = tk.PhotoImage(file=str(logo_path))
                img = img.subsample(max(1, img.width() // 40), max(1, img.height() // 40))
                logo_canvas.create_image(20, 20, image=img, anchor='center')
                logo_canvas.image = img
            except Exception as e:
                logger.debug(f"Ошибка отображения логотипа {name}: {e}")
                self._draw_placeholder(logo_canvas)
        else:
            self._draw_placeholder(logo_canvas)
        
        for widget in (row, info_frame, label_name, label_type):
            widget.bind('<Button-1>', lambda e, n=name: self._on_click(n))
    
    def _draw_placeholder(self, canvas: tk.Canvas):
        canvas.delete("all")
        canvas.create_text(20, 20, text="📺", font=('Arial', 16), fill=self.colors['text_secondary'])
    
    def _on_click(self, name: str):
        if self.on_select: self.on_select(name)
    
    def _on_edit(self, name: str, channel: Dict):
        if self.on_edit: self.on_edit(name, channel)
    
    def _on_record(self, name: str, channel: Dict):
        """Обработчик нажатия на кнопку записи у канала"""
        if self.on_record:
            self.on_record(name, channel)
        else:
            logger.info(f"Запрос записи канала: {name}")
    
    def _open_preview(self, channel: Dict):
        from gui.mini_player import MiniPlayer
        name = channel.get('name', 'Unknown')
        url = channel.get('url', '')
        
        if not url:
            messagebox.showwarning("Внимание", f"У канала '{name}' нет URL потока")
            return
            
        logger.info(f"Открыт предпросмотр: {name}")
        MiniPlayer(self.root, name, url)
    
    def _check_single(self, name: str, indicator: tk.Canvas, channel: Dict):
        indicator.itemconfig('dot', fill='#fbbf24')
        import threading
        def do_check():
            status, msg = self.checker.check(channel)
            color_map = {StreamStatus.GREEN: self.colors['green'], 
                        StreamStatus.YELLOW: self.colors['yellow'], 
                        StreamStatus.RED: self.colors['red']}
            color = color_map.get(status, '#555555')
            indicator.after(0, lambda: indicator.itemconfig('dot', fill=color))
            logger.info(f"Проверка {name}: {status.value} — {msg}")
        threading.Thread(target=do_check, daemon=True).start()
    
    def check_all(self):
        for name, widgets in self.channel_widgets.items():
            self._check_single(name, widgets['status_indicator'], widgets['channel'])
    
    def update_indicator(self, name: str, status: StreamStatus):
        if name in self.channel_widgets:
            color_map = {StreamStatus.GREEN: self.colors['green'], 
                        StreamStatus.YELLOW: self.colors['yellow'], 
                        StreamStatus.RED: self.colors['red']}
            color = color_map.get(status, '#555555')
            self.channel_widgets[name]['status_indicator'].itemconfig('dot', fill=color)