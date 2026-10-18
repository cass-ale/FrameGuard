"""FrameGuard theme catalogue and semantic Qt stylesheet.

All 15 public theme names are stable. Application-owned widgets use one dynamic
property, ``fgRole``, for semantic styling; type-specific selectors allow a role
such as ``toolbar`` or ``status`` to describe related controls and surfaces.
"""

from dataclasses import dataclass
from typing import Dict


FG_ROLE_PROPERTY = "fgRole"
ACTION_ROLES = ("primary", "secondary", "quiet", "destructive", "toolbar", "icon")
SURFACE_ROLES = ("page", "toolbar", "sidebar", "statistic", "framed-tool", "status")
TEXT_ROLES = ("heading", "muted", "metric", "status")
FG_ROLES = frozenset(ACTION_ROLES + SURFACE_ROLES + TEXT_ROLES)


def _rgb(value):
    value = value.lstrip("#")[:6]
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v))) for v in rgb))


def _mix(first, second, amount):
    a = _rgb(first)
    b = _rgb(second)
    return _hex(tuple(x + (y - x) * amount for x, y in zip(a, b)))


def _luminance(value):
    def channel(component):
        component /= 255.0
        if component <= 0.03928:
            return component / 12.92
        return ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(component) for component in _rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second):
    """Return the WCAG contrast ratio between two opaque hex colours."""
    low, high = sorted((_luminance(first), _luminance(second)))
    return (high + 0.05) / (low + 0.05)


def _best_foreground(*backgrounds):
    choices = ("#0B0F14", "#FFFFFF")
    return max(choices, key=lambda color: min(
        contrast_ratio(color, background) for background in backgrounds))


def _ensure_contrast(color, backgrounds, minimum):
    if isinstance(backgrounds, str):
        backgrounds = (backgrounds,)
    if min(contrast_ratio(color, bg) for bg in backgrounds) >= minimum:
        return color
    target = _best_foreground(*backgrounds)
    for step in range(1, 101):
        candidate = _mix(color, target, step / 100.0)
        if min(contrast_ratio(candidate, bg) for bg in backgrounds) >= minimum:
            return candidate
    return target


def _interactive_state_color(color, foreground, app_backgrounds):
    """Tune a fill until text and component-boundary contrast both pass."""
    target = "#FFFFFF" if _luminance(foreground) < 0.5 else "#000000"
    for step in range(101):
        candidate = _mix(color, target, step / 100.0)
        text_ok = contrast_ratio(foreground, candidate) >= 4.5
        boundary_ok = min(
            contrast_ratio(candidate, bg) for bg in app_backgrounds) >= 3.0
        if text_ok and boundary_ok:
            return candidate
    return color


def _status_color(color, background):
    """Tune status text against both the page and its derived surface."""
    target = _best_foreground(background)
    for step in range(101):
        candidate = _mix(color, target, step / 100.0)
        surface = _mix(background, candidate, 0.12)
        if (contrast_ratio(candidate, background) >= 4.5
                and contrast_ratio(candidate, surface) >= 4.5):
            return candidate
    return target


@dataclass
class ThemeColors:
    """Complete color palette, including every interactive state token."""

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
    accent_foreground: str = ""
    accent_pressed: str = ""
    selection: str = ""
    selection_foreground: str = ""
    focus: str = ""
    disabled_bg: str = ""
    disabled_text: str = ""
    destructive: str = ""
    destructive_hover: str = ""
    destructive_pressed: str = ""
    destructive_foreground: str = ""
    status_pass_surface: str = ""
    status_pass_border: str = ""
    status_fail_surface: str = ""
    status_fail_border: str = ""
    status_warn_surface: str = ""
    status_warn_border: str = ""
    status_skip_surface: str = ""
    status_skip_border: str = ""

    def __post_init__(self):
        state_backgrounds = (self.bg_primary, self.bg_secondary, self.bg_input)
        self.accent = _ensure_contrast(self.accent, state_backgrounds, 3.0)
        self.accent_foreground = (
            self.accent_foreground or _best_foreground(self.accent))
        self.accent = _interactive_state_color(
            self.accent, self.accent_foreground, state_backgrounds)
        self.accent_hover = _ensure_contrast(
            self.accent_hover, state_backgrounds, 3.0)
        self.accent_hover = _interactive_state_color(
            self.accent_hover, self.accent_foreground, state_backgrounds)
        if not self.accent_pressed:
            self.accent_pressed = _mix(self.accent, self.bg_primary, 0.22)
        self.accent_pressed = _ensure_contrast(
            self.accent_pressed, state_backgrounds, 3.0)
        self.accent_pressed = _interactive_state_color(
            self.accent_pressed, self.accent_foreground, state_backgrounds)
        self.selection = self.selection or self.accent
        self.selection_foreground = (
            self.selection_foreground or _best_foreground(self.selection))
        self.focus = self.focus or self.accent
        self.disabled_bg = self.disabled_bg or _mix(
            self.bg_input, self.bg_primary, 0.55)
        self.disabled_text = self.disabled_text or _ensure_contrast(
            self.text_tertiary, self.disabled_bg, 3.0)

        # Status colors are used as text at body sizes, including on the
        # subtly tinted surfaces derived below.
        for field_name in ("status_pass", "status_fail", "status_warn", "status_skip"):
            value = _status_color(getattr(self, field_name), self.bg_primary)
            setattr(self, field_name, value)
            surface_name = field_name + "_surface"
            border_name = field_name + "_border"
            if not getattr(self, surface_name):
                setattr(self, surface_name, _mix(self.bg_primary, value, 0.12))
            if not getattr(self, border_name):
                setattr(self, border_name, value)

        self.destructive = self.destructive or self.status_fail
        self.destructive_foreground = (
            self.destructive_foreground or _best_foreground(self.destructive))
        self.destructive = _interactive_state_color(
            self.destructive, self.destructive_foreground, state_backgrounds)
        self.destructive_hover = self.destructive_hover or _mix(
            self.destructive, self.text_primary, 0.12)
        self.destructive_hover = _interactive_state_color(
            self.destructive_hover, self.destructive_foreground, state_backgrounds)
        self.destructive_pressed = self.destructive_pressed or _mix(
            self.destructive, self.bg_primary, 0.20)
        self.destructive_pressed = _interactive_state_color(
            self.destructive_pressed, self.destructive_foreground,
            state_backgrounds)
        self.scrollbar_bg = self.scrollbar_bg or self.bg_primary
        self.scrollbar_handle = self.scrollbar_handle or self.border


