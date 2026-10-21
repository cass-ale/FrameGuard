"""The FrameGuard mark, drawn from vector geometry at whatever size is asked.

The Monogram is an F built from the top and left edges of a frame. Its
crossbar leaves the stem and turns into a tick in one stroke, so the letter
and the verdict are the same gesture.

Nothing here reads image files. The geometry is defined in a 100-unit glyph
space, so every surface is rendered crisp at its own pixel size: the app icon
(Dock, taskbar, title bar), the in-app mark (sidebar, splash) and the macOS
menu-bar template. The theme decides the colours. `tools_dev/make_brand_assets.py`
uses the same functions to regenerate the shipped .icns, .ico, PNG and SVG
files, so the static icons and the running app cannot drift apart.

Two optical masters:
  * regular: for anything drawn larger than about 48 px.
  * small: heavier strokes, a shorter top arm and a wider tick, so the
    menu bar, tray and 16-32 px icons stay sharp instead of turning to mush.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QGuiApplication, QIcon, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap)

# --- Geometry (100-unit glyph space) ---------------------------------------

Point = Tuple[float, float]

# (stem path points, stem width, tick points, tick width) per optical master.
# The stem's corner is a quadratic curve; see _stem_path.
_REGULAR = dict(arm_end=76.0, stem_width=10.0,
                tick=((24.0, 50.0), (42.0, 50.0), (55.0, 63.0), (83.0, 34.0)), tick_width=11.0)
_SMALL = dict(arm_end=70.0, stem_width=13.0,
              tick=((25.0, 50.0), (40.0, 50.0), (54.0, 64.5), (84.0, 37.0)), tick_width=14.0)

# Glyphs drawn at or below this many pixels use the small optical master.
SMALL_MASTER_MAX_PX = 48

# Apple's macOS app-icon grid: 1024 canvas, 824 body, continuous corners.
# Icons that follow it are shown full size; ones that do not (like the old
# shield) are shrunk onto a grey tile by macOS 26 and later.
MAC_GRID = 1024.0
MAC_BODY = 824.0
MAC_RADIUS = 185.4
# Share of the canvas the glyph's 100-unit box occupies on each grid.
_MAC_GLYPH = 600.0 / MAC_GRID
# Windows and Linux icons have no system margin, so the plate fills the canvas.
_FULL_BODY = 0.94
_FULL_RADIUS = 0.22
_FULL_GLYPH = 0.70


def _stroke(path: QPainterPath, width: float) -> QPainterPath:
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.RoundCap)
    stroker.setJoinStyle(Qt.RoundJoin)
    return stroker.createStroke(path).simplified()


def _polyline(points: Sequence[Point]) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    return path


def _stem_path(arm_end: float) -> QPainterPath:
    path = QPainterPath(QPointF(arm_end, 15.0))
    path.lineTo(33.0, 15.0)
    path.quadTo(18.0, 15.0, 18.0, 30.0)
    path.lineTo(18.0, 85.0)
    return path


_PATH_CACHE: dict = {}


def glyph_paths(small: bool = False) -> Tuple[QPainterPath, QPainterPath]:
    """(stem, tick) outlines in the 100-unit box. Paint the tick first."""
    key = bool(small)
    if key not in _PATH_CACHE:
        spec = _SMALL if small else _REGULAR
        stem = _stroke(_stem_path(spec["arm_end"]), spec["stem_width"])
        tick = _stroke(_polyline(spec["tick"]), spec["tick_width"])
        _PATH_CACHE[key] = (stem, tick)
    stem, tick = _PATH_CACHE[key]
    return QPainterPath(stem), QPainterPath(tick)


def silhouette(small: bool = False) -> QPainterPath:
    """The whole mark as one filled outline (for single-colour uses)."""
    stem, tick = glyph_paths(small)
    return stem.united(tick).simplified()


def continuous_rounded_rect(rect: QRectF, radius: float) -> QPainterPath:
    """Rounded rectangle with Apple's continuous (squircle-like) corners.

    Coefficients are the widely published reconstruction of the iOS 7+
    corner curve. The curve starts 1.528 radii from each corner instead of
    one radius, so the corner flows into the edge without a visible kink.
    """
    r = min(radius, rect.width() / 2 / 1.52866483, rect.height() / 2 / 1.52866483)
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    p = QPainterPath(QPointF(left + 1.52866483 * r, top))

    def corner(ox, oy, sx, sy, swap):
        # One corner, expressed in a local frame and mapped by (sx, sy, swap).
        pts = [(1.08849323, 0.0), (0.86840689, 0.0), (0.63149399, 0.07055062),
               (0.37282392, 0.18256297), (0.18256297, 0.37282392), (0.07055062, 0.63149399),
               (0.0, 0.86840701), (0.0, 1.08849299), (0.0, 1.52866483)]

        def m(a, b):
            if swap:
                a, b = b, a
            return QPointF(ox + sx * a * r, oy + sy * b * r)
        for i in range(0, 9, 3):
            p.cubicTo(m(*pts[i]), m(*pts[i + 1]), m(*pts[i + 2]))

    p.lineTo(right - 1.52866483 * r, top)
    corner(right, top, -1, 1, False)
    p.lineTo(right, bottom - 1.52866483 * r)
    corner(right, bottom, -1, -1, True)
    p.lineTo(left + 1.52866483 * r, bottom)
    corner(left, bottom, 1, -1, False)
    p.lineTo(left, top + 1.52866483 * r)
    corner(left, top, 1, 1, True)
    p.closeSubpath()
    return p


# --- Colour ------------------------------------------------------------------

@dataclass(frozen=True)
class IconPalette:
    plate_top: str
    plate_bottom: str
    stem: str
    tick: str
    tick_end: str
    edge: str


#: The brand colourway: porcelain plate, ink stem, electric-blue tick. Used by
#: the shipped bundle icons (what Finder, Launchpad and a closed Dock show).
BRAND = IconPalette(plate_top="#F7F4EE", plate_bottom="#E9E3D8", stem="#151722",
                    tick="#2E6BFF", tick_end="#2E6BFF", edge="#00000024")


def _is_dark(color: str) -> bool:
    return QColor(color).lightness() < 128


def theme_palette(theme) -> IconPalette:
    """Colourway for the running app's icon in `theme`.

    Dark themes get a plate in the theme's own surfaces; light themes a light
    plate. The stem uses the theme's primary text colour and the tick its
    accent, the same pairing the sidebar mark uses, so Dock, taskbar and
    window all read as one product.
    """
    if _is_dark(theme.bg_primary):
        return IconPalette(plate_top=theme.bg_secondary, plate_bottom=theme.bg_primary,
                           stem=theme.text_primary, tick=theme.accent,
                           tick_end=theme.accent_hover or theme.accent, edge="#FFFFFF2E")
    return IconPalette(plate_top=theme.bg_primary, plate_bottom=theme.bg_tertiary,
                       stem=theme.text_primary, tick=theme.accent,
                       tick_end=theme.accent_hover or theme.accent, edge="#0000002A")


def _color(value: str) -> QColor:
    # Accept #RRGGBBAA (CSS order) as well as Qt's #AARRGGBB.
    if isinstance(value, str) and value.startswith("#") and len(value) == 9:
        c = QColor(value[:7])
        c.setAlpha(int(value[7:9], 16))
        return c
    return QColor(value)


# --- Painting ----------------------------------------------------------------

def paint_glyph(painter: QPainter, box: QRectF, stem_color, tick_color,
                small: Optional[bool] = None, tick_end=None) -> None:
    """Draw the two-tone mark into `box` (the glyph's 100-unit square)."""
    if small is None:
        # Device pixels: the painter's own scale times the target's ratio.
        scale = painter.worldTransform().m11() or 1.0
        device = painter.device()
        ratio = device.devicePixelRatioF() if device is not None else 1.0
        small = box.width() * scale * ratio <= SMALL_MASTER_MAX_PX
    stem, tick = glyph_paths(small)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(box.topLeft())
    painter.scale(box.width() / 100.0, box.height() / 100.0)
    painter.setPen(Qt.NoPen)
    if tick_end is not None and str(tick_end) != str(tick_color):
        grad = QLinearGradient(tick.boundingRect().topRight(), tick.boundingRect().bottomLeft())
        grad.setColorAt(0.0, _color(tick_color))
        grad.setColorAt(1.0, _color(tick_end))
        painter.setBrush(QBrush(grad))
    else:
        painter.setBrush(_color(tick_color))
    painter.drawPath(tick)
    painter.setBrush(_color(stem_color))
    painter.drawPath(stem)
    painter.restore()


def _device_ratio() -> float:
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    ratios = [s.devicePixelRatio() for s in QGuiApplication.screens()] or [1.0]
    return max(1.0, max(ratios))


def glyph_pixmap(size: int, stem_color, tick_color, ratio: Optional[float] = None,
                 tick_end=None) -> QPixmap:
    """The two-tone mark as a `size` x `size` (logical px) pixmap.

    Rendered at the highest screen scale factor in use, so a Retina sidebar
    gets real pixels instead of an upscaled bitmap.
    """
    ratio = ratio or _device_ratio()
    px = max(1, int(math.ceil(size * ratio)))
    image = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    paint_glyph(painter, QRectF(0, 0, px, px), stem_color, tick_color,
                small=px <= SMALL_MASTER_MAX_PX, tick_end=tick_end)
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(px / float(size))
    return pixmap


def app_icon_image(px: int, palette: IconPalette = BRAND, grid: str = "mac") -> QImage:
    """One app-icon bitmap: the plate plus the mark, at `px` x `px`.

    grid="mac" follows Apple's 1024/824 template (transparent margin, continuous
    corners). grid="full" fills the canvas for Windows and Linux, where the
    system adds no margin of its own.
    """
    image = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    if grid == "mac":
        body = px * MAC_BODY / MAC_GRID
        plate_rect = QRectF((px - body) / 2, (px - body) / 2, body, body)
        plate = continuous_rounded_rect(plate_rect, px * MAC_RADIUS / MAC_GRID)
        glyph = px * _MAC_GLYPH
    else:
        body = px * _FULL_BODY
        plate_rect = QRectF((px - body) / 2, (px - body) / 2, body, body)
        plate = continuous_rounded_rect(plate_rect, body * _FULL_RADIUS)
        glyph = px * _FULL_GLYPH
    fill = QLinearGradient(plate_rect.topLeft(), plate_rect.bottomRight())
    fill.setColorAt(0.0, _color(palette.plate_top))
    fill.setColorAt(1.0, _color(palette.plate_bottom))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(fill))
    painter.drawPath(plate)
    # A faint top sheen gives the plate depth without a baked drop shadow
    # (macOS draws its own shadow; a second one looks muddy).
    sheen = QLinearGradient(plate_rect.topLeft(), QPointF(plate_rect.left(), plate_rect.center().y()))
    sheen.setColorAt(0.0, QColor(255, 255, 255, 26))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(QBrush(sheen))
    painter.drawPath(plate)
    if px >= 32:
        edge_w = max(1.0, px / 512.0)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(_color(palette.edge), edge_w))
        inset = edge_w / 2
        painter.drawPath(continuous_rounded_rect(
            plate_rect.adjusted(inset, inset, -inset, -inset),
            (px * MAC_RADIUS / MAC_GRID if grid == "mac" else body * _FULL_RADIUS) - inset))
    offset = (px - glyph) / 2
    # Centre the glyph optically on its own ink, not on the 100-unit box.
    small = glyph <= SMALL_MASTER_MAX_PX
    ink = silhouette(small).boundingRect()
    dx = (50.0 - ink.center().x()) * glyph / 100.0
    dy = (50.0 - ink.center().y()) * glyph / 100.0
    paint_glyph(painter, QRectF(offset + dx, offset + dy, glyph, glyph),
                palette.stem, palette.tick, small=small, tick_end=palette.tick_end)
    painter.end()
    return image


