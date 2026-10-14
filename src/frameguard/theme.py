"""
FrameGuard Desktop -- Theme System.
15 professional color themes (9 dark + 6 light) with modern design language.

Design Principles:
  - 8px grid spacing throughout
  - 12px radius rounded cards
  - Visual hierarchy via size + weight (not decoration)
  - Generous whitespace
  - Muted palette, accent used sparingly
  - Consistent 36px minimum touch targets

Every theme meets WCAG AA contrast, enforced by tests/test_design_system.py.

Accent colours deliberately avoid green and red: those already mean PASS and
FAIL in the results table, and an accent that reads as a verdict is a bad idea
in a QA tool.

Dark Themes:
  1. Meta Dark (default) — Deep navy, Meta blue accent
  2. Midnight Charcoal — True dark with cool gray tones
  3. Slate Professional — Medium-dark with blue-gray undertones
  4. Nord Frost — Nord-inspired dark with icy blue accents
  5. Monokai Pro — Warm dark with amber/orange accents
  6. Solarized Dark — Ethan Schoonover's classic dark palette
  7. Darkula — Dracula/IntelliJ-inspired, purple accent on warm charcoal
  8. Carbon — Neutral true-dark, IBM Carbon lineage, no colour cast
  9. Abyss Teal — Near-black with a cool teal accent

Light Themes:
  10. Clean Light — Pure white, Meta blue accent
  11. Warm Cream — Warm off-white with amber accent
  12. Arctic Blue — Cool silver with deep blue accent
  13. Sage — Natural green-gray with forest accent
  14. Pearl — Warm white with purple accent (Figma-inspired)
  15. Glacier — Cool silver-blue with teal accent (Vercel-inspired)
"""

from dataclasses import dataclass
from typing import Dict


# ============================================================================
# THEME DATA CLASS
# ============================================================================

@dataclass
class ThemeColors:
    """Complete color palette for a FrameGuard theme."""
    name: str
    accent: str
    accent_hover: str
    accent_dim: str
    bg_primary: str
    bg_secondary: str
    bg_tertiary: str
    bg_input: str
    surface: str
    surface_hover: str
    border: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    status_pass: str = "#22C55E"
    status_fail: str = "#EF4444"
    status_warn: str = "#F59E0B"
    status_skip: str = "#6B7280"
    scrollbar_bg: str = ""
    scrollbar_handle: str = ""

    def __post_init__(self):
        if not self.scrollbar_bg:
            self.scrollbar_bg = self.bg_primary
        if not self.scrollbar_handle:
            self.scrollbar_handle = self.border


# ============================================================================
# THEME DEFINITIONS (12 themes: 6 dark + 6 light)
# ============================================================================


