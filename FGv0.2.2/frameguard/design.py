"""FrameGuard design tokens.

A single source of truth for spacing, type, control sizing and radii.

WHY THIS EXISTS
    An audit of the UI found 10 distinct control heights (21, 24, 30, 31, 34,
    38, 48, 50px), 9 margin values and 7 font sizes, all chosen ad hoc. That is
    what "cluttered" actually looks like when measured: nothing shares an edge,
    nothing shares a rhythm, and every screen reads as slightly wrong without
    the viewer being able to say why.

THE SYSTEM
    8-point grid. Every spacing, padding and control dimension is a multiple of
    8, or of 4 for fine optical adjustment. This is the same basis used by
    Material Design, Apple HIG, IBM Carbon and Atlassian - not because it is
    magic, but because it removes arbitrary decisions and scales cleanly at
    1x/1.5x/2x display scaling.

    Type follows a modular scale at roughly a 1.2 ratio, so sizes relate to one
    another instead of drifting by one or two pixels.

    (The "golden ratio in UI layout" idea is folklore; there is no evidence
    behind it. The 8pt grid and a modular type scale are the actual
    professional standards.)
"""

# --- Spacing: 8pt grid ------------------------------------------------------
SPACE_0 = 0
SPACE_1 = 4     # hairline separation, icon-to-label
SPACE_2 = 8     # tight grouping, related controls
SPACE_3 = 12    # default gap inside a component
SPACE_4 = 16    # gap between components
SPACE_5 = 24    # gap between groups
SPACE_6 = 32    # page gutter (wide windows)
SPACE_7 = 48    # major section break

# --- Type scale (px, ~1.2 modular ratio) -----------------------------------
FONT_CAPTION = 11   # timestamps, helper text
FONT_SMALL = 12     # secondary/meta text
FONT_BODY = 14      # default UI text
FONT_SUBHEAD = 16   # emphasised body, group titles
FONT_TITLE = 20     # panel headings
FONT_DISPLAY = 24   # hero numerals (badge counts)
FONT_HERO = 30      # splash / drop-zone glyph

# Explicit weights, so "bold" is never a per-widget guess.
WEIGHT_NORMAL = 400
WEIGHT_MEDIUM = 500
WEIGHT_SEMIBOLD = 600
WEIGHT_BOLD = 700

# --- Control sizing ---------------------------------------------------------
# Three heights only. A row of controls at 30px next to one at 31px reads as a
# rendering fault even though nobody can name it.
CONTROL_SM = 28     # compact/inline actions
CONTROL_MD = 32     # default buttons, combos, inputs
CONTROL_LG = 40     # primary calls to action
CONTROL_XL = 48     # the single hero action (Start Scan)

ICON_BTN = 32       # square icon-only buttons

# --- Radii ------------------------------------------------------------------
RADIUS_SM = 4       # inputs, table cells
RADIUS_MD = 6       # buttons
RADIUS_LG = 8       # cards, group boxes
RADIUS_XL = 12      # large surfaces, drop zone
RADIUS_PILL = 999   # status pills

# --- Layout -----------------------------------------------------------------
SIDEBAR_W = 200
SIDEBAR_W_NARROW = 168
SIDEBAR_W_COLLAPSED = 56
TITLEBAR_H = 36
ROW_H = 40          # table rows: comfortable without wasting vertical space

# Responsive breakpoints (window width in px)
BP_NARROW = 1100
BP_WIDE = 1400


def page_margins(width: int) -> int:
    """Outer gutter for a panel at the given window width.

    A fixed 32px gutter costs 64px of a ~710px content area on a non-maximized
    window, which is why the UI reads as tight until it is maximized.
    """
    if width < BP_NARROW:
        return SPACE_4
    if width < BP_WIDE:
        return SPACE_5
    return SPACE_6


def sidebar_width(width: int) -> int:
    return SIDEBAR_W_NARROW if width < BP_NARROW else SIDEBAR_W


def font_css(size: int, weight: int = WEIGHT_NORMAL) -> str:
    """Font declarations for a stylesheet fragment."""
    return f"font-size: {size}px; font-weight: {weight};"
