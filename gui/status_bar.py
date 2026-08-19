# gui/status_bar.py
import tkinter as tk
from tkinter import ttk
from utils.config import Config


class StatusBar(ttk.Frame):
    """Нижняя панель статуса с индикатором записи"""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.colors = Config.COLORS
        self._recording_blink = False
        self._blink_id = None
        self._timer_id = None
        
        style = ttk.Style()
        style.configure('Status.TFrame', background=self.colors['bg_tertiary'])
        style.configure('Status.TLabel', 
                       background=self.colors['bg_tertiary'], 
                       foreground=self.colors['text_secondary'],
                       font=('Inter', 9))
        
        self.configure(style='Status.TFrame')
        
        # Левая часть: VPN и Интернет
        left_frame = ttk.Frame(self, style='Status.TFrame')
        left_frame.pack(side='left', padx=10, pady=3)
        
        self.vpn_label = ttk.Label(left_frame, text=" VPN", style='Status.TLabel')
        self.vpn_label.pack(side='left', padx=(0, 15))
        
        self.net_label = ttk.Label(left_frame, text="⚪ Интернет", style='Status.TLabel')
        self.net_label.pack(side='left')
        
        # Правая часть: Статус записи
        self.rec_frame = ttk.Frame(self, style='Status.TFrame')
        self.rec_frame.pack(side='right', padx=10, pady=3)
        
        # Красный индикатор
        self.rec_dot = tk.Canvas(self.rec_frame, width=10, height=10, 
                                bg=self.colors['bg_tertiary'], highlightthickness=0)
        self.rec_dot.pack(side='left', padx=(0, 5))
        self.rec_dot.create_oval(2, 2, 8, 8, fill='', outline='')
        
        self.rec_label = ttk.Label(self.rec_frame, text="", style='Status.TLabel')
        self.rec_label.pack(side='left')
        
        # Кнопка стоп (скрыта по умолчанию)
        self.btn_stop = ttk.Button(self.rec_frame, text="⏹ Стоп", 
                                  command=self._on_stop_clicked, style='TButton')
        
        sep = ttk.Separator(self, orient='horizontal')
        sep.pack(fill='x', side='top')
    
    def update_vpn_status(self, active: bool):
        self.vpn_label.config(text="🟢 VPN подключен" if active else "⚪ VPN отключен")
    
    def update_net_status(self, connected: bool):
        self.net_label.config(text=" Интернет" if connected else "🔴 Нет интернета")
    
    def update_recording_status(self, recording: bool, channel_name: str = "", 
                               on_stop: callable = None):
        """Обновляет статус записи"""
        self._stop_callback = on_stop
        
        if recording:
            self.rec_label.config(text=f"🔴 REC: {channel_name}")
            self.btn_stop.pack(side='left', padx=(10, 0))
            self._start_blinking()
            self._start_timer()
        else:
            self.rec_label.config(text="")
            self.btn_stop.pack_forget()
            self._stop_blinking()
            self._stop_timer()
    
    def _on_stop_clicked(self):
        if self._stop_callback:
            self._stop_callback()
    
    def _start_blinking(self):
        if not self._blink_id:
            self._blink()
    
    def _blink(self):
        self._recording_blink = not self._recording_blink
        color = '#ff4444' if self._recording_blink else '#550000'
        self.rec_dot.itemconfig('all', fill=color, outline=color)
        self._blink_id = self.after(500, self._blink)
    
    def _stop_blinking(self):
        if self._blink_id:
            self.after_cancel(self._blink_id)
            self._blink_id = None
        self.rec_dot.itemconfig('all', fill='', outline='')
    
    def _start_timer(self):
        self._update_timer()
    
    def _update_timer(self):
        # Таймер обновляется извне через recorder.get_recording_duration()
        # Здесь просто заглушка, реальное обновление делается через callback
        pass
    
    def _stop_timer(self):
        if self._timer_id:
            self.after_cancel(self._timer_id)
            self._timer_id = None