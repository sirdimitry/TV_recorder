#!/usr/bin/env python3
"""Сдержанный фон окна установки Finder (размер окна 600 × 380 pt)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT = Path(__file__).with_name('dmg_background.png')
SCALE = 1
WIDTH, HEIGHT = 600, 390


def font(size: int) -> ImageFont.FreeTypeFont:
    path = '/System/Library/Fonts/SFNS.ttf'
    return ImageFont.truetype(path, size * SCALE)


def main() -> None:
    image = Image.new('RGB', (WIDTH * SCALE, HEIGHT * SCALE))
    pixels = image.load()
    for y in range(HEIGHT * SCALE):
        t = y / (HEIGHT * SCALE)
        for x in range(WIDTH * SCALE):
            glow = max(0, 1 - ((x / SCALE - 300) / 420) ** 2 - ((y / SCALE + 80) / 360) ** 2)
            pixels[x, y] = (int(24 + 7 * t + 5 * glow),
                            int(28 + 8 * t + 9 * glow),
                            int(37 + 10 * t + 17 * glow))

    d = ImageDraw.Draw(image)
    def box(coords):
        return tuple(round(value * SCALE) for value in coords)

    # Тихий заголовок и знак воспроизведения, вторящий логотипу приложения.
    d.rounded_rectangle(box((30, 27, 65, 62)), radius=10 * SCALE,
                        fill=(46, 61, 81), outline=(83, 115, 153), width=2 * SCALE)
    d.rounded_rectangle(box((38, 37, 57, 51)), radius=2 * SCALE,
                        outline=(217, 231, 246), width=2 * SCALE)
    d.polygon([box((45, 40))[:2], box((45, 48))[:2], box((51, 44))[:2]],
              fill=(217, 231, 246))
    d.text((78 * SCALE, 26 * SCALE), 'TV Recorder', font=font(22), fill=(240, 245, 250))
    d.text((79 * SCALE, 55 * SCALE), 'Установка приложения', font=font(12), fill=(153, 169, 188))
    d.line(box((30, 85, 570, 85)), fill=(64, 76, 92), width=SCALE)

    # Нативные значки Finder располагаются по обе стороны стрелки.
    d.rounded_rectangle(box((271, 186, 329, 220)), radius=17 * SCALE,
                        fill=(49, 66, 86), outline=(82, 113, 148), width=SCALE)
    d.line(box((286, 203, 313, 203)), fill=(193, 217, 241), width=3 * SCALE)
    d.line(box((305, 195, 313, 203)), fill=(193, 217, 241), width=3 * SCALE)
    d.line(box((305, 211, 313, 203)), fill=(193, 217, 241), width=3 * SCALE)
    for left, right in ((90, 210), (390, 510)):
        d.rounded_rectangle(box((left, 242, right, 268)), radius=9 * SCALE,
                            fill=(211, 222, 234))
    d.line(box((30, 324, 570, 324)), fill=(64, 76, 92), width=SCALE)
    message = 'Перетащите TV Recorder в папку Applications'
    length = d.textbbox((0, 0), message, font=font(14))[2]
    d.text(((WIDTH * SCALE - length) / 2, 339 * SCALE), message,
           font=font(14), fill=(190, 205, 220))
    image.save(OUT, optimize=True)
    print(f'Фон установщика: {OUT}')


if __name__ == '__main__':
    main()
