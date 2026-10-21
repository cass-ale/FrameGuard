#!/usr/bin/env python3
"""Render the README banner (dark and light) and the GitHub social preview.

    python tools_dev/make_readme_banner.py

Writes assets/brand/banner-dark.png, banner-light.png and social-preview.png
at the repository root, drawing the mark with frameguard/brand.py and the
wordmark in Instrument Serif (branding/fonts, SIL Open Font License). Set the
social preview by hand under the repository's Settings > General > Social
preview.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QLinearGradient,  # noqa: E402
                           QPainter, QPen, QRadialGradient, QBrush)
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])
from frameguard import brand  # noqa: E402

FONTS = SRC / "branding" / "fonts"
for f in sorted(FONTS.glob("*.ttf")):
    if QFontDatabase.addApplicationFont(str(f)) < 0:
        sys.exit(f"Could not load font {f}")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC.parent / "assets" / "brand"
OUT.mkdir(parents=True, exist_ok=True)

THEMES = {
    "dark": dict(bg0="#0C0E14", bg1="#161A26", ink="#F4F1EA", muted="#9AA3B5", tick="#3D7BFF",
                 tick_end="#6E8CFF", grid=QColor(255, 255, 255, 14), mark=QColor(255, 255, 255, 34),
                 glow=QColor(61, 123, 255, 40)),
    "light": dict(bg0="#FAF8F3", bg1="#EFE9DE", ink="#151722", muted="#5B6070", tick="#2E6BFF",
                  tick_end="#2E6BFF", grid=QColor(21, 23, 34, 12), mark=QColor(21, 23, 34, 40),
                  glow=QColor(46, 107, 255, 22)),
}
STATUS = {"dark": [("PASS", "#2BB673"), ("WARNING", "#F0A93B"), ("FAIL", "#F2555A")],
          # darker on the light banner so every label clears 4.5:1
          "light": [("PASS", "#13703F"), ("WARNING", "#8F5400"), ("FAIL", "#B4232A")]}


def font(family, px, weight=400, italic=False, spacing=0.0):
    f = QFont(family)
    f.setPixelSize(int(px))
    f.setWeight(QFont.Weight(weight))
    try:
        f.setVariableAxis(QFont.Tag("wght"), float(weight))
    except Exception:
        pass
    f.setItalic(italic)
    if spacing:
        f.setLetterSpacing(QFont.PercentageSpacing, 100 + spacing)
    return f


def crop_marks(p, rect, arm, width, color):
    pen = QPen(color, width)
    pen.setCapStyle(Qt.FlatCap)
    p.setPen(pen)
    l, t, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
    for (x, y, dx, dy) in ((l, t, 1, 1), (r, t, -1, 1), (l, b, 1, -1), (r, b, -1, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + dx * arm, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + dy * arm))


def pill(p, x, y, text, color, h, f):
    p.setFont(f)
    fm = QFontMetricsF(f)
    w = fm.horizontalAdvance(text) + h * 0.9
    c = QColor(color)
    fill = QColor(c)
    fill.setAlpha(36)
    p.setPen(QPen(c, max(1.5, h / 22)))
    p.setBrush(fill)
    p.drawRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
    p.setPen(c)
    p.drawText(QRectF(x, y, w, h), Qt.AlignCenter, text)
    return w


def render(kind, W, H, scale, show_pills=True, tagline=True):
    t = THEMES[kind]
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    g = QLinearGradient(0, 0, W, H)
    g.setColorAt(0, QColor(t["bg0"]))
    g.setColorAt(1, QColor(t["bg1"]))
    p.fillRect(img.rect(), g)
    # faint safe-area grid
    p.setPen(QPen(t["grid"], 1 * scale))
    step = 60 * scale
    x = step
    while x < W:
        p.drawLine(QPointF(x, 0), QPointF(x, H))
        x += step
    y = step
    while y < H:
        p.drawLine(QPointF(0, y), QPointF(W, y))
        y += step
    # glow behind the mark
    gx, gy = W * 0.22, H * 0.5
    rg = QRadialGradient(QPointF(gx, gy), H * 0.6)
    rg.setColorAt(0, t["glow"])
    rg.setColorAt(1, QColor(0, 0, 0, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(rg))
    p.drawEllipse(QPointF(gx, gy), H * 0.6, H * 0.6)
    # broadcast crop marks around the composition
    m = 36 * scale
    crop_marks(p, QRectF(m, m, W - 2 * m, H - 2 * m), 34 * scale, 2.2 * scale, t["mark"])

    glyph = H * 0.46
    gx0 = W * 0.08
    gy0 = (H - glyph) / 2
    brand.paint_glyph(p, QRectF(gx0, gy0, glyph, glyph), t["ink"], t["tick"], small=False,
                      tick_end=t["tick_end"])
    # wordmark: Frame (roman) + Guard (italic), Instrument Serif
    wx = gx0 + glyph + 46 * scale
    size = H * 0.30
    roman = font("Instrument Serif", size)
    italic = font("Instrument Serif", size, italic=True)
    fm = QFontMetricsF(roman)
    base = H * 0.5 + (fm.capHeight() / 2) - (H * 0.05 if tagline else 0)
    p.setPen(QColor(t["ink"]))
    p.setFont(roman)
    p.drawText(QPointF(wx, base), "Frame")
    adv = fm.horizontalAdvance("Frame") + size * 0.02
    p.setFont(italic)
    p.drawText(QPointF(wx + adv, base), "Guard")
    if tagline:
        tf = font("Instrument Sans", H * 0.062, 500, spacing=2)
        p.setFont(tf)
        p.setPen(QColor(t["muted"]))
        p.drawText(QPointF(wx + size * 0.03, base + H * 0.135), "Video QA for XR deliverables  \u00b7  macOS and Windows")
    if show_pills:
        pf = font("Instrument Sans", H * 0.040, 650, spacing=6)
        ph = H * 0.085
        px = wx + size * 0.03
        py = base + H * 0.20
        for text, color in STATUS[kind]:
            px += pill(p, px, py, text, color, ph, pf) + ph * 0.35
    p.end()
    return img


if __name__ == "__main__":
    for kind in ("dark", "light"):
        render(kind, 2400, 720, 2.0).save(str(OUT / f"banner-{kind}.png"))
    render("dark", 1280, 640, 1.0).save(str(OUT / "social-preview.png"))
    print("ok", sorted(p.name for p in OUT.iterdir()))
