"""Vector icons drawn with QPainter.

WHY NOT FONT GLYPHS
    The sidebar previously used Unicode characters: U+25B6 (a play triangle)
    for Scan, U+2611 (ballot box) for Results, U+25A6 (a crosshatch texture)
    for Checks, U+1F4C2 (a full-colour emoji folder) for Watch, and U+2699 for
    Settings. That mixes four monochrome geometric shapes with one colour
    emoji, so stroke weight, optical size and colour were all inconsistent -
    and the exact rendering depended on whichever font the OS substituted,
    which differs between Windows and macOS.

    These are drawn instead. They inherit the theme colour, stay crisp at any
    DPI, share a single stroke weight, and render identically everywhere.

CONVENTION
    24x24 design grid, 2px stroke, round caps and joins - the same geometry
    used by Lucide/Feather, so the set reads as a coherent family.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

GRID = 24.0          # design grid the paths are authored against
STROKE = 2.0         # stroke width on that grid

_cache = {}


# --- path builders ----------------------------------------------------------
# Each returns (stroked_paths, filled_paths) in 24x24 coordinates.

def _scan():
    """Viewfinder corners with a scan line - reads as 'inspect this frame'."""
    p = QPainterPath()
    # top-left corner
    p.moveTo(3, 8); p.lineTo(3, 5); p.lineTo(6, 5)
    # top-right
    p.moveTo(18, 5); p.lineTo(21, 5); p.lineTo(21, 8)
    # bottom-right
    p.moveTo(21, 16); p.lineTo(21, 19); p.lineTo(18, 19)
    # bottom-left
    p.moveTo(6, 19); p.lineTo(3, 19); p.lineTo(3, 16)
    # the scan line itself
    p.moveTo(6, 12); p.lineTo(18, 12)
    return [p], []


def _results():
    """A list whose first row is ticked - a set of per-file verdicts."""
    stroke = QPainterPath()
    stroke.moveTo(3.5, 6.5); stroke.lineTo(5, 8); stroke.lineTo(8, 5)      # tick
    stroke.moveTo(3.5, 13); stroke.lineTo(8, 13)                            # row 2 mark
    stroke.moveTo(3.5, 18.5); stroke.lineTo(8, 18.5)                        # row 3 mark
    stroke.moveTo(11, 6.5); stroke.lineTo(20.5, 6.5)
    stroke.moveTo(11, 13); stroke.lineTo(20.5, 13)
    stroke.moveTo(11, 18.5); stroke.lineTo(20.5, 18.5)
    return [stroke], []


def _checks():
    """A 2x2 matrix - literally what the Checks panel shows."""
    p = QPainterPath()
    for x, y in ((3.5, 3.5), (13.5, 3.5), (3.5, 13.5), (13.5, 13.5)):
        p.addRoundedRect(QRectF(x, y, 7, 7), 1.5, 1.5)
    return [p], []


def _watch():
    """An eye - monitoring, rather than the folder being monitored."""
    outline = QPainterPath()
    outline.moveTo(2.5, 12)
    outline.cubicTo(6, 5.5, 18, 5.5, 21.5, 12)
    outline.cubicTo(18, 18.5, 6, 18.5, 2.5, 12)
    pupil = QPainterPath()
    pupil.addEllipse(QPointF(12, 12), 3.0, 3.0)
    return [outline, pupil], []


def _settings():
    """Sliders - the panel is a set of values, not a machine."""
    p = QPainterPath()
    for y in (6.5, 12, 17.5):
        p.moveTo(3.5, y); p.lineTo(20.5, y)
    knobs = QPainterPath()
    for x, y in ((8, 6.5), (15, 12), (10, 17.5)):
        knobs.addEllipse(QPointF(x, y), 2.4, 2.4)
    return [p, knobs], []


def _import():
    """Arrow entering a tray - importing a preset."""
    p = QPainterPath()
    p.moveTo(12, 3.5); p.lineTo(12, 14)          # shaft
    p.moveTo(8, 10); p.lineTo(12, 14); p.lineTo(16, 10)   # head
    p.moveTo(4, 17); p.lineTo(4, 20); p.lineTo(20, 20); p.lineTo(20, 17)  # tray
    return [p], []


def _folder():
    """A closed folder with a tab - the drop target's resting state.

    Replaces U+1F4C1, a full-colour emoji that ignored the theme entirely and
    rendered differently on every OS depending on the substituted font.
    """
    p = QPainterPath()
    p.moveTo(3, 8)
    p.lineTo(3, 5.5)
    p.lineTo(9.2, 5.5)
    p.lineTo(11.2, 8)
    p.lineTo(21, 8)
    p.lineTo(21, 19)
    p.lineTo(3, 19)
    p.closeSubpath()
    return [p], []


def _folder_open():
    """An open folder - browsing for, rather than holding, files."""
    back = QPainterPath()
    back.moveTo(3, 18.5)
    back.lineTo(3, 5.5)
    back.lineTo(9.2, 5.5)
    back.lineTo(11.2, 8)
    back.lineTo(19, 8)
    back.lineTo(19, 10.5)

    front = QPainterPath()
    front.moveTo(3, 19)
    front.lineTo(6.3, 11.5)
    front.lineTo(22, 11.5)
    front.lineTo(18.7, 19)
    front.closeSubpath()
    return [back, front], []


def _drop_files():
    """A file dropping into an open folder - the empty drop zone.

    Larger and more literal than the plain folder because it is the hero
    graphic of an otherwise empty panel, not a 20px row marker.
    """
    arrow = QPainterPath()
    arrow.moveTo(12, 2.5); arrow.lineTo(12, 10.5)
    arrow.moveTo(8.5, 7); arrow.lineTo(12, 10.5); arrow.lineTo(15.5, 7)

    tray = QPainterPath()
    tray.moveTo(3, 13.5)
    tray.lineTo(3, 20.5)
    tray.lineTo(21, 20.5)
    tray.lineTo(21, 13.5)
    return [arrow, tray], []


def _chevron_left():
    """Collapse the sidebar."""
    p = QPainterPath()
    p.moveTo(14.5, 6); p.lineTo(8.5, 12); p.lineTo(14.5, 18)
    return [p], []


def _chevron_right():
    """Expand the sidebar."""
    p = QPainterPath()
    p.moveTo(9.5, 6); p.lineTo(15.5, 12); p.lineTo(9.5, 18)
    return [p], []


def _reset():
    """Counter-clockwise arrow - restore a setting to its default.

    Replaces U+21BA, which rendered at roughly half the size of the other
    controls and in whatever weight the substituted font provided.
    """
    arc = QPainterPath()
    arc.arcMoveTo(QRectF(4.5, 4.5, 15, 15), 140)
    arc.arcTo(QRectF(4.5, 4.5, 15, 15), 140, -290)
    head = QPainterPath()
    head.moveTo(4.6, 4.8); head.lineTo(5.6, 9.7); head.lineTo(10.4, 8.4)
    return [arc, head], []


def _info():
    """Circled 'i' - inline hints (replaces the U+1F6C8 emoji, which macOS
    fonts lack and drew as a placeholder box)."""
    ring = QPainterPath()
    ring.addEllipse(QPointF(12, 12), 8.5, 8.5)
    stem = QPainterPath()
    stem.moveTo(12, 11); stem.lineTo(12, 16.5)
    dot = QPainterPath()
    dot.addEllipse(QPointF(12, 7.6), 1.25, 1.25)
    return [ring, stem], [dot]


BUILDERS = {
    "scan": _scan,
    "results": _results,
    "checks": _checks,
    "watch": _watch,
    "settings": _settings,
    "import": _import,
    "folder": _folder,
    "folder_open": _folder_open,
    "drop_files": _drop_files,
    "chevron_left": _chevron_left,
    "chevron_right": _chevron_right,
    "reset": _reset,
    "info": _info,
}


def icon_pixmap(name: str, size: int, color: str, dpr: float = 1.0) -> QPixmap:
    """Render `name` at `size` px in `color`."""
    builder = BUILDERS.get(name)
    if builder is None:
        return QPixmap()

    key = (name, size, color, round(dpr, 2))
    cached = _cache.get(key)
    if cached is not None:
        return cached

    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    scale = size / GRID
    painter.scale(scale, scale)

    pen = QPen(QColor(color))
    pen.setWidthF(STROKE)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    stroked, filled = builder()
    for path in stroked:
        painter.drawPath(path)
    if filled:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        for path in filled:
            painter.drawPath(path)
    painter.end()

    _cache[key] = pm
    return pm


def make_icon(name: str, size: int, color: str, dpr: float = 1.0) -> QIcon:
    return QIcon(icon_pixmap(name, size, color, dpr))


def clear_cache():
    """Call on theme change so icons are re-rendered in the new colour."""
    _cache.clear()
