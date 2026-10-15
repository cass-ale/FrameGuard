#!/usr/bin/env python3
"""Deterministic rendered-state audit for the FrameGuard desktop UI.

The audit measures realized Qt widgets after layouts and styles settle.  It is
safe to use as a CI gate and writes enough evidence to diagnose a failure:

    python tools_dev/gui_audit.py --json audit/report.json \
        --artifacts-dir audit/screenshots
    python tools_dev/gui_audit.py --matrix --json-dir audit

``--matrix`` starts a fresh process for every scale because Qt reads
``QT_SCALE_FACTOR`` before QApplication is constructed.
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
from typing import Iterable

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics, QImage, QTextDocument  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QScrollBar,
    QSpinBox,
    QDoubleSpinBox,
    QSplitter,
    QStyle,
    QStyleOptionButton,
    QTableWidget,
    QTextEdit,
    QWidget,
)

from frameguard import design as D  # noqa: E402

SIZES = ((1000, 700), (1280, 800), (1920, 1080))
SCALES = (1.0, 1.25, 1.5, 2.0)
MIN_HIT_AREA = D.CONTROL_SM
MIN_SCROLLBAR_EXTENT = D.SCROLLBAR_HIT_AREA
MIN_SPLITTER_EXTENT = D.SPLITTER_HIT_AREA
ACTION_ROLES = {"primary", "secondary", "quiet", "destructive", "toolbar", "icon"}
_TEXT_TOLERANCE = 2

# Intentional geometry intersections only. Keep this narrow: entries are
# (parent class, first child class/name, second child class/name). Qt's item
# views own viewport/corner/scrollbar children which legitimately meet at their
# edges, and QSizeGrip is deliberately painted over the window corner.
OVERLAP_ALLOWLIST = (
    ("QAbstractScrollArea", "*", "*"),
    ("QComboBoxPrivateContainer", "*", "*"),
    ("*", "QSizeGrip", "*"),
)

_VISUAL_STYLE_RE = re.compile(
    r"(?:^|[;{])\s*(?:background(?:-color)?|color|border(?:-[^:]+)?|"
    r"font(?:-[^:]+)?|padding|margin|letter-spacing)\s*:",
    re.IGNORECASE,
)
_ICON_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    state: str
    widget: str
    message: str


class Audit:
    """Collect independent findings for one or more realized widget states."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self._finding_keys: set[tuple[str, ...]] = set()
        self.states: list[dict] = []

    def add(self, severity: str, category: str, state: str,
            widget: str, message: str) -> None:
        finding = Finding(severity, category, state, widget, message)
        key = tuple(asdict(finding).values())
        if key not in self._finding_keys:
            self._finding_keys.add(key)
            self.findings.append(finding)

    @property
    def error_count(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    def report(self, scale: float) -> dict:
        counts = collections.Counter(f.severity for f in self.findings)
        categories = collections.Counter(f.category for f in self.findings)
        return {
            "schema_version": 1,
            "tool": "FrameGuard rendered-state audit",
            "platform": sys.platform,
            "platform_release": platform.release(),
            "python": platform.python_version(),
            "qt_platform": QApplication.platformName() if QApplication.instance() else None,
            "scale_factor": scale,
            "summary": {
                "states": len(self.states),
                "errors": counts["error"],
                "warnings": counts["warning"],
                "categories": dict(sorted(categories.items())),
            },
            "states": self.states,
            "findings": [asdict(f) for f in self.findings],
            "overlap_allowlist": [list(item) for item in OVERLAP_ALLOWLIST],
        }


def _widget_name(widget: QWidget) -> str:
    name = widget.objectName()
    if name:
        return f"{type(widget).__name__}#{name}"
    accessible = widget.accessibleName().strip()
    if accessible:
        return f"{type(widget).__name__}[{accessible!r}]"
    return type(widget).__name__


def widget_path(widget: QWidget, root: QWidget) -> str:
    parts = []
    current = widget
    while current is not None:
        parts.append(_widget_name(current))
        if current is root:
            break
        current = current.parentWidget()
    return "/".join(reversed(parts))


def global_rect(widget: QWidget) -> QRect:
    return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())