THEMES: Dict[str, ThemeColors] = {
    "Meta Dark": ThemeColors(
        name="Meta Dark", accent="#0A6EEA", accent_hover="#0073FF",
        accent_dim="#004AAD", bg_primary="#1A1A2E", bg_secondary="#16213E",
        bg_tertiary="#0F3460", bg_input="#1E2A45", surface="#1E2A45",
        surface_hover="#253555", border="#5A6A8C", text_primary="#E8E8EC",
        text_secondary="#9CA3AF", text_tertiary="#828997"),
    "Midnight Charcoal": ThemeColors(
        name="Midnight Charcoal", accent="#6366F1", accent_hover="#818CF8",
        accent_dim="#4F46E5", bg_primary="#111111", bg_secondary="#1A1A1A",
        bg_tertiary="#222222", bg_input="#1E1E1E", surface="#1E1E1E",
        surface_hover="#2A2A2A", border="#636363", text_primary="#FAFAFA",
        text_secondary="#A1A1AA", text_tertiary="#81818A"),
    "Slate Professional": ThemeColors(
        name="Slate Professional", accent="#3B82F6", accent_hover="#60A5FA",
        accent_dim="#2563EB", bg_primary="#1E293B", bg_secondary="#0F172A",
        bg_tertiary="#334155", bg_input="#1E293B", surface="#1E293B",
        surface_hover="#334155", border="#677589", text_primary="#F1F5F9",
        text_secondary="#94A3B8", text_tertiary="#8292A9"),
    "Nord Frost": ThemeColors(
        name="Nord Frost", accent="#88C0D0", accent_hover="#8FBCBB",
        accent_dim="#5E81AC", bg_primary="#2E3440", bg_secondary="#3B4252",
        bg_tertiary="#434C5E", bg_input="#3B4252", surface="#3B4252",
        surface_hover="#434C5E", border="#768094", text_primary="#ECEFF4",
        text_secondary="#D8DEE9", text_tertiary="#91B1D1",
        status_pass="#A3BE8C", status_fail="#BF616A", status_warn="#EBCB8B",
        status_skip="#4C566A"),
    "Monokai Pro": ThemeColors(
        name="Monokai Pro", accent="#FFD866", accent_hover="#FFE08A",
        accent_dim="#E6C05E", bg_primary="#2D2A2E", bg_secondary="#221F22",
        bg_tertiary="#403E41", bg_input="#353236", surface="#353236",
        surface_hover="#403E41", border="#78757B", text_primary="#FCFCFA",
        text_secondary="#C1C0C0", text_tertiary="#969496",
        status_pass="#A9DC76", status_fail="#FF6188", status_warn="#FFD866",
        status_skip="#727072"),
    "Solarized Dark": ThemeColors(
        name="Solarized Dark", accent="#268BD2", accent_hover="#2AA198",
        accent_dim="#073642", bg_primary="#002B36", bg_secondary="#073642",
        bg_tertiary="#0A4052", bg_input="#073642", surface="#073642",
        surface_hover="#0A4052", border="#60767D", text_primary="#FDF6E3",
        text_secondary="#93A1A1", text_tertiary="#869CA4",
        status_pass="#859900", status_fail="#DC322F", status_warn="#B58900",
        status_skip="#657B83"),
    "Darkula": ThemeColors(
        name="Darkula", accent="#BD93F9", accent_hover="#CFA9FF",
        accent_dim="#6C4FA8", bg_primary="#282A36", bg_secondary="#21222C",
        bg_tertiary="#343746", bg_input="#2E3040", surface="#2E3040",
        surface_hover="#3A3D4E", border="#6D7392", text_primary="#F8F8F2",
        text_secondary="#BDC1D6", text_tertiary="#8F94AE",
        status_pass="#50FA7B", status_fail="#FF5555", status_warn="#FFB86C",
        status_skip="#6272A4"),
    "Carbon": ThemeColors(
        name="Carbon", accent="#4589FF", accent_hover="#6EA6FF",
        accent_dim="#0043CE", bg_primary="#161616", bg_secondary="#1F1F1F",
        bg_tertiary="#292929", bg_input="#232323", surface="#232323",
        surface_hover="#333333", border="#6F6F6F", text_primary="#F4F4F4",
        text_secondary="#C6C6C6", text_tertiary="#9A9A9A",
        status_pass="#42BE65", status_fail="#FA4D56", status_warn="#F1C21B",
        status_skip="#8D8D8D"),
    "Abyss Teal": ThemeColors(
        name="Abyss Teal", accent="#2DD4BF", accent_hover="#5EEAD4",
        accent_dim="#0F766E", bg_primary="#0B1416", bg_secondary="#111E21",
        bg_tertiary="#18292D", bg_input="#142326", surface="#142326",
        surface_hover="#1E353A", border="#5C7A80", text_primary="#ECFEFF",
        text_secondary="#B4CDD1", text_tertiary="#8AA5AA",
        status_pass="#4ADE80", status_fail="#F87171", status_warn="#FBBF24",
        status_skip="#64848A"),
    "Clean Light": ThemeColors(
        name="Clean Light", accent="#1877F2", accent_hover="#0F6AE6",
        accent_dim="#D9E9FF", bg_primary="#FFFFFF", bg_secondary="#F6F8FA",
        bg_tertiary="#EEF2F6", bg_input="#FFFFFF", surface="#FFFFFF",
        surface_hover="#F2F5F8", border="#D6DDE6", text_primary="#0F172A",
        text_secondary="#475569", text_tertiary="#63738A",
        status_pass="#1F9D55", status_fail="#D92D20", status_warn="#B45309"),
    "Warm Cream": ThemeColors(
        name="Warm Cream", accent="#C26A00", accent_hover="#A85600",
        accent_dim="#FFE6C2", bg_primary="#FFF9F0", bg_secondary="#F6EFE4",
        bg_tertiary="#EFE6D8", bg_input="#FFFCF6", surface="#FFFCF6",
        surface_hover="#F4EDDF", border="#D7CFC0", text_primary="#1F2937",
        text_secondary="#4B5563", text_tertiary="#666D7B",
        status_pass="#15803D", status_fail="#B42318", status_warn="#A16207"),
    "Arctic Blue": ThemeColors(
        name="Arctic Blue", accent="#1E40AF", accent_hover="#1D4ED8",
        accent_dim="#DBEAFE", bg_primary="#F7FAFF", bg_secondary="#EEF4FB",
        bg_tertiary="#E5EDF7", bg_input="#FFFFFF", surface="#FFFFFF",
        surface_hover="#EAF1FB", border="#CBD5E1", text_primary="#0B1324",
        text_secondary="#334155", text_tertiary="#556274",
        status_pass="#0F8A4B", status_fail="#C81E1E", status_warn="#B45309"),
    "Sage": ThemeColors(
        name="Sage", accent="#2F6690", accent_hover="#255479",
        accent_dim="#DCE8F2", bg_primary="#F5F7F4", bg_secondary="#ECEFEA",
        bg_tertiary="#E2E7E1", bg_input="#FFFFFF", surface="#FFFFFF",
        surface_hover="#E8EEE8", border="#C7D0C7", text_primary="#111827",
        text_secondary="#374151", text_tertiary="#656C7A",
        status_pass="#147D3A", status_fail="#B42318", status_warn="#A16207"),
    "Pearl": ThemeColors(
        name="Pearl", accent="#7C3AED", accent_hover="#6D28D9",
        accent_dim="#7C3AED22", bg_primary="#FAFAF9", bg_secondary="#F5F5F4",
        bg_tertiary="#E7E5E4", bg_input="#FFFFFF", surface="#FFFFFF",
        surface_hover="#F5F5F4", border="#D6D3D1", text_primary="#1C1917",
        text_secondary="#57534E", text_tertiary="#756F6B",
        status_pass="#16A34A", status_fail="#DC2626", status_warn="#D97706"),
    "Glacier": ThemeColors(
        name="Glacier", accent="#0C6E93", accent_hover="#095878",
        accent_dim="#0C6E9322", bg_primary="#F0F4F8", bg_secondary="#E8EEF4",
        bg_tertiary="#DAE3ED", bg_input="#FFFFFF", surface="#FFFFFF",
        surface_hover="#E8EEF4", border="#C4D1DE", text_primary="#0F172A",
        text_secondary="#475569", text_tertiary="#5E6D82",
        status_pass="#059669", status_fail="#DC2626", status_warn="#D5730A"),
}

