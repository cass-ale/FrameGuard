#!/usr/bin/env python3
"""FrameGuard GUI audit -- measurement, not eyeballing.

Walks every widget in every panel at several window sizes and reports anything
that violates the design system: off-grid geometry, ad-hoc font sizes, clipped
text, horizontal overflow, inconsistent control heights, and low-contrast text.

    QT_QPA_PLATFORM=offscreen python tools_dev/gui_audit.py
    QT_QPA_PLATFORM=offscreen python tools_dev/gui_audit.py --verbose
"""

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QWidget, QLabel, QPushButton, QCheckBox, QComboBox,
    QLineEdit, QSpinBox, QDoubleSpinBox,
)

# --- The design system being enforced -------------------------------------
GRID = 8                      # 8-point grid; 4 tolerated for fine detail
HALF_GRID = 4
TYPE_SCALE = {11, 12, 13, 14, 16, 18, 20, 24, 30}   # modular, ~1.2 ratio
CONTROL_HEIGHTS = {28, 32, 40, 48}                   # sm / md / lg / xl
RADII = {4, 6, 8, 12, 999}                           # 999 = pill
MIN_CONTRAST_BODY = 4.5       # WCAG AA normal text
MIN_CONTRAST_LARGE = 3.0      # WCAG AA >=18px or >=14px bold

SIZES = [(1000, 700), (1280, 800), (1920, 1080)]

_px = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px")
_radius = re.compile(r"border-radius:\s*(\d+(?:\.\d+)?)px")
_pad = re.compile(r"padding:\s*([^;]+);")


def _rel_luminance(rgb):
    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    lo, hi = sorted((l1, l2))
    return (hi + 0.05) / (lo + 0.05)


def _hex_to_rgb(value):
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def off_grid(value, allow_half=True):
    if value <= 0:
        return False
    if value % GRID == 0:
        return False
    if allow_half and value % HALF_GRID == 0:
        return False
    return True