def visible_widgets(root: QWidget) -> list[QWidget]:
    widgets = [root]
    widgets.extend(root.findChildren(QWidget))
    return [w for w in widgets if w.isVisibleTo(root) and w.width() > 0 and w.height() > 0]


def _direct_widget_children(parent: QWidget) -> list[QWidget]:
    return [child for child in parent.children()
            if isinstance(child, QWidget) and child.isVisible() and
            child.width() > 0 and child.height() > 0]


def _class_matches(widget: QWidget, expected: str) -> bool:
    if expected == "*":
        return True
    if widget.objectName() == expected or type(widget).__name__ == expected:
        return True
    return any(base.__name__ == expected for base in type(widget).__mro__)


def _overlap_allowed(parent: QWidget, first: QWidget, second: QWidget) -> bool:
    # Popup/native windows are not siblings in the application's layout space.
    if first.isWindow() or second.isWindow():
        return True
    for parent_name, first_name, second_name in OVERLAP_ALLOWLIST:
        if not _class_matches(parent, parent_name):
            continue
        if ((_class_matches(first, first_name) and _class_matches(second, second_name)) or
                (_class_matches(first, second_name) and _class_matches(second, first_name))):
            return True
    return False


def check_sibling_overlaps(root: QWidget, audit: Audit, state: str) -> None:
    for parent in visible_widgets(root):
        children = _direct_widget_children(parent)
        for index, first in enumerate(children):
            first_rect = global_rect(first)
            for second in children[index + 1:]:
                if _overlap_allowed(parent, first, second):
                    continue
                intersection = first_rect.intersected(global_rect(second))
                # Merely touching at an edge produces an empty QRect. Tiny
                # antialiasing/layout rounding intersections are ignored.
                if intersection.width() <= 1 or intersection.height() <= 1:
                    continue
                audit.add(
                    "error", "sibling-overlap", state,
                    widget_path(parent, root),
                    f"{_widget_name(first)} overlaps {_widget_name(second)} by "
                    f"{intersection.width()}x{intersection.height()} logical px",
                )


def _plain_text(widget: QWidget) -> str:
    getter = getattr(widget, "text", None)
    if getter is None:
        return ""
    try:
        text = getter()
    except TypeError:
        return ""
    if not isinstance(text, str):
        return ""
    if isinstance(widget, QAbstractButton):
        text = text.replace("&&", "\0").replace("&", "").replace("\0", "&")
    return text.strip()


def _text_contents_rect(widget: QWidget) -> QRect:
    if isinstance(widget, QPushButton):
        option = QStyleOptionButton()
        widget.initStyleOption(option)
        rect = widget.style().subElementRect(QStyle.SE_PushButtonContents, option, widget)
        if not widget.icon().isNull():
            rect.setWidth(max(0, rect.width() - widget.iconSize().width() - 4))
        return rect
    if isinstance(widget, QCheckBox):
        option = QStyleOptionButton()
        widget.initStyleOption(option)
        return widget.style().subElementRect(QStyle.SE_CheckBoxContents, option, widget)
    if isinstance(widget, QRadioButton):
        option = QStyleOptionButton()
        widget.initStyleOption(option)
        return widget.style().subElementRect(QStyle.SE_RadioButtonContents, option, widget)
    return widget.contentsRect()


def _measure_text(widget: QWidget, text: str, available: QRect) -> QRect:
    if isinstance(widget, QLabel):
        looks_rich = widget.textFormat() == Qt.RichText or (
            widget.textFormat() == Qt.AutoText and bool(re.search(r"</?[A-Za-z][^>]*>", text))
        )
        if looks_rich:
            document = QTextDocument()
            document.setDocumentMargin(0)
            document.setDefaultFont(widget.font())
            document.setHtml(text)
            if widget.wordWrap():
                document.setTextWidth(max(1, available.width()))
            size = document.size().toSize()
            return QRect(0, 0, size.width(), size.height())
    metrics = QFontMetrics(widget.font())
    if isinstance(widget, QLabel) and widget.wordWrap():
        return metrics.boundingRect(
            QRect(0, 0, max(1, available.width()), 10000),
            int(Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop), text,
        )
    lines = text.splitlines() or [text]
    width = max(metrics.horizontalAdvance(line) for line in lines)
    height = metrics.lineSpacing() * len(lines)
    return QRect(0, 0, width, height)