_current_theme_name = "Meta Dark"


def get_theme(name: str = None) -> ThemeColors:
    """Return a named theme, or the current theme when no name is supplied."""
    if name is None:
        name = _current_theme_name
    return THEMES.get(name, THEMES["Meta Dark"])


def set_current_theme(name: str):
    """Set the current theme when ``name`` is in the stable catalogue."""
    global _current_theme_name
    if name in THEMES:
        _current_theme_name = name


def get_theme_names() -> list:
    return list(THEMES.keys())


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


_CSS_TEMPLATE = r"""
/* FrameGuard Quiet Pro semantic stylesheet. Typography is point-sized; all
   geometry remains in logical pixels. */
QMainWindow, QDialog, QWidget {
    background-color: %%bg_primary%%;
    color: %%text_primary%%;
    font-size: 11pt;
}
QWidget:disabled { color: %%disabled_text%%; }
QWidget[fgRole="page"], #content_area { background-color: %%bg_primary%%; }
QWidget[fgRole="toolbar"] { background-color: %%bg_secondary%%; border-bottom: 1px solid %%border%%; }
QWidget[fgRole="sidebar"], #sidebar { background-color: %%bg_primary%%; border: none; border-right: 1px solid %%divider%%; }
/* Layout-only containers inside the sidebar must never repaint the page
   colour over it: that is what split the sidebar into mismatched blocks and
   left its right border visible only in fragments. */
#logo_area, #sidebar_body, #sidebar_toggle_column { background: transparent; border: none; }
QWidget[fgRole="statistic"] { background-color: %%surface%%; border: 1px solid %%border%%; border-radius: 8px; }
QWidget[fgRole="framed-tool"] { background-color: %%surface%%; border: 1px solid %%border%%; border-radius: 8px; }
QWidget[fgRole="status"] { background-color: %%bg_secondary%%; border: 1px solid %%border%%; border-radius: 4px; }

QLabel { padding: 0px; background: transparent; }
QLabel[fgRole="heading"], QLabel[class="section-header"] { color: %%text_primary%%; font-size: 16pt; font-weight: 600; }
QLabel[fgRole="muted"], QLabel[class="section-subtitle"], QLabel[class="group-label"], QLabel[class="about-text"] { color: %%text_secondary%%; font-size: 10pt; }
QLabel[fgRole="metric"] { color: %%text_primary%%; font-size: 20pt; font-weight: 600; }
QLabel[fgRole="status"] { color: %%text_secondary%%; font-size: 10pt; font-weight: 600; }
QLabel[class="hint"], QLabel[class="field-label"], #version_label { color: %%text_tertiary%%; font-size: 9pt; }
QLabel[class="field-label"] { font-weight: 600; }
#logo_label { color: %%text_primary%%; font-size: 12pt; font-weight: 700; }

/* Complete fallback: an unclassified button never leaks native styling. */
QPushButton, QToolButton {
    background-color: %%surface%%;
    border: 1px solid %%border%%;
    border-radius: 6px;
    color: %%text_primary%%;
    font-size: 10pt;
    font-weight: 500;
    min-height: 18px;
    padding: 6px 14px;
}
QPushButton:hover, QToolButton:hover { background-color: %%surface_hover%%; border-color: %%accent%%; }
QPushButton:pressed, QToolButton:pressed { background-color: %%bg_tertiary%%; border-color: %%accent_pressed%%; }
QPushButton:checked, QToolButton:checked { background-color: %%selection%%; color: %%selection_foreground%%; border-color: %%selection%%; }
QPushButton:focus, QToolButton:focus { border: 2px solid %%focus%%; padding: 5px 13px; }
QPushButton:disabled, QToolButton:disabled { background-color: %%disabled_bg%%; color: %%disabled_text%%; border-color: %%border%%; }
QPushButton:default { border: 2px solid %%focus%%; padding: 5px 13px; }

QPushButton[fgRole="primary"], QPushButton#primary_btn, QMessageBox QPushButton:default {
    background-color: %%accent%%; color: %%accent_foreground%%; border-color: %%accent%%; font-weight: 600;
}
QPushButton[fgRole="primary"]:hover, QPushButton#primary_btn:hover, QMessageBox QPushButton:default:hover { background-color: %%accent_hover%%; border-color: %%accent_hover%%; }
QPushButton[fgRole="primary"]:pressed, QPushButton#primary_btn:pressed, QMessageBox QPushButton:default:pressed { background-color: %%accent_pressed%%; border-color: %%accent_pressed%%; }
QPushButton[fgRole="primary"]:checked, QPushButton#primary_btn:checked { background-color: %%accent_pressed%%; color: %%accent_foreground%%; }
QPushButton[fgRole="primary"]:focus, QPushButton#primary_btn:focus { border: 2px solid %%focus%%; }
QPushButton[fgRole="primary"]:disabled, QPushButton#primary_btn:disabled { background-color: %%disabled_bg%%; color: %%disabled_text%%; border-color: %%border%%; }
QPushButton[fgRole="primary"]:default, QPushButton#primary_btn:default { border: 2px solid %%focus%%; }

QPushButton[fgRole="secondary"], QPushButton#secondary_btn { background-color: %%surface%%; color: %%text_primary%%; border-color: %%border%%; }
QPushButton[fgRole="secondary"]:hover, QPushButton#secondary_btn:hover { background-color: %%surface_hover%%; border-color: %%accent%%; }
QPushButton[fgRole="secondary"]:pressed, QPushButton#secondary_btn:pressed { background-color: %%bg_tertiary%%; border-color: %%accent_pressed%%; }
QPushButton[fgRole="secondary"]:checked, QPushButton#secondary_btn:checked { background-color: %%selection%%; color: %%selection_foreground%%; }
QPushButton[fgRole="secondary"]:focus, QPushButton#secondary_btn:focus { border: 2px solid %%focus%%; }
QPushButton[fgRole="secondary"]:disabled, QPushButton#secondary_btn:disabled { background-color: %%disabled_bg%%; color: %%disabled_text%%; border-color: %%border%%; }
QPushButton[fgRole="secondary"]:default, QPushButton#secondary_btn:default { border: 2px solid %%focus%%; }

QPushButton[fgRole="quiet"] { background: transparent; border-color: transparent; color: %%text_secondary%%; }
QPushButton[fgRole="quiet"]:hover { background-color: %%surface_hover%%; color: %%text_primary%%; }
QPushButton[fgRole="quiet"]:pressed { background-color: %%bg_tertiary%%; }
QPushButton[fgRole="quiet"]:checked { background-color: %%selection%%; color: %%selection_foreground%%; }
QPushButton[fgRole="quiet"]:focus, QPushButton[fgRole="quiet"]:default { border: 2px solid %%focus%%; }
QPushButton[fgRole="quiet"]:disabled { background: transparent; color: %%disabled_text%%; border-color: transparent; }

QPushButton[fgRole="destructive"] { background-color: %%destructive%%; color: %%destructive_foreground%%; border-color: %%destructive%%; font-weight: 600; }
QPushButton[fgRole="destructive"]:hover { background-color: %%destructive_hover%%; border-color: %%destructive_hover%%; }
QPushButton[fgRole="destructive"]:pressed, QPushButton[fgRole="destructive"]:checked { background-color: %%destructive_pressed%%; border-color: %%destructive_pressed%%; }
QPushButton[fgRole="destructive"]:focus, QPushButton[fgRole="destructive"]:default { border: 2px solid %%focus%%; }
QPushButton[fgRole="destructive"]:disabled { background-color: %%disabled_bg%%; color: %%disabled_text%%; border-color: %%border%%; }
QPushButton#title_btn_close { min-width: 24px; min-height: 24px; padding: 4px; }

QPushButton[fgRole="toolbar"] { background: transparent; border-color: transparent; color: %%text_secondary%%; padding: 4px 10px; }
QPushButton[fgRole="toolbar"]:hover { background-color: %%surface_hover%%; color: %%text_primary%%; }
QPushButton[fgRole="toolbar"]:pressed { background-color: %%bg_tertiary%%; }
QPushButton[fgRole="toolbar"]:checked { background-color: %%selection%%; color: %%selection_foreground%%; }
QPushButton[fgRole="toolbar"]:focus, QPushButton[fgRole="toolbar"]:default { border: 2px solid %%focus%%; }
QPushButton[fgRole="toolbar"]:disabled { background: transparent; color: %%disabled_text%%; border-color: transparent; }

QPushButton[fgRole="icon"], QPushButton[class="reset-btn"] { background: transparent; border-color: transparent; min-width: 24px; min-height: 24px; padding: 4px; }
QPushButton[fgRole="icon"]:hover, QPushButton[class="reset-btn"]:hover { background-color: %%surface_hover%%; border-color: %%border%%; }
QPushButton[fgRole="icon"]:pressed, QPushButton[class="reset-btn"]:pressed { background-color: %%bg_tertiary%%; }
QPushButton[fgRole="icon"]:checked { background-color: %%selection%%; color: %%selection_foreground%%; }
QPushButton[fgRole="icon"]:focus, QPushButton[fgRole="icon"]:default { border: 2px solid %%focus%%; }
QPushButton[fgRole="icon"]:disabled, QPushButton[class="reset-btn"]:disabled { background: transparent; color: %%disabled_text%%; border-color: transparent; }

#sidebar QPushButton { background: transparent; border-color: transparent; color: %%text_secondary%%; min-height: 22px; padding: 8px 12px; text-align: left; }
#sidebar QPushButton:hover { background-color: %%surface_hover%%; color: %%text_primary%%; }
#sidebar QPushButton:pressed { background-color: %%bg_tertiary%%; }
#sidebar QPushButton:focus { border: 2px solid %%focus%%; padding: 7px 11px; }
#sidebar QPushButton:disabled { color: %%disabled_text%%; }
#sidebar QPushButton[active="true"] { background-color: %%bg_tertiary%%; border-color: transparent; border-left: 3px solid %%accent%%; border-top-left-radius: 0px; border-bottom-left-radius: 0px; color: %%text_primary%%; font-weight: 600; padding-left: 9px; }
#sidebar QPushButton[active="true"]:focus { border: 2px solid %%focus%%; border-left: 3px solid %%accent%%; padding: 7px 11px 7px 9px; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QComboBox {
    background-color: %%bg_input%%; border: 1px solid %%border%%; border-radius: 6px;
    color: %%text_primary%%; selection-background-color: %%selection%%;
    selection-color: %%selection_foreground%%; padding: 6px 10px; min-height: 18px;
}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QDateTimeEdit:hover, QComboBox:hover { border-color: %%accent%%; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus, QComboBox:focus { border: 2px solid %%focus%%; padding: 5px 9px; }
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QDateTimeEdit:disabled, QComboBox:disabled { background-color: %%disabled_bg%%; color: %%disabled_text%%; border-color: %%border%%; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button, QDateTimeEdit::up-button, QDateTimeEdit::down-button { width: 18px; border: none; background: transparent; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 28px; border: none; background: transparent; }
QComboBox::down-arrow { image: %%img_chevron%%; width: 12px; height: 12px; }
QComboBox::down-arrow:disabled { image: %%img_chevron_disabled%%; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateTimeEdit::up-arrow { image: %%img_chevron_up%%; width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateTimeEdit::down-arrow { image: %%img_chevron%%; width: 10px; height: 10px; }
QComboBox QAbstractItemView { background-color: %%surface%%; color: %%text_primary%%; border: 1px solid %%border%%; selection-background-color: %%selection%%; selection-color: %%selection_foreground%%; outline: none; padding: 4px; }
QComboBox QAbstractItemView::item { min-height: 28px; padding: 6px 10px; }
QComboBox QAbstractItemView::item:hover { background-color: %%surface_hover%%; }
QComboBox QAbstractItemView::item:selected { background-color: %%selection%%; color: %%selection_foreground%%; }
QComboBox QAbstractItemView::item:disabled { color: %%disabled_text%%; }

QCheckBox, QRadioButton { color: %%text_primary%%; spacing: 8px; padding: 4px; }
QCheckBox:hover, QRadioButton:hover { color: %%text_primary%%; }
QCheckBox:focus, QRadioButton:focus { border: 1px solid %%focus%%; border-radius: 4px; padding: 3px; }
QCheckBox:disabled, QRadioButton:disabled { color: %%disabled_text%%; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; border: 2px solid %%border%%; background-color: %%bg_input%%; }
QCheckBox::indicator { border-radius: 4px; }
QRadioButton::indicator { border-radius: 10px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: %%accent%%; }
QCheckBox::indicator:pressed, QRadioButton::indicator:pressed { border-color: %%accent_pressed%%; background-color: %%bg_tertiary%%; }
QCheckBox::indicator:checked { border: 2px solid %%accent%%; background-color: %%accent%%; image: %%img_check%%; }
QCheckBox::indicator:checked:hover { border-color: %%accent_hover%%; background-color: %%accent_hover%%; }
QRadioButton::indicator:checked { border: 2px solid %%accent%%; background-color: %%accent%%; image: %%img_dot%%; }
QRadioButton::indicator:checked:hover { border-color: %%accent_hover%%; background-color: %%accent_hover%%; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled { border-color: %%disabled_text%%; background-color: %%disabled_bg%%; image: none; }

QProgressBar { background-color: %%bg_tertiary%%; border: 1px solid %%border%%; border-radius: 4px; color: %%text_primary%%; text-align: center; min-height: 8px; }
QProgressBar::chunk { background-color: %%accent%%; border-radius: 3px; }
QProgressBar:disabled { background-color: %%disabled_bg%%; color: %%disabled_text%%; }
QSlider { min-height: 32px; }
QSlider::groove:horizontal { height: 4px; background: %%bg_tertiary%%; border-radius: 2px; }
QSlider::handle:horizontal { width: 18px; margin: -7px 0; background: %%accent%%; border: 2px solid %%bg_primary%%; border-radius: 10px; }
QSlider::handle:horizontal:hover { background: %%accent_hover%%; }
QSlider::handle:horizontal:pressed { background: %%accent_pressed%%; }
QSlider::handle:horizontal:disabled { background: %%disabled_text%%; }
QSlider::sub-page:horizontal { background: %%accent%%; border-radius: 2px; }

QAbstractItemView { background-color: %%bg_primary%%; alternate-background-color: %%bg_secondary%%; border: 1px solid %%border%%; color: %%text_primary%%; selection-background-color: %%selection%%; selection-color: %%selection_foreground%%; outline: none; }
QAbstractItemView::item { padding: 6px 10px; border: none; border-bottom: 1px solid %%divider%%; }
QTableView { gridline-color: %%divider%%; }
QAbstractItemView::indicator { width: 14px; height: 14px; border: 2px solid %%border%%; border-radius: 4px; background-color: %%bg_input%%; }
QAbstractItemView::indicator:hover { border-color: %%accent%%; }
QAbstractItemView::indicator:checked { border-color: %%accent%%; background-color: %%accent%%; image: %%img_check%%; }
QAbstractItemView::item:hover { background-color: %%surface_hover%%; }
QAbstractItemView::item:selected { background-color: %%selection%%; color: %%selection_foreground%%; }
QAbstractItemView::item:focus { border: 1px solid %%focus%%; }
QAbstractItemView::item:disabled { color: %%disabled_text%%; background-color: %%disabled_bg%%; }
QHeaderView { background-color: %%bg_secondary%%; border: none; }
QHeaderView::section { background-color: %%bg_secondary%%; border: none; border-bottom: 1px solid %%border%%; color: %%text_secondary%%; font-size: 9pt; font-weight: 600; padding: 7px 10px; }
QTableCornerButton::section { background-color: %%bg_secondary%%; border: none; border-bottom: 1px solid %%border%%; }
QHeaderView::section:hover { background-color: %%surface_hover%%; color: %%text_primary%%; }
QHeaderView::section:pressed, QHeaderView::section:checked { background-color: %%selection%%; color: %%selection_foreground%%; }

QMenu, QMenuBar { background-color: %%surface%%; color: %%text_primary%%; border: 1px solid %%border%%; }
QMenuBar { border: none; }
QMenu::item, QMenuBar::item { padding: 6px 24px 6px 10px; background: transparent; }
QMenu::item:selected, QMenuBar::item:selected { background-color: %%selection%%; color: %%selection_foreground%%; }
QMenu::item:pressed, QMenuBar::item:pressed { background-color: %%accent_pressed%%; color: %%accent_foreground%%; }
QMenu::item:disabled, QMenuBar::item:disabled { color: %%disabled_text%%; }
QMenu::separator { height: 1px; background-color: %%border%%; margin: 5px 8px; }
QToolTip { background-color: %%surface%%; border: 1px solid %%border%%; border-radius: 4px; color: %%text_primary%%; padding: 6px 8px; font-size: 9pt; }

QGroupBox { background: transparent; border: none; margin-top: 18px; padding: 12px 0 0 0; font-weight: 600; }
QGroupBox::title { color: %%text_secondary%%; subcontrol-origin: margin; subcontrol-position: top left; left: 0px; padding: 0 4px 0 0; }
QGroupBox[fgRole="page"] { padding-top: 0px; }
QGroupBox[fgRole="page"]::title { left: -2px; }
QGroupBox[fgRole="framed-tool"], QGroupBox[fgRole="statistic"] { background-color: %%surface%%; border: 1px solid %%border%%; border-radius: 8px; padding: 16px; }
QGroupBox[fgRole="framed-tool"]::title, QGroupBox[fgRole="statistic"]::title { padding: 0 6px; }

QTabWidget::pane { border: none; background-color: %%bg_primary%%; }
QTabBar::tab { background: transparent; border: none; border-bottom: 2px solid transparent; color: %%text_secondary%%; padding: 8px 14px; }
QTabBar::tab:hover { color: %%text_primary%%; background-color: %%surface_hover%%; }
QTabBar::tab:selected { color: %%text_primary%%; border-bottom-color: %%accent%%; font-weight: 600; }
QTabBar::tab:focus { border: 1px solid %%focus%%; }
QTabBar::tab:disabled { color: %%disabled_text%%; }

QScrollBar:vertical { background: %%scrollbar_bg%%; width: 14px; margin: 0px; }
QScrollBar::handle:vertical { background: %%scrollbar_handle%%; border-radius: 3px; min-height: 40px; margin: 2px 4px; }
QScrollBar::handle:vertical:hover { background: %%text_tertiary%%; margin: 2px 3px; }
QScrollBar::handle:vertical:pressed { background: %%accent%%; margin: 2px 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: %%scrollbar_bg%%; height: 14px; margin: 0px; }
QScrollBar::handle:horizontal { background: %%scrollbar_handle%%; border-radius: 3px; min-width: 40px; margin: 4px 2px; }
QScrollBar::handle:horizontal:hover { background: %%text_tertiary%%; margin: 3px 2px; }
QScrollBar::handle:horizontal:pressed { background: %%accent%%; margin: 3px 2px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 8px; border-left: 1px solid %%border%%; border-right: 1px solid transparent; }
QSplitter::handle:vertical { height: 8px; border-top: 1px solid %%border%%; border-bottom: 1px solid transparent; }
QSplitter::handle:hover { background-color: %%surface_hover%%; border-color: %%accent%%; }
QSplitter::handle:pressed { background-color: %%bg_tertiary%%; border-color: %%accent_pressed%%; }

QDialog, QMessageBox { background-color: %%bg_primary%%; color: %%text_primary%%; }
QMessageBox QLabel { color: %%text_primary%%; }
QFrame[frameShape="4"] { color: %%border%%; max-height: 1px; }
QFrame[frameShape="5"] { color: %%border%%; max-width: 1px; }
#drop_zone { background-color: %%bg_secondary%%; border: 2px dashed %%border%%; border-radius: 12px; min-height: 180px; }
#drop_zone QWidget#drop_zone_empty, #drop_zone QWidget#drop_zone_queue { background: transparent; border: none; }
#drop_zone:hover { border-color: %%accent%%; background-color: %%surface_hover%%; }

#badge_pass { background-color: %%status_pass_surface%%; border: 1px solid %%status_pass_border%%; color: %%status_pass%%; border-radius: 8px; padding: 3px 8px; font-weight: 600; }
#badge_fail { background-color: %%status_fail_surface%%; border: 1px solid %%status_fail_border%%; color: %%status_fail%%; border-radius: 8px; padding: 3px 8px; font-weight: 600; }
#badge_warn { background-color: %%status_warn_surface%%; border: 1px solid %%status_warn_border%%; color: %%status_warn%%; border-radius: 8px; padding: 3px 8px; font-weight: 600; }
#badge_skip { background-color: %%status_skip_surface%%; border: 1px solid %%status_skip_border%%; color: %%status_skip%%; border-radius: 8px; padding: 3px 8px; font-weight: 600; }
"""


