"""FrameGuard design tokens and metric helpers.

Geometry uses logical pixels on an 8-point grid. Typography uses point-sized
``QFont`` instances so layout measurements and rendered text share one source
of truth across platforms and display scales.
"""

import sys


# Spacing: 8-point grid, with 4 px optical exceptions.
SPACE_0 = 0
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24
SPACE_6 = 32
SPACE_7 = 48

# Semantic point-size progression. These values are passed to QFont and emitted
# as ``pt`` by font_css; they are never viewport-scaled.
FONT_CAPTION = 9
FONT_SMALL = 10
FONT_BODY = 11
FONT_SUBHEAD = 13
FONT_TITLE = 16
FONT_DISPLAY = 20
FONT_HERO = 24

WEIGHT_NORMAL = 400
WEIGHT_MEDIUM = 500
WEIGHT_SEMIBOLD = 600
WEIGHT_BOLD = 700

# Reusable typography roles: (point size, weight).
TYPE_CAPTION = (FONT_CAPTION, WEIGHT_NORMAL)
TYPE_BODY = (FONT_BODY, WEIGHT_NORMAL)
TYPE_MUTED = (FONT_SMALL, WEIGHT_NORMAL)
TYPE_SUBHEAD = (FONT_SUBHEAD, WEIGHT_SEMIBOLD)
TYPE_HEADING = (FONT_TITLE, WEIGHT_SEMIBOLD)
TYPE_METRIC = (FONT_DISPLAY, WEIGHT_SEMIBOLD)
TYPE_STATUS = (FONT_SMALL, WEIGHT_SEMIBOLD)

# Control scale. These are design minimums; helpers may grow them when realized
# font/style metrics need more room.
CONTROL_SM = 28
CONTROL_MD = 32
CONTROL_LG = 40
CONTROL_XL = 48
ICON_BTN = 32

# Radius scale.
RADIUS_SM = 4
RADIUS_MD = 6
RADIUS_LG = 8
RADIUS_XL = 12
RADIUS_PILL = 999

# Layout tokens.
# One sidebar width at every window size (see page_margins). 168 px is the
# width the narrow layout already proved fits every label.
SIDEBAR_W = 168
SIDEBAR_W_COLLAPSED = 56
ROW_H = 40
SCROLLBAR_HIT_AREA = 14
SPLITTER_HIT_AREA = 8
BP_NARROW = 1100

_UI_FONT_FAMILY = None
_MONO_FONT_FAMILY = None

UI_FONT_CANDIDATES = {
    # Windows 11's system face first (what Settings, Explorer and the new
    # Office use), then the Windows 10 one.
    "win32": (
        "Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI",
        "Selawik", "Tahoma",
    ),
    "darwin": ("SF Pro Text", "Helvetica Neue", "Helvetica"),
    "default": ("Inter", "Roboto", "Cantarell", "DejaVu Sans"),
}

MONO_FONT_CANDIDATES = {
    "win32": ("Cascadia Mono", "Consolas", "Segoe UI Mono", "Courier New"),
    "darwin": ("SF Mono", "Menlo", "Monaco"),
    "default": ("JetBrains Mono", "Roboto Mono", "DejaVu Sans Mono", "monospace"),
}


def page_margins(width: int) -> int:
    """The outer page gutter for a window width: 24 px, easing to 16 px on
    narrow windows.

    0.8-0.9.0 stepped it 16 -> 24 -> 32 px at 1100 / 1400 px and narrowed the
    sidebar 200 -> 168 px at 1100 px, so dragging a window edge across those
    widths made the whole page jump sideways by up to 48 px mid-drag. The
    sidebar is now one width and the gutter changes continuously, by at most
    8 px across the 1000-1200 px band (1 px per 25 px of drag), so content
    stays anchored while windows up to 1000 px keep the narrow layout's room.
    """
    t = min(1.0, max(0.0, (width - 1000) / 200.0))
    return int(round(SPACE_4 + (SPACE_5 - SPACE_4) * t))


def sidebar_width(width: int) -> int:
    """The expanded sidebar width: constant, for the reason page_margins gives."""
    return SIDEBAR_W


def font_css(size: int, weight: int = WEIGHT_NORMAL) -> str:
    """Return point-sized QSS declarations for a semantic type role."""
    return f"font-size: {size}pt; font-weight: {weight};"


def _resolve_family(candidates, fallback_generic):
    from PySide6.QtGui import QFontDatabase

    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
    return QFontDatabase.systemFont(fallback_generic).family()