MAC_ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)
FULL_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def app_icon(palette: IconPalette = BRAND, grid: str = "mac",
             sizes: Optional[Iterable[int]] = None) -> QIcon:
    """A multi-resolution QIcon, so each consumer picks real pixels."""
    icon = QIcon()
    for px in sizes or (MAC_ICON_SIZES if grid == "mac" else FULL_ICON_SIZES):
        icon.addPixmap(QPixmap.fromImage(app_icon_image(px, palette, grid)))
    return icon


def platform_grid(platform: str) -> str:
    return "mac" if platform == "darwin" else "full"


def menubar_template_image(points: int = 18, scale: int = 2, padding: float = 0.5) -> QImage:
    """Black silhouette that fills the menu-bar slot, for use as a template.

    Fitted to the glyph's own ink (not its 100-unit box), so it is as tall as
    neighbouring status items instead of floating in empty margin.
    """
    px = points * scale
    image = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    shape = silhouette(small=True)
    ink = shape.boundingRect()
    avail = px - 2 * padding * scale
    k = avail / max(ink.width(), ink.height())
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate((px - ink.width() * k) / 2 - ink.left() * k,
                      (px - ink.height() * k) / 2 - ink.top() * k)
    painter.scale(k, k)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0))
    painter.drawPath(shape)
    painter.end()
    return image