# ---------------------------------------------------------------------------
# Indicator artwork
# ---------------------------------------------------------------------------
# Qt style sheets cannot draw a chevron, tick or radio dot from borders alone:
# the old zero-size "CSS triangle" rendered as a flat bar on every combo box,
# and a 5px checked border made checked boxes and radios visibly larger than
# unchecked ones. Tiny SVGs, written once per colour, keep every indicator the
# same size in every state. Qt's bundled svg image-format plugin renders them.
_SVG_SHAPES = {
    "chevron": '<path d="M3.5 6l4.5 4.5L12.5 6" fill="none" stroke="{c}" '
               'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "chevron_up": '<path d="M3.5 10l4.5-4.5L12.5 10" fill="none" stroke="{c}" '
                  'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "check": '<path d="M3.5 8.3l3 3 6-6.6" fill="none" stroke="{c}" '
             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    "dot": '<circle cx="8" cy="8" r="3.2" fill="{c}"/>',
}


def _asset_dir():
    """Per-user cache folder for generated indicator artwork.

    Not the temp folder: macOS purges files there that go unused for about
    three days, and a FrameGuard left running in the tray for that long lost
    its chevrons, ticks and radio dots on the next restyle.
    """
    import os
    import sys
    import tempfile
    try:
        from frameguard import __version__ as version
    except ImportError:  # pragma: no cover - package import always works
        version = "dev"
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Caches", "FrameGuard")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                            "FrameGuard", "Cache")
    else:
        base = os.path.join(os.environ.get("XDG_CACHE_HOME")
                            or os.path.join(home, ".cache"), "frameguard")
    return os.path.join(base, f"ui-{version}")