def check_realized_text(root: QWidget, audit: Audit, state: str) -> None:
    for widget in visible_widgets(root):
        if not isinstance(widget, (QLabel, QPushButton, QCheckBox, QRadioButton)):
            continue
        if isinstance(widget, QLabel):
            pixmap = widget.pixmap()
            if pixmap is not None and not pixmap.isNull():
                continue
        text = _plain_text(widget)
        if not text:
            continue
        available = _text_contents_rect(widget)
        measured = _measure_text(widget, text, available)
        path = widget_path(widget, root)
        if measured.width() > available.width() + _TEXT_TOLERANCE:
            # Intentional one-line elision is acceptable only when the full
            # value remains discoverable.
            exposed = (widget.toolTip() or widget.accessibleDescription()).strip()
            if text not in exposed:
                audit.add(
                    "error", "text-clipped", state, path,
                    f"realized text needs {measured.width()}px, contents rect has "
                    f"{available.width()}px: {text[:80]!r}",
                )
        if measured.height() > available.height() + _TEXT_TOLERANCE:
            audit.add(
                "error", "text-clipped", state, path,
                f"realized text needs {measured.height()}px high, contents rect has "
                f"{available.height()}px: {text[:80]!r}",
            )


def check_headers(root: QWidget, audit: Audit, state: str) -> None:
    for table in root.findChildren(QTableWidget):
        if not table.isVisibleTo(root):
            continue
        header = table.horizontalHeader()
        model = table.model()
        metrics = QFontMetrics(header.font())
        for section in range(table.columnCount()):
            value = model.headerData(section, Qt.Horizontal, Qt.DisplayRole)
            text = "" if value is None else str(value)
            if not text:
                continue
            lines = text.splitlines()
            needed_width = max(metrics.horizontalAdvance(line) for line in lines) + 16
            needed_height = metrics.lineSpacing() * len(lines) + 8
            available_width = header.sectionSize(section)
            path = f"{widget_path(table, root)}/header[{section}]"
            tooltip = model.headerData(section, Qt.Horizontal, Qt.ToolTipRole)
            accessible = model.headerData(section, Qt.Horizontal, Qt.AccessibleTextRole)
            full_text_exposed = text in str(tooltip or "") or text in str(accessible or "")
            if needed_width > available_width + _TEXT_TOLERANCE and not full_text_exposed:
                audit.add(
                    "error", "header-clipped", state, path,
                    f"header needs {needed_width}px, section has {available_width}px: {text!r}; "
                    "add width or expose the full label via tooltip/accessibility",
                )
            if needed_height > header.height() + _TEXT_TOLERANCE:
                audit.add(
                    "error", "header-height", state, path,
                    f"{len(lines)}-line header needs {needed_height}px, header has "
                    f"{header.height()}px: {text!r}",
                )


def _scroll_ancestor(widget: QWidget) -> QAbstractScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def _interactive(widget: QWidget) -> bool:
    return isinstance(widget, (
        QAbstractButton, QAbstractItemView, QComboBox, QLineEdit,
        QSpinBox, QDoubleSpinBox, QTextEdit,
    ))


def effective_visible_rect(widget: QWidget) -> QRect:
    """Return the clickable rect after non-scroll ancestors clip the widget.

    A control below a scroll viewport remains reachable by scrolling, so the
    viewport itself does not shrink its practical target. Ordinary containers
    do clip children, which is how a nominal 32px button can expose only 24px.
    """
    rect = global_rect(widget)
    scroll = _scroll_ancestor(widget)
    parent = widget.parentWidget()
    while parent is not None:
        if scroll is not None and (parent is scroll or parent is scroll.viewport()):
            break
        rect = rect.intersected(global_rect(parent))
        parent = parent.parentWidget()
    return rect