THEMES: Dict[str, ThemeColors] = {
    # --- DARK THEMES ---
    "Meta Dark": ThemeColors(
        name="Meta Dark",
        accent="#0A6EEA", accent_hover="#0073FF", accent_dim="#004AAD",
        bg_primary="#1A1A2E", bg_secondary="#16213E", bg_tertiary="#0F3460",
        bg_input="#1E2A45", surface="#1E2A45", surface_hover="#253555",
        border="#5A6A8C",
        text_primary="#E8E8EC", text_secondary="#9CA3AF", text_tertiary="#828997",
    ),
    "Midnight Charcoal": ThemeColors(
        name="Midnight Charcoal",
        accent="#6366F1", accent_hover="#818CF8", accent_dim="#4F46E5",
        bg_primary="#111111", bg_secondary="#1A1A1A", bg_tertiary="#222222",
        bg_input="#1E1E1E", surface="#1E1E1E", surface_hover="#2A2A2A",
        border="#636363",
        text_primary="#FAFAFA", text_secondary="#A1A1AA", text_tertiary="#81818A",
    ),
    "Slate Professional": ThemeColors(
        name="Slate Professional",
        accent="#3B82F6", accent_hover="#60A5FA", accent_dim="#2563EB",
        bg_primary="#1E293B", bg_secondary="#0F172A", bg_tertiary="#334155",
        bg_input="#1E293B", surface="#1E293B", surface_hover="#334155",
        border="#677589",
        text_primary="#F1F5F9", text_secondary="#94A3B8", text_tertiary="#8292A9",
    ),
    "Nord Frost": ThemeColors(
        name="Nord Frost",
        accent="#88C0D0", accent_hover="#8FBCBB", accent_dim="#5E81AC",
        bg_primary="#2E3440", bg_secondary="#3B4252", bg_tertiary="#434C5E",
        bg_input="#3B4252", surface="#3B4252", surface_hover="#434C5E",
        border="#768094",
        text_primary="#ECEFF4", text_secondary="#D8DEE9", text_tertiary="#91B1D1",
        status_pass="#A3BE8C", status_fail="#BF616A", status_warn="#EBCB8B",
        status_skip="#4C566A",
    ),
    "Monokai Pro": ThemeColors(
        name="Monokai Pro",
        accent="#FFD866", accent_hover="#FFE08A", accent_dim="#E6C05E",
        bg_primary="#2D2A2E", bg_secondary="#221F22", bg_tertiary="#403E41",
        bg_input="#353236", surface="#353236", surface_hover="#403E41",
        border="#78757B",
        text_primary="#FCFCFA", text_secondary="#C1C0C0", text_tertiary="#969496",
        status_pass="#A9DC76", status_fail="#FF6188", status_warn="#FFD866",
        status_skip="#727072",
    ),
    "Solarized Dark": ThemeColors(
        name="Solarized Dark",
        accent="#268BD2", accent_hover="#2AA198", accent_dim="#073642",
        bg_primary="#002B36", bg_secondary="#073642", bg_tertiary="#0A4052",
        bg_input="#073642", surface="#073642", surface_hover="#0A4052",
        border="#60767D",
        text_primary="#FDF6E3", text_secondary="#93A1A1", text_tertiary="#869CA4",
        status_pass="#859900", status_fail="#DC322F", status_warn="#B58900",
        status_skip="#657B83",
    ),
    "Darkula": ThemeColors(
        name="Darkula",
        accent="#BD93F9", accent_hover="#CFA9FF", accent_dim="#6C4FA8",
        bg_primary="#282A36", bg_secondary="#21222C", bg_tertiary="#343746",
        bg_input="#2E3040", surface="#2E3040", surface_hover="#3A3D4E",
        border="#6D7392",
        text_primary="#F8F8F2", text_secondary="#BDC1D6", text_tertiary="#8F94AE",
        status_pass="#50FA7B", status_fail="#FF5555", status_warn="#FFB86C",
        status_skip="#6272A4",
    ),
    # Neutral true-dark, IBM Carbon lineage. No colour cast anywhere in the
    # greys, which keeps attention on the status colours in the results table.
    "Carbon": ThemeColors(
        name="Carbon",
        accent="#4589FF", accent_hover="#6EA6FF", accent_dim="#0043CE",
        bg_primary="#161616", bg_secondary="#1F1F1F", bg_tertiary="#292929",
        bg_input="#232323", surface="#232323", surface_hover="#333333",
        border="#6F6F6F",
        text_primary="#F4F4F4", text_secondary="#C6C6C6", text_tertiary="#9A9A9A",
        status_pass="#42BE65", status_fail="#FA4D56", status_warn="#F1C21B",
        status_skip="#8D8D8D",
    ),
    # Near-black with a cool teal accent. Teal is deliberately chosen over a
    # green accent: green already means PASS, and an accent that reads as a
    # verdict is a bad idea in a QA tool.
    "Abyss Teal": ThemeColors(
        name="Abyss Teal",
        accent="#2DD4BF", accent_hover="#5EEAD4", accent_dim="#0F766E",
        bg_primary="#0B1416", bg_secondary="#111E21", bg_tertiary="#18292D",
        bg_input="#142326", surface="#142326", surface_hover="#1E353A",
        border="#5C7A80",
        text_primary="#ECFEFF", text_secondary="#B4CDD1", text_tertiary="#8AA5AA",
        status_pass="#4ADE80", status_fail="#F87171", status_warn="#FBBF24",
        status_skip="#64848A",
    ),

    # --- LIGHT THEMES ---
    "Clean Light": ThemeColors(
        name="Clean Light",
        accent="#1877F2", accent_hover="#0F6AE6", accent_dim="#D9E9FF",
        bg_primary="#FFFFFF", bg_secondary="#F6F8FA", bg_tertiary="#EEF2F6",
        bg_input="#FFFFFF", surface="#FFFFFF", surface_hover="#F2F5F8",
        border="#D6DDE6",
        text_primary="#0F172A", text_secondary="#475569", text_tertiary="#63738A",
        status_pass="#1F9D55", status_fail="#D92D20", status_warn="#B45309",
    ),
    "Warm Cream": ThemeColors(
        name="Warm Cream",
        accent="#C26A00", accent_hover="#A85600", accent_dim="#FFE6C2",
        bg_primary="#FFF9F0", bg_secondary="#F6EFE4", bg_tertiary="#EFE6D8",
        bg_input="#FFFCF6", surface="#FFFCF6", surface_hover="#F4EDDF",
        border="#D7CFC0",
        text_primary="#1F2937", text_secondary="#4B5563", text_tertiary="#666D7B",
        status_pass="#15803D", status_fail="#B42318", status_warn="#A16207",
    ),
    "Arctic Blue": ThemeColors(
        name="Arctic Blue",
        accent="#1E40AF", accent_hover="#1D4ED8", accent_dim="#DBEAFE",
        bg_primary="#F7FAFF", bg_secondary="#EEF4FB", bg_tertiary="#E5EDF7",
        bg_input="#FFFFFF", surface="#FFFFFF", surface_hover="#EAF1FB",
        border="#CBD5E1",
        text_primary="#0B1324", text_secondary="#334155", text_tertiary="#556274",
        status_pass="#0F8A4B", status_fail="#C81E1E", status_warn="#B45309",
    ),
    "Sage": ThemeColors(
        name="Sage",
        # Slate blue rather than forest green: the previous accent sat only 42
        # perceptual units from this theme's own PASS colour, so accent buttons
        # and PASS badges were nearly indistinguishable side by side.
        accent="#2F6690", accent_hover="#255479", accent_dim="#DCE8F2",
        bg_primary="#F5F7F4", bg_secondary="#ECEFEA", bg_tertiary="#E2E7E1",
        bg_input="#FFFFFF", surface="#FFFFFF", surface_hover="#E8EEE8",
        border="#C7D0C7",
        text_primary="#111827", text_secondary="#374151", text_tertiary="#656C7A",
        status_pass="#147D3A", status_fail="#B42318", status_warn="#A16207",
    ),
    "Pearl": ThemeColors(
        name="Pearl",
        accent="#7C3AED", accent_hover="#6D28D9", accent_dim="#7C3AED22",
        bg_primary="#FAFAF9", bg_secondary="#F5F5F4", bg_tertiary="#E7E5E4",
        bg_input="#FFFFFF", surface="#FFFFFF", surface_hover="#F5F5F4",
        border="#D6D3D1",
        text_primary="#1C1917", text_secondary="#57534E", text_tertiary="#756F6B",
        status_pass="#16A34A", status_fail="#DC2626", status_warn="#D97706",
    ),
    "Glacier": ThemeColors(
        name="Glacier",
        # Shifted from teal toward blue: the old accent was 55 units from this
        # theme's PASS colour, and only 3.39:1 against the background.
        accent="#0C6E93", accent_hover="#095878", accent_dim="#0C6E9322",
        bg_primary="#F0F4F8", bg_secondary="#E8EEF4", bg_tertiary="#DAE3ED",
        bg_input="#FFFFFF", surface="#FFFFFF", surface_hover="#E8EEF4",
        border="#C4D1DE",
        text_primary="#0F172A", text_secondary="#475569", text_tertiary="#5E6D82",
        status_pass="#059669", status_fail="#DC2626", status_warn="#D5730A",
    ),
}

