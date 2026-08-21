# gui/status_bar.py
import customtkinter as ctk

from utils.config import Config
from utils.icons import get_icon


class StatusBar(ctk.CTkFrame):
    """Нижняя панель статуса: индикаторы VPN и подключения к интернету."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=Config.COLORS['bg_tertiary'], corner_radius=0, height=34)
        self.colors = Config.COLORS
        self.pack_propagate(False)

        c = self.colors

        left_frame = ctk.CTkFrame(self, fg_color='transparent')
        left_frame.pack(side='left', padx=14)

        self.vpn_icon = ctk.CTkLabel(left_frame, text="", image=get_icon('shield', c['text_muted'], 12))
        self.vpn_icon.pack(side='left', padx=(0, 4))
        self.vpn_label = ctk.CTkLabel(left_frame, text="VPN отключен", font=ctk.CTkFont(size=10),
                                       text_color=c['text_secondary'])
        self.vpn_label.pack(side='left', padx=(0, 18))

        self.net_icon = ctk.CTkLabel(left_frame, text="", image=get_icon('wifi', c['text_muted'], 12))
        self.net_icon.pack(side='left', padx=(0, 4))
        self.net_label = ctk.CTkLabel(left_frame, text="Проверка сети…", font=ctk.CTkFont(size=10),
                                       text_color=c['text_secondary'])
        self.net_label.pack(side='left')

    def update_vpn_status(self, active: bool):
        color = self.colors['green'] if active else self.colors['text_muted']
        self.vpn_icon.configure(image=get_icon('shield', color, 12))
        self.vpn_label.configure(text="VPN подключен" if active else "VPN отключен")

    def update_net_status(self, connected: bool):
        color = self.colors['green'] if connected else self.colors['red']
        self.net_icon.configure(image=get_icon('wifi', color, 12))
        self.net_label.configure(text="Интернет доступен" if connected else "Нет интернета")