def check_reachability(root: QWidget, audit: Audit, state: str) -> None:
    root_rect = global_rect(root)
    for widget in visible_widgets(root):
        if not _interactive(widget):
            continue
        rect = global_rect(widget)
        if root_rect.contains(rect):
            continue
        scroll = _scroll_ancestor(widget)
        if scroll is not None:
            continue
        audit.add(
            "error", "unreachable", state, widget_path(widget, root),
            f"interactive rect {rect.getRect()} is outside window rect {root_rect.getRect()} "
            "without a scroll ancestor",
        )

    for area in root.findChildren(QAbstractScrollArea):
        if not area.isVisibleTo(root):
            continue
        viewport = area.viewport()
        if isinstance(area, QScrollArea) and area.widget() is not None:
            content = area.widget()
            if content.height() > viewport.height() + 1 and area.verticalScrollBar().maximum() <= 0:
                audit.add(
                    "error", "scroll-unreachable", state, widget_path(area, root),
                    f"content is {content.height()}px high in a {viewport.height()}px viewport "
                    "but the vertical scrollbar cannot move",
                )
            if content.width() > viewport.width() + 1 and area.horizontalScrollBar().maximum() <= 0:
                audit.add(
                    "error", "scroll-unreachable", state, widget_path(area, root),
                    f"content is {content.width()}px wide in a {viewport.width()}px viewport "
                    "but the horizontal scrollbar cannot move",
                )
        if isinstance(area, QTableWidget):
            header_width = sum(area.columnWidth(i) for i in range(area.columnCount()))
            if header_width > viewport.width() + 2 and area.horizontalScrollBar().maximum() <= 0:
                audit.add(
                    "error", "scroll-unreachable", state, widget_path(area, root),
                    f"table columns need {header_width}px in a {viewport.width()}px viewport "
                    "but horizontal scrolling is unavailable",
                )


def check_hit_areas(root: QWidget, audit: Audit, state: str) -> None:
    for widget in visible_widgets(root):
        path = widget_path(widget, root)
        if isinstance(widget, QAbstractButton) and widget.isEnabled():
            hit_rect = effective_visible_rect(widget)
            if hit_rect.width() < MIN_HIT_AREA or hit_rect.height() < MIN_HIT_AREA:
                audit.add(
                    "error", "hit-area", state, path,
                    f"visible hit rect is {hit_rect.width()}x{hit_rect.height()} logical px "
                    f"(widget {widget.width()}x{widget.height()}); minimum practical target "
                    f"is {MIN_HIT_AREA}x{MIN_HIT_AREA}",
                )
        if isinstance(widget, QScrollBar):
            extent = widget.width() if widget.orientation() == Qt.Vertical else widget.height()
            if extent < MIN_SCROLLBAR_EXTENT:
                audit.add(
                    "error", "hit-area", state, path,
                    f"scrollbar hit extent is {extent}px; minimum is {MIN_SCROLLBAR_EXTENT}px",
                )
        if isinstance(widget, QSplitter) and widget.count() > 1:
            if widget.handleWidth() < MIN_SPLITTER_EXTENT:
                audit.add(
                    "error", "hit-area", state, path,
                    f"splitter handle is {widget.handleWidth()}px; minimum is "
                    f"{MIN_SPLITTER_EXTENT}px",
                )


def check_accessibility(root: QWidget, audit: Audit, state: str) -> None:
    for widget in visible_widgets(root):
        path = widget_path(widget, root)
        if isinstance(widget, QPushButton):
            role = str(widget.property("fgRole") or "")
            if role not in ACTION_ROLES:
                audit.add(
                    "error", "semantic-role", state, path,
                    f"application-owned button has fgRole={role!r}; expected one of "
                    f"{sorted(ACTION_ROLES)}",
                )
        if isinstance(widget, QAbstractButton):
            text = _plain_text(widget)
            icon_only = not text or bool(_ICON_ONLY_RE.fullmatch(text))
            if icon_only and not widget.accessibleName().strip():
                audit.add(
                    "error", "accessible-name", state, path,
                    "icon-only button has no accessible name",
                )
        if isinstance(widget, QTableWidget) and not widget.accessibleName().strip():
            audit.add(
                "error", "accessible-name", state, path,
                "data table has no accessible name",
            )
        if isinstance(widget, QLabel):
            role = str(widget.property("fgRole") or widget.property("class") or "")
            looks_like_field_label = role in {"field-label", "field_label"} or _plain_text(widget).endswith(":")
            if looks_like_field_label and widget.buddy() is None:
                audit.add(
                    "error", "label-buddy", state, path,
                    f"field label {_plain_text(widget)!r} is not associated with its control",
                )