# Default theme
_current_theme_name = "Meta Dark"


def get_theme(name: str = None) -> ThemeColors:
    """Get a theme by name. Returns current theme if name is None."""
    if name is None:
        name = _current_theme_name
    return THEMES.get(name, THEMES["Meta Dark"])


def set_current_theme(name: str):
    """Set the current theme by name."""
    global _current_theme_name
    if name in THEMES:
        _current_theme_name = name


def get_theme_names() -> list:
    """Return list of available theme names."""
    return list(THEMES.keys())


# ============================================================================
# WARNING: These module-level constants are computed ONCE at import time.
# They will NOT update if set_current_theme() is called at runtime.
# For dynamic theme access, always use get_theme() instead.

# BACKWARD-COMPATIBLE EXPORTS (default theme)
# ============================================================================

_t = get_theme()
META_BLUE = _t.accent
META_BLUE_HOVER = _t.accent_hover
META_BLUE_DIM = _t.accent_dim
BG_PRIMARY = _t.bg_primary
BG_SECONDARY = _t.bg_secondary
BG_TERTIARY = _t.bg_tertiary
BG_INPUT = _t.bg_input
SURFACE = _t.surface
SURFACE_HOVER = _t.surface_hover
BORDER = _t.border
TEXT_PRIMARY = _t.text_primary
TEXT_SECONDARY = _t.text_secondary
TEXT_TERTIARY = _t.text_tertiary
STATUS_PASS = _t.status_pass
STATUS_FAIL = _t.status_fail
STATUS_WARN = _t.status_warn
STATUS_SKIP = _t.status_skip
SCROLLBAR_BG = _t.scrollbar_bg
SCROLLBAR_HANDLE = _t.scrollbar_handle


