# -*- coding: utf-8 -*-
"""生成 OpinionLens 应用图标（深海青品牌）。

用法（仓库根目录）:
    & MediaCrawler\.venv\Scripts\python.exe app\scripts\make_app_icons.py

产物:
    app/desktop/renderer/assets/app-icon-1024.png
    app/desktop/renderer/assets/app-icon.ico  (16/24/32/48/64/128/256)
"""
import io
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # app/
ASSETS = os.path.join(ROOT, 'desktop', 'renderer', 'assets')
PNG_OUT = os.path.join(ASSETS, 'app-icon-1024.png')
ICO_OUT = os.path.join(ASSETS, 'app-icon.ico')

S = 4096          # 超采样画布
K = S / 1024.0    # 1024 设计坐标 → 画布坐标

BG_TOP = (10, 35, 37)       # #0a2325
BG_BOTTOM = (6, 20, 20)     # #061414
INK_LIGHT = (238, 246, 243) # #eef6f3
ACCENT = (20, 184, 166)     # #14b8a6
ACCENT_SOFT = (20, 184, 166, 150)


def rounded_mask(size, radius):
    mask = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def gradient(size, top, bottom):
    img = Image.new('RGB', (size, size), top)
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        d.line([(0, y), (size, y)], fill=(
            round(top[0] + (bottom[0] - top[0]) * t),
            round(top[1] + (bottom[1] - top[1]) * t),
            round(top[2] + (bottom[2] - top[2]) * t),
        ))
    return img


def draw_mark(img):
    d = ImageDraw.Draw(img)
    cx, cy = 428 * K, 512 * K
    # 透镜外环
    r = 246 * K
    w = 62 * K
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=INK_LIGHT, width=int(w))
    # 中心观察点
    ir = 78 * K
    d.ellipse([cx - ir, cy - ir, cx + ir, cy + ir], fill=ACCENT)
    # 观察波纹（两道同心弧，朝右）
    for radius, width, color in ((340 * K, 54 * K, ACCENT), (428 * K, 46 * K, ACCENT_SOFT)):
        box = [cx - radius, cy - radius, cx + radius, cy + radius]
        d.arc(box, start=-42, end=42, fill=color, width=int(width))
        # 圆弧端头补圆，接近圆角笔触
        import math
        for ang in (-42, 42):
            a = math.radians(ang)
            ex, ey = cx + radius * math.cos(a), cy + radius * math.sin(a)
            rr = width / 2.0
            d.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=color)
    return img


def main():
    canvas = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    bg = gradient(S, BG_TOP, BG_BOTTOM).convert('RGBA')
    bg.putalpha(rounded_mask(S, int(224 * K)))
    canvas.alpha_composite(bg)
    draw_mark(canvas)
    icon = canvas.resize((1024, 1024), Image.LANCZOS)
    icon.save(PNG_OUT)
    icon.save(ICO_OUT, format='ICO',
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print('written:', PNG_OUT)
    print('written:', ICO_OUT)


if __name__ == '__main__':
    main()