def check_fixed_size_risks(root: QWidget, audit: Audit, state: str) -> None:
    for widget in visible_widgets(root):
        if not isinstance(widget, (QLabel, QAbstractButton, QComboBox, QLineEdit)):
            continue
        fixed_width = widget.minimumWidth() > 0 and widget.minimumWidth() == widget.maximumWidth()
        fixed_height = widget.minimumHeight() > 0 and widget.minimumHeight() == widget.maximumHeight()
        if not (fixed_width or fixed_height):
            continue
        text = _plain_text(widget)
        if not text:
            continue
        available = _text_contents_rect(widget)
        measured = _measure_text(widget, text, available)
        risky = ((fixed_width and measured.width() > available.width() + _TEXT_TOLERANCE) or
                 (fixed_height and measured.height() > available.height() + _TEXT_TOLERANCE))
        if risky:
            audit.add(
                "warning", "fixed-size-dpi-risk", state, widget_path(widget, root),
                f"fixed geometry {widget.width()}x{widget.height()} does not contain realized "
                f"text {measured.width()}x{measured.height()}",
            )


def check_inline_styles(root: QWidget, audit: Audit, state: str) -> None:
    for widget in visible_widgets(root):
        stylesheet = widget.styleSheet()
        if not stylesheet or not _VISUAL_STYLE_RE.search(stylesheet):
            continue
        if bool(widget.property("fgInlineStyleAllowed")):
            continue
        audit.add(
            "warning", "inline-visual-style", state, widget_path(widget, root),
            "widget owns visual QSS without fgInlineStyleAllowed=true; prefer fgRole/the theme",
        )


def _image_bytes(image: QImage) -> bytes:
    converted = image.convertToFormat(QImage.Format_RGBA8888)
    return converted.constBits().tobytes()


def check_focus_visibility(root: QWidget, panel: QWidget, app: QApplication,
                           audit: Audit, state: str) -> None:
    candidates = [w for w in panel.findChildren(QWidget)
                  if w.isVisibleTo(panel) and w.isEnabled() and _interactive(w) and
                  w.focusPolicy() != Qt.NoFocus and w.width() >= MIN_HIT_AREA and
                  w.height() >= MIN_HIT_AREA]
    if not candidates:
        return
    target = candidates[0]
    target.clearFocus()
    app.processEvents()
    before = _image_bytes(target.grab().toImage())
    target.setFocus(Qt.TabFocusReason)
    app.processEvents()
    after = _image_bytes(target.grab().toImage())
    changed = sum(a != b for a, b in zip(before, after))
    if changed < 12:
        audit.add(
            "warning", "focus-visibility", state, widget_path(target, root),
            "keyboard focus changed fewer than 12 image bytes; verify the focus treatment",
        )


def audit_state(root: QWidget, panel: QWidget, app: QApplication, audit: Audit,
                state: str, artifacts_dir: Path | None = None) -> None:
    before = audit.error_count
    check_sibling_overlaps(root, audit, state)
    check_realized_text(root, audit, state)
    check_headers(root, audit, state)
    check_reachability(root, audit, state)
    check_hit_areas(root, audit, state)
    check_accessibility(root, audit, state)
    check_fixed_size_risks(root, audit, state)
    check_inline_styles(root, audit, state)
    check_focus_visibility(root, panel, app, audit, state)
    state_errors = audit.error_count - before
    record = {"name": state, "errors": state_errors}
    if state_errors and artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", state).strip("_")
        screenshot = artifacts_dir / f"{safe_name}.png"
        if not root.grab().save(str(screenshot), "PNG"):
            audit.add("warning", "artifact-write", state, widget_path(root, root),
                      f"could not save failure screenshot to {screenshot}")
        else:
            record["screenshot"] = str(screenshot)
    audit.states.append(record)