# ============================================================================
# STYLESHEET GENERATOR — Modern Design Language
# ============================================================================

_CSS_TEMPLATE = """
/* ================================================================
   FrameGuard v0.5 — Modern Design System
   8px grid | 12px radius | Visual hierarchy via weight
   ================================================================ */

/* ===== Global Reset ===== */
QMainWindow, QWidget {
    background-color: %%bg_primary%%;
    color: %%text_primary%%;
    font-family: "Segoe UI", "SF Pro Display", "Helvetica Neue", sans-serif;
    font-size: 13px;
    outline: none;
}

QLabel {
    padding: 0px;
    background: transparent;
}

/* ===== Sidebar ===== */
#sidebar {
    background-color: %%bg_secondary%%;
    border-right: 1px solid %%border%%;
}

/* Nav rows are inset pills. The sidebar layout supplies an 8px gutter on both
   sides, so padding here only handles the inside of the pill.
   Previously: padding 12px 16px 12px 13px plus a 3px left border, which gave
   an asymmetric row and a 44px height that matched nothing else. */
#sidebar QPushButton {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: %%text_secondary%%;
    font-size: 14px;
    font-weight: 500;
    padding: 8px 12px;
    text-align: left;
    min-height: 24px;
}

#sidebar QPushButton:hover {
    background-color: %%surface_hover%%;
    color: %%text_primary%%;
}

/* The active row needs a cue that survives all 15 themes. Measured:
     accent label on bg_tertiary      -> 2.6:1 worst case (Meta Dark) - fails
     white label on an accent pill    -> 1.4:1 worst case (Monokai Pro) - far worse
     text_primary on bg_tertiary      -> 7.5:1 worst case - comfortable
   The filled pill alone is not enough either: bg_tertiary against bg_secondary
   is only 1.06:1 in the light themes. So the label carries the contrast and a
   left accent bar carries the colour. padding-left compensates for the border
   so the row stays optically symmetric with the inactive rows. */
#sidebar QPushButton[active="true"] {
    background-color: %%bg_tertiary%%;
    border-left: 3px solid %%accent%%;
    padding-left: 9px;
    color: %%text_primary%%;
    font-weight: 600;
}

/* ===== Logo area ===== */
/* Full-bleed header: the sidebar layout supplies no horizontal inset here, so
   the banner spans the whole sidebar width instead of floating 8px in from
   each edge. Its vertical padding comes from the layout, not from here. */
#logo_area {
    background-color: %%bg_tertiary%%;
    border-bottom: 1px solid %%border%%;
    padding: 0px;
}

#logo_label {
    color: %%text_primary%%;
    font-size: 16px;
    font-weight: bold;
    letter-spacing: -0.3px;
}

#version_label {
    color: %%text_tertiary%%;
    font-size: 11px;
    font-weight: normal;
}

/* ===== Content area ===== */
#content_area {
    background-color: %%bg_primary%%;
}

/* ===== Section headers ===== */
QLabel[class="section-header"] {
    color: %%text_primary%%;
    font-size: 22px;
    font-weight: bold;
    padding-bottom: 4px;
    letter-spacing: -0.5px;
}

QLabel[class="section-subtitle"] {
    color: %%text_secondary%%;
    font-size: 13px;
    font-weight: normal;
}

/* ===== Primary Buttons (pill-shaped) ===== */
QPushButton#primary_btn {
    background-color: %%accent%%;
    border: none;
    border-radius: 20px;
    color: white;
    font-size: 13px;
    font-weight: bold;
    padding: 10px 28px;
    min-height: 20px;
}

QPushButton#primary_btn:hover {
    background-color: %%accent_hover%%;
}

QPushButton#primary_btn:disabled {
    background-color: %%bg_tertiary%%;
    color: %%text_tertiary%%;
}

/* ===== Secondary Buttons ===== */
QPushButton#secondary_btn {
    background-color: transparent;
    border: 1px solid %%border%%;
    border-radius: 20px;
    color: %%text_primary%%;
    font-size: 13px;
    font-weight: 500;
    padding: 8px 20px;
}

QPushButton#secondary_btn:hover {
    background-color: %%surface_hover%%;
    border-color: %%accent%%;
    color: %%accent%%;
}

/* ===== Progress Bar (thin, elegant) ===== */
QProgressBar {
    background-color: %%bg_tertiary%%;
    border: none;
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
    text-align: center;
}

QProgressBar::chunk {
    background-color: %%accent%%;
    border-radius: 3px;
}

/* ===== Table ===== */
QTableWidget {
    background-color: %%bg_primary%%;
    border: 1px solid %%border%%;
    border-radius: 12px;
    gridline-color: %%border%%;
    color: %%text_primary%%;
    selection-background-color: %%surface_hover%%;
    alternate-background-color: %%bg_secondary%%;
}

QTableWidget::item {
    padding: 8px 12px;
    border-bottom: 1px solid %%border%%;
}

QTableWidget::item:selected {
    background-color: %%accent_dim%%;
    color: %%text_primary%%;
}

QHeaderView::section {
    background-color: %%bg_secondary%%;
    border: none;
    border-bottom: 1px solid %%border%%;
    color: %%text_tertiary%%;
    font-size: 11px;
    font-weight: bold;
    padding: 8px 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* ===== Scroll bars (minimal) ===== */
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 4px 2px;
    border-radius: 3px;
}

QScrollBar::handle:vertical {
    background: %%border%%;
    border-radius: 3px;
    min-height: 40px;
}

QScrollBar::handle:vertical:hover {
    background: %%text_tertiary%%;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background: transparent;
    height: 6px;
    margin: 2px 4px;
    border-radius: 3px;
}

QScrollBar::handle:horizontal {
    background: %%border%%;
    border-radius: 3px;
    min-width: 40px;
}

QScrollBar::handle:horizontal:hover {
    background: %%text_tertiary%%;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ===== Group Box (card style) ===== */
QGroupBox {
    background-color: %%surface%%;
    border: 1px solid %%border%%;
    border-radius: 12px;
    font-size: 13px;
    font-weight: bold;
    margin-top: 16px;
    padding: 20px 16px 16px 16px;
}

QGroupBox::title {
    color: %%text_secondary%%;
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 12px;
    font-size: 12px;
    font-weight: bold;
    letter-spacing: 0.3px;
}

/* ===== Labels inside containers ===== */
QLabel[class="section-header"] {
    color: %%text_primary%%;
    font-size: 22px;
    font-weight: bold;
}

QLabel[class="section-subtitle"] {
    color: %%text_secondary%%;
    font-size: 13px;
}

QLabel[class="hint"] {
    color: %%text_tertiary%%;
    font-size: 11px;
    font-style: italic;
}

QLabel[class="group-label"] {
    color: %%text_secondary%%;
}

QLabel[class="field-label"] {
    color: %%text_tertiary%%;
    font-size: 11px;
    font-weight: bold;
    letter-spacing: 0.3px;
}

QLabel[class="about-text"] {
    color: %%text_secondary%%;
    font-size: 12px;
}

/* ===== Line Edit ===== */
QLineEdit {
    background-color: %%bg_input%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    color: %%text_primary%%;
    padding: 8px 12px;
    /* 22 + 16 padding + 2 border = 40px, matching CONTROL_LG. At 20 this
       rendered 38px, which sat off the control scale next to every other
       input in the app. */
    min-height: 22px;
    selection-background-color: %%accent%%;
    selection-color: #FFFFFF;
}

QLineEdit:focus {
    border-color: %%accent%%;
}

/* ===== Check Box ===== */
QCheckBox {
    color: %%text_primary%%;
    spacing: 10px;
    font-size: 13px;
    padding: 8px 0;
}

QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 2px solid %%border%%;
    border-radius: 4px;
    background-color: %%bg_input%%;
}

QCheckBox::indicator:hover {
    border-color: %%accent%%;
}

QCheckBox::indicator:checked {
    background-color: %%accent%%;
    border-color: %%accent%%;
}

/* Radio buttons carry the Quick/Deep scan-mode choice. They had no rules at
   all, so they fell back to the native indicator, which is invisible against
   a dark stylesheet — the default (Deep) looked unselected, and unlabelled. */
QRadioButton {
    color: %%text_primary%%;
    spacing: 10px;
    font-size: 13px;
    padding: 4px 0;
}

QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border: 2px solid %%border%%;
    border-radius: 10px;
    background-color: %%bg_input%%;
}

QRadioButton::indicator:hover {
    border-color: %%accent%%;
}

/* Keep the border width identical between states: Qt sizes the indicator as
   content + border, so a thicker border on :checked would make the selected
   dot physically larger than the unselected one. The inner dot comes from a
   radial gradient instead. */
QRadioButton::indicator:checked {
    border: 2px solid %%accent%%;
    background-color: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 %%accent%%, stop:0.55 %%accent%%,
        stop:0.6 %%bg_input%%, stop:1 %%bg_input%%);
}

QRadioButton:disabled {
    color: %%text_tertiary%%;
}

/* ===== SpinBox ===== */
QSpinBox, QDoubleSpinBox {
    background-color: %%bg_input%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    color: %%text_primary%%;
    padding: 6px 12px;
    min-height: 20px;
    font-size: 13px;
}

QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: %%accent%%;
}

QSpinBox::up-button, QDoubleSpinBox::up-button {
    border: none;
    width: 16px;
}

QSpinBox::down-button, QDoubleSpinBox::down-button {
    border: none;
    width: 16px;
}

/* ===== TabWidget ===== */
QTabWidget::pane {
    border: 1px solid %%border%%;
    border-radius: 12px;
    background-color: %%bg_secondary%%;
    padding: 8px;
}

QTabBar::tab {
    background-color: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 10px 20px;
    color: %%text_secondary%%;
    font-weight: 500;
    margin-right: 4px;
}

QTabBar::tab:selected {
    color: %%accent%%;
    border-bottom: 2px solid %%accent%%;
    font-weight: bold;
}

QTabBar::tab:hover {
    color: %%text_primary%%;
}

/* ===== Tooltip ===== */
QToolTip {
    background-color: %%surface%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    color: %%text_primary%%;
    padding: 8px 12px;
    font-size: 12px;
}

/* ===== Drop zone ===== */
#drop_zone {
    background-color: %%bg_secondary%%;
    border: 2px dashed %%border%%;
    border-radius: 16px;
    min-height: 180px;
}

#drop_zone:hover {
    border-color: %%accent%%;
    background-color: %%surface_hover%%;
}

/* ===== Status badges (pill-shaped) ===== */
#badge_pass {
    background-color: rgba(34, 197, 94, 0.12);
    border: 1px solid rgba(34, 197, 94, 0.25);
    border-radius: 12px;
    color: %%status_pass%%;
    font-size: 12px;
    font-weight: bold;
    padding: 4px 12px;
}

#badge_fail {
    background-color: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.25);
    border-radius: 12px;
    color: %%status_fail%%;
    font-size: 12px;
    font-weight: bold;
    padding: 4px 12px;
}

#badge_warn {
    background-color: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.25);
    border-radius: 12px;
    color: %%status_warn%%;
    font-size: 12px;
    font-weight: bold;
    padding: 4px 12px;
}

#badge_skip {
    background-color: rgba(107, 114, 128, 0.12);
    border: 1px solid rgba(107, 114, 128, 0.25);
    border-radius: 12px;
    color: %%status_skip%%;
    font-size: 12px;
    font-weight: bold;
    padding: 4px 12px;
}

/* ===== Sliders (thin track, clean handle) ===== */
QSlider {
    min-height: 36px;
    padding: 4px 0;
}

QSlider::groove:horizontal {
    height: 4px;
    background: %%bg_tertiary%%;
    border-radius: 2px;
    border: none;
}

QSlider::handle:horizontal {
    width: 18px;
    height: 18px;
    margin: -7px 0;
    background: %%accent%%;
    border-radius: 9px;
    border: 2px solid %%bg_primary%%;
}

QSlider::handle:horizontal:hover {
    background: %%accent_hover%%;
    width: 20px;
    height: 20px;
    margin: -8px 0;
    border-radius: 10px;
}

QSlider::sub-page:horizontal {
    background: %%accent%%;
    border-radius: 2px;
}

QSlider::add-page:horizontal {
    background: %%bg_tertiary%%;
    border-radius: 2px;
}

/* ===== Dropdowns / ComboBox ===== */
QComboBox {
    min-height: 36px;
    padding: 6px 12px;
    font-size: 13px;
    background: %%bg_input%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    color: %%text_primary%%;
}

QComboBox:hover {
    border-color: %%accent%%;
}

QComboBox::drop-down {
    width: 32px;
    border: none;
    border-left: 1px solid %%border%%;
    background: transparent;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}

QComboBox::down-arrow {
    width: 0px;
    height: 0px;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid %%text_tertiary%%;
}

QComboBox::down-arrow:on {
    border-top: none;
    border-bottom: 6px solid %%accent%%;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
}

/* Dropdown popup list */
QComboBox QAbstractItemView {
    background-color: %%surface%%;
    color: %%text_primary%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    selection-background-color: %%accent%%;
    selection-color: #FFFFFF;
    padding: 4px;
    outline: none;
}

QComboBox QAbstractItemView::item {
    padding: 8px 12px;
    min-height: 28px;
    border-radius: 4px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: %%surface_hover%%;
}

/* ===== Reset Buttons (circular) ===== */
QPushButton[class="reset-btn"] {
    min-width: 36px;
    min-height: 36px;
    max-width: 36px;
    max-height: 36px;
    font-size: 18px;
    font-weight: bold;
    border-radius: 18px;
    border: 1px solid %%border%%;
    background: %%surface%%;
    color: %%text_secondary%%;
    padding: 0px;
}

QPushButton[class="reset-btn"]:hover {
    background: %%accent_dim%%;
    border-color: %%accent%%;
    color: %%accent%%;
}

/* ===== Text Edit / Log areas ===== */
QTextEdit {
    background-color: %%bg_secondary%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    color: %%text_primary%%;
    padding: 8px;
    font-family: "JetBrains Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
    selection-background-color: %%accent%%;
    selection-color: #FFFFFF;
}

/* ===== List Widget ===== */
QListWidget {
    background-color: %%bg_primary%%;
    border: 1px solid %%border%%;
    border-radius: 8px;
    color: %%text_primary%%;
    padding: 4px;
    outline: none;
}

QListWidget::item {
    padding: 6px 12px;
    border-radius: 6px;
    margin: 2px 0;
}

QListWidget::item:hover {
    background-color: %%surface_hover%%;
}

QListWidget::item:selected {
    background-color: %%accent_dim%%;
    color: %%text_primary%%;
}

/* ===== Selection (theme-aware) ===== */
QTableWidget::item:selected { background-color: %%accent_dim%%; color: %%text_primary%%; }
QTreeWidget::item:selected { background-color: %%accent_dim%%; color: %%text_primary%%; }

/* ===== Dialog ===== */
QDialog {
    background-color: %%bg_primary%%;
    border-radius: 12px;
}

/* ===== Message Box ===== */
QMessageBox {
    background-color: %%bg_primary%%;
}

QMessageBox QPushButton {
    background-color: %%accent%%;
    border: none;
    border-radius: 16px;
    color: white;
    font-weight: bold;
    padding: 8px 24px;
    min-width: 80px;
}

QMessageBox QPushButton:hover {
    background-color: %%accent_hover%%;
}

/* ===== Splitter ===== */
QSplitter::handle {
    background: %%border%%;
    width: 1px;
    margin: 0 4px;
}

/* ===== Frame ===== */
QFrame[frameShape="4"] {
    color: %%border%%;
    max-height: 1px;
}

QFrame[frameShape="5"] {
    color: %%border%%;
    max-width: 1px;
}
"""