class Audit:
    def __init__(self):
        self.findings = collections.defaultdict(list)
        self.fonts = collections.Counter()
        self.heights = collections.Counter()
        self.radii = collections.Counter()
        self.margins = collections.Counter()
        self.spacings = collections.Counter()

    def add(self, severity, category, detail):
        self.findings[(severity, category)].append(detail)

    # -- individual checks --------------------------------------------------

    def check_clipping(self, w, path, size_label):
        """Text that does not fit the space the widget was actually given."""
        if not w.isVisible() or w.width() <= 0:
            return
        hint = w.sizeHint()
        if not hint.isValid():
            return
        text = getattr(w, "text", lambda: "")() if hasattr(w, "text") else ""
        wraps = getattr(w, "wordWrap", lambda: False)()
        if isinstance(w, (QLabel, QPushButton, QCheckBox)) and text and not wraps:
            if hint.width() > w.width() + 1:
                self.add("HIGH", "clipped text",
                         f"[{size_label}] {path}: needs {hint.width()}px, "
                         f"has {w.width()}px -- {text[:44]!r}")
        if hint.height() > w.height() + 1 and w.height() > 0 and not wraps:
            # Content taller than the viewport inside a QScrollArea is the
            # point of a scroll area, not a defect.
            if "QScrollArea" in path or "scrollarea" in path:
                return
            self.add("MEDIUM", "clipped vertically",
                     f"[{size_label}] {path}: needs {hint.height()}px, has {w.height()}px")

    def check_overflow(self, w, path, size_label):
        parent = w.parentWidget()
        if parent is None or not w.isVisible():
            return
        need = w.minimumSizeHint().width()
        if need > parent.width() + 1 and parent.width() > 0:
            self.add("HIGH", "horizontal overflow",
                     f"[{size_label}] {path}: min {need}px inside {parent.width()}px parent")

    def check_geometry_grid(self, w, path):
        # Hidden widgets report meaningless geometry (a collapsed row reads as
        # 480px tall), so only measure what is actually on screen.
        if not w.isVisible():
            return
        h = w.height()
        if isinstance(w, (QPushButton, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox)):
            if h and h not in CONTROL_HEIGHTS:
                self.heights[h] += 1
                self.add("MEDIUM", "off-scale control height",
                         f"{path}: {h}px (expected one of {sorted(CONTROL_HEIGHTS)})")

    def check_stylesheet(self, w, path):
        ss = w.styleSheet()
        if not ss:
            return
        for m in _px.finditer(ss):
            size = float(m.group(1))
            self.fonts[size] += 1
            if size not in TYPE_SCALE:
                self.add("MEDIUM", "off-scale font size",
                         f"{path}: {size:g}px (scale: {sorted(TYPE_SCALE)})")
        for m in _radius.finditer(ss):
            r = float(m.group(1))
            self.radii[r] += 1
            if r not in RADII and r < 90:
                self.add("LOW", "off-scale radius", f"{path}: border-radius {r:g}px")
        for m in _pad.finditer(ss):
            for token in m.group(1).split():
                if token.endswith("px"):
                    try:
                        v = float(token[:-2])
                    except ValueError:
                        continue
                    if off_grid(v):
                        self.add("LOW", "off-grid padding", f"{path}: padding {v:g}px")

    def check_font(self, w, path):
        f = w.font()
        size = f.pointSize()
        if size > 0:
            self.fonts[size] += 1
            # Point sizes render ~1.33x px on Windows; compare in px terms.
            approx_px = round(size * 96 / 72)
            if approx_px not in TYPE_SCALE:
                self.add("MEDIUM", "off-scale font size",
                         f"{path}: QFont {size}pt (~{approx_px}px)")

    def check_layout(self, w, path):
        lay = w.layout()
        if lay is None:
            return
        m = lay.contentsMargins()
        for name, v in (("left", m.left()), ("top", m.top()),
                        ("right", m.right()), ("bottom", m.bottom())):
            self.margins[v] += 1
            if off_grid(v):
                self.add("LOW", "off-grid margin", f"{path}: {name} margin {v}px")
        s = lay.spacing()
        if s >= 0:
            self.spacings[s] += 1
            if off_grid(s):
                self.add("LOW", "off-grid spacing", f"{path}: spacing {s}px")
        if m.left() != m.right():
            self.add("MEDIUM", "asymmetric margins",
                     f"{path}: left {m.left()}px != right {m.right()}px")

    def check_contrast(self, theme):
        """Theme colour pairs that fail WCAG AA."""
        bg = _hex_to_rgb(theme.bg_primary)
        bg2 = _hex_to_rgb(theme.bg_secondary)
        pairs = [
            ("text_primary on bg_primary", theme.text_primary, theme.bg_primary, MIN_CONTRAST_BODY),
            ("text_secondary on bg_primary", theme.text_secondary, theme.bg_primary, MIN_CONTRAST_BODY),
            ("text_tertiary on bg_primary", theme.text_tertiary, theme.bg_primary, MIN_CONTRAST_BODY),
            ("text_secondary on bg_secondary", theme.text_secondary, theme.bg_secondary, MIN_CONTRAST_BODY),
            ("text_tertiary on bg_secondary", theme.text_tertiary, theme.bg_secondary, MIN_CONTRAST_BODY),
            ("accent on bg_primary", theme.accent, theme.bg_primary, MIN_CONTRAST_LARGE),
            ("status_pass on bg_primary", theme.status_pass, theme.bg_primary, MIN_CONTRAST_LARGE),
            ("status_fail on bg_primary", theme.status_fail, theme.bg_primary, MIN_CONTRAST_LARGE),
            ("status_warn on bg_primary", theme.status_warn, theme.bg_primary, MIN_CONTRAST_LARGE),
        ]
        if not bg or not bg2:
            return
        for label, fg_hex, bg_hex, minimum in pairs:
            fg, back = _hex_to_rgb(fg_hex), _hex_to_rgb(bg_hex)
            if not fg or not back:
                continue
            ratio = contrast_ratio(fg, back)
            if ratio < minimum:
                self.add("HIGH", "contrast below WCAG AA",
                         f"{theme.name}: {label} = {ratio:.2f}:1 (needs {minimum})")

    def check_alignment(self, panel, path, size_label):
        """Direct children of a vertical layout should share a left edge."""
        lay = panel.layout()
        if lay is None or lay.__class__.__name__ != "QVBoxLayout":
            return
        lefts = collections.Counter()
        for i in range(lay.count()):
            wdg = lay.itemAt(i).widget()
            if wdg is not None and wdg.isVisible():
                lefts[wdg.x()] += 1
        if len(lefts) > 1:
            common = lefts.most_common(1)[0][0]
            strays = {x: n for x, n in lefts.items() if x != common}
            self.add("MEDIUM", "misaligned left edge",
                     f"[{size_label}] {path}: most children at x={common}, strays {strays}")