def ui_font(size: int = FONT_BODY, weight=None):
    """Return a point-sized font in the platform's realized UI family."""
    from PySide6.QtGui import QFont, QFontDatabase

    global _UI_FONT_FAMILY
    if _UI_FONT_FAMILY is None:
        key = sys.platform if sys.platform in UI_FONT_CANDIDATES else "default"
        _UI_FONT_FAMILY = _resolve_family(
            UI_FONT_CANDIDATES[key], QFontDatabase.GeneralFont)
    font = QFont(_UI_FONT_FAMILY, size)
    if weight is not None:
        font.setWeight(QFont.Weight(weight))
    return font


def mono_font(size: int = FONT_SMALL, weight=None):
    """Return a point-sized fixed-width font for tabular/numeric content."""
    from PySide6.QtGui import QFont, QFontDatabase

    global _MONO_FONT_FAMILY
    if _MONO_FONT_FAMILY is None:
        key = sys.platform if sys.platform in MONO_FONT_CANDIDATES else "default"
        _MONO_FONT_FAMILY = _resolve_family(
            MONO_FONT_CANDIDATES[key], QFontDatabase.FixedFont)
    font = QFont(_MONO_FONT_FAMILY, size)
    font.setStyleHint(QFont.Monospace)
    if weight is not None:
        font.setWeight(QFont.Weight(weight))
    return font


def _metrics(font_or_metrics):
    """Normalize a QFont or QFontMetrics-like object to QFontMetrics."""
    from PySide6.QtGui import QFontMetrics

    if hasattr(font_or_metrics, "horizontalAdvance") and hasattr(
            font_or_metrics, "lineSpacing"):
        return font_or_metrics
    return QFontMetrics(font_or_metrics)


def _grid_ceil(value: int, step: int = SPACE_1) -> int:
    return ((int(value) + step - 1) // step) * step


def line_height(font_or_metrics) -> int:
    """Return the realized single-line height in logical pixels."""
    return _metrics(font_or_metrics).lineSpacing()


def label_height(font_or_metrics, lines: int = 1,
                 vertical_padding: int = SPACE_0) -> int:
    """Return a grid-aligned height for one or more lines of text."""
    if lines < 1:
        raise ValueError("lines must be at least 1")
    metrics = _metrics(font_or_metrics)
    height = metrics.height() + (lines - 1) * metrics.lineSpacing()
    return _grid_ceil(height + 2 * vertical_padding)


def control_height(font_or_metrics, style=None, minimum: int = CONTROL_MD) -> int:
    """Return a control height derived from realized font and style metrics.

    ``minimum`` is a design token. Font height and ``PM_ButtonMargin`` are
    runtime-derived because platform styles and fallback fonts vary.
    """
    margin = SPACE_2
    if style is not None:
        from PySide6.QtWidgets import QStyle
        margin = max(margin, style.pixelMetric(QStyle.PM_ButtonMargin))
    required = _metrics(font_or_metrics).height() + margin + 2
    return max(minimum, _grid_ceil(required))


def table_row_height(font_or_metrics) -> int:
    """Return a readable, grid-aligned table row height."""
    required = _metrics(font_or_metrics).height() + 2 * SPACE_2
    return max(ROW_H, _grid_ceil(required))


def table_header_height(font_or_metrics, lines: int = 1) -> int:
    """Return a header height that explicitly supports one or two lines."""
    return max(CONTROL_MD, label_height(
        font_or_metrics, lines=lines, vertical_padding=SPACE_2))


def bounded_text_width(text: str, font_or_metrics, minimum: int = 0,
                       maximum: int = 16777215,
                       horizontal_padding: int = SPACE_4) -> int:
    """Measure text with realized metrics, padding it and clamping the result."""
    if minimum < 0 or maximum < minimum:
        raise ValueError("width bounds must satisfy 0 <= minimum <= maximum")
    width = _metrics(font_or_metrics).horizontalAdvance(str(text))
    return max(minimum, min(maximum, width + 2 * horizontal_padding))


def screen_safe_geometry(preferred_size, available_geometry,
                         minimum_size=None, margin: int = SPACE_2):
    """Center and clamp a preferred size within a screen's available geometry.

    Sizes and rectangles are Qt logical-pixel values. The returned ``QRect``
    never extends beyond the available rectangle, even when the requested
    minimum is larger than the screen.
    """
    from PySide6.QtCore import QRect, QSize

    if margin < 0:
        raise ValueError("margin must be non-negative")
    preferred = QSize(preferred_size)
    available = QRect(available_geometry).adjusted(margin, margin, -margin, -margin)
    if available.width() <= 0 or available.height() <= 0:
        available = QRect(available_geometry)
    minimum = QSize(minimum_size) if minimum_size is not None else QSize(0, 0)
    width = min(available.width(), max(minimum.width(), preferred.width()))
    height = min(available.height(), max(minimum.height(), preferred.height()))
    x = available.x() + (available.width() - width) // 2
    y = available.y() + (available.height() - height) // 2
    return QRect(x, y, width, height)