def generate_stylesheet(theme: "ThemeColors" = None) -> str:
    """Generate the full Qt stylesheet for a given theme.
    
    Uses safe placeholder substitution to avoid CSS brace conflicts.
    """
    if theme is None:
        theme = get_theme()
    
    t = theme
    css = _CSS_TEMPLATE
    
    # Substitute all theme variables
    replacements = {
        "%%accent%%": t.accent,
        "%%accent_hover%%": t.accent_hover,
        "%%accent_dim%%": t.accent_dim,
        "%%bg_primary%%": t.bg_primary,
        "%%bg_secondary%%": t.bg_secondary,
        "%%bg_tertiary%%": t.bg_tertiary,
        "%%bg_input%%": t.bg_input,
        "%%surface%%": t.surface,
        "%%surface_hover%%": t.surface_hover,
        "%%border%%": t.border,
        "%%text_primary%%": t.text_primary,
        "%%text_secondary%%": t.text_secondary,
        "%%text_tertiary%%": t.text_tertiary,
        "%%status_pass%%": t.status_pass,
        "%%status_fail%%": t.status_fail,
        "%%status_warn%%": t.status_warn,
        "%%status_skip%%": t.status_skip,
        "%%scrollbar_bg%%": t.scrollbar_bg,
        "%%scrollbar_handle%%": t.scrollbar_handle,
    }
    
    for placeholder, value in replacements.items():
        css = css.replace(placeholder, value)
    
    return css


# Pre-rendered default stylesheet (used by gui.py at import time)
DARK_STYLESHEET = generate_stylesheet(THEMES["Meta Dark"])