def _fallback_asset_dir():
    import os
    import tempfile
    try:
        from frameguard import __version__ as version
    except ImportError:  # pragma: no cover
        version = "dev"
    return os.path.join(tempfile.gettempdir(), f"frameguard-ui-{version}")


def _svg_url(shape: str, color: str) -> str:
    """Return a stylesheet ``url(...)`` for one coloured indicator, or ``none``."""
    return _svg_url_in(shape, color, _asset_dir())


def _svg_url_in(shape: str, color: str, directory: str) -> str:
    import os

    path = os.path.join(directory, f"{shape}-{color.lstrip('#').upper()}.svg")
    body = ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" '
            'viewBox="0 0 16 16">' + _SVG_SHAPES[shape].format(c=color) + "</svg>")
    try:
        if not os.path.isfile(path):
            os.makedirs(directory, exist_ok=True)
            temporary = f"{path}.{os.getpid()}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temporary, path)
    except OSError:
        if directory != _fallback_asset_dir():
            return _svg_url_in(shape, color, _fallback_asset_dir())
        return "none"
    return 'url("' + path.replace("\\", "/") + '")'


def _indicator_images(theme: "ThemeColors") -> Dict[str, str]:
    return {
        "img_chevron": _svg_url("chevron", theme.text_secondary),
        "img_chevron_up": _svg_url("chevron_up", theme.text_secondary),
        "img_chevron_disabled": _svg_url("chevron", theme.disabled_text),
        "img_check": _svg_url("check", theme.accent_foreground),
        "img_dot": _svg_url("dot", theme.accent_foreground),
    }