def menubar_template_icon() -> QIcon:
    """macOS status-item icon: 18 pt at 1x and 2x, flagged as a mask so
    AppKit renders it as a template (white on dark bars, black on light)."""
    icon = QIcon()
    for scale in (1, 2):
        pixmap = QPixmap.fromImage(menubar_template_image(18, scale))
        pixmap.setDevicePixelRatio(scale)
        icon.addPixmap(pixmap)
    icon.setIsMask(True)
    return icon


def svg_path_data(path: QPainterPath) -> str:
    """Serialise an outline (lines and cubics) as SVG path data."""
    out = []
    i, n = 0, path.elementCount()
    while i < n:
        e = path.elementAt(i)
        if e.type == QPainterPath.ElementType.MoveToElement:
            if out:
                out.append("Z")
            out.append(f"M{e.x:.2f} {e.y:.2f}")
            i += 1
        elif e.type == QPainterPath.ElementType.LineToElement:
            out.append(f"L{e.x:.2f} {e.y:.2f}")
            i += 1
        elif e.type == QPainterPath.ElementType.CurveToElement:
            c2, end = path.elementAt(i + 1), path.elementAt(i + 2)
            out.append(f"C{e.x:.2f} {e.y:.2f} {c2.x:.2f} {c2.y:.2f} {end.x:.2f} {end.y:.2f}")
            i += 3
        else:
            i += 1
    if out:
        out.append("Z")
    return " ".join(out)
