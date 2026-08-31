# utils/icons.py
"""Минималистичные плоские иконки, отрисованные на лету через Pillow.

Заменяют эмодзи в интерфейсе на аккуратные векторные значки одного стиля.
Рисуются с суперсэмплингом (в 4 раза крупнее) и уменьшаются с LANCZOS —
это даёт сглаженные края даже на маленьких размерах кнопок.
"""
import math
from typing import Dict, Tuple

from PIL import Image, ImageDraw
import customtkinter as ctk

_CACHE: Dict[Tuple[str, str, int], "ctk.CTkImage"] = {}
_SCALE = 4


def _draw_icon(name: str, color: str, size: int) -> Image.Image:
    px = size * _SCALE
    img = Image.new('RGBA', (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = max(2, round(px * 0.09))

    if name == 'record':
        d.ellipse([px*0.16, px*0.16, px*0.84, px*0.84], fill=color)

    elif name == 'edit':
        d.line([px*0.22, px*0.78, px*0.62, px*0.38], fill=color, width=stroke)
        d.polygon([(px*0.62, px*0.38), (px*0.74, px*0.26), (px*0.82, px*0.34),
                   (px*0.70, px*0.46)], fill=color)
        d.ellipse([px*0.17, px*0.73, px*0.27, px*0.83], fill=color)

    elif name == 'trash':
        d.rounded_rectangle([px*0.28, px*0.34, px*0.72, px*0.84], radius=px*0.05,
                             outline=color, width=stroke)
        d.line([px*0.20, px*0.34, px*0.80, px*0.34], fill=color, width=stroke)
        d.line([px*0.42, px*0.20, px*0.58, px*0.20], fill=color, width=stroke)

    elif name == 'plus':
        d.line([px*0.5, px*0.18, px*0.5, px*0.82], fill=color, width=stroke)
        d.line([px*0.18, px*0.5, px*0.82, px*0.5], fill=color, width=stroke)

    elif name == 'refresh':
        d.arc([px*0.16, px*0.16, px*0.84, px*0.84], start=-40, end=230, fill=color, width=stroke)
        d.polygon([(px*0.80, px*0.12), (px*0.94, px*0.30), (px*0.72, px*0.32)], fill=color)

    elif name == 'pause':
        bw = px*0.16
        d.rounded_rectangle([px*0.26, px*0.20, px*0.26+bw, px*0.80], radius=bw*0.3, fill=color)
        d.rounded_rectangle([px*0.58, px*0.20, px*0.58+bw, px*0.80], radius=bw*0.3, fill=color)

    elif name == 'play':
        d.polygon([(px*0.28, px*0.18), (px*0.28, px*0.82), (px*0.82, px*0.5)], fill=color)

    elif name == 'stop':
        d.rounded_rectangle([px*0.26, px*0.26, px*0.74, px*0.74], radius=px*0.06, fill=color)

    elif name == 'close':
        d.line([px*0.26, px*0.26, px*0.74, px*0.74], fill=color, width=stroke)
        d.line([px*0.74, px*0.26, px*0.26, px*0.74], fill=color, width=stroke)

    elif name == 'download':
        d.line([px*0.5, px*0.14, px*0.5, px*0.60], fill=color, width=stroke)
        d.polygon([(px*0.30, px*0.44), (px*0.70, px*0.44), (px*0.5, px*0.68)], fill=color)
        d.line([px*0.18, px*0.82, px*0.82, px*0.82], fill=color, width=stroke)

    elif name == 'folder':
        d.polygon([(px*0.14, px*0.30), (px*0.42, px*0.30), (px*0.48, px*0.38),
                    (px*0.86, px*0.38), (px*0.86, px*0.80), (px*0.14, px*0.80)],
                   outline=color, width=stroke)

    elif name == 'tv':
        d.rounded_rectangle([px*0.12, px*0.22, px*0.88, px*0.68], radius=px*0.07,
                             outline=color, width=stroke)
        d.line([px*0.5, px*0.68, px*0.5, px*0.80], fill=color, width=stroke)
        d.line([px*0.32, px*0.80, px*0.68, px*0.80], fill=color, width=stroke)

    elif name == 'link':
        d.rounded_rectangle([px*0.14, px*0.30, px*0.62, px*0.50], radius=px*0.10,
                             outline=color, width=stroke)
        d.rounded_rectangle([px*0.38, px*0.44, px*0.86, px*0.64], radius=px*0.10,
                             outline=color, width=stroke)

    elif name == 'globe':
        d.ellipse([px*0.14, px*0.14, px*0.86, px*0.86], outline=color, width=stroke)
        d.ellipse([px*0.14, px*0.30, px*0.86, px*0.70], outline=color, width=stroke)
        d.line([px*0.5, px*0.14, px*0.5, px*0.86], fill=color, width=stroke)

    elif name == 'chevron_down':
        d.line([px*0.22, px*0.38, px*0.5, px*0.66], fill=color, width=stroke)
        d.line([px*0.5, px*0.66, px*0.78, px*0.38], fill=color, width=stroke)

    elif name == 'wifi':
        cx = px*0.5
        for i, r in enumerate((0.30, 0.20, 0.10)):
            bbox = [cx - px*r, px*0.75 - px*r, cx + px*r, px*0.75 + px*r]
            d.arc(bbox, start=225, end=315, fill=color, width=stroke)
        d.ellipse([cx-px*0.04, px*0.75-px*0.04, cx+px*0.04, px*0.75+px*0.04], fill=color)

    elif name == 'signal_off':
        cx = px*0.5
        for i, r in enumerate((0.30, 0.20, 0.10)):
            bbox = [cx - px*r, px*0.75 - px*r, cx + px*r, px*0.75 + px*r]
            d.arc(bbox, start=225, end=315, fill=color, width=stroke)
        d.ellipse([cx-px*0.04, px*0.75-px*0.04, cx+px*0.04, px*0.75+px*0.04], fill=color)
        d.line([px*0.16, px*0.16, px*0.84, px*0.84], fill=color, width=stroke)

    elif name == 'grid':
        gap = px * 0.08
        cell = (px * 0.84 - gap) / 2
        r = px * 0.05
        for row in range(2):
            for col in range(2):
                x0 = px * 0.08 + col * (cell + gap)
                y0 = px * 0.08 + row * (cell + gap)
                d.rounded_rectangle([x0, y0, x0 + cell, y0 + cell], radius=r, fill=color)

    elif name == 'bolt':
        d.polygon([(px*0.56, px*0.12), (px*0.24, px*0.56), (px*0.46, px*0.56),
                    (px*0.40, px*0.88), (px*0.78, px*0.40), (px*0.54, px*0.40)], fill=color)

    elif name == 'calendar':
        d.rounded_rectangle([px*0.14, px*0.22, px*0.86, px*0.84], radius=px*0.06,
                             outline=color, width=stroke)
        d.line([px*0.14, px*0.40, px*0.86, px*0.40], fill=color, width=stroke)
        d.line([px*0.32, px*0.14, px*0.32, px*0.28], fill=color, width=stroke)
        d.line([px*0.68, px*0.14, px*0.68, px*0.28], fill=color, width=stroke)

    elif name == 'volume':
        d.polygon([(px*0.14, px*0.38), (px*0.34, px*0.38), (px*0.52, px*0.20),
                   (px*0.52, px*0.80), (px*0.34, px*0.62), (px*0.14, px*0.62)], fill=color)
        d.arc([px*0.56, px*0.30, px*0.78, px*0.70], start=-50, end=50, fill=color, width=stroke)
        d.arc([px*0.62, px*0.20, px*0.92, px*0.80], start=-50, end=50, fill=color, width=stroke)

    elif name == 'volume_off':
        d.polygon([(px*0.14, px*0.38), (px*0.34, px*0.38), (px*0.52, px*0.20),
                   (px*0.52, px*0.80), (px*0.34, px*0.62), (px*0.14, px*0.62)], fill=color)
        d.line([px*0.60, px*0.32, px*0.88, px*0.68], fill=color, width=stroke)
        d.line([px*0.88, px*0.32, px*0.60, px*0.68], fill=color, width=stroke)

    elif name == 'shield':
        d.polygon([(px*0.5, px*0.14), (px*0.82, px*0.26), (px*0.82, px*0.52),
                    (px*0.5, px*0.86), (px*0.18, px*0.52), (px*0.18, px*0.26)],
                   outline=color, width=stroke)

    return img.resize((size, size), Image.LANCZOS)


def get_icon(name: str, color: str, size: int = 16) -> "ctk.CTkImage":
    """Возвращает закэшированную CTkImage с иконкой заданного цвета и размера."""
    key = (name, color, size)
    cached = _CACHE.get(key)
    if cached is None:
        pil_img = _draw_icon(name, color, size)
        cached = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(size, size))
        _CACHE[key] = cached
    return cached