def generate_stylesheet(theme: ThemeColors = None) -> str:
    """Render the complete semantic stylesheet for ``theme``."""
    theme = theme or get_theme()
    replacements = {
        "accent": theme.accent,
        "accent_hover": theme.accent_hover,
        "accent_pressed": theme.accent_pressed,
        "accent_foreground": theme.accent_foreground,
        "selection": theme.selection,
        "selection_foreground": theme.selection_foreground,
        "focus": theme.focus,
        "disabled_bg": theme.disabled_bg,
        "disabled_text": theme.disabled_text,
        "destructive": theme.destructive,
        "destructive_hover": theme.destructive_hover,
        "destructive_pressed": theme.destructive_pressed,
        "destructive_foreground": theme.destructive_foreground,
        "bg_primary": theme.bg_primary,
        "bg_secondary": theme.bg_secondary,
        "bg_tertiary": theme.bg_tertiary,
        "bg_input": theme.bg_input,
        "surface": theme.surface,
        "surface_hover": theme.surface_hover,
        "border": theme.border,
        "text_primary": theme.text_primary,
        "text_secondary": theme.text_secondary,
        "text_tertiary": theme.text_tertiary,
        "status_pass": theme.status_pass,
        "status_fail": theme.status_fail,
        "status_warn": theme.status_warn,
        "status_skip": theme.status_skip,
        "status_pass_surface": theme.status_pass_surface,
        "status_pass_border": theme.status_pass_border,
        "status_fail_surface": theme.status_fail_surface,
        "status_fail_border": theme.status_fail_border,
        "status_warn_surface": theme.status_warn_surface,
        "status_warn_border": theme.status_warn_border,
        "status_skip_surface": theme.status_skip_surface,
        "status_skip_border": theme.status_skip_border,
        "scrollbar_bg": theme.scrollbar_bg,
        "scrollbar_handle": theme.scrollbar_handle,
    }
    replacements["divider"] = _mix(theme.border, theme.bg_primary, 0.45)
    replacements.update(_indicator_images(theme))
    css = _CSS_TEMPLATE
    for token, value in replacements.items():
        css = css.replace("%%" + token + "%%", value)
    return css


DARK_STYLESHEET = generate_stylesheet(THEMES["Meta Dark"])