def walk(widget, audit, path, size_label, depth=0):
    if depth > 14:
        return
    name = widget.objectName() or widget.__class__.__name__
    here = f"{path}/{name}"
    audit.check_clipping(widget, here, size_label)
    audit.check_overflow(widget, here, size_label)
    audit.check_geometry_grid(widget, here)
    audit.check_stylesheet(widget, here)
    audit.check_font(widget, here)
    audit.check_layout(widget, here)
    # Direct children only; walk() recurses itself so recursive find would
    # revisit the same subtree once per level.
    for child in widget.children():
        if isinstance(child, QWidget):
            walk(child, audit, here, size_label, depth + 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--max-per-category", type=int, default=8)
    args = parser.parse_args()

    app = QApplication.instance() or QApplication(sys.argv)
    from frameguard import gui as G
    from frameguard.core import QAResult
    from frameguard.theme import THEMES, get_theme, set_current_theme

    def mk(n, kind):
        r = QAResult("C:/videos/" + n)
        r.set_metadata("video_codec", "hevc")
        r.set_metadata("resolution", "3840x2160")
        r.set_metadata("duration_display", "00:01:30")
        r.set_metadata("file_size_mb", 1234.5)
        r.add_pass("Container Format", "mov accepted")
        if kind != "pass":
            r.add_warn("Black Frames", "2 sequences detected", confidence=60)
        if kind == "fail":
            r.add_fail("Bitrate", "12.5 Mbps is below the 20 Mbps minimum")
        r.finalize()
        return r

    audit = Audit()
    window = G.MainWindow()
    results = [mk(f"SHOW_EP{i:02}_4K_30p_MASTER_v3.mov",
                  ["pass", "warn", "fail"][i % 3]) for i in range(9)]
    window.results_panel.set_results(results, append=True)
    window.checks_panel.set_results(window.results_panel._results)
    window.show()

    panels = [(G.PANEL_SCAN, "Scan"), (G.PANEL_RESULTS, "Results"),
              (G.PANEL_CHECKS, "Checks"), (G.PANEL_WATCH, "Watch"),
              (G.PANEL_SETTINGS, "Settings")]

    for w, h in SIZES:
        window.resize(w, h)
        app.processEvents()
        label = f"{w}x{h}"
        for index, pname in panels:
            window._switch_panel(index)
            for _ in range(3):
                app.processEvents()
            panel = window.stack.widget(index)
            walk(panel, audit, pname, label)
            audit.check_alignment(panel, pname, label)

    for theme_name in THEMES:
        set_current_theme(theme_name)
        audit.check_contrast(get_theme(theme_name))

    # --- report ---
    print("=" * 74)
    print("  FrameGuard GUI Audit")
    print("=" * 74)

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    total = 0
    for (severity, category), items in sorted(
            audit.findings.items(), key=lambda kv: (order[kv[0][0]], -len(kv[1]))):
        unique = sorted(set(items))
        total += len(unique)
        print(f"\n[{severity}] {category}  ({len(unique)})")
        limit = len(unique) if args.verbose else args.max_per_category
        for line in unique[:limit]:
            print(f"    {line}")
        if len(unique) > limit:
            print(f"    ... and {len(unique) - limit} more (--verbose)")

    print("\n" + "-" * 74)
    print("Distinct values in use (a design system should have few):")
    print(f"  font sizes      : {sorted(audit.fonts)}")
    print(f"  control heights : {sorted(audit.heights)}")
    print(f"  border radii    : {sorted(audit.radii)}")
    print(f"  layout margins  : {sorted(audit.margins)}")
    print(f"  layout spacings : {sorted(audit.spacings)}")
    print(f"\nTOTAL FINDINGS: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
