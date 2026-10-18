"""Генерирует иконку приложения build-assets/icon.ico.

Мотив — круглая кнопка запуска из интерфейса: тёмный графитовый квадрат,
светлое кольцо и треугольник play. Монохром, как и весь интерфейс.

Запуск:  python tools/make-icon.py      (нужен Pillow)
"""
import os

from PIL import Image, ImageDraw

S = 1024                                   # рисуем крупно, потом уменьшаем — чтобы края были гладкие
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build-assets", "icon.ico")

BG = (20, 22, 26, 255)
EDGE = (48, 53, 63, 255)
LIGHT = (226, 228, 232, 255)
HOLE = (10, 11, 13, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22),
                    fill=BG, outline=EDGE, width=int(S * 0.012))

cx = cy = S // 2
rad, w = int(S * 0.30), int(S * 0.045)
d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=LIGHT, width=w)

ir = rad - w - int(S * 0.035)
d.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=HOLE)

t = int(S * 0.105)
d.polygon([(cx - t * 0.55, cy - t), (cx - t * 0.55, cy + t), (cx + t * 0.95, cy)], fill=LIGHT)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save(
    OUT, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("готово:", os.path.normpath(OUT))
