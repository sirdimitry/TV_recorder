#!/usr/bin/env python3
# packaging/generate_icon.py
"""Строит packaging/icon.icns с нуля: рисует squircle-иконку приложения
(тот же процедурный Pillow-подход, что и у иконок интерфейса в
utils/icons.py, но с градиентом/тенью — полноценная macOS app icon, а не
плоский монохромный значок), раскладывает по стандартным размерам
.iconset и компилирует через iconutil (входит в macOS, отдельно ставить
не нужно)."""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).resolve().parent
ICONSET_DIR = OUT_DIR / 'icon.iconset'
ICNS_PATH = OUT_DIR / 'icon.icns'


def make_icon(size: int = 1024) -> Image.Image:
    S = size
    canvas = Image.new('RGBA', (S, S), (0, 0, 0, 0))

    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)

    bg = Image.new('RGBA', (S, S), (0, 0, 0, 255))
    bgd = ImageDraw.Draw(bg)
    top_color, bottom_color = (32, 34, 58), (79, 128, 227)
    for y in range(S):
        t = y / S
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        bgd.line([(0, y), (S, y)], fill=(r, g, b, 255))
    bg.putalpha(mask)
    canvas = Image.alpha_composite(canvas, bg)

    gloss = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse([S * 0.10, -S * 0.42, S * 0.90, S * 0.38], fill=(255, 255, 255, 20))
    gloss.putalpha(Image.composite(gloss.split()[3], Image.new('L', (S, S), 0), mask))
    canvas = Image.alpha_composite(canvas, gloss)

    stroke = int(S * 0.055)
    cx, cy = S * 0.485, S * 0.475
    w, h = S * 0.60, S * 0.385
    screen_box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
    glyph_color = (255, 255, 255, 255)

    shadow_layer = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    off = S * 0.016
    sd.rounded_rectangle([screen_box[0] + off, screen_box[1] + off * 1.6,
                           screen_box[2] + off, screen_box[3] + off * 1.6],
                          radius=S * 0.05, fill=(0, 0, 0, 110))
    stand_top_y = screen_box[3]
    stand_bot_y = stand_top_y + S * 0.09
    foot_w = S * 0.15
    sd.rectangle([cx - stroke * 0.5 + off, stand_top_y + off * 1.6,
                  cx + stroke * 0.5 + off, stand_bot_y + off * 1.6], fill=(0, 0, 0, 110))
    sd.rounded_rectangle([cx - foot_w + off, stand_bot_y - stroke * 0.4 + off * 1.6,
                           cx + foot_w + off, stand_bot_y + stroke * 0.5 + off * 1.6],
                          radius=stroke * 0.3, fill=(0, 0, 0, 110))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(S * 0.018))
    canvas = Image.alpha_composite(canvas, shadow_layer)

    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle(screen_box, radius=S * 0.05, outline=glyph_color, width=stroke)
    d.line([cx, stand_top_y, cx, stand_bot_y], fill=glyph_color, width=stroke)
    d.rounded_rectangle([cx - foot_w, stand_bot_y - stroke * 0.4, cx + foot_w, stand_bot_y + stroke * 0.5],
                         radius=stroke * 0.3, fill=glyph_color)

    tri_h = h * 0.50
    tri_w = tri_h * 0.92
    tx = cx - tri_w * 0.32
    d.polygon([(tx, cy - tri_h / 2), (tx, cy + tri_h / 2), (tx + tri_w, cy)], fill=glyph_color)

    rec_r = S * 0.095
    rec_cx = screen_box[2] + rec_r * 0.35
    rec_cy = screen_box[1] - rec_r * 0.15
    ring_pad = S * 0.02
    d.ellipse([rec_cx - rec_r - ring_pad, rec_cy - rec_r - ring_pad,
               rec_cx + rec_r + ring_pad, rec_cy + rec_r + ring_pad], fill=(24, 25, 40, 255))
    d.ellipse([rec_cx - rec_r, rec_cy - rec_r, rec_cx + rec_r, rec_cy + rec_r], fill=(240, 90, 120, 255))

    return canvas


def main():
    if sys.platform != 'darwin':
        print('generate_icon.py требует macOS (iconutil)', file=sys.stderr)
        sys.exit(1)

    master = make_icon(1024)

    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir()

    for s in (16, 32, 128, 256, 512):
        master.resize((s, s), Image.LANCZOS).save(ICONSET_DIR / f'icon_{s}x{s}.png')
        master.resize((s * 2, s * 2), Image.LANCZOS).save(ICONSET_DIR / f'icon_{s}x{s}@2x.png')

    subprocess.run(['iconutil', '-c', 'icns', str(ICONSET_DIR), '-o', str(ICNS_PATH)], check=True)
    shutil.rmtree(ICONSET_DIR)
    print(f'Иконка собрана: {ICNS_PATH}')


if __name__ == '__main__':
    main()
