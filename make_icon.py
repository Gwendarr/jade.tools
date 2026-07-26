# -*- coding: utf-8 -*-
"""Создаёт иконку приложения (logo.ico) для трея и exe-файла.

Знак jade.tools — нефритовый скруглённый квадрат с тёмной сеткой 2×2
(тот же мотив «набор инструментов», что и логотип в веб-интерфейсе)."""
from PIL import Image, ImageDraw

JADE = (33, 201, 138, 255)   # --accent
DARK = (10, 12, 11, 255)     # тёмные ячейки сетки


def make_logo(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Скруглённый нефритовый фон.
    pad = size // 12
    radius = size // 5
    d.rounded_rectangle([pad, pad, size - pad, size - pad],
                        radius=radius, fill=JADE)

    # Сетка 2×2 из тёмных скруглённых квадратов по центру.
    a, b = size * 0.28, size * 0.72
    gap = size * 0.08
    cell = (b - a - gap) / 2
    cr = max(2, int(cell * 0.22))
    for cx in (a, a + cell + gap):
        for cy in (a, a + cell + gap):
            d.rounded_rectangle([cx, cy, cx + cell, cy + cell],
                                radius=cr, fill=DARK)
    return img


if __name__ == "__main__":
    logo = make_logo(256)
    # .ico с несколькими размерами — для трея и окна.
    sizes = [16, 24, 32, 48, 64, 128, 256]
    logo.save("logo.ico", format="ICO", sizes=[(s, s) for s in sizes])
    logo.save("logo.png", format="PNG")
    print("Создано: logo.ico, logo.png")