def _make_result(name: str, outcome: str):
    from frameguard.core import QAResult

    result = QAResult("C:/FrameGuard QA/Very Long Delivery Path/" + name)
    result.set_metadata("video_codec", "ProRes 422 HQ")
    result.set_metadata("resolution", "7680x4320")
    result.set_metadata("duration_display", "12:34:56")
    result.set_metadata("file_size_mb", 12345.6)
    result.add_pass("Container Format", "QuickTime/MOV accepted")
    result.add_pass("Audio Codec", "Linear PCM accepted")
    result.add_pass("Resolution", "7680x4320 delivery raster")
    if outcome != "pass":
        result.add_warn("Black Frames", "12 sequences detected", confidence=60)
    if outcome == "fail":
        result.add_fail("Bitrate", "12.5 Mbps is below the 80 Mbps minimum", confidence=95)
    result.finalize()
    return result


def _isolate_qsettings(gui_module, directory: Path) -> None:
    ini = str(directory / "audit-settings.ini")

    class AuditQSettings(QSettings):
        def __init__(self, *args, **kwargs):
            super().__init__(ini, QSettings.IniFormat)

    gui_module.QSettings = AuditQSettings


def _settle(app: QApplication, rounds: int = 6) -> None:
    for _ in range(rounds):
        app.processEvents()


def _theme_names(theme_mode: str, available: Iterable[str]) -> list[str]:
    names = list(available)
    if theme_mode == "all":
        return names
    preferred = [name for name in ("Meta Light", "Clean Light", "Meta Dark") if name in names]
    # One representative light and Meta Dark are the key full-matrix themes.
    light = next((name for name in preferred if "Light" in name), None)
    selected = [name for name in (light, "Meta Dark") if name and name in names]
    return list(dict.fromkeys(selected or names[:2]))


def _parse_sizes(raw: str) -> tuple[tuple[int, int], ...]:
    if raw == "all":
        return SIZES
    sizes = []
    for item in raw.split(","):
        match = re.fullmatch(r"(\d+)x(\d+)", item.strip())
        if not match:
            raise ValueError(f"invalid size {item!r}; expected WIDTHxHEIGHT")
        sizes.append((int(match.group(1)), int(match.group(2))))
    return tuple(sizes)


def run_worker(args: argparse.Namespace) -> int:
    scale = float(args.scale)
    with tempfile.TemporaryDirectory(prefix="frameguard-gui-audit-") as temp:
        isolated_home = Path(temp)
        os.environ["HOME"] = str(isolated_home)
        os.environ["USERPROFILE"] = str(isolated_home)

        app = QApplication.instance() or QApplication([sys.argv[0]])
        app.setQuitOnLastWindowClosed(False)

        from frameguard import gui as gui_module
        from frameguard.theme import THEMES

        _isolate_qsettings(gui_module, isolated_home)
        audit = Audit()
        window = gui_module.MainWindow()
        if getattr(window, "_watch_monitor", None) is not None:
            window._watch_monitor.stop()

        results = [
            _make_result(
                f"SHOW_EP{i:02}_SEQUENCE_WITH_A_DELIBERATELY_LONG_MASTER_FILENAME_v123.mov",
                ("pass", "warn", "fail")[i % 3],
            )
            for i in range(12)
        ]
        window.results_panel.set_results(results, append=True)
        window.checks_panel.set_results(window.results_panel._results)
        window.show()
        _settle(app)

        panels = (
            (gui_module.PANEL_SCAN, "Scan"),
            (gui_module.PANEL_RESULTS, "Results"),
            (gui_module.PANEL_CHECKS, "Checks"),
            (gui_module.PANEL_WATCH, "Watch"),
            (gui_module.PANEL_SETTINGS, "Settings"),
        )
        themes = _theme_names(args.themes, THEMES)
        sizes = _parse_sizes(args.sizes)
        artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else None

        for theme_name in themes:
            window._apply_theme(theme_name)
            for width, height in sizes:
                window.resize(width, height)
                _settle(app)
                for index, panel_name in panels:
                    window._switch_panel(index)
                    _settle(app)
                    panel = window.stack.widget(index)
                    state = f"{theme_name}-{panel_name}-{width}x{height}-scale-{scale:g}"
                    audit_state(window, panel, app, audit, state, artifacts_dir)

        report = audit.report(scale)
        if args.json:
            report_path = Path(args.json)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
        _print_report(report, args.max_per_category)

        window._cleanup_worker()
        if getattr(window, "_watch_monitor", None) is not None:
            window._watch_monitor.stop()
        window.hide()
        window.deleteLater()
        _settle(app, 2)
        return 1 if audit.error_count else 0


