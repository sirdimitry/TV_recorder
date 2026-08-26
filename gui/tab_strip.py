# gui/tab_strip.py
"""Компактный переключатель разделов с иконками — замена встроенному
ctk.CTkTabview, у которого сегментированная кнопка текстовая и не умеет
показывать иконку рядом с подписью (см. CTkSegmentedButton API)."""
from typing import Callable, List, Optional, Tuple

import customtkinter as ctk

from utils.config import Config
from utils.icons import get_icon


class TabStrip(ctk.CTkFrame):
    """Ряд компактных "пилюль" (иконка + короткая подпись). Сам не хранит
    содержимое разделов — только состояние выбора и подсветку; переключение
    самих страниц делает вызывающий код через command."""

    def __init__(self, parent, tabs: List[Tuple[str, str, str]],
                 command: Optional[Callable[[str], None]] = None, colors: Optional[dict] = None):
        c = colors or Config.COLORS
        super().__init__(parent, fg_color=c['bg_tertiary'], corner_radius=Config.RADIUS)
        self._colors = c
        self._command = command
        self._buttons = {}
        self._active = tabs[0][0] if tabs else None

        for key, label, icon in tabs:
            btn = ctk.CTkButton(
                self, text=label, image=get_icon(icon, c['text_secondary'], 15), compound='left',
                height=32, corner_radius=16, font=ctk.CTkFont(size=12),
                command=lambda k=key: self.set(k))
            btn.pack(side='left', padx=3, pady=3)
            self._buttons[key] = (btn, icon)

        self._refresh()

    def get(self) -> Optional[str]:
        return self._active

    def set(self, key: str):
        """В отличие от CTkTabview.set() всегда зовёт command, даже если key
        совпадает с уже активным — так право-панельный код (app_window.py)
        может безусловно синхронизироваться с текущей вкладкой, не заботясь
        о том, реально ли она поменялась."""
        if key not in self._buttons:
            return
        self._active = key
        self._refresh()
        if self._command:
            self._command(key)

    def _refresh(self):
        c = self._colors
        for key, (btn, icon) in self._buttons.items():
            active = key == self._active
            text_color = c['accent_text'] if active else c['text_secondary']
            btn.configure(
                fg_color=c['accent'] if active else 'transparent',
                hover_color=c['accent_hover'] if active else c['bg_hover'],
                text_color=text_color,
                image=get_icon(icon, text_color, 15),
            )