def _print_report(report: dict, max_per_category: int) -> None:
    summary = report["summary"]
    print("=" * 76)
    print(f"FrameGuard rendered-state audit | scale {report['scale_factor']:g} | "
          f"{report['platform']} | {summary['states']} states")
    print("=" * 76)
    grouped: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for finding in report["findings"]:
        grouped[(finding["severity"], finding["category"])].append(finding)
    order = {"error": 0, "warning": 1}
    for key in sorted(grouped, key=lambda value: (order[value[0]], value[1])):
        severity, category = key
        findings = grouped[key]
        print(f"\n[{severity.upper()}] {category} ({len(findings)})")
        for finding in findings[:max_per_category]:
            print(f"  {finding['state']} | {finding['widget']}: {finding['message']}")
        if len(findings) > max_per_category:
            print(f"  ... {len(findings) - max_per_category} more; see JSON report")
    print(f"\nAUDIT RESULT: {summary['errors']} error(s), "
          f"{summary['warnings']} warning(s)")


def run_matrix(args: argparse.Namespace) -> int:
    json_dir = Path(args.json_dir)
    json_dir.mkdir(parents=True, exist_ok=True)
    artifact_root = Path(args.artifacts_dir) if args.artifacts_dir else json_dir / "screenshots"
    reports = []
    return_codes = []
    for scale in SCALES:
        tag = str(scale).replace(".", "_")
        report_path = json_dir / f"gui-audit-scale-{tag}.json"
        screenshot_dir = artifact_root / f"scale-{tag}"
        command = [
            sys.executable, str(Path(__file__).resolve()),
            "--worker", "--scale", str(scale),
            "--sizes", args.sizes, "--themes", args.themes,
            "--json", str(report_path),
            "--artifacts-dir", str(screenshot_dir),
            "--max-per-category", str(args.max_per_category),
        ]
        environment = os.environ.copy()
        environment["QT_SCALE_FACTOR"] = str(scale)
        environment["QT_QPA_PLATFORM"] = environment.get("QT_QPA_PLATFORM", "offscreen")
        completed = subprocess.run(command, env=environment, check=False)
        return_codes.append(completed.returncode)
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            reports.append({
                "scale_factor": report.get("scale_factor", scale),
                "file": report_path.name,
                "summary": report.get("summary", {}),
            })

    combined = {
        "schema_version": 1,
        "tool": "FrameGuard rendered-state audit matrix",
        "scales": list(SCALES),
        "reports": reports,
        "summary": {
            "workers": len(SCALES),
            "completed_reports": len(reports),
            "errors": sum(report["summary"].get("errors", 0) for report in reports),
            "warnings": sum(report["summary"].get("warnings", 0) for report in reports),
            "worker_return_codes": return_codes,
        },
    }
    combined_path = json_dir / "gui-audit-matrix.json"
    combined_path.write_text(json.dumps(combined, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    if len(reports) != len(SCALES):
        print(f"Audit matrix incomplete: {len(reports)} of {len(SCALES)} reports written",
              file=sys.stderr)
        return 2
    return 1 if any(code != 0 for code in return_codes) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--matrix", action="store_true",
                      help="run all supported scale factors in fresh subprocesses")
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scale", type=float,
                        default=float(os.environ.get("QT_SCALE_FACTOR", "1")))
    parser.add_argument("--sizes", default="all",
                        help="all or a comma-separated WIDTHxHEIGHT list")
    parser.add_argument("--themes", choices=("key", "all"), default="key")
    parser.add_argument("--json", help="write this worker's JSON report")
    parser.add_argument("--json-dir", default="gui-audit-artifacts",
                        help="matrix output directory")
    parser.add_argument("--artifacts-dir",
                        help="directory for screenshots of failing states")
    parser.add_argument("--max-per-category", type=int, default=6)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.matrix:
        return run_matrix(args)
    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
