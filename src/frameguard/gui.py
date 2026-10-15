"""
FrameGuard Desktop -- PySide6 GUI Application.
Dark-themed video QA tool with drag-drop, real-time progress, and PDF export.

Recent changes:
  - Complete UI overhaul: modern design language
  - 15 color themes, added Pearl and Glacier
  - Pill-shaped buttons, thin progress bars, card-based panels
  - Sidebar left-accent-bar navigation indicators
  - 8px grid spacing, 22px section headers, generous whitespace
  - Theme-aware PDF reports

v0.4.4 changes:
  - Custom frameless title bar: seamlessly integrated, theme-aware
  - Title bar has minimize/maximize/close buttons matching selected theme
  - Drag-to-move and double-click-to-maximize on title bar
  - PDF exports default to Documents/FrameGuard folder
  - Documents/FrameGuard folder created on first launch
  - Build script: winget/choco FFmpeg install before direct download fallback
  - Installer: Desktop + Start Menu shortcut prompts

v0.4.2 changes:
  - Full theme-awareness: every visible pixel updates on theme change
  - Tool status bar in Settings is now fully theme-aware
  - All inline styles replaced with live theme lookups
  - apply_theme() cascaded to all panels and child widgets
  - Performance optimizations in core engine (filter threading, decimation)

v0.4.0 changes:
  - File queue list replaces drop zone after files added
  - Combined Browse button (files + folders)
  - Entire drop zone is clickable (opens browse dialog)
  - Profile dropdown has visible arrow symbol
  - Results summary click-to-expand popup with full details
  - Hint text above chart for summary popup
  - Horizontal scroll on summary section
  - Double-click detail popup with larger font
  - MediaConch/MediaInfo install buttons in Settings
  - General tab scrollable with proper spacing
  - About section scroll bar
  - Theme color preview squares in dropdown
  - Logo recoloring to match theme
  - Theme persistence across sessions (QSettings)
"""

import sys
import os
import json
from pathlib import Path
import shutil
import subprocess
from typing import List, Optional
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QSystemTrayIcon, QFileDialog, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QCheckBox, QGroupBox, QMessageBox, QTextEdit, QFrame, QSplitter,
    QAbstractItemView, QScrollArea, QSpinBox, QSlider, QGridLayout,
    QListWidget, QListWidgetItem, QDialog, QSizeGrip, QStyledItemDelegate,
    QSizePolicy, QProgressDialog, QLineEdit
)
from PySide6.QtCore import (
    Qt, QObject, QThread, QRunnable, QThreadPool,
    Signal, QSize, QTimer, QSettings, QRect, QRectF, QEvent,
    QPropertyAnimation, QEasingCurve
)
from PySide6.QtGui import (
    QDragEnterEvent, QDropEvent, QFont, QColor, QIcon, QPixmap,
    QPainter, QPen, QBrush, QMouseEvent, QCursor, QFontMetrics
)

from frameguard.core import (
    PRESETS, DEFAULT_CONFIG, QAResult, check_ffmpeg_available
)
from frameguard.worker import ScanWorker
from frameguard.presets import (
    export_preset, list_available_presets,
    save_imported_preset, is_preset_file, FG_EXTENSION,
    get_presets_dir, get_user_presets_dir
)
from frameguard.watchfolder import WatchFolderMonitor
from frameguard.updater import UpdateManager
from frameguard import design as D
from frameguard.theme import (
    generate_stylesheet, get_theme, get_theme_names, set_current_theme,
    THEMES, ThemeColors
)

from frameguard import __version__ as APP_VERSION

# Settings key for theme persistence
_SETTINGS_ORG = "FrameGuard"
_SETTINGS_APP = "FrameGuardDesktop"

# Windows gets a frameless window with our own title bar (and a hand-rolled
# Aero Snap implementation in MainWindow.nativeEvent). macOS keeps its native
# chrome: dropping it there costs the traffic lights, the window shadow,
# full-edge resize and green-button fullscreen, none of which we reimplement.
USE_CUSTOM_CHROME = sys.platform == "win32"



def _style_combobox_popup(combo, theme):
    """Force QComboBox popup to use theme colors on Windows.
    
    Windows renders combo popups as native widgets that ignore Qt stylesheets.
    We use BOTH QPalette AND inline stylesheet on the view for maximum compatibility.
    """
    from PySide6.QtWidgets import QListView
    from PySide6.QtGui import QPalette, QColor
    
    # Force Qt-drawn popup (not native Windows popup)
    if not isinstance(combo.view(), QListView):
        combo.setView(QListView())
    
    view = combo.view()
    
    # Method 1: Inline stylesheet on the view (highest priority, always wins)
    view.setStyleSheet(
        f"QListView {{"
        f"  background-color: {theme.bg_secondary};"
        f"  color: {theme.text_primary};"
        f"  border: 1px solid {theme.border};"
        f"  outline: none;"
        f"}}"
        f"QListView::item {{"
        f"  padding: 8px 12px;"
        f"  min-height: 24px;"
        f"}}"
        f"QListView::item:hover {{"
        f"  background-color: {theme.surface_hover};"
        f"}}"
        f"QListView::item:selected {{"
        f"  background-color: {theme.accent};"
        f"  color: #FFFFFF;"
        f"}}"
    )
    
    # Method 2: QPalette as fallback (some Qt builds ignore inline styles on popup)
    palette = view.palette()
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.bg_secondary))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text_primary))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.bg_secondary))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text_primary))
    view.setPalette(palette)
    
    # Method 3: Inline stylesheet on the combo closed state too
    combo.setStyleSheet(
        f"QComboBox {{"
        f"  background-color: {theme.bg_tertiary};"
        f"  color: {theme.text_primary};"
        f"  border: 1px solid {theme.border};"
        f"  border-radius: 6px;"
        f"  padding: 8px 12px;"
        f"  min-height: 30px;"
        f"}}"
        f"QComboBox:hover {{"
        f"  border-color: {theme.accent};"
        f"}}"
        f"QComboBox::drop-down {{"
        f"  width: 36px;"
        f"  border: none;"
        f"  border-left: 1px solid {theme.border};"
        f"}}"
    )


def _resource_path(relative_path: str) -> str:
    """Resolve asset paths for both development and PyInstaller frozen mode."""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


_ASSET_DIR = _resource_path("assets")
_LOGO_PATH = os.path.join(_ASSET_DIR, "frameguard_logo.png")
_ICON_PATH = os.path.join(_ASSET_DIR, "frameguard.ico")


def _tint_pixmap(pixmap: QPixmap, color: QColor) -> QPixmap:
    """Tint a pixmap to match the given color (for logo recoloring)."""
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.transparent)
    painter = QPainter(tinted)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
    painter.fillRect(tinted.rect(), color)
    painter.end()
    return tinted


def _colorize_pixmap(pixmap: QPixmap, color: QColor) -> QPixmap:
    """Recolour artwork to `color` while keeping its internal shading.

    _tint_pixmap flattens everything to one flat colour, which is fine for a
    16px tray badge but destroys the logo's structure (film strip, play mark,
    shield layers) at hero size. This converts to greyscale to preserve the
    light/dark detail, multiplies by the target colour, then restores the
    original alpha so the silhouette is unchanged.

    Composition modes do the work, so this stays fast enough to run on every
    theme switch.
    """
    from PySide6.QtGui import QImage

    source = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)

    # Greyscale, but lifted: multiplying by the accent darkens everything, so
    # start brighter or the result turns muddy.
    grey = source.convertToFormat(QImage.Format_Grayscale8) \
                 .convertToFormat(QImage.Format_ARGB32)

    out = QPixmap(pixmap.size())
    out.fill(Qt.transparent)

    painter = QPainter(out)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.drawImage(0, 0, grey)

    # Lighten first so mid-tones survive the multiply.
    painter.setCompositionMode(QPainter.CompositionMode_Screen)
    painter.fillRect(out.rect(), QColor(90, 90, 90))

    painter.setCompositionMode(QPainter.CompositionMode_Multiply)
    painter.fillRect(out.rect(), color)

    # Grayscale conversion drops alpha; put the original silhouette back.
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return out


def _hex_to_rgb(hex_color: str) -> str:
    """Convert hex color to 'r, g, b' string for rgba()."""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        return f"{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}"
    return "128, 128, 128"


def apply_responsive_margins(widget, width: int):
    """Scale a panel's outer gutter to the space actually available.

    A fixed 32px gutter costs 64px of a ~710px content area on a non-maximized
    window — noticeable crowding that disappears when maximized, which is
    exactly the symptom users report.
    """
    layout = widget.layout()
    if layout is None:
        return
    h = D.page_margins(width)
    v = max(D.SPACE_4, h - D.SPACE_2)
    current = layout.contentsMargins()
    if current.left() != h or current.top() != v:
        layout.setContentsMargins(h, v, h, v)


def _fps_text(fps: float) -> str:
    """Format an fps value the way the fps combo box labels it ('30', '29.97')."""
    fps = float(fps)
    return str(int(fps)) if fps == int(fps) else str(fps)


_UI_FONT_FAMILY = None
_MONO_FONT_FAMILY = None

# Preference order for the UI face. Each is tried in turn and only accepted if
# the system actually resolves it - asking for a missing family silently gives
# you a substitute with different metrics, so the request has to be verified.
#
# Segoe UI Variable is Windows 11's refined system face. "Text" is its
# optical-size instance tuned for body copy, which is noticeably cleaner than
# plain Segoe UI at 12-14px, where most of this UI lives.
_UI_FONT_CANDIDATES = {
    "win32": ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI",
              "Selawik", "Tahoma"],
    "darwin": ["SF Pro Text", "Helvetica Neue", "Helvetica"],
    "default": ["Inter", "Roboto", "Cantarell", "DejaVu Sans"],
}

# Numeric columns (timecodes, resolutions, bitrates) need TABULAR figures -
# digits of equal width - or values fail to line up down a column and the
# table becomes hard to scan. Proportional digits are the reason "00:01:30"
# and "00:11:02" don't align in most UI fonts.
_MONO_FONT_CANDIDATES = {
    "win32": ["Cascadia Mono", "Consolas", "Segoe UI Mono", "Courier New"],
    "darwin": ["SF Mono", "Menlo", "Monaco"],
    "default": ["JetBrains Mono", "Roboto Mono", "DejaVu Sans Mono", "monospace"],
}


def _resolve_family(candidates: list, fallback_generic) -> str:
    """First candidate the system genuinely has, else a sane generic.

    QFontInfo reports the family actually used after substitution, so it is the
    only reliable way to tell "this font exists" from "Qt quietly gave me
    something else".
    """
    from PySide6.QtGui import QFontDatabase, QFontInfo

    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
        # Variable-font named instances are not always listed as families but
        # still resolve, so confirm via QFontInfo before rejecting.
        probe = QFont(name)
        if QFontInfo(probe).family() == name:
            return name

    return QFontDatabase.systemFont(fallback_generic).family()


def ui_font(size: int, weight=None) -> QFont:
    """A QFont in the platform's UI face, with tabular-safe defaults.

    "Segoe UI" does not exist on macOS or Linux; Qt silently substitutes
    something with different metrics, so weights and spacing drift. Resolve the
    real system UI family once and reuse it.
    """
    global _UI_FONT_FAMILY
    if _UI_FONT_FAMILY is None:
        from PySide6.QtGui import QFontDatabase
        key = sys.platform if sys.platform in _UI_FONT_CANDIDATES else "default"
        _UI_FONT_FAMILY = _resolve_family(
            _UI_FONT_CANDIDATES[key], QFontDatabase.GeneralFont)

    font = QFont(_UI_FONT_FAMILY, size)
    if weight is not None:
        font.setWeight(weight)
    return font


def mono_font(size: int, weight=None) -> QFont:
    """A font with tabular figures, for columns of numbers."""
    global _MONO_FONT_FAMILY
    if _MONO_FONT_FAMILY is None:
        from PySide6.QtGui import QFontDatabase
        key = sys.platform if sys.platform in _MONO_FONT_CANDIDATES else "default"
        _MONO_FONT_FAMILY = _resolve_family(
            _MONO_FONT_CANDIDATES[key], QFontDatabase.FixedFont)

    font = QFont(_MONO_FONT_FAMILY, size)
    font.setStyleHint(QFont.Monospace)
    if weight is not None:
        font.setWeight(weight)
    return font


def resolved_font_families() -> tuple:
    """(ui, mono) families actually in use - for the About/report dialogs."""
    ui_font(10)
    mono_font(10)
    return _UI_FONT_FAMILY, _MONO_FONT_FAMILY


def _open_path(path: str) -> bool:
    """Open a file or folder with the OS default handler.

    Returns True when a handler was launched. Callers (open_in_player's
    fallback) need this: the old version swallowed every OSError, so a dead
    file association read as a successful open.
    """
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - documented Windows API
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        return False
    return True


def _crash_log_dir() -> str:
    """Where main.py writes crash and error logs.

    Mirrors main._log_dir(); duplicated rather than imported because main.py is
    the entry script and importing it from a package module would re-run it.
    """
    if sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Logs", "FrameGuard")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "FrameGuard", "Logs")
    else:
        path = os.path.join(os.path.expanduser("~"), ".local", "share", "FrameGuard", "logs")
    return path


def _reveal_in_file_manager(path: str):
    """Show a file in Explorer/Finder with it selected."""
    try:
        if sys.platform == "win32":
            # /select, requires a backslash path and no shell quoting
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except OSError:
        pass


_vlc_path_cache = []


def find_vlc() -> Optional[str]:
    """Locate the VLC executable, or None if it isn't installed.

    Only a positive hit is cached: caching the miss pinned "not found" for
    the rest of the session, so installing VLC mid-session (or fixing PATH)
    never took effect until restart.
    """
    if _vlc_path_cache:
        return _vlc_path_cache[0] or None

    found = None
    if sys.platform == "win32":
        # The App Paths key is the authoritative location and survives
        # non-default install directories. HKCU has to be checked too: a
        # per-user VLC install writes there and not to HKLM, so a machine with
        # VLC installed still reported "not found".
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vlc.exe",
                    ) as key:
                        candidate = winreg.QueryValueEx(key, "")[0]
                        if candidate and os.path.isfile(candidate):
                            found = candidate
                            break
                except OSError:
                    continue
        except ImportError:
            pass
        if not found:
            for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                         os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         os.environ.get("LOCALAPPDATA", "")):
                if not base:
                    continue
                candidate = os.path.join(base, "VideoLAN", "VLC", "vlc.exe")
                if os.path.isfile(candidate):
                    found = candidate
                    break
    elif sys.platform == "darwin":
        candidate = "/Applications/VLC.app/Contents/MacOS/VLC"
        if os.path.isfile(candidate):
            found = candidate

    if not found:
        found = shutil.which("vlc")

    if found:
        # A stale negative sticks around from before the fix; replace it.
        _vlc_path_cache.clear()
        _vlc_path_cache.append(found)
        return found
    return None


def _player_env() -> dict:
    """Environment for launching an external player from a frozen build.

    PyInstaller points the dynamic loader at its own bundled Qt/ICU libraries.
    VLC inherits that and can fail to start, or start against the wrong libs,
    which is one way "Open in VLC" appears to do nothing at all.
    """
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            original = env.pop(f"{var}_ORIG", None)
            if original is not None:
                env[var] = original
            else:
                env.pop(var, None)
        # The bundled-Qt directory must also leave PATH: Windows resolves
        # VLC's DLLs against it first, so VLC starts against our Qt/ICU and
        # dies before showing a window.
        bundle_dirs = set()
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundle_dirs.add(os.path.normcase(os.path.abspath(meipass)))
            bundle_dirs.add(os.path.normcase(os.path.abspath(
                os.path.join(meipass, "_internal"))))
        try:
            exe_dir = os.path.normcase(os.path.abspath(
                os.path.dirname(sys.executable)))
            bundle_dirs.add(exe_dir)
        except Exception:
            pass
        path_sep = os.pathsep
        path_val = env.get("PATH", "")
        if path_val:
            kept = [p for p in path_val.split(path_sep)
                    if os.path.normcase(os.path.abspath(p)) not in bundle_dirs]
            if len(kept) != len(path_val.split(path_sep)):
                env["PATH"] = path_sep.join(kept)
    return env


def open_in_player(path: str) -> str:
    """Open a video in VLC, falling back to the system default player.

    Returns "vlc", "default", "missing" or "failed", for status messaging.
    Callers must surface anything other than "vlc": silently substituting the
    default player is why operators reported that VLC "did not open".
    Leadership specifically asked for a hand-off to VLC rather than an in-app
    preview.
    """
    if not os.path.isfile(path):
        return "missing"

    vlc = find_vlc()
    if vlc:
        try:
            proc = subprocess.Popen([vlc, path], env=_player_env())
            # Popen succeeding only means the process spawned. VLC can still
            # die instantly (wrong DLLs, broken install); treat that as "VLC
            # did not open" and fall back instead of reporting success.
            try:
                if proc.poll() is not None:
                    raise OSError(f"VLC exited immediately (code {proc.poll()})")
            except OSError:
                raise
            except Exception:
                pass
            return "vlc"
        except OSError:
            pass
    try:
        if not _open_path(path):
            return "failed"
    except OSError:
        return "failed"
    return "default"


# Engine config keys that the Settings UI has a widget for. Preset keys outside
# this set (accepted codecs/containers, sample rate, CFR...) have no control yet,
# so they are carried through to the engine untouched via SettingsPanel._preset_extras.
_UI_BACKED_CONFIG_KEYS = frozenset({
    "target_width", "target_height", "target_fps", "fps_tolerance",
    "min_bitrate_mbps", "audio_required", "silence_duration",
    "use_hwaccel", "segment_enabled", "segment_count",
})

# Upper bound on results kept in memory / rendered in the table.
MAX_RETAINED_RESULTS = 500

# User-facing wording for each internal QAResult.status value.
#
# The internal values stay as-is ("MISMATCH") because core logic and the
# tests key off them; only what the operator reads changes.
# Leadership found "MISMATCH" too soft for a sign-off tool.
STATUS_LABELS = {
    "PASS": "PASS",
    "PASS_WITH_WARNINGS": "WARNING",
    "MISMATCH": "FAIL",
}

# Sort weight: failures first, then warnings, then clean files.
STATUS_PRIORITY = {"MISMATCH": 0, "PASS_WITH_WARNINGS": 1, "PASS": 2}


def _result_severity(result) -> float:
    """Severity score for sort order: cached finalize() value, else computed.

    A corrupted file (weight ~100) sorts above a framerate-only file
    (weight ~20) within the same MISMATCH bucket.
    """
    score = getattr(result, "severity_score", None)
    if isinstance(score, (int, float)):
        return float(score)
    try:
        from frameguard.core import result_severity as _rs
        return float(_rs(result)[0])
    except Exception:
        return 0.0


def _severity_key(result) -> tuple:
    """Sort key: status bucket first, then severity, then filename."""
    return (STATUS_PRIORITY.get(_result_status(result), 3),
            -_result_severity(result),
            (result.filename or "").lower())

# Sidebar / QStackedWidget panel indices. Named because inserting a panel
# silently shifts every hardcoded index that refers to a later one.
PANEL_SCAN = 0
PANEL_RESULTS = 1
PANEL_CHECKS = 2
PANEL_WATCH = 3
PANEL_SETTINGS = 4

# Wording for an individual check's status (as recorded by add_pass/add_fail/
# add_warn/add_skip), as opposed to a file's overall status above.
CHECK_STATUS_LABELS = {
    "PASS": "PASS",
    "FAIL": "FAIL",
    "WARN": "WARN",
    "SKIP": "SKIP",
}


def check_status_colors(theme) -> dict:
    return {
        "PASS": theme.status_pass,
        "FAIL": theme.status_fail,
        "WARN": theme.status_warn,
        "SKIP": theme.status_skip,
    }


def status_label(status: str) -> str:
    """Operator-facing wording for an internal status value."""
    return STATUS_LABELS.get(status, str(status or "UNKNOWN"))


def _result_status(result) -> str:
    """Internal status for a result, tolerating one that was never finalized."""
    status = getattr(result, "status", None)
    if status:
        return status
    if not result.passed:
        return "MISMATCH"
    if result.warnings:
        # Strict mode escalates any warning to a failure, mirroring
        # QAResult.overall_status(). Without this an unfinalized strict
        # result briefly read as WARNING before finalize() ran.
        if getattr(result, "strict", False):
            return "MISMATCH"
        return "PASS_WITH_WARNINGS"
    return "PASS"


def _status_display(result, theme) -> tuple:
    """Return (label, colour) for a result.

    Derived from ``QAResult.status`` so table rows, badge counts, the summary
    popup and the watch log can never disagree about the same scan.
    """
    status = _result_status(result)
    colour = {
        "PASS": theme.status_pass,
        "PASS_WITH_WARNINGS": theme.status_warn,
        "MISMATCH": theme.status_fail,
    }.get(status, theme.text_secondary)
    return status_label(status), colour


# ============================================================================
# CUSTOM TITLE BAR (frameless window chrome)
# ============================================================================

class TitleBar(QWidget):
    """Custom title bar that replaces the default OS window chrome.

    Provides minimize, maximize/restore, and close buttons that are fully
    theme-aware.  Supports drag-to-move and double-click-to-maximize.
    Follows the proven pattern from PythonGUIs / Qt community best practices
    (FramelessWindowHint + startSystemMove).
    """

    def __init__(self, parent: QMainWindow):
        super().__init__(parent)
        self._window = parent
        self._is_maximized = False
        self.setFixedHeight(D.TITLEBAR_H)
        self.setObjectName("custom_title_bar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(0)

        # App icon (small)
        self._icon_label = QLabel()
        if os.path.isfile(_ICON_PATH):
            pix = QPixmap(_ICON_PATH).scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._icon_label.setPixmap(pix)
        elif os.path.isfile(_LOGO_PATH):
            pix = QPixmap(_LOGO_PATH).scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._icon_label.setPixmap(pix)
        self._icon_label.setFixedSize(20, 20)
        layout.addWidget(self._icon_label)

        layout.addSpacing(6)

        # Title text
        self._title_label = QLabel(f"FrameGuard Desktop v{APP_VERSION}")
        self._title_label.setObjectName("title_bar_label")
        layout.addWidget(self._title_label)

        layout.addStretch()

        # Window control buttons
        btn_size = QSize(40, 28)

        self._btn_minimize = QPushButton("\u2013")  # en-dash
        self._btn_minimize.setObjectName("title_btn")
        self._btn_minimize.setFixedSize(btn_size)
        self._btn_minimize.setToolTip("Minimize")
        self._btn_minimize.clicked.connect(self._on_minimize)
        layout.addWidget(self._btn_minimize)

        self._btn_maximize = QPushButton("\u25A1")  # white square
        self._btn_maximize.setObjectName("title_btn")
        self._btn_maximize.setFixedSize(btn_size)
        self._btn_maximize.setToolTip("Maximize")
        self._btn_maximize.clicked.connect(self._on_maximize_restore)
        layout.addWidget(self._btn_maximize)

        self._btn_close = QPushButton("\u2715")  # multiplication X
        self._btn_close.setObjectName("title_btn_close")
        self._btn_close.setFixedSize(btn_size)
        self._btn_close.setToolTip("Close")
        self._btn_close.clicked.connect(self._window.close)
        layout.addWidget(self._btn_close)

        # Apply initial theme
        t = get_theme()
        self.apply_theme(t)

    # ---- Theme ----
    def apply_theme(self, t: ThemeColors):
        """Update all inline styles to match the current theme."""
        self.setStyleSheet(
            f"QWidget#custom_title_bar {{ background-color: {t.bg_primary}; "
            f"border-bottom: 1px solid {t.border}; }}"
        )
        self._title_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 12px; font-weight: normal; "
            f"background: transparent; border: none;"
        )
        btn_style = (
            f"QPushButton#title_btn {{ background: transparent; border: none; "
            f"color: {t.text_secondary}; font-size: 14px; border-radius: 0px; }}"
            f"QPushButton#title_btn:hover {{ background-color: {t.surface}; "
            f"color: {t.text_primary}; }}"
        )
        self._btn_minimize.setStyleSheet(btn_style)
        self._btn_maximize.setStyleSheet(btn_style)
        self._btn_close.setStyleSheet(
            f"QPushButton#title_btn_close {{ background: transparent; border: none; "
            f"color: {t.text_secondary}; font-size: 14px; border-radius: 0px; }}"
            f"QPushButton#title_btn_close:hover {{ background-color: #E81123; "
            f"color: #FFFFFF; }}"
        )

    def set_icon_pixmap(self, pixmap: QPixmap):
        """Swap the title bar mark, e.g. when the theme accent changes."""
        if pixmap and not pixmap.isNull():
            self._icon_label.setPixmap(
                pixmap.scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ---- Window controls ----
    def _on_minimize(self):
        self._window.showMinimized()

    def _on_maximize_restore(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self.sync_maximize_button()

    def sync_maximize_button(self):
        """Match the glyph to the real window state.

        Aero Snap, Win+Arrow and double-click all change state without going
        through the button, so this is driven from the window's changeEvent.
        """
        if self._window.isMaximized():
            self._btn_maximize.setText("\u2752")  # overlapping squares
            self._btn_maximize.setToolTip("Restore Down")
        else:
            self._btn_maximize.setText("\u25A1")
            self._btn_maximize.setToolTip("Maximize")

    def is_draggable_point(self, window_pos) -> bool:
        """Whether a window-relative point counts as caption (drag) area.

        Excludes the min/max/close buttons — reporting HTCAPTION over them
        would make Windows swallow the click and they'd stop working.
        """
        local = self.mapFrom(self._window, window_pos)
        if not self.rect().contains(local):
            return False
        for btn in (self._btn_minimize, self._btn_maximize, self._btn_close):
            if btn.geometry().contains(local):
                return False
        return True

    # ---- Drag-to-move ----
    def mousePressEvent(self, event: QMouseEvent):
        # On Windows the drag is handled natively via HTCAPTION (see
        # MainWindow.nativeEvent), which is what gives us Aero Snap.
        # startSystemMove is the cross-platform fallback.
        if event.button() == Qt.LeftButton and sys.platform != "win32":
            handle = self._window.windowHandle()
            if handle:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._on_maximize_restore()
        super().mouseDoubleClickEvent(event)


# ============================================================================
# THEME COLOR PREVIEW DELEGATE (for combo box)
# ============================================================================

class ThemePreviewDelegate(QStyledItemDelegate):
    """Custom delegate that draws 3 color squares next to theme name in dropdown."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        theme_name = index.data(Qt.DisplayRole)
        if theme_name and theme_name in THEMES:
            theme = THEMES[theme_name]
            colors = [theme.accent, theme.bg_primary, theme.text_primary]
            sq_size = 12
            spacing = 3
            total_w = len(colors) * (sq_size + spacing)
            x_start = option.rect.right() - total_w - 8
            y_center = option.rect.center().y() - sq_size // 2

            painter.save()
            for i, hex_color in enumerate(colors):
                rect = QRect(x_start + i * (sq_size + spacing), y_center, sq_size, sq_size)
                painter.setPen(QPen(QColor("#555555"), 1))
                painter.setBrush(QBrush(QColor(hex_color)))
                painter.drawRoundedRect(rect, 2, 2)
            painter.restore()

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setWidth(hint.width() + 60)  # Extra space for color squares
        return hint


# ============================================================================
# DROP ZONE WIDGET (clickable, shows file queue)
# ============================================================================

class _QueueList(QListWidget):
    """The queued-files list inside the drop zone.

    The list used to set ``WA_TransparentForMouseEvents`` on its viewport so
    that clicking it still opened the browse dialog. That also threw away
    wheel events, so the only way to scroll the queue was to drag the
    scrollbar — which is exactly what operators reported.

    Handling the press here instead keeps click-to-browse while leaving the
    wheel to QListWidget's own handler.

    The signal is deliberately not named ``clicked``: QAbstractItemView
    already defines ``clicked(QModelIndex)`` and shadowing it would break
    every inherited user of that signal.
    """

    pressed_anywhere = Signal()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.pressed_anywhere.emit()
        event.accept()


class DropZone(QFrame):
    """Drag-and-drop zone for video files and folders. Clickable to browse."""
    files_dropped = Signal(list)
    preset_dropped = Signal(str)  # Emits path to .fg preset file
    browse_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drop_zone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(200)
        self.setCursor(Qt.PointingHandCursor)

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(D.SPACE_5, D.SPACE_5, D.SPACE_5, D.SPACE_5)
        self._main_layout.setSpacing(D.SPACE_2)

        # Empty state (icon + text)
        self._empty_widget = QWidget()
        empty_layout = QVBoxLayout(self._empty_widget)
        empty_layout.setContentsMargins(D.SPACE_2, D.SPACE_2, D.SPACE_2, D.SPACE_2)
        empty_layout.setSpacing(D.SPACE_2)
        empty_layout.setAlignment(Qt.AlignCenter)

        self.icon_label = QLabel()
        self.icon_label.setStyleSheet("border: none; background: transparent;")
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setFixedHeight(48)

        t = get_theme()
        self.text_label = QLabel("Drop video files or folders here")
        self.text_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; font-weight: normal; border: none;")
        self.text_label.setAlignment(Qt.AlignCenter)

        self.hint_label = QLabel("or click anywhere to browse")
        self.hint_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 12px; border: none;")
        self.hint_label.setAlignment(Qt.AlignCenter)

        empty_layout.addWidget(self.icon_label)
        empty_layout.addWidget(self.text_label)
        empty_layout.addWidget(self.hint_label)

        # File queue list (shown after files added)
        self._queue_widget = QWidget()
        queue_layout = QVBoxLayout(self._queue_widget)
        queue_layout.setContentsMargins(D.SPACE_2, D.SPACE_2, D.SPACE_2, D.SPACE_2)
        queue_layout.setSpacing(D.SPACE_2)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(4)

        queue_header = QHBoxLayout()
        self._queue_title = QLabel("Queued Files")
        self._queue_title.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: bold; border: none;")
        queue_header.addWidget(self._queue_title)
        queue_header.addStretch()

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("secondary_btn")
        self._clear_btn.setFixedHeight(D.CONTROL_SM)
        self._clear_btn.setStyleSheet(f"font-size: 11px; padding: 4px 8px; color: {t.text_secondary}; border: 1px solid {t.border}; border-radius: 4px; background: transparent;")
        self._clear_btn.clicked.connect(self._clear_queue)
        queue_header.addWidget(self._clear_btn)

        queue_layout.addLayout(queue_header)

        self._file_list = _QueueList()
        # The queue list covers the whole drop zone once files are added.
        # Refusing drops here lets Qt propagate them to the DropZone parent, so
        # "drop more files onto the queue" keeps working; the clicked signal
        # restores "click anywhere to browse".
        self._file_list.setAcceptDrops(False)
        self._file_list.setDragDropMode(QAbstractItemView.NoDragDrop)
        self._file_list.setFocusPolicy(Qt.NoFocus)
        self._file_list.setSelectionMode(QAbstractItemView.NoSelection)
        self._file_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._file_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._file_list.pressed_anywhere.connect(self.browse_requested)
        self._apply_list_style(t)
        # No setMaximumHeight: a 120px cap showed 3 of 6 queued files while the
        # drop zone below it sat empty. The list now takes the space it has.
        self._file_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        queue_layout.addWidget(self._file_list, 1)

        self._queue_widget.setVisible(False)

        self._main_layout.addWidget(self._empty_widget)
        self._main_layout.addWidget(self._queue_widget, 1)

        self._apply_icon(t)

    def _apply_icon(self, t: ThemeColors):
        """Render the drop-zone glyph in the current theme colour.

        It used to be U+1F4C1, a colour emoji: fixed hue regardless of theme,
        and a different shape on every OS depending on font substitution.
        """
        from frameguard import icons as _icons

        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        self.icon_label.setPixmap(
            _icons.icon_pixmap("drop_files", 44, t.text_secondary, dpr))

    def _apply_list_style(self, t: ThemeColors):
        """Style the queue list, including a scrollbar wide enough to grab.

        The inherited scrollbar was ~6px, which operators described as tiny
        and hard to move.
        """
        self._file_list.setStyleSheet(
            f"QListWidget {{ background-color: transparent; border: none;"
            f" color: {t.text_secondary}; font-size: 12px; }}"
            f"QListWidget::item {{ padding: 5px 4px; border: none; }}"
            f"QListWidget QScrollBar:vertical {{ background: transparent;"
            f" width: 14px; margin: 0px; }}"
            f"QListWidget QScrollBar::handle:vertical {{"
            f" background: {t.border}; border-radius: 7px; min-height: 36px; }}"
            f"QListWidget QScrollBar::handle:vertical:hover {{"
            f" background: {t.accent}; }}"
            f"QListWidget QScrollBar::add-line:vertical,"
            f" QListWidget QScrollBar::sub-line:vertical {{ height: 0px; }}"
            f"QListWidget QScrollBar::add-page:vertical,"
            f" QListWidget QScrollBar::sub-page:vertical {{ background: none; }}"
        )

    def apply_theme(self, t: ThemeColors):
        """Update all inline styles to match the current theme."""
        self.text_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; font-weight: normal; border: none;")
        self.hint_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 12px; border: none;")
        self._queue_title.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: bold; border: none;")
        self._clear_btn.setStyleSheet(f"font-size: 11px; padding: 4px 8px; color: {t.text_secondary}; border: 1px solid {t.border}; border-radius: 4px; background: transparent;")
        self._apply_list_style(t)
        self._apply_icon(t)

    def mousePressEvent(self, event: QMouseEvent):
        """Click anywhere in drop zone to browse."""
        if event.button() == Qt.LeftButton:
            self.browse_requested.emit()
        super().mousePressEvent(event)

    @staticmethod
    def _dropped_paths(mime) -> List[str]:
        if not mime.hasUrls():
            return []
        # toLocalFile() yields forward slashes on Windows; normalize so tooltips
        # and report paths match what the user sees in Explorer.
        return [os.path.normpath(u.toLocalFile())
                for u in mime.urls() if u.isLocalFile()]

    def _accepts(self, mime) -> bool:
        return bool(self._dropped_paths(mime))

    def dragEnterEvent(self, event: QDragEnterEvent):
        if not self._accepts(event.mimeData()):
            event.ignore()
            return
        event.acceptProposedAction()
        t = get_theme()
        self.setStyleSheet(
            f"#drop_zone {{ border-color: {t.accent}; "
            f"background-color: rgba({_hex_to_rgb(t.accent)}, 0.08); }}"
        )

    def dragMoveEvent(self, event):
        # Without this Qt can refuse the drop on some Windows source apps even
        # though dragEnterEvent accepted it.
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")

    def dropEvent(self, event: QDropEvent):
        self.setStyleSheet("")
        paths = self._dropped_paths(event.mimeData())
        if not paths:
            event.ignore()
            return

        # Tell the source app the drop succeeded — omitting this leaves Explorer
        # showing a "rejected" cursor and can abort the operation.
        event.acceptProposedAction()

        preset_files = [p for p in paths if is_preset_file(p)]
        video_files = [p for p in paths if not is_preset_file(p)]

        # Presets first: they configure the scan the videos will run under.
        for pf in preset_files:
            self.preset_dropped.emit(pf)

        if video_files:
            self.files_dropped.emit(video_files)

    def show_file_queue(self, files: List[str]):
        """Switch from empty state to file queue display."""
        self._empty_widget.setVisible(False)
        self._queue_widget.setVisible(True)
        self._file_list.clear()
        self._queue_title.setText(f"Queued Files ({len(files)})")
        for f in files:
            item = QListWidgetItem(os.path.basename(f))
            item.setToolTip(f)
            self._file_list.addItem(item)

    def show_empty_state(self):
        """Switch back to empty state."""
        self._empty_widget.setVisible(True)
        self._queue_widget.setVisible(False)
        self._file_list.clear()

    def _clear_queue(self):
        """Clear the file queue and return to empty state."""
        self.show_empty_state()
        self.files_dropped.emit([])  # Signal empty list to parent


# ============================================================================
# DETAIL POPUP DIALOG
# ============================================================================

class DetailPopup(QDialog):
    """Popup dialog showing detailed check results in larger font."""

    def __init__(self, result: QAResult, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Details: {result.filename}")
        self.setMinimumSize(700, 500)
        self.resize(800, 600)

        t = get_theme()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        # Header
        status = _result_status(result)
        status_color = t.status_pass if status == "PASS" else t.status_warn if status == "PASS_WITH_WARNINGS" else t.status_fail
        header = QLabel(f"<span style='color:{status_color}; font-size:18px; font-weight:700;'>{status_label(status)}</span>"
                        f"  <span style='font-size:16px;'>{result.filename}</span>")
        header.setTextFormat(Qt.RichText)
        layout.addWidget(header)

        # Summary
        if hasattr(result, 'plain_summary'):
            summary = result.plain_summary()
            if summary:
                sum_lbl = QLabel(summary)
                sum_lbl.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; padding: 4px 0;")
                sum_lbl.setWordWrap(True)
                layout.addWidget(sum_lbl)

        # Detailed checks in larger font
        detail_text = QTextEdit()
        detail_text.setReadOnly(True)
        detail_text.setStyleSheet(f"font-size: 14px; background-color: {t.bg_secondary}; border: 1px solid {t.border}; border-radius: 8px; padding: 12px;")

        html_lines = ["<table style='width:100%;border-collapse:collapse;'>"]
        html_lines.append(
            f"<tr style='border-bottom:2px solid {t.border};'>"
            f"<th style='text-align:left;padding: 8px;color:{t.text_secondary};width:100px;font-size:14px;'>Status</th>"
            f"<th style='text-align:left;padding: 8px;color:{t.text_secondary};width:160px;font-size:14px;'>Check</th>"
            f"<th style='text-align:left;padding: 8px;color:{t.text_secondary};font-size:14px;'>Detail</th></tr>"
        )

        status_colors = check_status_colors(t)
        status_labels = CHECK_STATUS_LABELS
        for check_name, st, detail, *_ in result.checks:
            color = status_colors.get(st, t.text_secondary)
            label = status_labels.get(st, st)
            html_lines.append(
                f"<tr style='border-bottom:1px solid {t.border};'>"
                f"<td style='padding: 8px;color:{color};font-weight:700;font-size:14px;'>{label}</td>"
                f"<td style='padding: 8px;color:{t.text_primary};font-size:14px;'>{check_name}</td>"
                f"<td style='padding: 8px;color:{t.text_secondary};font-size:13px;'>{detail}</td></tr>"
            )
        html_lines.append("</table>")

        if result.metadata:
            html_lines.append(f"<h3 style='color:{t.text_secondary};margin-top:16px;font-size:15px;'>Metadata</h3>")
            html_lines.append("<table style='width:100%;border-collapse:collapse;'>")
            for k, v in result.metadata.items():
                html_lines.append(
                    f"<tr><td style='padding: 4px 8px;color:{t.text_secondary};width:180px;font-size:13px;'>{k}</td>"
                    f"<td style='padding: 4px 8px;color:{t.text_primary};font-size:13px;'>{v}</td></tr>"
                )
            html_lines.append("</table>")

        detail_text.setHtml("".join(html_lines))
        layout.addWidget(detail_text)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary_btn")
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


# ============================================================================
# SUMMARY POPUP DIALOG
# ============================================================================

class SummaryPopup(QDialog):
    """Popup showing full summary details when user clicks the summary badge."""

    def __init__(self, results: List[QAResult], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan Summary \u2014 Full Details")
        self.setMinimumSize(600, 400)
        self.resize(700, 500)

        t = get_theme()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        total = len(results)
        passed = sum(1 for r in results if _result_status(r) == "PASS")
        mismatched = sum(1 for r in results if _result_status(r) == "MISMATCH")
        warned = sum(1 for r in results if _result_status(r) == "PASS_WITH_WARNINGS")

        header = QLabel("<span style='font-size:18px;font-weight:700;'>Scan Summary</span>")
        header.setTextFormat(Qt.RichText)
        layout.addWidget(header)

        stats = QLabel(
            f"<span style='color:{t.status_pass};font-weight:600;'>{passed} Passed</span> &nbsp;|&nbsp; "
            f"<span style='color:{t.status_fail};font-weight:600;'>{mismatched} Failed</span> &nbsp;|&nbsp; "
            f"<span style='color:{t.status_warn};font-weight:600;'>{warned} Warnings</span> &nbsp;|&nbsp; "
            f"<span style='font-weight:600;'>{total} Total</span>"
        )
        stats.setTextFormat(Qt.RichText)
        stats.setStyleSheet("font-size: 14px; padding: 8px 0;")
        layout.addWidget(stats)

        # Full file list with summaries
        detail_text = QTextEdit()
        detail_text.setReadOnly(True)
        detail_text.setStyleSheet(f"font-size: 14px; background-color: {t.bg_secondary}; border: 1px solid {t.border}; border-radius: 8px; padding: 12px;")

        html_lines = ["<table style='width:100%;border-collapse:collapse;'>"]
        html_lines.append(
            f"<tr style='border-bottom:2px solid {t.border};'>"
            f"<th style='text-align:left;padding: 8px;color:{t.text_secondary};width:80px;'>Result</th>"
            f"<th style='text-align:left;padding: 8px;color:{t.text_secondary};width:200px;'>Filename</th>"
            f"<th style='text-align:left;padding: 8px;color:{t.text_secondary};'>Summary</th></tr>"
        )

        for r in results:
            st_text, st_color = _status_display(r, t)

            summary = r.plain_summary() if hasattr(r, 'plain_summary') else ""
            html_lines.append(
                f"<tr style='border-bottom:1px solid {t.border};'>"
                f"<td style='padding: 8px;color:{st_color};font-weight:700;'>{st_text}</td>"
                f"<td style='padding: 8px;color:{t.text_primary};'>{r.filename}</td>"
                f"<td style='padding: 8px;color:{t.text_secondary};font-size:12px;'>{summary}</td></tr>"
            )
        html_lines.append("</table>")
        detail_text.setHtml("".join(html_lines))
        layout.addWidget(detail_text)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary_btn")
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


# ============================================================================
# SCAN PANEL
# ============================================================================

class ScanPanel(QWidget):
    """Main scan panel with file selection, preset picker, and progress."""

    scan_requested = Signal(list, str, bool, bool)  # files, preset, quick, naming
    edit_profile_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files: List[str] = []
        self._init_ui()

    def _init_ui(self):
        self._scan_start_time = None
        # Verdict-banner state machine: idle / scanning / done / cancelled /
        # error. The banner used to persist until the next scan started, with
        # no error state and no auto-dismiss.
        self._banner_state = "idle"
        self._banner_counts = (0, 0, 0)
        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(self._on_banner_timeout)
        # Holds _FileDiscoveryRunner refs: QThreadPool auto-deletes the
        # runnable after run(), and without a Python-side holder the
        # `finished` signal's sender is garbage-collected before the queued
        # delivery — files would silently never load. Same pattern as
        # MainWindow._export_runners.
        self._discovery_runners = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(D.SPACE_6, D.SPACE_5, D.SPACE_6, D.SPACE_5)
        layout.setSpacing(20)

        t = get_theme()

        # Header
        self._header = QLabel("Scan Files")
        self._header.setProperty("class", "section-header")
        self._header.setStyleSheet(f"color: {t.text_primary}; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;")
        self._subtitle = QLabel("Select video files or a folder to run QA checks")
        self._subtitle.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; margin-bottom: 4px;")
        layout.addWidget(self._header)
        layout.addWidget(self._subtitle)

        # Drop zone (clickable). Given stretch so the queue list grows with the
        # window instead of scrolling inside a fixed 120px box.
        self.drop_zone = DropZone()
        self.drop_zone.files_dropped.connect(self._on_files_dropped)
        self.drop_zone.browse_requested.connect(self._browse_combined)
        layout.addWidget(self.drop_zone, 1)

        # File count + single browse button
        file_row = QHBoxLayout()
        self.file_count_label = QLabel("No files selected")
        self.file_count_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px;")
        file_row.addWidget(self.file_count_label)
        file_row.addStretch()

        self.browse_btn = QPushButton("  Browse")
        self.browse_btn.setObjectName("secondary_btn")
        self.browse_btn.setFixedHeight(D.CONTROL_MD)
        self.browse_btn.setIconSize(QSize(18, 18))
        self.browse_btn.setToolTip("Browse for video files or a folder")
        self.browse_btn.clicked.connect(self._browse_combined)
        file_row.addWidget(self.browse_btn)

        layout.addLayout(file_row)

        # Options row
        options_layout = QHBoxLayout()
        options_layout.setSpacing(16)

        # Preset selector with arrow symbol
        preset_group = QVBoxLayout()
        preset_label = QLabel("Profile")
        preset_label.setProperty("class", "field-label")
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom (config defaults)", "custom")
        
        # Load user-imported presets from disk
        try:
            for p in list_available_presets():
                if p.get("source") == "imported":
                    self.preset_combo.addItem(p["name"], f"imported:{p['name']}")
        except Exception:
            pass  # Don't crash if presets dir is unavailable
        for key, val in PRESETS.items():
            self.preset_combo.addItem(val["label"], key)
        self.preset_combo.setCurrentIndex(1)  # Default to 4k30

        # Hint shown when Custom preset is selected
        self._custom_hint = QLabel("\u2139 Using values from Settings")
        self._custom_hint.setWordWrap(True)
        self._custom_hint.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; font-style: italic; padding: 4px 0;")
        self._custom_hint.setVisible(False)
        preset_group.addWidget(self._custom_hint)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.preset_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.preset_combo.setMinimumContentsLength(22)
        preset_group.addWidget(preset_label)
        preset_group.addWidget(self.preset_combo)

        # Operators asked for "an easier way of inputting custom profile
        # parameters, rather than importing preset". The controls already
        # existed in Settings but nothing on this page pointed at them.
        self.edit_profile_btn = QPushButton("Edit values\u2026")
        self.edit_profile_btn.setObjectName("secondary_btn")
        self.edit_profile_btn.setFixedHeight(D.CONTROL_SM)
        self.edit_profile_btn.setToolTip(
            "Set resolution, frame rate and bitrate by hand, and save them as a "
            "shareable .fg preset")
        self.edit_profile_btn.clicked.connect(self.edit_profile_requested)
        preset_group.addWidget(self.edit_profile_btn)

        options_layout.addLayout(preset_group)

        # Scan depth. This was a single "Quick mode" checkbox, which left
        # operators unsure whether Deep was the default when it was unticked.
        # Two radio buttons make the active mode explicit and mutually
        # exclusive by construction.
        cb_group = QVBoxLayout()
        cb_label = QLabel("Scan mode")
        cb_label.setProperty("class", "field-label")

        from PySide6.QtWidgets import QRadioButton, QButtonGroup
        self.deep_radio = QRadioButton("Deep scan")
        self.deep_radio.setToolTip(
            "Decodes every frame: black frames, freeze frames and audio silence "
            "are checked in addition to the metadata checks. Slower.")
        self.quick_radio = QRadioButton("Quick scan")
        self.quick_radio.setToolTip(
            "Container, codec, resolution, frame rate, bitrate and audio only. "
            "Skips the frame-level deep scan.")
        self.deep_radio.setChecked(True)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.deep_radio)
        self._mode_group.addButton(self.quick_radio)

        # Spells out what each mode actually does, so the choice does not rely
        # on hovering for a tooltip.
        self._mode_hint = QLabel("Deep also checks black/freeze frames and silence")
        self._mode_hint.setWordWrap(True)
        self._mode_hint.setStyleSheet(
            f"color: {t.text_tertiary}; font-size: 11px; font-style: italic;")
        self.deep_radio.toggled.connect(self._on_mode_changed)

        cb_group.addWidget(cb_label)
        cb_group.addWidget(self.deep_radio)
        cb_group.addWidget(self.quick_radio)
        cb_group.addWidget(self._mode_hint)
        options_layout.addLayout(cb_group)

        options_layout.addStretch()
        layout.addLayout(options_layout)

        # Progress section
        self.progress_frame = QFrame()
        self.progress_frame.setVisible(False)
        progress_layout = QVBoxLayout(self.progress_frame)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        # Qt's default 6px spacing is off the 8pt grid; set it explicitly.
        progress_layout.setSpacing(D.SPACE_2)

        self.progress_label = QLabel("Scanning...")
        self.progress_label.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: normal;")
        self.progress_label.setWordWrap(True)
        self.progress_label.setMaximumHeight(40)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMinimumHeight(D.SPACE_2 * 2)

        # Separate percentage label with fixed width to prevent cutoff
        self.pct_label = QLabel("0%")
        self.pct_label.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: bold;")
        self.pct_label.setFixedWidth(50)
        self.pct_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        bar_row.addWidget(self.progress_bar, 1)
        bar_row.addWidget(self.pct_label)

        self.progress_detail = QLabel("")
        self.progress_detail.setStyleSheet(f"color: {t.text_secondary}; font-size: 12px;")
        self.progress_detail.setWordWrap(True)

        # Scan outcome, shown where the operator is already looking. The
        # completion text used to be written into progress_label immediately
        # after set_scanning(False) had hidden the whole progress frame, so
        # nothing was ever displayed when a scan ended.
        self.completion_banner = QLabel()
        self.completion_banner.setVisible(False)
        self.completion_banner.setWordWrap(True)
        self.completion_banner.setAlignment(Qt.AlignCenter)

        progress_layout.addWidget(self.progress_label)
        progress_layout.addLayout(bar_row)
        progress_layout.addWidget(self.progress_detail)
        layout.addWidget(self.progress_frame)
        layout.addWidget(self.completion_banner)

        # Scan button row
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("secondary_btn")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setAccessibleName("Cancel scan")
        self.cancel_btn.setAccessibleDescription(
            "Stop the running scan. Files already checked are kept.")
        btn_row.addWidget(self.cancel_btn)

        self.scan_btn = QPushButton("Start Scan")
        self.scan_btn.setObjectName("primary_btn")
        self.scan_btn.setEnabled(False)
        self.scan_btn.setToolTip("Start scanning the queued files  (Ctrl+Enter or F5)")
        self.scan_btn.setAccessibleName("Start scan")
        self.scan_btn.setAccessibleDescription(
            "Start scanning the queued files with the selected profile.")
        self.scan_btn.clicked.connect(self._start_scan)
        self.scan_btn.setShortcut("Ctrl+Return")
        btn_row.addWidget(self.scan_btn)

        layout.addLayout(btn_row)
        self._apply_button_icons(t)
        self._on_preset_changed(self.preset_combo.currentIndex())
        
        






    def apply_theme(self, t: ThemeColors):
        """Update all inline styles to match the current theme."""
        self._header.setStyleSheet(f"color: {t.text_primary}; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;")
        self._subtitle.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px;")
        # Update file count label based on current state
        if self._files:
            self.file_count_label.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: normal;")
        else:
            self.file_count_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px;")
        self.progress_label.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: normal;")
        self.pct_label.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: bold;")
        self.progress_detail.setStyleSheet(f"color: {t.text_secondary}; font-size: 12px;")
        self._custom_hint.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; font-style: italic; padding: 4px 0;")
        self._mode_hint.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; font-style: italic;")
        self._apply_button_icons(t)
        self.drop_zone.apply_theme(t)
        # Re-render the verdict against the new palette instead of wiping it:
        # switching themes used to silently discard the scan outcome.
        if hasattr(self, "_banner_state") and self._banner_state != "idle":
            state = self._banner_state
            total, failed, warned = self._banner_counts
            # Recompute style only; keep text and visibility.
            self._render_banner(state, total, failed, warned)

    def _apply_button_icons(self, t: ThemeColors):
        """Re-render the page's vector icons in the current theme colour."""
        from frameguard import icons as _icons

        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        self.browse_btn.setIcon(_icons.make_icon("folder_open", 18, t.text_secondary, dpr))

    def _browse_combined(self):
        """Combined browse: offer files or folder in a single dialog flow."""
        msg = QMessageBox(self)
        msg.setWindowTitle("Browse")
        msg.setText("What would you like to add?")
        files_btn = msg.addButton("Video Files", QMessageBox.AcceptRole)
        folder_btn = msg.addButton("Folder", QMessageBox.ActionRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == files_btn:
            files, _ = QFileDialog.getOpenFileNames(
                self, "Select Video Files", "",
                "Video Files (*.mp4 *.mov *.mkv *.mxf *.avi *.ts *.webm);;All Files (*)"
            )
            if files:
                self._set_files(files)
        elif clicked == folder_btn:
            folder = QFileDialog.getExistingDirectory(self, "Select Folder")
            if folder:
                self._discover_files_async([folder])

    def _discover_files_async(self, paths):
        """Resolve dropped/browsed paths without freezing the UI.

        Small local picks resolve synchronously (instant). Anything that may
        touch a network / Drive mount goes to _FileDiscoveryRunner on the
        thread pool: the old sync os.listdir blocked the event loop and
        Windows flagged the app "Not Responding".
        """
        try:
            from frameguard.core import discover_video_files as _discover
            # Fast path: single existing files need no thread hop.
            if len(paths) == 1 and os.path.isfile(paths[0]):
                files = _discover(paths[0])
                if files:
                    self._set_files(files)
                else:
                    t = get_theme()
                    self.file_count_label.setText("No supported video files in selection")
                    self.file_count_label.setStyleSheet(
                        f"color: {t.status_warn}; font-size: 14px;")
                return
        except Exception:
            pass
        # Slow path: folders (possibly network) -> background runner.
        t = get_theme()
        self.file_count_label.setText("Looking for video files\u2026")
        self.file_count_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px;")
        self.scan_btn.setEnabled(False)
        runner = _FileDiscoveryRunner(list(paths))
        self._discovery_runners.append(runner)
        runner.signals.finished.connect(self._on_discovery_finished)
        QThreadPool.globalInstance().start(runner)

    def _on_discovery_finished(self, files):
        # Runners are tiny; keep only recent ones so the holder list cannot
        # grow without bound across a long session of folder browsing.
        try:
            self._discovery_runners = self._discovery_runners[-8:]
        except Exception:
            pass
        if files:
            self._set_files(files)
        else:
            t = get_theme()
            self.file_count_label.setText("No supported video files found")
            self.file_count_label.setStyleSheet(
                f"color: {t.status_warn}; font-size: 14px;")
            self.scan_btn.setEnabled(False)

    def _on_files_dropped(self, paths):
        if not paths:
            self._files = []
            t = get_theme()
            self.file_count_label.setText("No files selected")
            self.file_count_label.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px;")
            self.scan_btn.setEnabled(False)
            self.hide_completion("files-cleared")
            return
        self._discover_files_async(list(paths))

    def _set_files(self, files: List[str]):
        self._files = files
        # A new selection invalidates the previous scan's verdict banner.
        self.hide_completion("files-changed")
        # Auto-detect preset from filename if set to Custom
        if files and self.preset_combo.currentData() == "custom":
            from frameguard.presets import detect_preset_from_filename
            from frameguard.core import PRESETS
            detected = detect_preset_from_filename(os.path.basename(files[0]), PRESETS)
            if detected:
                for i in range(self.preset_combo.count()):
                    if self.preset_combo.itemData(i) == detected:
                        self.preset_combo.setCurrentIndex(i)
                        break
        t = get_theme()
        count = len(files)
        self.file_count_label.setText(f"{count} file{'s' if count != 1 else ''} selected")
        self.file_count_label.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; font-weight: normal;")
        self.scan_btn.setEnabled(True)
        self.drop_zone.show_file_queue(files)

    def engine_preset(self) -> Optional[str]:
        """Return the built-in PRESETS key for the current selection, else None.

        "Custom" and imported presets both resolve to None, meaning "scan with
        whatever the Settings panel currently holds".
        """
        preset = self.preset_combo.currentData()
        if not preset or preset == "custom" or str(preset).startswith("imported:"):
            return None
        return str(preset)

    def _start_scan(self):
        if not self._files:
            return
        quick = self.quick_radio.isChecked()
        self.scan_requested.emit(self._files, self.engine_preset() or "", quick, False)

    def set_scanning(self, active: bool):
        """Toggle UI between scanning and idle states."""
        if active:
            import time
            self._scan_start_time = time.time()
            self.hide_completion("scan-started")
            self._banner_state = "scanning"
        self.scan_btn.setEnabled(not active)
        self.browse_btn.setEnabled(not active)
        self.preset_combo.setEnabled(not active)
        self.deep_radio.setEnabled(not active)
        self.quick_radio.setEnabled(not active)
        self.drop_zone.setAcceptDrops(not active)
        self.progress_frame.setVisible(active)
        self.cancel_btn.setVisible(active)

    def hide_completion(self, reason: str = ""):
        """Clear the verdict banner (new files, navigation, new scan)."""
        self._banner_timer.stop()
        self._banner_state = "idle"
        self._banner_counts = (0, 0, 0)
        self.completion_banner.setVisible(False)

    def _on_banner_timeout(self):
        """Auto-dismiss clean verdicts and transient notes.

        PASS auto-hides after 10s so the page resets itself, and transient
        info notes dismiss after 8s. FAIL/WARN stay sticky — dismissing a
        failure without operator action is data loss.
        """
        if self._banner_state == "done" and self._banner_counts[1] == 0:
            self.hide_completion("auto-dismiss")
        elif self._banner_state == "info":
            self.hide_completion("info-expired")

    def show_completion(self, total: int, failed: int, warned: int, cancelled: bool = False,
                        error: str = ""):
        """Announce the outcome of a finished scan.

        A scan that ends is the moment the operator most needs a clear signal,
        and previously the only one was the progress frame disappearing.
        """
        self._banner_timer.stop()
        if error:
            self._banner_state = "error"
            self._banner_counts = (total, failed, warned)
            self._render_banner("error", total, failed, warned, error)
            return
        if cancelled:
            self._banner_state = "cancelled"
            self._banner_counts = (0, 0, 0)
            self._render_banner("cancelled", 0, 0, 0)
            return
        if total <= 0:
            self.hide_completion("empty")
            return
        self._banner_state = "done"
        self._banner_counts = (total, failed, warned)
        self._render_banner("done", total, failed, warned)
        # Clean verdicts dismiss themselves; failures persist until the
        # operator starts a new scan, changes files, or navigates away.
        if failed == 0:
            self._banner_timer.start(10000)

    def show_error(self, message: str):
        """Error-state verdict: replaces any stale PASS/FAIL text.

        _on_scan_error previously left the previous scan's banner in place,
        so the page kept announcing the old outcome after a failure.
        """
        self._banner_timer.stop()
        self._banner_state = "error"
        self._banner_counts = (0, 0, 0)
        self._render_banner("error", 0, 0, 0, message)

    def _render_banner(self, state: str, total: int, failed: int, warned: int,
                       error: str = ""):
        t = get_theme()
        if state == "cancelled":
            text = "Scan cancelled"
            bg, fg = t.status_warn, "#1A1A1A"
        elif state == "error":
            text = f"\u2717  Scan failed \u2014 {error[:160]}" if error else "\u2717  Scan failed"
            bg, fg = t.status_fail, "#FFFFFF"
        elif state == "info":
            # Transient note (auto-export path, watch notice): neutral card,
            # and — unlike a verdict — never sticky.
            text = error
            bg, fg = t.bg_secondary, t.text_primary
        elif failed:
            text = (f"\u2717  Scan complete \u2014 {failed} of {total} "
                    f"file{'s' if total != 1 else ''} FAILED")
            bg, fg = t.status_fail, "#FFFFFF"
        elif warned:
            text = (f"\u26A0  Scan complete \u2014 {total} file{'s' if total != 1 else ''} "
                    f"checked, {warned} with warnings")
            bg, fg = t.status_warn, "#1A1A1A"
        elif total:
            text = (f"\u2713  Scan complete \u2014 all {total} "
                    f"file{'s' if total != 1 else ''} passed")
            bg, fg = t.status_pass, "#06210F"
        else:
            self.completion_banner.setVisible(False)
            return

        if self._scan_start_time and state in ("done", "cancelled"):
            import time
            elapsed_s = int(time.time() - self._scan_start_time)
            text += f"   ({elapsed_s // 60}m {elapsed_s % 60}s)"

        self.completion_banner.setText(text)
        self.completion_banner.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-size: 15px; "
            f"font-weight: bold; padding: 12px 16px; border-radius: 8px;"
        )
        self.completion_banner.setVisible(True)

    def show_info_note(self, text: str, timeout_ms: int = 8000):
        """Transient note in the banner slot (auto-export path, watch notice).

        Informational only: never overwrites a FAIL verdict, auto-dismisses,
        and clears on navigation — so unlike the old direct writes it can
        never read as a permanent result flag.
        """
        state = getattr(self, "_banner_state", "idle")
        counts = getattr(self, "_banner_counts", (0, 0, 0))
        if state == "done" and counts[1] > 0:
            return
        if state in ("scanning", "error", "cancelled"):
            return
        self._banner_timer.stop()
        self._banner_state = "info"
        self._banner_counts = (0, 0, 0)
        self._render_banner("info", 0, 0, 0, text)
        self._banner_timer.start(timeout_ms)

    def update_progress(self, file_idx: int, total_files: int,
                        phase: str, current: float, total: float, label: str):
        """Update progress bar and labels with elapsed time and ETA."""
        import time
        if total_files <= 0:
            return
        phase_pct = (current / total) if total > 0 else 0
        # decode occupies the first half of a file's progress, filtering the second
        file_progress = phase_pct * 0.5 if phase == "decode" else 0.5 + phase_pct * 0.5

        overall_pct = (file_idx + file_progress) / total_files * 100
        pct_val = int(min(overall_pct, 100))
        self.progress_bar.setValue(pct_val)
        self.pct_label.setText(f"{pct_val}%")
        self.progress_label.setText(f"File {file_idx + 1}/{total_files}: {label}")

        elapsed = ""
        eta = ""
        if self._scan_start_time:
            elapsed_s = time.time() - self._scan_start_time
            elapsed = f"Elapsed: {int(elapsed_s // 60)}m {int(elapsed_s % 60)}s"
            if overall_pct > 2:
                total_est = elapsed_s / (overall_pct / 100)
                remaining = max(0, total_est - elapsed_s)
                eta = f"  |  ETA: {int(remaining // 60)}m {int(remaining % 60)}s"

        if total > 0:
            self.progress_detail.setText(f"{current:.0f}s / {total:.0f}s  |  {elapsed}{eta}")
        elif elapsed:
            self.progress_detail.setText(f"{elapsed}{eta}")


    def _on_mode_changed(self, deep_selected: bool):
        """Keep the scan-mode explanation in step with the selected radio."""
        self._mode_hint.setText(
            "Deep also checks black/freeze frames and silence" if deep_selected
            else "Quick checks metadata only \u2014 no frame-level analysis"
        )

    def _on_preset_changed(self, index):
        """Show/hide custom config hint based on preset selection."""
        is_custom = self.preset_combo.currentData() == "custom"
        if hasattr(self, '_custom_hint'):
            self._custom_hint.setVisible(is_custom)
        # Long profile names clip inside the combo with no ellipsis, so the
        # full name has to be reachable somehow.
        self.preset_combo.setToolTip(
            f"{self.preset_combo.currentText()}\n\nSelect a delivery profile preset")


# ============================================================================
# RESULTS PANEL
# ============================================================================

class ResultsPanel(QWidget):
    """Results panel with summary table and detail drawer."""

    export_requested = Signal()
    reveal_requested = Signal(str)  # filepath to show in the file manager
    history_cleared = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: List[QAResult] = []
        # Insertion order, so "Scan order" can undo the failures-first sort.
        self._scan_order: dict = {}
        self._scan_seq = 0
        self._sort_by_severity = True
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(D.SPACE_6, D.SPACE_5, D.SPACE_6, D.SPACE_5)
        layout.setSpacing(16)

        t = get_theme()

        # Header row.
        #
        # Eight always-visible controls needed ~1450px but a non-maximized
        # window only offers ~710px, so everything squeezed and the labels
        # clipped. Only the primary action and the one high-traffic filter stay
        # on the bar; the rest live in an overflow menu.
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self._header = QLabel("Results")
        self._header.setStyleSheet(f"color: {t.text_primary}; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;")
        header_row.addWidget(self._header)
        header_row.addStretch()

        self._issues_only_cb = QCheckBox("Issues only")
        self._issues_only_cb.setToolTip(
            "Hide files that passed cleanly. Selection and exports are unaffected."
        )
        self._issues_only_cb.setAccessibleName("Show issues only")
        self._issues_only_cb.setAccessibleDescription(
            "Hide files that passed cleanly. Selection and exports are unaffected.")
        self._issues_only_cb.toggled.connect(self._apply_row_filter)
        header_row.addWidget(self._issues_only_cb)

        # Operators could not find the reset: it was buried in the overflow
        # menu as "Clear History", which does not read as "empty this page".
        self.clear_btn = QPushButton("Clear Results")
        self.clear_btn.setObjectName("secondary_btn")
        self.clear_btn.setEnabled(False)
        self.clear_btn.setToolTip("Remove all results from this page")
        self.clear_btn.setAccessibleName("Clear results")
        self.clear_btn.clicked.connect(self._on_clear_history)
        header_row.addWidget(self.clear_btn)

        self._more_btn = QPushButton("\u22EF")
        self._more_btn.setObjectName("secondary_btn")
        self._more_btn.setFixedSize(D.ICON_BTN, D.CONTROL_MD)
        self._more_btn.setToolTip("More actions")
        self._more_btn.setAccessibleName("More actions")
        # Inherited button padding made this 32px control demand 55px.
        self._more_btn.setStyleSheet("padding: 0px; text-align: center;")
        self._more_btn.clicked.connect(self._show_actions_menu)
        header_row.addWidget(self._more_btn)

        self.export_btn = QPushButton("Export PDF")
        self.export_btn.setObjectName("primary_btn")
        self.export_btn.setEnabled(False)
        self.export_btn.setToolTip("Export checked files to PDF (or all files if none selected)")
        self.export_btn.setAccessibleName("Export PDF report")
        self.export_btn.clicked.connect(lambda _=False: self.export_requested.emit())
        header_row.addWidget(self.export_btn)

        layout.addLayout(header_row)

        # Attention banner — headline verdict for the batch, so a failure is
        # visible before the operator reads a single row.
        self._attention_banner = QLabel()
        self._attention_banner.setVisible(False)
        self._attention_banner.setWordWrap(True)
        layout.addWidget(self._attention_banner)

        # Hint text for summary popup
        self._summary_hint = QLabel("\U0001F6C8  Click any summary badge below to see full details")
        self._summary_hint.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; font-style: italic;")
        self._summary_hint.setVisible(False)
        layout.addWidget(self._summary_hint)

        # Summary badges row with horizontal scroll
        summary_scroll = QScrollArea()
        summary_scroll.setWidgetResizable(True)
        summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        summary_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 130px of a 700px window was ~19% of the height for four numbers.
        # _apply_responsive_layout shrinks this further on short windows.
        summary_scroll.setFixedHeight(104)
        summary_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._summary_scroll = summary_scroll

        summary_widget = QWidget()
        self.summary_row = QHBoxLayout(summary_widget)
        self.summary_row.setContentsMargins(4, 4, 4, 4)
        self.summary_row.setSpacing(12)

        # Store badge references for theme updates
        self._badge_frames = {}
        self._badge_count_labels = {}
        self._badge_name_labels = {}
        self._badge_colors = {}

        self.badge_total = self._make_badge("total", "Total", "0", t.accent)
        self.badge_pass = self._make_badge("passed", "Passed", "0", t.status_pass)
        self.badge_fail = self._make_badge("mismatched", "Failed", "0", t.status_fail)
        self.badge_warn = self._make_badge("warnings", "Warnings", "0", t.status_warn)
        self.summary_row.addWidget(self.badge_total)
        self.summary_row.addWidget(self.badge_pass)
        self.summary_row.addWidget(self.badge_fail)
        self.summary_row.addWidget(self.badge_warn)
        self.summary_row.addStretch()

        summary_scroll.setWidget(summary_widget)
        layout.addWidget(summary_scroll)

        # Splitter: table + detail
        splitter = QSplitter(Qt.Vertical)

        # Results table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["\u2610", "Status", "Filename", "Codec", "Resolution", "Duration", "Summary"])
        header = self.table.horizontalHeader()
        # Size the metadata columns from real font metrics rather than either
        # hardcoded pixels (which clipped "3840x2160") or ResizeToContents
        # (which over-pads so generously that Filename was squeezed to 101px).
        # Filename and Summary then share everything that is left.
        header.setMinimumSectionSize(28)
        self.table.setColumnWidth(0, 32)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        for col in (1, 3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)    # filename
        header.setSectionResizeMode(6, QHeaderView.Stretch)    # summary
        self._autosize_metadata_columns()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_row_context_menu)
        self.table.currentCellChanged.connect(self._on_row_selected)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        splitter.addWidget(self.table)

        # Detail panel
        self.detail_panel = QTextEdit()
        self.detail_panel.setReadOnly(True)
        self.detail_panel.setStyleSheet(
            f"background-color: {t.bg_secondary}; border: 1px solid {t.border}; "
            f"border-radius: 8px; color: {t.text_primary}; padding: 12px; font-size: 12px;"
        )
        self.detail_panel.setPlaceholderText("Select a file above to view detailed check results.\nDouble-click a row to open in a larger popup window.")
        splitter.addWidget(self.detail_panel)

        splitter.setSizes([300, 200])
        layout.addWidget(splitter)



    def _make_badge(self, key: str, label: str, count: str, color: str) -> QFrame:
        """Build a summary badge.

        ``key`` is the stable identifier used by _update_badge_count and
        apply_theme; ``label`` is the display text. They are separate on
        purpose — deriving the key from the label meant renaming "Mismatched"
        to "Failed" silently stopped the counter from ever updating.
        """
        frame = QFrame()
        frame.setCursor(Qt.PointingHandCursor)
        frame.setStyleSheet(
            f"background-color: rgba({_hex_to_rgb(color)}, 0.1); "
            f"border: 1px solid rgba({_hex_to_rgb(color)}, 0.25); "
            f"border-radius: 16px; padding: 8px 16px;"
        )
        frame.setMinimumWidth(D.SPACE_6 * 3 + D.SPACE_1)  # 100
        frame.setMinimumHeight(D.SPACE_7 + D.SPACE_5 + D.SPACE_4)   # 88 - measured content need
        frame.setMaximumHeight(D.SPACE_7 + D.SPACE_5 + D.SPACE_4)

        t = get_theme()

        badge_layout = QVBoxLayout(frame)
        badge_layout.setContentsMargins(D.SPACE_3, D.SPACE_2, D.SPACE_3, D.SPACE_2)
        badge_layout.setSpacing(4)

        count_lbl = QLabel(count)
        count_lbl.setObjectName(f"badge_count_{key}")
        count_lbl.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: bold; border: none; padding: 4px 0px;")
        count_lbl.setAlignment(Qt.AlignCenter)

        name_lbl = QLabel(label)
        name_lbl.setStyleSheet(f"color: {t.text_secondary}; font-size: 11px; font-weight: normal; border: none;")
        name_lbl.setAlignment(Qt.AlignCenter)

        badge_layout.addWidget(count_lbl)
        badge_layout.addWidget(name_lbl)

        # Store references for theme updates
        self._badge_frames[key] = frame
        self._badge_count_labels[key] = count_lbl
        self._badge_name_labels[key] = name_lbl
        self._badge_colors[key] = color  # Will be updated on theme change

        # Make badge clickable
        frame.mousePressEvent = lambda e: self._show_summary_popup()
        return frame

    def apply_theme(self, t: ThemeColors):
        """Update all inline styles to match the current theme."""
        self._header.setStyleSheet(f"color: {t.text_primary}; font-size: 20px; font-weight: bold;")
        self._summary_hint.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; font-style: italic;")
        self.detail_panel.setStyleSheet(
            f"background-color: {t.bg_secondary}; border: 1px solid {t.border}; "
            f"border-radius: 8px; color: {t.text_primary}; padding: 12px; font-size: 12px;"
        )

        # Update badge colors to match new theme
        color_map = {
            "total": t.accent,
            "passed": t.status_pass,
            "mismatched": t.status_fail,
            "warnings": t.status_warn,
        }
        for key, color in color_map.items():
            self._badge_colors[key] = color
            if key in self._badge_frames:
                self._badge_frames[key].setStyleSheet(
                    f"background-color: rgba({_hex_to_rgb(color)}, 0.1); "
                    f"border: 1px solid rgba({_hex_to_rgb(color)}, 0.25); "
                    f"border-radius: 8px; padding: 8px 16px;"
                )
            if key in self._badge_count_labels:
                self._badge_count_labels[key].setStyleSheet(
                    f"color: {color}; font-size: 20px; font-weight: bold; border: none; padding: 4px 0px;"
                )
            if key in self._badge_name_labels:
                self._badge_name_labels[key].setStyleSheet(
                    f"color: {t.text_secondary}; font-size: 11px; font-weight: normal; border: none;"
                )

        # Re-render detail panel if results are loaded
        if self._results and self.table.currentRow() >= 0:
            row = self.table.currentRow()
            if 0 <= row < len(self._results):
                html = self._format_detail_html(self._results[row])
                self.detail_panel.setHtml(html)

    def _show_summary_popup(self):
        """Show full summary popup when badge is clicked."""
        if self._results:
            popup = SummaryPopup(self._results, self)
            popup.exec()

    def set_results(self, results: List[QAResult], append: bool = True):
        """Populate the results table. If append=True, adds to existing."""
        if append:
            self._results.extend(results)
        else:
            self._results = list(results)
            self._scan_order.clear()
        for r in results:
            if id(r) not in self._scan_order:
                self._scan_seq += 1
                self._scan_order[id(r)] = self._scan_seq
        # Cap accumulated results to keep memory and redraw cost bounded.
        # Trim by scan order, not list position: _results is already sorted
        # failures-first from the previous call, so slicing [-MAX:] discarded
        # precisely the failed files the operator needs to see.
        if len(self._results) > MAX_RETAINED_RESULTS:
            self._results.sort(key=lambda r: self._scan_order.get(id(r), 0))
            dropped = self._results[:-MAX_RETAINED_RESULTS]
            self._results = self._results[-MAX_RETAINED_RESULTS:]
            for r in dropped:
                self._scan_order.pop(id(r), None)

        # Failures first so the files needing attention are never below the
        # fold, then by severity so a corrupted file outranks one with only
        # an off-spec framerate. Sorting the backing list (not the view)
        # keeps row index and _results index identical, which
        # get_selected_results(), _on_row_selected() and the context menu
        # all rely on.
        if self._sort_by_severity:
            self._results.sort(key=_severity_key)

        results = self._results
        t = get_theme()
        total = len(results)
        passed = sum(1 for r in results if _result_status(r) == "PASS")
        mismatched = sum(1 for r in results if _result_status(r) == "MISMATCH")
        warned = sum(1 for r in results if _result_status(r) == "PASS_WITH_WARNINGS")

        self._update_badge_count("total", str(total))
        self._update_badge_count("passed", str(passed))
        self._update_badge_count("mismatched", str(mismatched))
        self._update_badge_count("warnings", str(warned))

        self._update_attention_banner(mismatched, warned, total)

        # Row order must stay locked to _results order: get_selected_results(),
        # _on_row_selected() and _on_row_double_clicked() all index positionally.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(total)
        for idx, r in enumerate(results):
            status = _result_status(r)
            status_text, status_color = _status_display(r, t)
            failed = status == "MISMATCH"

            # Checkbox for selective export
            check_item = QTableWidgetItem()
            check_item.setCheckState(Qt.Unchecked)
            self.table.setItem(idx, 0, check_item)

            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(QColor(status_color))
            status_item.setFont(ui_font(11 if failed else 10, QFont.Bold))
            status_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(idx, 1, status_item)

            filename_item = QTableWidgetItem(r.filename)
            filename_item.setToolTip(r.filepath)
            if failed:
                filename_item.setFont(ui_font(10, QFont.Bold))
            self.table.setItem(idx, 2, filename_item)

            codec = r.metadata.get("video_codec", "N/A")
            self.table.setItem(idx, 3, QTableWidgetItem(codec))

            # Resolution / duration get tabular figures so the digits line up
            # down the column. With proportional digits "00:01:30" and
            # "00:11:02" sit at different offsets and the column is hard to
            # scan for an outlier.
            res_item = QTableWidgetItem(r.metadata.get("resolution", "N/A"))
            res_item.setFont(mono_font(9))
            self.table.setItem(idx, 4, res_item)

            dur_item = QTableWidgetItem(r.metadata.get("duration_display", "N/A"))
            dur_item.setFont(mono_font(9))
            self.table.setItem(idx, 5, dur_item)

            summary_text = r.plain_summary() if hasattr(r, 'plain_summary') else "N/A"
            summary_item = QTableWidgetItem(summary_text)
            summary_item.setToolTip(
                f"<div style='max-width:400px;padding: 4px;'>"
                f"<b>{r.filename}</b><br/><br/>{summary_text}</div>"
            )
            self.table.setItem(idx, 6, summary_item)

            # Tint the whole row so failures read as a block, not one red word.
            # Alpha scales with severity: a corrupted file (score ~100) hits
            # harder than a framerate-only file (score ~20).
            if failed or status == "PASS_WITH_WARNINGS":
                tint = QColor(status_color)
                sev = _result_severity(r)
                if failed:
                    alpha = 30 + int(min(100.0, sev) / 100.0 * 45)
                else:
                    alpha = 18 + int(min(100.0, sev) / 100.0 * 20)
                tint.setAlpha(max(18, min(90, alpha)))
                for col in range(self.table.columnCount()):
                    item = self.table.item(idx, col)
                    if item:
                        item.setBackground(tint)
                if failed and sev >= 80:
                    # Critical files also get a bolder filename: at a glance
                    # the eye lands on corruption before framerate.
                    filename_item.setFont(ui_font(11, QFont.Bold))

        self.export_btn.setEnabled(total > 0)
        self.clear_btn.setEnabled(total > 0)
        self._summary_hint.setVisible(total > 0)
        self._autosize_metadata_columns()
        self._apply_row_filter()

    def _update_attention_banner(self, failed: int, warned: int, total: int):
        """Headline count above the table so failures are impossible to miss.

        Includes a critical-vs-warning split so the operator gets a sense of
        scale: "2 critical, 3 warnings" reads differently from "5 failed".
        """
        t = get_theme()
        critical = 0
        try:
            for r in self._results:
                if _result_status(r) == "MISMATCH" and _result_severity(r) >= 80:
                    critical += 1
        except Exception:
            critical = 0
        if failed:
            if critical:
                text = (f"\u26A0  {failed} of {total} file{'s' if total != 1 else ''} FAILED "
                        f"\u2014 {critical} critical, {failed - critical} other")
            else:
                text = (f"\u26A0  {failed} of {total} file{'s' if total != 1 else ''} FAILED "
                        f"and need attention")
            self._attention_banner.setText(text)
            self._attention_banner.setStyleSheet(
                f"background-color: {t.status_fail}; color: #FFFFFF; font-size: 14px;"
                f"font-weight: bold; padding: 8px 16px; border-radius: 8px;"
            )
            self._attention_banner.setVisible(True)
        elif warned:
            self._attention_banner.setText(
                f"\u26A0  {warned} of {total} file{'s' if total != 1 else ''} passed with warnings"
            )
            self._attention_banner.setStyleSheet(
                f"background-color: {t.status_warn}; color: #1A1A1A; font-size: 14px;"
                f"font-weight: bold; padding: 9px 16px; border-radius: 8px;"
            )
            self._attention_banner.setVisible(True)
        elif total:
            self._attention_banner.setText(f"\u2713  All {total} file(s) passed")
            self._attention_banner.setStyleSheet(
                f"background-color: {t.status_pass}; color: #06210F; font-size: 14px;"
                f"font-weight: bold; padding: 9px 16px; border-radius: 8px;"
            )
            self._attention_banner.setVisible(True)
        else:
            self._attention_banner.setVisible(False)

    def _toggle_sort_order(self, severity_first: bool):
        """Switch between failures-first and original scan order."""
        self._sort_by_severity = severity_first
        if not severity_first:
            self._results.sort(key=lambda r: self._scan_order.get(id(r), 0))
        self.set_results([], append=True)

    def _apply_row_filter(self):
        """Hide clean rows when 'Issues only' is checked.

        Rows are hidden, never reordered or removed, so the row -> _results
        index mapping used by selection and detail views stays valid.
        """
        issues_only = self._issues_only_cb.isChecked()
        hidden = 0
        for row, r in enumerate(self._results):
            if row >= self.table.rowCount():
                break
            # _result_status, not the raw attribute: an unfinalized result has
            # status None, which is not "PASS" but is not an issue either.
            clean = _result_status(r) == "PASS"
            hide = issues_only and clean
            self.table.setRowHidden(row, hide)
            hidden += hide

        if issues_only and hidden:
            self._summary_hint.setText(
                f"\U0001F6C8  Showing {len(self._results) - hidden} of "
                f"{len(self._results)} files ({hidden} clean file(s) hidden)"
            )
        else:
            self._summary_hint.setText(
                "\U0001F6C8  Click any summary badge below to see full details"
            )
    
    def get_selected_results(self):
        """Return results whose checkbox is checked. If none checked, return all."""
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                if row < len(self._results):
                    selected.append(self._results[row])
        return selected if selected else list(self._results)
    
    def select_all(self, checked: bool = True):
        """Check/uncheck all checkboxes."""
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(state)
    
    def _on_clear_history(self):
        self._clear_all_results()
        self.history_cleared.emit()

    def clear_results(self):
        """Public reset: empty the page and tell dependent panels to follow."""
        self._on_clear_history()

    def _clear_all_results(self):
        """Clear all accumulated results from history."""
        self._results.clear()
        self._scan_order.clear()
        self.table.setRowCount(0)
        self.export_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self._update_badge_count("total", "0")
        self._update_badge_count("passed", "0")
        self._update_badge_count("mismatched", "0")
        self._update_badge_count("warnings", "0")
        self._summary_hint.setVisible(False)
        # The verdict banner is a summary of the results, so it has to go with
        # them. It used to be left behind, still announcing "8 of 24 files
        # passed with warnings" over an empty table.
        self._update_attention_banner(0, 0, 0)
        self.detail_panel.clear()

    def _autosize_metadata_columns(self):
        """Fit Status/Codec/Resolution/Duration to their widest actual value.

        Measured, not guessed: "3840x2160" needs 117px and "WARNING" in bold
        needs 91px, both of which overflowed the old hardcoded widths.
        """
        metrics = QFontMetrics(self.table.font())
        bold = QFont(self.table.font())
        bold.setBold(True)
        bold_metrics = QFontMetrics(bold)
        # Resolution and duration render in the tabular face, which is wider
        # per character than the UI face.
        mono_metrics = QFontMetrics(mono_font(9))

        for col, header_text in ((1, "Status"), (3, "Codec"),
                                 (4, "Resolution"), (5, "Duration")):
            widest = metrics.horizontalAdvance(header_text)
            for row in range(self.table.rowCount()):
                item = self.table.item(row, col)
                if item is None:
                    continue
                if col == 1:
                    fm = bold_metrics       # Status renders bold
                elif col in (4, 5):
                    fm = mono_metrics       # tabular figures
                else:
                    fm = metrics
                widest = max(widest, fm.horizontalAdvance(item.text()))
            self.table.setColumnWidth(col, widest + 20)

    def _show_actions_menu(self):
        """Secondary actions, kept off the toolbar so it fits a small window."""
        from PySide6.QtWidgets import QMenu

        has_results = bool(self._results)
        menu = QMenu(self)

        sort_action = menu.addAction("Failures first")
        sort_action.setCheckable(True)
        sort_action.setChecked(self._sort_by_severity)
        sort_action.setToolTip("Sort failed files to the top, or list them in scan order.")

        menu.addSeparator()
        play = menu.addAction("\u25B6  Open selected in VLC")
        # Enabled whenever there is anything to play. Gating on currentRow()
        # left a dead menu entry when no row had been clicked yet, which reads
        # as the feature being broken; _play_selected() explains what to do.
        play.setEnabled(has_results)

        menu.addSeparator()
        select_all = menu.addAction("Select All")
        clear_sel = menu.addAction("Clear Selection")
        for action in (select_all, clear_sel):
            action.setEnabled(has_results)

        menu.addSeparator()
        clear_history = menu.addAction("Clear History")
        clear_history.setEnabled(has_results)

        chosen = menu.exec(self._more_btn.mapToGlobal(self._more_btn.rect().bottomLeft()))
        if chosen is sort_action:
            self._toggle_sort_order(sort_action.isChecked())
        elif chosen is play:
            self._play_selected()
        elif chosen is select_all:
            self.select_all(True)
        elif chosen is clear_sel:
            self.select_all(False)
        elif chosen is clear_history:
            self._on_clear_history()

    def set_failures_first(self, enabled: bool):
        """Public toggle for the failures-first ordering (also driven by the menu)."""
        self._toggle_sort_order(enabled)

    def _show_row_context_menu(self, pos):
        """Right-click a result to jump straight to the offending file."""
        from PySide6.QtWidgets import QMenu

        row = self.table.rowAt(pos.y())
        if not (0 <= row < len(self._results)):
            return
        result = self._results[row]

        menu = QMenu(self)
        play = menu.addAction("Open in VLC")
        reveal = menu.addAction("Show in Explorer" if sys.platform == "win32"
                                else "Reveal in Finder" if sys.platform == "darwin"
                                else "Open Containing Folder")
        menu.addSeparator()
        copy_path = menu.addAction("Copy File Path")
        copy_summary = menu.addAction("Copy Summary")
        menu.addSeparator()
        details = menu.addAction("View Full Details")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is reveal:
            _reveal_in_file_manager(result.filepath)
        elif chosen is play:
            self._play_result(result)
        elif chosen is copy_path:
            QApplication.clipboard().setText(result.filepath)
        elif chosen is copy_summary:
            QApplication.clipboard().setText(
                f"{result.filename}: {status_label(_result_status(result))} - {result.plain_summary()}")
        elif chosen is details:
            DetailPopup(result, self).exec()

    def _play_result(self, result):
        """Hand a file off to VLC (or the default player if VLC is absent)."""
        used = open_in_player(result.filepath)
        if used == "missing":
            QMessageBox.warning(
                self, "File Not Found",
                f"The file is no longer at:\n{result.filepath}"
            )
        elif used == "failed":
            QMessageBox.warning(
                self, "Could Not Open File",
                f"No application could be launched for:\n{result.filepath}"
            )
        elif used == "default":
            # Falling back silently is why this read as "VLC did not open".
            QMessageBox.information(
                self, "VLC Not Found",
                "VLC is not installed, so the file was opened in your default "
                "video player instead.\n\nInstall VLC from videolan.org to use "
                "this action as intended."
            )

    def _play_selected(self):
        """Toolbar action: play the currently highlighted row."""
        row = self.table.currentRow()
        if 0 <= row < len(self._results):
            self._play_result(self._results[row])
        else:
            QMessageBox.information(
                self, "No File Selected",
                "Select a result row first, then choose Open in VLC."
            )

    def _update_badge_count(self, name: str, value: str):
        lbl = self.findChild(QLabel, f"badge_count_{name}")
        if lbl:
            lbl.setText(value)

    def _on_row_selected(self, row, col, prev_row, prev_col):
        if row < 0 or row >= len(self._results):
            return
        r = self._results[row]
        html = self._format_detail_html(r)
        self.detail_panel.setHtml(html)

    def _on_row_double_clicked(self, index):
        """Open detail popup on double-click."""
        row = index.row()
        if 0 <= row < len(self._results):
            popup = DetailPopup(self._results[row], self)
            popup.exec()

    def _format_detail_html(self, r: QAResult) -> str:
        t = get_theme()
        lines = [f"<h3 style='color:{t.text_primary};margin-bottom:8px;'>{r.filename}</h3>"]
        lines.append("<table style='width:100%;border-collapse:collapse;'>")
        lines.append(
            f"<tr style='border-bottom:1px solid {t.border};'>"
            f"<th style='text-align:left;padding: 4px 8px;color:{t.text_secondary};width:80px;'>Status</th>"
            f"<th style='text-align:left;padding: 4px 8px;color:{t.text_secondary};width:130px;'>Check</th>"
            f"<th style='text-align:left;padding: 4px 8px;color:{t.text_secondary};'>Detail</th></tr>"
        )

        status_colors = check_status_colors(t)
        status_labels = CHECK_STATUS_LABELS
        for check_name, status, detail, *_ in r.checks:
            color = status_colors.get(status, t.text_secondary)
            label = status_labels.get(status, status)
            lines.append(
                f"<tr style='border-bottom:1px solid {t.border};'>"
                f"<td style='padding: 4px 8px;color:{color};font-weight:600;'>{label}</td>"
                f"<td style='padding: 4px 8px;color:{t.text_primary};'>{check_name}</td>"
                f"<td style='padding: 4px 8px;color:{t.text_secondary};font-size:11px;'>{detail}</td></tr>"
            )

        lines.append("</table>")

        if r.metadata:
            lines.append(f"<h4 style='color:{t.text_secondary};margin-top:12px;'>Metadata</h4>")
            lines.append("<table style='width:100%;border-collapse:collapse;'>")
            for k, v in r.metadata.items():
                lines.append(
                    f"<tr><td style='padding: 4px 8px;color:{t.text_secondary};width:160px;'>{k}</td>"
                    f"<td style='padding: 4px 8px;color:{t.text_primary};'>{v}</td></tr>"
                )
            lines.append("</table>")

        return "".join(lines)


# ============================================================================
# SETTINGS PANEL (General + Advanced tabs)
# ============================================================================

class SettingsPanel(QWidget):
    theme_changed = Signal(str)
    preset_imported = Signal(str, dict)  # name, config
    settings_changed = Signal()  # Emitted when any scan parameter changes
    install_update_requested = Signal()
    report_problem_requested = Signal()
    delta_update_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._t = get_theme()
        self._defaults = dict(DEFAULT_CONFIG)
        # Preset parameters with no UI control yet; merged back in by
        # get_config_overrides() so imported presets aren't silently truncated.
        self._preset_extras = {}
        # Injected by MainWindow so the panel and the window share one manager.
        self._update_manager = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        root.addWidget(self._scroll)

        self._content = QWidget()
        self._scroll.setWidget(self._content)

        self._main_layout = QVBoxLayout(self._content)
        self._main_layout.setContentsMargins(D.SPACE_6, D.SPACE_5, D.SPACE_6, D.SPACE_5)
        self._main_layout.setSpacing(16)

        self._title_font_css = "font-size: 14px; font-weight: bold;"
        self._label_font_css = "font-size: 14px;"
        self._desc_font_css = "font-size: 12px;"

        self._reset_buttons = []
        self._desc_labels = []
        self._name_labels = []
        self._group_boxes = []

        # --- Delivery Profile ---

        # --- Simple / Advanced Mode Toggle ---
        self._advanced_mode = False  # Start in simple mode
        self._simple_mode_btn = QPushButton("▼  Show Advanced Settings")
        self._simple_mode_btn.setToolTip("Simple mode shows only the most common settings")
        self._simple_mode_btn.setFixedHeight(D.CONTROL_LG)
        self._simple_mode_btn.setObjectName("advToggleBtn")
        self._simple_mode_btn.clicked.connect(self._toggle_simple_mode)
        self._main_layout.addWidget(self._simple_mode_btn)
        self._main_layout.addSpacing(12)
        
        # Track which groups are "advanced only"
        self._advanced_groups = []

        delivery_box = self._make_group("Delivery Profile")
        delivery_layout = delivery_box.layout()

        # Resolution preset + optional custom width/height
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["4K 3840x2160", "1080p 1920x1080", "720p 1280x720", "Custom"])
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_preset_changed)

        self.custom_width = QSpinBox()
        self.custom_width.setRange(1, 99999)
        self.custom_width.setSingleStep(2)

        self.custom_height = QSpinBox()
        self.custom_height.setRange(1, 99999)
        self.custom_height.setSingleStep(2)

        self.custom_res_row = QWidget()
        custom_res_layout = QHBoxLayout(self.custom_res_row)
        custom_res_layout.setContentsMargins(0, 0, 0, 0)
        custom_res_layout.setSpacing(12)
        wlab = QLabel("Width")
        hlab = QLabel("Height")
        self._name_labels.extend([wlab, hlab])
        custom_res_layout.addWidget(wlab)
        custom_res_layout.addWidget(self.custom_width)
        custom_res_layout.addSpacing(8)
        custom_res_layout.addWidget(hlab)
        custom_res_layout.addWidget(self.custom_height)
        custom_res_layout.addStretch(1)

        self._add_setting(
            delivery_layout,
            key="delivery_resolution_preset",
            title="Resolution",
            control=self.resolution_combo,
            description="Expected video resolution for deliverables",
            resetter=self._reset_resolution
        )
        delivery_layout.addWidget(self.custom_res_row)
        self.custom_res_row.setVisible(False)

        # Frame rate dropdown
        self.fps_combo = QComboBox()
        fps_values = ["23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60"]
        self.fps_combo.addItems(fps_values)
        self._add_setting(
            delivery_layout,
            key="delivery_fps_target",
            title="Frame Rate",
            control=self.fps_combo,
            description="Target frame rate. Files outside tolerance will be flagged",
            resetter=lambda: self._set_combo_by_text(self.fps_combo, _fps_text(self._defaults.get("target_fps", 30.0)))
        )

        # FPS tolerance slider (0.1 - 2.0) in 0.1 increments, ticks at 0.5
        self.fps_tol_slider, self.fps_tol_value = self._make_float_slider(
            min_v=0.1, max_v=2.0, step=0.1, tick=0.5, suffix=" fps"
        )
        self._add_setting(
            delivery_layout,
            key="delivery_fps_tolerance",
            title="FPS Tolerance",
            control=self._wrap_slider_with_value(self.fps_tol_slider, self.fps_tol_value),
            description="Default: 0.5 fps. Accommodates NTSC drop-frame rates (29.97 counts as 30fps, 59.94 counts as 60fps). Captures report either exact or NTSC-equivalent frame rates.",
            resetter=lambda: self._set_float_slider(self.fps_tol_slider, 0.1, 2.0, 0.1, float(self._defaults.get("fps_tolerance", 0.5)))
        )

        # Min bitrate slider (1-100 Mbps), ticks at 5
        self.min_bitrate_slider, self.min_bitrate_value = self._make_int_slider(
            min_v=1, max_v=100, tick=5, suffix=" Mbps"
        )
        self._add_setting(
            delivery_layout,
            key="delivery_min_bitrate_mbps",
            title="Min Bitrate",
            control=self._wrap_slider_with_value(self.min_bitrate_slider, self.min_bitrate_value),
            description="Default: 20 Mbps for 4K. Typical values: 4K@30fps=20 Mbps, 4K@60fps=30 Mbps, 1080p=8 Mbps, 360 VR=40 Mbps. Files within 10% below are a soft pass.",
            resetter=lambda: self._set_int_slider(self.min_bitrate_slider, int(self._defaults.get("min_bitrate_mbps", 20)))
        )

        self._main_layout.addWidget(delivery_box)

        # --- Audio Requirements ---
        audio_box = self._make_group("Audio Requirements")
        audio_layout = audio_box.layout()

        self.audio_required_cb = QCheckBox()
        self._add_setting(
            audio_layout,
            key="audio_required",
            title="Audio Required",
            control=self.audio_required_cb,
            description="Flag files that have no audio track",
            resetter=lambda: self.audio_required_cb.setChecked(bool(self._defaults.get("audio_required", True)))
        )

        self.silence_slider, self.silence_value = self._make_float_slider(
            min_v=1.0, max_v=30.0, step=0.5, tick=5.0, suffix=" s"
        )
        self._add_setting(
            audio_layout,
            key="audio_silence_duration_s",
            title="Silence Duration",
            control=self._wrap_slider_with_value(self.silence_slider, self.silence_value),
            description="How many seconds of silence triggers a warning",
            resetter=lambda: self._set_float_slider(self.silence_slider, 1.0, 30.0, 0.5, float(self._defaults.get("silence_duration", 2.0)))
        )


        self._main_layout.addWidget(audio_box)

        # --- Deep Scan ---
        scan_box = self._make_group("Deep Scan")
        scan_layout = scan_box.layout()

        self.hwaccel_cb = QCheckBox()
        self._add_setting(
            scan_layout,
            key="deep_hardware_accel",
            title="Hardware Acceleration",
            control=self.hwaccel_cb,
            description="Use GPU for faster decode when available",
            resetter=lambda: self.hwaccel_cb.setChecked(bool(self._defaults.get("use_hwaccel", True)))
        )

        self.segmented_cb = QCheckBox()
        self._add_setting(
            scan_layout,
            key="deep_segmented_scan",
            title="Segmented Scan",
            control=self.segmented_cb,
            description="Split large files into parallel segments for faster scanning",
            resetter=lambda: self.segmented_cb.setChecked(bool(self._defaults.get("segment_enabled", True)))
        )

        self.segment_count_slider, self.segment_count_value = self._make_int_slider(
            min_v=2, max_v=16, tick=1, suffix=""
        )
        self._add_setting(
            scan_layout,
            key="deep_segment_count",
            title="Segment Count",
            control=self._wrap_slider_with_value(self.segment_count_slider, self.segment_count_value),
            description="Number of parallel segments (more = faster but uses more CPU)",
            resetter=lambda: self._set_int_slider(self.segment_count_slider, int(self._defaults.get("segment_count", 4)))
        )

        self.strict_cb = QCheckBox()
        self._add_setting(
            scan_layout,
            key="deep_strict_mode",
            title="Strict Mode",
            control=self.strict_cb,
            description="Treat any warning as a failure",
            resetter=lambda: self.strict_cb.setChecked(False)
        )

        self._main_layout.addWidget(scan_box)

        # --- Results ---
        # Always visible: this governs everyday behaviour, so it must not be
        # buried behind "Show Advanced Settings" the way the deep-scan tuning is.
        results_box = self._make_group("Results")
        results_layout = results_box.layout()

        # Operators expected a new batch to give them a fresh Results page and
        # were surprised to find the previous batch still listed.
        self.reset_results_cb = QCheckBox()
        self.reset_results_cb.setChecked(True)
        self._add_setting(
            results_layout,
            key="results_reset_per_scan",
            title="Clear Results On New Scan",
            control=self.reset_results_cb,
            description=(
                "Start each scan with an empty Results page. Turn this off to "
                "accumulate results across batches. Watch-folder runs always "
                "accumulate so their activity log stays intact."
            ),
            resetter=lambda: self.reset_results_cb.setChecked(True)
        )

        self._main_layout.addWidget(results_box)

                # Naming Convention: REMOVED in v0.4.4 (kept in core.py for future re-enable)

        # --- Appearance ---
        appearance_box = self._make_group("Appearance")
        appearance_layout = appearance_box.layout()
        
        # Theme selector: button grid (no dropdown - guaranteed theme-aware)
        theme_label = QLabel("Color Theme")
        theme_label.setStyleSheet("border: none; background: transparent; font-size: 14px; font-weight: bold;")
        appearance_layout.addWidget(theme_label)
        
        theme_grid = QGridLayout()
        theme_grid.setSpacing(8)
        
        self._theme_buttons = {}
        self._current_theme_name = "Meta Dark"
        
        theme_names = list(get_theme_names())
        cols = 3  # 3-column grid; rows are derived from len(THEMES)
        
        for idx, name in enumerate(theme_names):
            t_data = THEMES[name]
            btn = QPushButton(name)
            # Not setFixedSize: "Slate Professional" needs 210px and was being
            # clipped at every window size. Let the grid distribute the width
            # and keep only the height on the control scale.
            btn.setMinimumHeight(D.CONTROL_XL)
            btn.setMaximumHeight(D.CONTROL_XL)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setCursor(Qt.PointingHandCursor)
            
            # Style each button with ITS OWN theme colors (always, regardless of current theme)
            btn.setStyleSheet(
                f"QPushButton {{"
                f"  background-color: {t_data.bg_primary};"
                f"  color: {t_data.text_primary};"
                f"  border: 2px solid {t_data.border};"
                f"  border-radius: 8px;"
                f"  font-size: 11px;"
                f"  font-weight: bold;"
                f"  padding: 4px;"
                f"}}"
                f"QPushButton:hover {{"
                f"  border-color: {t_data.accent};"
                f"  background-color: {t_data.bg_secondary};"
                f"}}"
            )
            
            btn.clicked.connect(lambda checked, n=name: self._select_theme(n))
            
            row = idx // cols
            col = idx % cols
            theme_grid.addWidget(btn, row, col)
            self._theme_buttons[name] = btn
        
        appearance_layout.addLayout(theme_grid)
        
        # Mark the default as selected
        self._highlight_active_theme("Meta Dark")
        
        self._main_layout.addWidget(appearance_box)
        self._main_layout.addStretch(1)


        # --- Watch Folder Section ---
        # Watch folder section moved to its own tab (WatchFolderPanel)
        self._init_preset_section(self._main_layout)
        
        # Mark advanced-only groups (hidden in simple mode).
        # Simple mode shows: Delivery Profile, Results, Appearance, Presets.
        # Advanced mode adds: Audio Requirements, Deep Scan.
        #
        # Held by reference, not by position in _group_boxes: the old
        # index-based lookup silently pointed at the wrong groups as soon as
        # another _make_group() call was inserted anywhere above.
        self._advanced_groups = [audio_box, scan_box]

        # Apply simple mode on startup (hide advanced groups)
        for group in self._advanced_groups:
            group.hide()

        self._reset_to_defaults()
        self.apply_theme(self._t)

        self._connect_change_signals()


    def apply_theme(self, t):
        self._t = t

        # Labels/descriptions: use theme text colors. Avoid setting stylesheet on the panel itself.
        text_primary = getattr(t, "text_primary", "#EDEDED")
        text_secondary = getattr(t, "text_secondary", "#B8B8B8")
        bg_secondary = getattr(t, "bg_secondary", "transparent")
        border = getattr(t, "border", getattr(t, "bg_tertiary", "#2A2A2A"))
        accent = getattr(t, "accent", "#0A6EEA")


        # Advanced toggle button - theme-aware text color
        if hasattr(self, '_simple_mode_btn'):
            self._simple_mode_btn.setStyleSheet(
                f"QPushButton {{"
                f"  background-color: transparent;"
                f"  border: 2px solid {border};"
                f"  border-radius: 6px;"
                f"  color: {text_primary};"
                f"  font-size: 14px;"
                f"  font-weight: bold;"
                f"  padding: 8px 16px;"
                f"}}"
                f"QPushButton:hover {{"
                f"  border-color: {accent};"
                f"  color: {accent};"
                f"}}"
            )

        for lab in self._name_labels:
            lab.setStyleSheet(f"{self._label_font_css} color: {text_primary};")

        for lab in self._desc_labels:
            lab.setStyleSheet(f"{self._desc_font_css} color: {text_secondary};")

        # Get theme colors for interactive controls
        accent = getattr(t, "accent", "#0A6EEA")
        bg_tertiary = getattr(t, "bg_tertiary", bg_secondary)
        surface_hover = getattr(t, "surface_hover", bg_tertiary)
        
        for gb in self._group_boxes:
            gb.setStyleSheet(
                "QGroupBox {"
                f"  color: {text_primary};"
                f"  background-color: {bg_secondary};"
                f"  border: 1px solid {border};"
                "  border-radius: 8px;"
                "  margin-top: 12px;"
                "  padding: 12px;"
                "}"
                "QGroupBox::title {"
                "  subcontrol-origin: margin;"
                "  subcontrol-position: top left;"
                "  padding: 0 8px;"
                f"  {self._title_font_css}"
                "}"
                "QComboBox {"
                f"  color: {text_primary};"
                f"  background: {bg_tertiary};"
                f"  border: 1px solid {border};"
                "  border-radius: 6px;"
                "  min-height: 30px;"
                "  padding: 8px 12px;"
                "}"
                "QComboBox:hover {"
                f"  border-color: {accent};"
                "}"
                "QComboBox::drop-down {"
                "  width: 36px;"
                "  border: none;"
                f"  border-left: 1px solid {border};"
                "}"
                "QComboBox QAbstractItemView {"
                f"  background-color: {bg_secondary};"
                f"  color: {text_primary};"
                f"  border: 1px solid {border};"
                f"  selection-background-color: {accent};"
                "  selection-color: #FFFFFF;"
                "  padding: 4px;"
                "}"
                "QComboBox QAbstractItemView::item {"
                "  padding: 8px 12px;"
                "  min-height: 28px;"
                "}"
                "QComboBox QAbstractItemView::item:hover {"
                f"  background-color: {surface_hover};"
                "}"
            )

        # Reset buttons styling (small, subtle)
        accent = getattr(t, "accent", getattr(t, "text_primary", "#EDEDED"))
        for b in self._reset_buttons:
            b.setStyleSheet(
                "QPushButton {"
                f"  color: {accent};"
                "  font-size: 14px;"
                "  padding: 4px 8px;"
                "  border-radius: 6px;"
                "}"
                "QPushButton:hover {"
                "  text-decoration: underline;"
                "}"
            )



        # Force combo popups to use new theme colors (Windows palette fix)
        from PySide6.QtWidgets import QComboBox
        for combo in self.findChildren(QComboBox):
            _style_combobox_popup(combo, t)

    def _connect_change_signals(self):
        """Connect all setting controls to emit settings_changed when values change."""
        # Resolution and frame rate
        if hasattr(self, 'resolution_combo'):
            self.resolution_combo.currentIndexChanged.connect(lambda: self.settings_changed.emit())
        if hasattr(self, 'fps_combo'):
            self.fps_combo.currentIndexChanged.connect(lambda: self.settings_changed.emit())
        if hasattr(self, 'fps_tol_slider'):
            self.fps_tol_slider.valueChanged.connect(lambda: self.settings_changed.emit())
        if hasattr(self, 'min_bitrate_slider'):
            self.min_bitrate_slider.valueChanged.connect(lambda: self.settings_changed.emit())
        # Custom resolution
        if hasattr(self, 'custom_width'):
            self.custom_width.valueChanged.connect(lambda: self.settings_changed.emit())
        if hasattr(self, 'custom_height'):
            self.custom_height.valueChanged.connect(lambda: self.settings_changed.emit())


    def _init_preset_section(self, parent_layout):
        """Add Preset import/export section."""
        preset_box = self._make_group("Scan Presets")
        preset_layout = preset_box.layout()
        
        preset_layout = preset_box.layout()
        # A 2x2 grid rather than one row of four: four buttons side by side
        # needed ~790px and forced the whole Settings page to scroll
        # horizontally on a non-maximized window.
        btn_grid = QGridLayout()
        btn_grid.setSpacing(8)
        self.export_preset_btn = QPushButton("Export Current")
        self.export_preset_btn.setToolTip("Save current settings as a shareable .fg preset file")
        self.export_preset_btn.clicked.connect(self._export_current_preset)
        self.import_preset_btn = QPushButton("Import Preset")
        self.import_preset_btn.setToolTip("Load settings from a preset file")
        self.import_preset_btn.clicked.connect(self._import_preset_file)
        self.open_presets_btn = QPushButton("Open Folder")
        self.open_presets_btn.setToolTip("Open the presets directory")
        self.open_presets_btn.clicked.connect(self._open_presets_folder)

        for _b in (self.export_preset_btn, self.import_preset_btn, self.open_presets_btn):
            _b.setMinimumHeight(D.CONTROL_MD)
        btn_grid.addWidget(self.export_preset_btn, 0, 0)
        btn_grid.addWidget(self.import_preset_btn, 0, 1)
        btn_grid.addWidget(self.open_presets_btn, 1, 0)
        preset_layout.addLayout(btn_grid)

        # Info label
        info = QLabel(
            "A preset (.fg) is just this page's Delivery Profile, Audio and Deep "
            "Scan values saved to a file.\n"
            "\u2022  To create one: set the values above, then click Export Current "
            "and give it a name.\n"
            "\u2022  To use someone else's: click Import Preset, or drag the .fg file "
            "anywhere onto FrameGuard.\n"
            "Imported presets appear in the Profile dropdown on the Scan page."
        )
        # Without wordWrap this single label demands ~1050px, which forces the
        # entire Settings page into horizontal scrolling on a non-maximized window.
        info.setWordWrap(True)
        info.setStyleSheet("border: none; background: transparent; padding: 4px;")
        preset_layout.addWidget(info)

        parent_layout.addWidget(preset_box)

        # --- Updates -------------------------------------------------------
        # Previously the update controls lived inside "Scan Presets", which is
        # both semantically wrong and part of what overflowed that row.
        update_box = self._make_group("Updates")
        update_layout = update_box.layout()

        # Three buttons side by side overflowed the Settings viewport at the
        # minimum window size, so they go in a grid like the preset actions.
        update_grid = QGridLayout()
        update_grid.setSpacing(D.SPACE_2)
        self.check_update_btn = QPushButton("Check for Updates")
        self.check_update_btn.setToolTip("Check if a newer version of FrameGuard is available")
        self.check_update_btn.setMinimumHeight(D.CONTROL_MD)
        self.check_update_btn.clicked.connect(self._check_for_updates)
        update_grid.addWidget(self.check_update_btn, 0, 0)

        # Appears only once an update has actually been found. The app used to
        # tell users to "go to Settings to install" with no control to do it.
        self.install_update_btn = QPushButton("Install Update")
        self.install_update_btn.setObjectName("primary_btn")
        self.install_update_btn.setVisible(False)
        self.install_update_btn.setMinimumHeight(D.CONTROL_MD)
        self.install_update_btn.clicked.connect(self.install_update_requested.emit)
        update_grid.addWidget(self.install_update_btn, 0, 1)

        # Differential update: pull a release folder and transfer only the
        # files that differ, rather than re-running the ~90MB installer.
        self.delta_update_btn = QPushButton("Update from Folder...")
        self.delta_update_btn.setMinimumHeight(D.CONTROL_MD)
        self.delta_update_btn.setToolTip(
            "Update from a published release folder, downloading only the "
            "files that changed")
        self.delta_update_btn.clicked.connect(self._pick_release_folder)
        update_grid.addWidget(self.delta_update_btn, 1, 0)

        # There was previously no way to report a problem from inside the app.
        self.report_problem_btn = QPushButton("Report a Problem...")
        self.report_problem_btn.setMinimumHeight(D.CONTROL_MD)
        self.report_problem_btn.setToolTip(
            "Collect version, tool and session details into a report you can send")
        self.report_problem_btn.clicked.connect(self.report_problem_requested.emit)
        update_grid.addWidget(self.report_problem_btn, 1, 1)

        update_layout.addLayout(update_grid)

        # Optional differential channel. Blank deliberately means the normal,
        # architecture-aware GitHub Releases installer flow.
        channel_row = QHBoxLayout()
        channel_row.setSpacing(D.SPACE_2)
        channel_label = QLabel("Optional delta channel:")
        channel_row.addWidget(channel_label)

        from frameguard.updater import get_update_channel
        self.channel_edit = QLineEdit(get_update_channel())
        self.channel_edit.setMinimumHeight(D.CONTROL_MD)
        self.channel_edit.setPlaceholderText(
            r"Leave blank for GitHub Releases, or enter \\share\FrameGuard\release")
        self.channel_edit.setToolTip(
            "Optional filesystem or static-HTTP differential channel.\n"
            "Leave blank to use complete installers from GitHub Releases.")
        self.channel_edit.editingFinished.connect(self._save_update_channel)
        channel_row.addWidget(self.channel_edit, 1)
        update_layout.addLayout(channel_row)

        parent_layout.addWidget(update_box)

    def show_available_update(self, info):
        """Reveal the Install Update button for a found update."""
        self.install_update_btn.setText(f"Install Update v{info.version}")
        self.install_update_btn.setToolTip(
            f"Downloads and runs the v{info.version} installer, then restarts FrameGuard."
        )
        self.install_update_btn.setVisible(True)
    
    def _export_current_preset(self):
        """Export current settings as a preset file."""
        from PySide6.QtWidgets import QInputDialog, QFileDialog

        name, ok = QInputDialog.getText(
            self, "Export Preset", "Preset name:", text="Custom Profile"
        )
        name = name.strip() if ok else ""
        if not name:
            return

        config = self.get_config_overrides()
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Export Preset",
            str(get_user_presets_dir() / f"{name}{FG_EXTENSION}"),
            f"FrameGuard Presets (*{FG_EXTENSION})"
        )
        if not save_path:
            return
        if not save_path.lower().endswith(FG_EXTENSION):
            save_path += FG_EXTENSION

        try:
            path = export_preset(name, config, save_path)
        except OSError as e:
            QMessageBox.critical(self, "Export Failed", f"Could not write the preset file:\n{e}")
            return

        QMessageBox.information(
            self, "Preset Exported",
            f"Saved to:\n{path}\n\nShare this file with your team — they can drag it "
            f"onto FrameGuard to import it."
        )
    
    def _import_preset_file(self):
        """Import a preset from a file."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Import Preset", 
            str(get_presets_dir()),
            f"FrameGuard Presets (*{FG_EXTENSION});;Legacy Presets (*.json);;All Files (*)"
        )
        if filepath:
            preset = save_imported_preset(filepath)
            if preset:
                self._apply_preset_config(preset["config"])
                # Notify parent to add this preset to the dropdown
                self.preset_imported.emit(preset["name"], preset["config"])
                QMessageBox.information(
                    self, "Preset Imported",
                    f"Loaded: {preset['name']}\n\nThis preset is now saved and will appear in the Scan page dropdown."
                )
            else:
                QMessageBox.warning(self, "Import Failed", "Invalid preset file format.")
    
    def _apply_preset_config(self, config: dict):
        """Apply a preset's config values to the settings controls.

        ``config`` is keyed as in ``core.DEFAULT_CONFIG``. Only the parameters
        the UI actually exposes are applied; the rest still travel with the
        preset file and are handed to the engine by ``_scan_config()``.
        """
        from frameguard.presets import normalize_preset_config

        config = normalize_preset_config(config)
        self._preset_extras = {
            k: v for k, v in config.items() if k not in _UI_BACKED_CONFIG_KEYS
        }

        if "target_width" in config and "target_height" in config:
            self._set_resolution_combo(int(config["target_width"]), int(config["target_height"]))

        if "target_fps" in config:
            self._set_combo_by_text(self.fps_combo, _fps_text(config["target_fps"]))

        if "fps_tolerance" in config:
            self._set_float_slider(self.fps_tol_slider, 0.1, 2.0, 0.1, float(config["fps_tolerance"]))

        if "min_bitrate_mbps" in config:
            self._set_int_slider(self.min_bitrate_slider, int(config["min_bitrate_mbps"]))

        if "audio_required" in config:
            self.audio_required_cb.setChecked(bool(config["audio_required"]))

        if "silence_duration" in config:
            self._set_float_slider(self.silence_slider, 1.0, 30.0, 0.5, float(config["silence_duration"]))

        if "use_hwaccel" in config:
            self.hwaccel_cb.setChecked(bool(config["use_hwaccel"]))

        if "segment_enabled" in config:
            self.segmented_cb.setChecked(bool(config["segment_enabled"]))

        if "segment_count" in config:
            self._set_int_slider(self.segment_count_slider, int(config["segment_count"]))
    
    def _open_presets_folder(self):
        """Open the presets directory in the file manager."""
        _open_path(str(get_presets_dir()))
    
    def _save_update_channel(self):
        """Persist the release location this machine checks."""
        from frameguard.updater import set_update_channel
        set_update_channel(self.channel_edit.text())

    def _pick_release_folder(self):
        """Choose a published release directory to update from."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Release Folder (must contain manifest.json)")
        if folder:
            self.delta_update_requested.emit(folder)

    def _check_for_updates(self):
        """Manually trigger an update check via the shared UpdateManager."""
        if self._update_manager is None:
            QMessageBox.warning(self, "Unavailable", "The update service is not running.")
            return

        self.check_update_btn.setEnabled(False)
        self.check_update_btn.setText("Checking...")

        checker = self._update_manager

        def _reset():
            self.check_update_btn.setEnabled(True)
            self.check_update_btn.setText("Check for Updates")
            # Reconnecting on every click would stack duplicate handlers and
            # show N dialogs after N presses.
            for sig, slot in ((checker.update_available, _on_found),
                              (checker.update_not_found, _on_none),
                              (checker.check_failed, _on_error)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        def _on_found(info):
            _reset()
            if info.is_newer:
                QMessageBox.information(
                    self, "Update Available",
                    f"v{info.version} is available (you have v{APP_VERSION}).\n\n{info.changelog[:200]}")
            else:
                QMessageBox.information(
                    self, "Up to Date", f"You're running the latest version (v{APP_VERSION}).")

        def _on_none():
            _reset()
            QMessageBox.information(
                self, "Up to Date", f"You're running the latest version (v{APP_VERSION}).")

        def _on_error(msg):
            _reset()
            QMessageBox.information(
                self, "Check Failed",
                "Could not check for updates.\nMake sure you're connected to the team network.\n\n"
                f"Error: {msg[:200]}")

        checker.update_available.connect(_on_found)
        checker.update_not_found.connect(_on_none)
        checker.check_failed.connect(_on_error)
        checker.check_now()

    def get_config_overrides(self) -> dict:
        """Return scan settings keyed by the names ``core.DEFAULT_CONFIG`` uses.

        Every key here must exist in ``DEFAULT_CONFIG`` or the engine will
        silently ignore it; ``test_config_keys.py`` enforces that.
        """
        d = {}

        # Delivery
        preset = self.resolution_combo.currentText()
        if preset == "Custom":
            d["target_width"] = int(self.custom_width.value())
            d["target_height"] = int(self.custom_height.value())
        else:
            w, h = self._parse_resolution_from_preset(preset)
            if w and h:
                d["target_width"] = int(w)
                d["target_height"] = int(h)

        d["target_fps"] = float(self.fps_combo.currentText())
        d["fps_tolerance"] = self._get_float_slider(self.fps_tol_slider, 0.1)
        d["min_bitrate_mbps"] = int(self.min_bitrate_slider.value())

        # Audio
        d["audio_required"] = bool(self.audio_required_cb.isChecked())
        d["silence_duration"] = self._get_float_slider(self.silence_slider, 0.5)

        # Deep scan
        d["use_hwaccel"] = bool(self.hwaccel_cb.isChecked())
        d["segment_enabled"] = bool(self.segmented_cb.isChecked())
        d["segment_count"] = int(self.segment_count_slider.value())

        # Parameters carried by an imported preset that have no UI control yet
        # (accepted codecs/containers, sample rate, CFR). Applied first so an
        # explicit widget value always wins.
        return {**self._preset_extras, **d}

    # --- Session persistence -------------------------------------------------

    def save_settings(self):
        """Persist the scan configuration so it survives a restart.

        Previously only the theme was remembered, so a delivery spec had to be
        re-entered from scratch every session.
        """
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue("scan/config", json.dumps(self.get_config_overrides()))
        settings.setValue("scan/strict", self.strict_cb.isChecked())
        settings.setValue("scan/reset_results", self.reset_results_cb.isChecked())
        settings.setValue("scan/advanced_mode", bool(getattr(self, "_advanced_mode", False)))

    def restore_settings(self):
        """Reload the persisted scan configuration, ignoring anything stale."""
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        raw = settings.value("scan/config", "")
        if raw:
            try:
                config = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                config = None
            if isinstance(config, dict):
                # normalize drops any key the current engine no longer reads,
                # so an old saved blob can never reintroduce the key mismatch.
                self._apply_preset_config(config)

        strict = settings.value("scan/strict", False)
        self.strict_cb.setChecked(strict in (True, "true", "True", 1, "1"))

        # QSettings round-trips booleans as strings on some backends.
        reset = settings.value("scan/reset_results", True)
        self.reset_results_cb.setChecked(reset in (True, "true", "True", 1, "1"))

        # save_settings() has always persisted this, but nothing ever read it
        # back, so the panel reopened collapsed every session no matter how
        # often the operator expanded it.
        advanced = settings.value("scan/advanced_mode", False)
        if advanced in (True, "true", "True", 1, "1") and not self._advanced_mode:
            self._toggle_simple_mode()

    def resets_results_per_scan(self) -> bool:
        return bool(self.reset_results_cb.isChecked())

    def is_strict_mode(self) -> bool:
        return bool(self.strict_cb.isChecked())

    def _reset_to_defaults(self):
        # Delivery
        self._reset_resolution()

        self._set_combo_by_text(self.fps_combo, _fps_text(self._defaults.get("target_fps", 30.0)))
        self._set_float_slider(
            self.fps_tol_slider, 0.1, 2.0, 0.1,
            float(self._defaults.get("fps_tolerance", 0.5))
        )
        self._set_int_slider(self.min_bitrate_slider, int(self._defaults.get("min_bitrate_mbps", 20)))

        # Audio
        self.audio_required_cb.setChecked(bool(self._defaults.get("audio_required", True)))
        self._set_float_slider(
            self.silence_slider, 1.0, 30.0, 0.5,
            float(self._defaults.get("silence_duration", 2.0))
        )

        # Deep scan
        self.hwaccel_cb.setChecked(bool(self._defaults.get("use_hwaccel", True)))
        self.segmented_cb.setChecked(bool(self._defaults.get("segment_enabled", True)))
        self._set_int_slider(self.segment_count_slider, int(self._defaults.get("segment_count", 4)))
        self.strict_cb.setChecked(False)
        self.reset_results_cb.setChecked(True)


    # ----------------------------
    # UI helpers
    # ----------------------------
    def _make_group(self, title: str) -> QGroupBox:
        from frameguard.theme import get_theme
        t = get_theme()
        gb = QGroupBox(title)
        gb.setStyleSheet(f"""
            QGroupBox {{
                border: 1px solid {t.border};
                border-radius: 12px;
                margin-top: 24px;
                padding: 20px 16px 16px 16px;
                background: {t.surface};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 12px;
                font-size: 14px;
                font-weight: bold;
                color: {t.text_secondary};
                letter-spacing: 0.3px;
                background: transparent;
            }}
        """)
        lay = QVBoxLayout(gb)
        lay.setContentsMargins(8, 16, 8, 12)
        # Each setting is a self-contained block that carries its own vertical
        # margins, so the group must not add more between them.
        lay.setSpacing(0)
        self._group_boxes.append(gb)
        return gb

    # Every setting's control sits in a fixed-width, right-aligned cell so the
    # column has one left edge and one right edge. Without it a 260px combo, a
    # 194px slider and a 22px checkbox each started at a different x, which is
    # most of what read as "uneven".
    #
    # 240 rather than the 260 the widest combo asks for: at 260 the settings
    # page needed 772px inside a 770px viewport and grew a horizontal
    # scrollbar. Controls are capped to the column so none can overflow it.
    CONTROL_COL_W = 240

    def _add_setting(self, parent_layout: QVBoxLayout, key: str, title: str, control: QWidget, description: str, resetter):
        """Add one setting: name + control on a row, description beneath.

        The three parts are wrapped in a single container so the whole setting
        is one visual block. Previously the name sat 8px above its own
        description and only 12px below the *previous* setting's description,
        so a description read as belonging to either neighbour and the control
        — which aligns to the name — appeared to float above the text
        explaining it.

        Spacing lives in the layout, not in stylesheet padding: apply_theme()
        rewrites these labels' stylesheets wholesale, so any padding set here
        was silently discarded the first time the theme changed.
        """
        # --- Name label (floating, no border) ---
        name = QLabel(title)
        name.setFont(ui_font(15, QFont.DemiBold))
        name.setStyleSheet("border: none; background: transparent; padding: 0;")
        self._name_labels.append(name)

        # --- Reset button (minimal, circular) ---
        reset_btn = QPushButton("↺")
        reset_btn.setFixedSize(D.CONTROL_MD, D.CONTROL_MD)
        reset_btn.setCursor(Qt.PointingHandCursor)
        reset_btn.setToolTip("Reset to default")
        reset_btn.setStyleSheet("""
            QPushButton {
                border: none;
                border-radius: 16px;
                background: transparent;
                font-size: 14px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton:hover {
                background: rgba(10, 110, 234, 0.2);
            }
        """)
        reset_btn.clicked.connect(resetter)
        self._reset_buttons.append(reset_btn)

        # --- Control cell: fixed width, contents right-aligned and centred ---
        control.setMaximumWidth(self.CONTROL_COL_W)
        cell = QWidget()
        cell.setFixedWidth(self.CONTROL_COL_W)
        cell.setStyleSheet("border: none; background: transparent;")
        cell_lay = QHBoxLayout(cell)
        cell_lay.setContentsMargins(0, 0, 0, 0)
        cell_lay.setSpacing(0)
        cell_lay.addStretch(1)
        cell_lay.addWidget(control, 0, Qt.AlignVCenter)

        # --- Row: name + stretch + control cell + reset ---
        row = QWidget()
        row.setStyleSheet("border: none; background: transparent;")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(12)
        row_lay.addWidget(name, 0, Qt.AlignVCenter)
        row_lay.addStretch(1)
        row_lay.addWidget(cell, 0, Qt.AlignVCenter)
        row_lay.addWidget(reset_btn, 0, Qt.AlignVCenter)

        # --- Description (floating text, subdued) ---
        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setFont(ui_font(11))
        desc.setStyleSheet("border: none; background: transparent; padding: 0;")
        self._desc_labels.append(desc)

        # --- One block per setting ---
        block = QWidget()
        block.setStyleSheet("border: none; background: transparent;")
        block_lay = QVBoxLayout(block)
        # Generous outside, tight inside: that ratio is what makes the name,
        # its control and its description read as one thing. Measured, not
        # guessed — name-to-own-description ~17px against ~38px to the next
        # setting, where the two used to be 8px and 12px apart.
        block_lay.setContentsMargins(0, D.SPACE_3, 0, D.SPACE_3)
        block_lay.setSpacing(2)
        block_lay.addWidget(row)
        block_lay.addWidget(desc)

        parent_layout.addWidget(block)

    def _wrap_slider_with_value(self, slider: QSlider, value_label: QLabel) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addWidget(slider, 1)
        lay.addWidget(value_label, 0, Qt.AlignRight)
        return w

    def _make_int_slider(self, min_v: int, max_v: int, tick: int = 1, suffix: str = ""):
        slider = QSlider(Qt.Horizontal)
        slider.setMinimumHeight(40)
        slider.setRange(min_v, max_v)
        slider.setTickPosition(QSlider.TicksBelow)
        slider.setTickInterval(tick)
        slider.setSingleStep(1)
        slider.setPageStep(tick)

        value = QLabel("")
        self._name_labels.append(value)  # styled as label (14px)
        value.setMinimumWidth(80)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def update(v):
            value.setText(f"{v}{suffix}")

        slider.valueChanged.connect(update)
        update(slider.value())
        return slider, value

    def _make_float_slider(self, min_v: float, max_v: float, step: float, tick: float, suffix: str = ""):
        scale = int(round(1.0 / step))
        imin = int(round(min_v * scale))
        imax = int(round(max_v * scale))
        itick = max(1, int(round(tick * scale)))

        slider = QSlider(Qt.Horizontal)
        slider.setMinimumHeight(40)
        slider.setRange(imin, imax)
        slider.setTickPosition(QSlider.TicksBelow)
        slider.setTickInterval(itick)
        slider.setSingleStep(1)
        slider.setPageStep(itick)

        value = QLabel("")
        self._name_labels.append(value)  # styled as label (14px)
        value.setMinimumWidth(80)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def update(iv):
            fv = iv / scale
            text = f"{fv:.3f}".rstrip("0").rstrip(".")
            value.setText(f"{text}{suffix}")

        slider.valueChanged.connect(update)
        update(slider.value())
        return slider, value

    def _set_combo_by_text(self, combo: QComboBox, text: str):
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _set_int_slider(self, slider: QSlider, v: int):
        # No blockSignals + manual re-emit here: that combination blocked
        # nothing (the manual emit reached every slot anyway) while making the
        # intent look like the opposite. setValue emits only on a real change,
        # which is exactly what the value label and settings_changed want.
        slider.setValue(int(v))

    def _set_float_slider(self, slider: QSlider, min_v: float, max_v: float, step: float, v: float):
        scale = int(round(1.0 / step))
        imin = int(round(min_v * scale))
        imax = int(round(max_v * scale))
        iv = int(round(float(v) * scale))
        slider.setValue(max(imin, min(imax, iv)))

    def _get_float_slider(self, slider: QSlider, step: float) -> float:
        scale = int(round(1.0 / step))
        return slider.value() / scale

    # ----------------------------
    # Resolution handling
    # ----------------------------
    def _parse_resolution_from_preset(self, preset_text: str):
        # Expect trailing "WxH"
        try:
            tail = preset_text.split()[-1]
            if "x" in tail:
                w, h = tail.split("x", 1)
                return int(w), int(h)
        except Exception:
            pass
        return None, None

    def _on_resolution_preset_changed(self, _idx: int):
        is_custom = (self.resolution_combo.currentText() == "Custom")
        self.custom_res_row.setVisible(is_custom)

    def _set_resolution_combo(self, width: int, height: int):
        """Select the combo entry matching WxH, falling back to Custom.

        Presets such as 5760x2880 and 7680x4320 have no dedicated entry, so
        they must land on "Custom" with the spin boxes populated — otherwise
        the selection silently keeps whatever was there before.
        """
        target = f"{width}x{height}"
        for i in range(self.resolution_combo.count()):
            if self.resolution_combo.itemText(i).endswith(target):
                self._set_combo_by_text(self.resolution_combo, self.resolution_combo.itemText(i))
                break
        else:
            self.custom_width.setValue(int(width))
            self.custom_height.setValue(int(height))
            self._set_combo_by_text(self.resolution_combo, "Custom")
        self._on_resolution_preset_changed(self.resolution_combo.currentIndex())

    def _reset_resolution(self):
        default_w = self._defaults.get("target_width", 1920)
        default_h = self._defaults.get("target_height", 1080)
        self._set_resolution_combo(int(default_w), int(default_h))

    # ----------------------------
    # Theme handling
    # ----------------------------

    def _select_theme(self, name: str):
        """Handle theme button click - activate the selected theme."""
        if name == self._current_theme_name:
            return  # Already selected, do nothing (prevents deselection)
        self._current_theme_name = name
        self._highlight_active_theme(name)
        self.theme_changed.emit(name)
    
    def _highlight_active_theme(self, active_name: str):
        """Update button borders to show which theme is selected."""
        for name, btn in self._theme_buttons.items():
            t_data = THEMES[name]
            if name == active_name:
                # Selected: bright accent border, slightly elevated look
                btn.setStyleSheet(
                    f"QPushButton {{"
                    f"  background-color: {t_data.bg_primary};"
                    f"  color: {t_data.text_primary};"
                    f"  border: 3px solid {t_data.accent};"
                    f"  border-radius: 8px;"
                    f"  font-size: 11px;"
                    f"  font-weight: bold;"
                    f"  padding: 4px;"
                    f"}}"
                )
            else:
                # Not selected: subtle border
                btn.setStyleSheet(
                    f"QPushButton {{"
                    f"  background-color: {t_data.bg_primary};"
                    f"  color: {t_data.text_primary};"
                    f"  border: 2px solid {t_data.border};"
                    f"  border-radius: 8px;"
                    f"  font-size: 11px;"
                    f"  font-weight: bold;"
                    f"  padding: 4px;"
                    f"}}"
                    f"QPushButton:hover {{"
                    f"  border-color: {t_data.accent};"
                    f"  background-color: {t_data.bg_secondary};"
                    f"}}"
                )

    def _toggle_simple_mode(self):
        """Toggle between simple and advanced settings view."""
        self._advanced_mode = not self._advanced_mode
        if self._advanced_mode:
            self._simple_mode_btn.setText("\u25B2  Hide Advanced Settings")
            for group in self._advanced_groups:
                group.show()
        else:
            self._simple_mode_btn.setText("\u25BC  Show Advanced Settings")
            for group in self._advanced_groups:
                group.hide()

class _FileDiscoverySignals(QObject):
    """Signals for file discovery worker."""
    finished = Signal(list)


class _FileDiscoveryRunner(QRunnable):
    """Discovers video files in background to avoid blocking GUI thread."""

    def __init__(self, paths):
        super().__init__()
        self.paths = paths if isinstance(paths, (list, tuple)) else [paths]
        self.signals = _FileDiscoverySignals()
        self.setAutoDelete(True)

    def run(self):
        from frameguard.core import discover_video_files
        all_files = []
        for p in self.paths:
            all_files.extend(discover_video_files(p))
        self.signals.finished.emit(all_files)


class _PdfExportSignals(QObject):
    """Signals for PDF export worker."""
    finished = Signal(str)  # output path on success
    error = Signal(str)     # error message on failure


class _PdfExportRunner(QRunnable):
    """Generates PDF report in background to avoid blocking GUI thread."""

    def __init__(self, results, output_path, preset_label=None):
        super().__init__()
        self.results = results
        self.output_path = output_path
        self.preset_label = preset_label
        self.signals = _PdfExportSignals()
        self.setAutoDelete(True)

    def run(self):
        try:
            from frameguard.report import generate_report
            generate_report(self.results, self.output_path, preset_label=self.preset_label)
            self.signals.finished.emit(self.output_path)
        except Exception as e:
            self.signals.error.emit(str(e))



class ChecksPanel(QWidget):
    """Batch check matrix: every scanned file against every check.

    Answers the question the per-file view can't: "is this one bad export, or
    is the whole batch failing the same check?" Columns are derived from the
    results themselves, so adding or removing a check in core.py needs no
    change here.
    """

    CELL = {
        "PASS": ("\u2713", "status_pass"),
        "FAIL": ("\u2717", "status_fail"),
        "WARN": ("!", "status_warn"),
        "SKIP": ("\u2013", "status_skip"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._results: List[QAResult] = []
        self._columns: List[str] = []
        self._init_ui()

    def _init_ui(self):
        t = get_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(D.SPACE_6, D.SPACE_5, D.SPACE_6, D.SPACE_5)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        self._header = QLabel("Checks")
        self._header.setStyleSheet(
            f"color: {t.text_primary}; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;")
        header_row.addWidget(self._header)
        header_row.addStretch()

        self._legend = QLabel()
        self._legend.setTextFormat(Qt.RichText)
        header_row.addWidget(self._legend)
        layout.addLayout(header_row)

        self._subtitle = QLabel(
            "Every scanned file against every check. Columns with red are failing "
            "across the batch \u2014 usually a wrong preset rather than bad footage."
        )
        self._subtitle.setWordWrap(True)
        layout.addWidget(self._subtitle)

        self._empty = QLabel("No scans yet.\n\nRun a scan and the matrix will appear here.")
        self._empty.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._empty, 1)

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.setWordWrap(False)
        self.table.cellDoubleClicked.connect(self._on_cell_activated)
        self.table.setVisible(False)
        layout.addWidget(self.table, 1)

        self.apply_theme(t)

    # -- data ---------------------------------------------------------------

    def set_results(self, results: List[QAResult]):
        """Rebuild the matrix from the full accumulated result list."""
        self._results = list(results)
        t = get_theme()

        # Column order: first-seen order across the batch, so it matches the
        # order checks actually run in rather than an arbitrary sort.
        columns, seen = [], set()
        for r in self._results:
            for check in r.checks:
                name = check[0]
                if name not in seen:
                    seen.add(name)
                    columns.append(name)
        self._columns = columns

        has_data = bool(self._results and columns)
        self._empty.setVisible(not has_data)
        self.table.setVisible(has_data)
        if not has_data:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            return

        fail_counts = {name: 0 for name in columns}
        for r in self._results:
            for check in r.checks:
                if len(check) > 1 and check[1] == "FAIL":
                    fail_counts[check[0]] = fail_counts.get(check[0], 0) + 1

        self.table.setColumnCount(len(columns) + 1)
        self.table.setHorizontalHeaderLabels(
            ["File"] + [self._column_title(c, fail_counts[c]) for c in columns]
        )
        self.table.setRowCount(len(self._results))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 260)
        # Size each check column to its own header. A flat 104px clipped the
        # longer names ("Black Frames" needs 156px), and a truncated column
        # header in a matrix makes the whole grid unreadable - you cannot tell
        # which check a red cell belongs to.
        metrics = QFontMetrics(self.table.font())
        for col, name in enumerate(columns, start=1):
            title = self._column_title(name, fail_counts[name])
            needed = max(metrics.horizontalAdvance(line) for line in title.split("\n"))
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, max(96, needed + 24))

        for row, r in enumerate(self._results):
            status = _result_status(r)
            name_item = QTableWidgetItem(r.filename)
            name_item.setToolTip(r.filepath)
            if status == "MISMATCH":
                name_item.setFont(ui_font(10, QFont.Bold))
                name_item.setForeground(QColor(t.status_fail))
            self.table.setItem(row, 0, name_item)

            by_name = {c[0]: c for c in r.checks if c}
            for col, check_name in enumerate(columns, start=1):
                check = by_name.get(check_name)
                if check is None:
                    item = QTableWidgetItem("")
                    item.setToolTip(f"{check_name}: not run for this file")
                else:
                    check_status = check[1] if len(check) > 1 else "SKIP"
                    glyph, colour_attr = self.CELL.get(check_status, ("?", "text_secondary"))
                    item = QTableWidgetItem(glyph)
                    item.setForeground(QColor(getattr(t, colour_attr)))
                    item.setFont(ui_font(12, QFont.Bold))
                    detail = check[2] if len(check) > 2 else ""
                    item.setToolTip(
                        f"<b>{check_name}</b> \u2014 {check_status}<br/>{detail}<br/><br/>"
                        f"<i>Double-click for full details</i>"
                    )
                    if check_status == "FAIL":
                        tint = QColor(t.status_fail)
                        tint.setAlpha(38)
                        item.setBackground(tint)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)

        self._update_legend(t)

    @staticmethod
    def _column_title(name: str, fail_count: int) -> str:
        # Keep headers narrow; the failing count is the signal worth surfacing.
        short = name.replace("Container ", "").replace("Audio ", "Aud ")
        return f"{short}\n{fail_count} fail" if fail_count else short

    def clear(self):
        self.set_results([])

    def _on_cell_activated(self, row, _col):
        if 0 <= row < len(self._results):
            DetailPopup(self._results[row], self).exec()

    # -- theming ------------------------------------------------------------

    def _update_legend(self, t):
        self._legend.setText(
            f"<span style='color:{t.status_pass};font-weight:700;'>\u2713</span> pass &nbsp; "
            f"<span style='color:{t.status_fail};font-weight:700;'>\u2717</span> fail &nbsp; "
            f"<span style='color:{t.status_warn};font-weight:700;'>!</span> warn &nbsp; "
            f"<span style='color:{t.status_skip};font-weight:700;'>\u2013</span> skipped"
        )

    def apply_theme(self, t: ThemeColors):
        self._header.setStyleSheet(
            f"color: {t.text_primary}; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;")
        self._subtitle.setStyleSheet(f"color: {t.text_secondary}; font-size: 12px;")
        self._empty.setStyleSheet(f"color: {t.text_tertiary}; font-size: 14px;")
        self._update_legend(t)
        if self._results:
            self.set_results(self._results)


class WatchFolderPanel(QWidget):
    """Watch folder management panel — monitors a directory for new video files."""
    
    # Signals
    scan_files = Signal(list)  # Emits list of files to scan
    
    def __init__(self, monitor: "WatchFolderMonitor" = None, parent=None):
        super().__init__(parent)
        self._monitor = monitor
        self._init_ui()
    
    def set_monitor(self, monitor):
        """Set the WatchFolderMonitor instance."""
        self._monitor = monitor
        if monitor:
            monitor.status_changed.connect(self._update_status)
            monitor.files_detected.connect(self._on_files_detected)
            monitor.error.connect(self._on_error)
    
    def _init_ui(self):
        from frameguard.theme import get_theme
        t = get_theme()
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(D.SPACE_6, D.SPACE_5, D.SPACE_6, D.SPACE_5)
        layout.setSpacing(20)
        
        # Header
        header = QLabel("Watch Folder")
        header.setStyleSheet(f"color: {t.text_primary}; font-size: 20px; font-weight: bold; letter-spacing: -0.5px; border: none;")
        layout.addWidget(header)
        
        subtitle = QLabel("Automatically scan new video files as they appear in a folder")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; border: none;")
        layout.addWidget(subtitle)
        
        layout.addSpacing(12)
        
        # Enable toggle
        self._enable_check = QCheckBox("Enable auto-scan")
        self._enable_check.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; spacing: 10px;")
        self._enable_check.toggled.connect(self._on_toggle)
        layout.addWidget(self._enable_check)
        
        # Auto-export PDF after scan
        self._auto_export_check = QCheckBox("Auto-export PDF report after scan")
        self._auto_export_check.setToolTip("Automatically saves a PDF to Documents/FrameGuard when a scan completes")
        self._auto_export_check.setChecked(False)
        self._auto_export_check.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; spacing: 10px;")
        layout.addWidget(self._auto_export_check)
        
        # Launch at login (Windows Run key / macOS LaunchAgent)
        self._startup_check = QCheckBox(
            "Start FrameGuard when I log in"
            if sys.platform != "win32" else "Start FrameGuard with Windows"
        )
        self._startup_check.setToolTip("Launch FrameGuard automatically when you log in")
        self._startup_check.setChecked(self._is_startup_enabled())
        self._startup_check.toggled.connect(self._toggle_startup)
        self._startup_check.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; spacing: 10px;")
        self._startup_check.setEnabled(sys.platform in ("win32", "darwin"))
        layout.addWidget(self._startup_check)
        
        layout.addSpacing(8)
        
        # Folder path row
        path_group = QHBoxLayout()
        path_group.setSpacing(8)
        
        self._path_label = QLabel("No folder selected")
        self._path_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 14px; padding: 8px 12px; "
            f"background: {t.bg_tertiary}; border-radius: 6px; border: none;"
        )
        path_group.addWidget(self._path_label, 1)
        
        self._browse_btn = QPushButton("Browse...")
        self._browse_btn.setMinimumWidth(D.SPACE_6 * 4)  # label needs ~131px
        self._browse_btn.setFixedHeight(D.CONTROL_MD)
        self._browse_btn.clicked.connect(self._browse)
        path_group.addWidget(self._browse_btn)
        
        self._sync_btn = QPushButton("⟳ Sync")
        self._sync_btn.setMinimumWidth(D.SPACE_6 * 3)   # label needs ~92px
        self._sync_btn.setFixedHeight(D.CONTROL_MD)
        self._sync_btn.setToolTip("Re-scan folder to pick up any missed files")
        self._sync_btn.clicked.connect(self._on_sync)
        self._sync_btn.setEnabled(False)
        path_group.addWidget(self._sync_btn)

        layout.addLayout(path_group)

        # Watching only picks up files that arrive AFTER it starts. Pointing it
        # at a folder that already has content and seeing nothing happen is the
        # most common confusion, so make the opt-in explicit.
        self._backlog_btn = QPushButton("Scan files already in this folder")
        self._backlog_btn.setFixedHeight(D.CONTROL_MD)
        self._backlog_btn.setToolTip(
            "Watching only picks up files that arrive after it starts.\n"
            "Use this to queue everything already in the folder.")
        self._backlog_btn.clicked.connect(self._on_scan_backlog)
        self._backlog_btn.setEnabled(False)
        layout.addWidget(self._backlog_btn)
        
        # Status area
        layout.addSpacing(16)
        
        self._status_label = QLabel("Status: Inactive")
        self._status_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 14px; font-style: italic; border: none;"
        )
        layout.addWidget(self._status_label)
        
        # Activity log
        self._log = QLabel("No activity yet")
        self._log.setStyleSheet(
            f"color: {t.text_tertiary}; font-size: 12px; padding: 12px; "
            f"background: {t.bg_tertiary}; border-radius: 6px; border: none;"
        )
        self._log.setWordWrap(True)
        self._log.setMinimumHeight(80)
        layout.addWidget(self._log)
        
        layout.addStretch()
        
        # Info footer
        info = QLabel("Files are scanned using the profile selected on the Scan page.\n"
                      "New files are detected via polling (works on all drives).\n"
                      "Use Sync to manually pick up any files missed during batch placement.")
        info.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; border: none;")
        info.setWordWrap(True)
        layout.addWidget(info)
    
    def _browse(self):
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(
            self, "Select Watch Folder",
            str(Path.home() / "Videos")
        )
        if path:
            self._path_label.setText(path)
            self._path_label.setToolTip(path)
            if self._monitor:
                self._monitor.set_watch_path(path)
    
    def _on_toggle(self, enabled: bool):
        if not self._monitor:
            return
        if enabled:
            if not self._monitor.watch_path:
                self._browse()
            if self._monitor.watch_path:
                self._monitor.start()
                self._sync_btn.setEnabled(True)
                self._backlog_btn.setEnabled(True)
            else:
                self._enable_check.setChecked(False)
        else:
            self._monitor.stop()
            self._sync_btn.setEnabled(False)
            self._backlog_btn.setEnabled(False)
    
    def save_watch_settings(self):
        """Persist the watch configuration.

        "Start FrameGuard when I log in" was already persisted while the watch
        folder was not, so the documented set-and-forget workflow produced an
        app that launched faithfully at every login and monitored nothing.
        """
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        monitor = self._monitor
        settings.setValue("watch/path", monitor.watch_path if monitor else "")
        settings.setValue("watch/enabled", bool(self._enable_check.isChecked()))
        settings.setValue("watch/auto_export", bool(self._auto_export_check.isChecked()))

    def restore_watch_settings(self):
        """Reload the watch configuration and resume watching if it was on."""
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        path = settings.value("watch/path", "") or ""

        auto_export = settings.value("watch/auto_export", False)
        if isinstance(auto_export, str):
            auto_export = auto_export.lower() == "true"
        self._auto_export_check.setChecked(bool(auto_export))

        if not path or not os.path.isdir(path):
            # Folder is gone (unplugged drive, deleted share). Don't silently
            # resume against nothing.
            if path:
                self._log.setText(
                    f"Previous watch folder is unavailable:\n{path}")
            return

        if self._monitor:
            self._monitor.set_watch_path(path)
        self._path_label.setText(path)

        enabled = settings.value("watch/enabled", False)
        if isinstance(enabled, str):
            enabled = enabled.lower() == "true"
        if enabled:
            was_blocked = self._enable_check.blockSignals(True)
            self._enable_check.setChecked(True)
            self._enable_check.blockSignals(was_blocked)
            self._on_toggle(True)

    def _on_scan_backlog(self):
        """Queue everything already in the folder, after confirming the count."""
        if not self._monitor or not self._monitor.watch_path:
            return
        found = self._monitor._scan_directory()
        count = len(found) if found else 0
        if not count:
            QMessageBox.information(
                self, "Nothing to Scan",
                "No scannable video files were found in this folder.")
            return
        reply = QMessageBox.question(
            self, "Scan Existing Files",
            f"Queue {count} file{'s' if count != 1 else ''} already in this folder "
            f"for scanning?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.Yes:
            self._monitor.scan_backlog()

    def _on_sync(self):
        """Manual sync — re-scan the folder and pick up missed files."""
        if self._monitor:
            self._monitor.sync()
    
    def _update_status(self, status: str):
        self._status_label.setText(f"Status: {status}")
    
    def _on_files_detected(self, files: list):
        count = len(files)
        names = ", ".join(os.path.basename(f) for f in files[:3])
        if count > 3:
            names += f" (+{count-3} more)"
        self._log.setText(f"Detected {count} new file(s):\n{names}")
        self.scan_files.emit(files)
    
    def _on_error(self, msg: str):
        self._status_label.setText(f"Status: Error - {msg}")
    
    def apply_theme(self, t):
        """Update all panel widgets to match the new theme."""
        # Header
        for child in self.findChildren(QLabel):
            style = child.styleSheet()
            if 'font-size: 20px' in style:
                child.setStyleSheet(f"color: {t.text_primary}; font-size: 20px; font-weight: bold; border: none;")
            elif 'font-size: 14px' in style and 'italic' not in style:
                child.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; border: none;")
            elif 'italic' in style:
                child.setStyleSheet(f"color: {t.text_secondary}; font-size: 14px; font-style: italic; border: none;")
            elif 'font-size: 12px' in style or 'background' in style:
                child.setStyleSheet(
                    f"color: {t.text_tertiary}; font-size: 12px; padding: 12px; "
                    f"background: {t.bg_tertiary}; border-radius: 6px; border: none;"
                )
            elif 'font-size: 11px' in style:
                child.setStyleSheet(f"color: {t.text_tertiary}; font-size: 11px; border: none;")
            elif 'padding: 8px' in style: child.setStyleSheet( f"color: {t.text_secondary}; font-size: 14px; padding: 8px 12px; "
                    f"background: {t.bg_tertiary}; border-radius: 6px; border: none;"
                )
        
        # Checkbox
        if hasattr(self, '_enable_check'):
            self._enable_check.setStyleSheet(f"color: {t.text_primary}; font-size: 14px; spacing: 10px;")
        
        # Browse button uses app-level stylesheet (no inline override needed)rent




    # r"...\\..." would be a LITERAL double backslash and every OpenKey fails
    # silently, which is why "Start with Windows" never did anything.
    _RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _RUN_VALUE = "FrameGuard Desktop"
    _LAUNCH_AGENT_ID = "com.caprilaria.frameguard.launchatlogin"

    def _launch_agent_path(self):
        from pathlib import Path
        return Path.home() / "Library" / "LaunchAgents" / f"{self._LAUNCH_AGENT_ID}.plist"

    def _is_startup_enabled(self) -> bool:
        """Check whether FrameGuard is set to launch at login."""
        if sys.platform == "darwin":
            return self._launch_agent_path().is_file()
        if sys.platform != "win32":
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY, 0, winreg.KEY_READ) as key:
                try:
                    winreg.QueryValueEx(key, self._RUN_VALUE)
                    return True
                except FileNotFoundError:
                    return False
        except OSError:
            return False

    def _launch_command(self) -> list:
        """Argv that relaunches this build of FrameGuard."""
        if getattr(sys, "frozen", False):
            if sys.platform == "darwin":
                # sys.executable is Contents/MacOS/FrameGuard; launch the
                # bundle itself so LaunchServices treats it as an app.
                marker = ".app/Contents/MacOS/"
                if marker in sys.executable:
                    return ["/usr/bin/open", "-a",
                            sys.executable.split("/Contents/MacOS/")[0]]
            return [sys.executable]
        main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "main.py"))
        return [sys.executable, main_py]

    def _toggle_startup(self, enabled: bool):
        """Add or remove FrameGuard from the OS login items."""
        try:
            if sys.platform == "darwin":
                self._toggle_startup_macos(enabled)
            elif sys.platform == "win32":
                self._toggle_startup_windows(enabled)
            else:
                return
        except OSError as e:
            was_blocked = self._startup_check.blockSignals(True)
            self._startup_check.setChecked(not enabled)
            self._startup_check.blockSignals(was_blocked)
            QMessageBox.warning(
                self, "Startup Setting Failed",
                f"Could not update your login items.\n\n{e}"
            )

    def _toggle_startup_macos(self, enabled: bool):
        plist_path = self._launch_agent_path()
        if not enabled:
            plist_path.unlink(missing_ok=True)
            return

        import plistlib
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": self._LAUNCH_AGENT_ID,
            "ProgramArguments": self._launch_command(),
            "RunAtLoad": True,
            # Login item only — do not restart it if the user quits.
            "KeepAlive": False,
        }
        with open(plist_path, "wb") as f:
            plistlib.dump(payload, f)

    def _toggle_startup_windows(self, enabled: bool):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = " ".join(f'"{part}"' for part in self._launch_command())
                winreg.SetValueEx(key, self._RUN_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, self._RUN_VALUE)
                except FileNotFoundError:
                    pass

class MainWindow(QMainWindow):
    """FrameGuard Desktop main window."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle(f"FrameGuard Desktop v{APP_VERSION}")
        self.setMinimumSize(1000, 700)
        self.resize(1200, 800)

        # Frameless window with a custom title bar on Windows, where we also
        # reimplement Aero Snap via WM_NCHITTEST. On macOS the same flag would
        # strip the traffic lights, the window shadow, native resize on all
        # edges and green-button fullscreen — so use native chrome there.
        #
        # This must happen BEFORE the WS_THICKFRAME call below: setWindowFlags
        # destroys and recreates the native window, so a style applied to the
        # old HWND is silently discarded and the resize borders never work.
        if USE_CUSTOM_CHROME:
            self.setWindowFlags(
                Qt.FramelessWindowHint | Qt.Window
                | Qt.WindowMinMaxButtonsHint  # keeps taskbar right-click menu
            )

        # Enable Windows Aero Snap (drag-to-edge resize gestures).
        # Frameless windows lose this by default; WS_THICKFRAME restores it.
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(self.winId())
                GWL_STYLE = -16
                WS_THICKFRAME = 0x00040000
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
                style |= WS_THICKFRAME
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            except (OSError, AttributeError):
                pass  # Non-critical: snap gestures just won't work

        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAcceptDrops(True)

        self._worker: Optional[ScanWorker] = None
        self._worker_thread: Optional[QThread] = None
        self._cancel_connected = False
        # Guards the Settings <-> preset-dropdown two-way sync against re-entry.
        self._syncing_preset = False
        # name -> engine config dict, for presets imported at runtime
        self._imported_presets: dict = {}
        # Keeps background PDF export runnables (and their signal objects) alive
        # until their queued completion signal has been delivered.
        self._export_runners: list = []
        # True while a watch-folder-triggered batch is running, so the
        # per-scan Results reset does not wipe the unattended run's history.
        self._watch_batch_in_progress = False
        # Files the watcher found while a scan was already running. Created
        # here, not lazily on first detection: _on_scan_cancelled reads it
        # unguarded, so cancelling any scan before the watcher had ever fired
        # raised AttributeError and tripped the crash handler.
        self._watch_pending_queue: list = []

        # Logo pixmaps for recoloring
        self._logo_pixmap_original = None
        self._sidebar_logo_label = None
        self._sidebar_logo_small = None

        # Ensure Documents/FrameGuard folder exists for PDF exports
        self._docs_folder = os.path.join(
            os.path.expanduser("~"), "Documents", "FrameGuard"
        )
        os.makedirs(self._docs_folder, exist_ok=True)

        self._init_ui()
        # The ffmpeg -version probe spawns two subprocesses; on a slow or
        # hung tool install that blocked first paint entirely. Defer it so
        # the window shows immediately and the warning lands afterwards.
        QTimer.singleShot(500, lambda: QApplication.platformName() != "offscreen" and self._check_ffmpeg())

        # Restore the persisted scan configuration before the theme cascade so
        # the preset dropdown resolves against the values actually in effect.
        self.settings_panel.restore_settings()
        # Resume watching if it was left on. Deferred so the monitor and its
        # signal connections exist first.
        QTimer.singleShot(0, self.watch_panel.restore_watch_settings)
        self._sync_preset_from_settings()

        # Apply persisted theme on startup
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        saved_theme = settings.value("theme", "Meta Dark")
        if saved_theme in THEMES:
            # Always route through _apply_theme, even for the default: it also
            # syncs the Settings panel's selected-theme highlight and the tray
            # icon tint. Skipping it left the panel showing "Meta Dark" while
            # the app rendered something else, making that button a no-op.
            self._apply_theme(saved_theme)

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        # Outer layout: title bar on top, content below
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # Custom title bar (Windows only — macOS uses its native titlebar).
        # Still constructed on macOS so theme cascades and the icon-update path
        # don't need platform branches everywhere; it just isn't shown.
        self._title_bar = TitleBar(self)
        self._title_bar.setVisible(USE_CUSTOM_CHROME)
        outer_layout.addWidget(self._title_bar)

        # Content row: sidebar + toggle + stack
        content_row = QWidget()
        main_layout = QHBoxLayout(content_row)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        t = get_theme()

        # ---- Sidebar ----
        self._sidebar = QWidget()
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setMinimumWidth(0)
        self._sidebar.setMaximumWidth(200)
        self._sidebar_expanded_width = 200
        self._sidebar_collapsed = False
        sidebar_layout = QVBoxLayout(self._sidebar)
        # No horizontal inset here: the banner at the top is full-bleed, so the
        # gutter is applied to the body container below instead. Otherwise the
        # banner sat 8px in from each edge and read as a floating panel rather
        # than a header.
        sidebar_layout.setContentsMargins(D.SPACE_0, D.SPACE_0, D.SPACE_0, D.SPACE_4)
        sidebar_layout.setSpacing(D.SPACE_1)

        # Logo area with icon — spans the full sidebar width.
        logo_area = QWidget()
        logo_area.setObjectName("logo_area")
        logo_layout = QHBoxLayout(logo_area)
        # The banner box is full-bleed, but its *contents* still line up with
        # the nav labels: body gutter (SPACE_2) + the nav button's own left
        # padding (12px, set in the stylesheet).
        logo_layout.setContentsMargins(
            D.SPACE_2 + D.SPACE_3, D.SPACE_4, D.SPACE_3, D.SPACE_3)
        logo_layout.setSpacing(D.SPACE_2)

        # Logo icon (small, next to text)
        self._sidebar_logo_small = QLabel()
        if os.path.isfile(_LOGO_PATH):
            self._logo_pixmap_original = QPixmap(_LOGO_PATH)
            pix = self._logo_pixmap_original.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._sidebar_logo_small.setPixmap(pix)
        self._sidebar_logo_small.setFixedSize(28, 28)
        logo_layout.addWidget(self._sidebar_logo_small)

        logo_text_area = QVBoxLayout()
        logo_text_area.setSpacing(0)
        logo = QLabel("FrameGuard")
        logo.setObjectName("logo_label")
        version = QLabel(f"v{APP_VERSION}")
        version.setObjectName("version_label")
        logo_text_area.addWidget(logo)
        logo_text_area.addWidget(version)
        logo_layout.addLayout(logo_text_area)
        logo_layout.addStretch()

        sidebar_layout.addWidget(logo_area)

        # Everything below the banner keeps the 8px gutter that makes each nav
        # row read as an inset pill.
        sidebar_body = QWidget()
        sidebar_body.setStyleSheet("background: transparent; border: none;")
        body_layout = QVBoxLayout(sidebar_body)
        body_layout.setContentsMargins(D.SPACE_2, D.SPACE_1, D.SPACE_2, D.SPACE_0)
        body_layout.setSpacing(D.SPACE_1)
        sidebar_layout.addWidget(sidebar_body, 1)

        # Nav buttons. Real vector icons rather than Unicode glyphs: the old set
        # mixed geometric shapes with a colour emoji and depended on whatever
        # font the OS substituted.
        self.nav_buttons = []
        self._results_badge_count = 0  # New results not yet seen
        self._nav_icon_names = ["scan", "results", "checks", "watch", "settings"]
        for idx, (icon_name, label) in enumerate(zip(
                self._nav_icon_names, ["Scan", "Results", "Checks", "Watch", "Settings"])):
            btn = QPushButton(f"   {label}")
            btn.setProperty("active", idx == 0)
            btn.setIconSize(QSize(20, 20))
            btn.setMinimumHeight(D.CONTROL_LG)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, i=idx: self._switch_panel(i))
            body_layout.addWidget(btn)
            self.nav_buttons.append(btn)
        self._refresh_nav_icons()

        body_layout.addStretch()

        # Import Preset lives here rather than buried in Settings: importing a
        # teammate's .fg is the most common preset action.
        self._sidebar_import_btn = QPushButton("   Import Preset")
        self._sidebar_import_btn.setObjectName("secondary_btn")
        self._sidebar_import_btn.setIconSize(QSize(18, 18))
        self._sidebar_import_btn.setMinimumHeight(D.CONTROL_MD)
        self._sidebar_import_btn.setToolTip(
            "Load a .fg preset shared by a teammate (or just drag one onto the window)"
        )
        self._sidebar_import_btn.clicked.connect(
            lambda: self.settings_panel._import_preset_file()
        )
        body_layout.addWidget(self._sidebar_import_btn)

        # Bottom sidebar
        sidebar_bottom = QVBoxLayout()
        sidebar_bottom.setSpacing(D.SPACE_2)

        self._sidebar_logo_label = QLabel()
        if os.path.isfile(_LOGO_PATH) and self._logo_pixmap_original:
            pix = self._logo_pixmap_original.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._sidebar_logo_label.setPixmap(pix)
        self._sidebar_logo_label.setAlignment(Qt.AlignCenter)
        self._sidebar_logo_label.setStyleSheet("padding: 0px; border: none;")
        sidebar_bottom.addWidget(self._sidebar_logo_label)

        self._dss_label = QLabel("DSS-2 Compliant")
        self._dss_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 10px; padding: 4px 8px; "
            f"border: 1px solid {t.border}; border-radius: 4px;"
        )
        self._dss_label.setAlignment(Qt.AlignCenter)
        sidebar_bottom.addWidget(self._dss_label)

        self._version_footer = QLabel(f"v{APP_VERSION}")
        self._version_footer.setStyleSheet(
            f"color: {t.text_tertiary}; font-size: 9px; padding: 0px;"
        )
        self._version_footer.setAlignment(Qt.AlignCenter)
        sidebar_bottom.addWidget(self._version_footer)

        body_layout.addLayout(sidebar_bottom)

        # Sidebar toggle button (hamburger icon)
        self._toggle_container = QWidget()
        self._toggle_container.setFixedWidth(24)
        self._toggle_container.setStyleSheet("background: transparent; border: none;")
        toggle_layout = QVBoxLayout(self._toggle_container)
        toggle_layout.setContentsMargins(0, 8, 0, 0)
        toggle_layout.setSpacing(0)

        self._sidebar_toggle_btn = QPushButton("\u25C0")  # left arrow = collapse
        self._sidebar_toggle_btn.setFixedSize(D.SPACE_5, D.CONTROL_SM)
        self._sidebar_toggle_btn.setToolTip("Toggle sidebar")
        self._sidebar_toggle_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {t.text_secondary}; "
            f"font-size: 16px; padding: 0px; }}"
            f"QPushButton:hover {{ color: {t.accent}; }}"
        )
        self._sidebar_toggle_btn.clicked.connect(self._toggle_sidebar)
        toggle_layout.addWidget(self._sidebar_toggle_btn)
        toggle_layout.addStretch()

        main_layout.addWidget(self._sidebar)
        main_layout.addWidget(self._toggle_container)

        # ---- Content area ----
        self.stack = QStackedWidget()
        self.stack.setObjectName("content_area")

        self.scan_panel = ScanPanel()
        self.scan_panel.scan_requested.connect(self._on_scan_requested)
        self.scan_panel.drop_zone.preset_dropped.connect(self._on_preset_dropped)
        self.scan_panel.edit_profile_requested.connect(self._show_profile_editor)

        self.results_panel = ResultsPanel()
        self.results_panel.export_requested.connect(self._export_pdf)

        self.settings_panel = SettingsPanel()

        self.stack.addWidget(self.scan_panel)
        self.stack.addWidget(self.results_panel)

        # Checks matrix (batch view of every check across every file)
        self.checks_panel = ChecksPanel()
        self.stack.addWidget(self.checks_panel)
        self.results_panel.history_cleared.connect(self.checks_panel.clear)
        # The Scan page verdict summarizes the Results page, so it must clear
        # with it — otherwise a FAIL banner stays "permanent" over an empty
        # table after Clear Results.
        self.results_panel.history_cleared.connect(
            lambda: self.scan_panel.hide_completion("history-cleared"))

        # Watch Folder Panel (its own tab)
        self.watch_panel = WatchFolderPanel(parent=self)
        self.stack.addWidget(self.watch_panel)
        
        self.stack.addWidget(self.settings_panel)

        # Wire theme switching
        
        # --- Watch Folder Monitor ---
        self._force_quit = False
        self._watch_monitor = WatchFolderMonitor(self)
        
        # Auto-update manager (checks once per 24h on startup)
        self._update_manager = UpdateManager(self)
        self._update_manager.update_available.connect(self._on_update_available)
        self._update_manager.delta_update_available.connect(self._on_delta_update_available)
        self._update_manager.start_auto_check(delay_seconds=8)
        # Share it with the Settings panel so "Check for Updates" doesn't spawn
        # a second manager with its own duplicate signal connections.
        self.settings_panel._update_manager = self._update_manager
        self.watch_panel.set_monitor(self._watch_monitor)
        # Deliberately NOT connected to scan_panel._set_files: that call
        # re-enables the Start Scan button, so a watch detection arriving
        # during a running scan let the user launch a second concurrent scan
        # that orphaned the first worker thread. _on_watch_files_detected owns
        # the queue and applies the running-scan guard.
        self._watch_monitor.files_detected.connect(self._on_watch_files_detected)
        self._watch_monitor.status_changed.connect(self._on_watch_status_changed)
        self._watch_monitor.error.connect(self._on_watch_error)
        self._watch_monitor.folder_lost.connect(self._on_watch_folder_lost)
        # (Auto-update handled by self._update_manager above)
        # --- Keyboard Shortcuts ---
        from PySide6.QtGui import QShortcut, QKeySequence
        
        # Navigation
        # Navigation (indices follow the sidebar order: Scan, Results, Checks, Watch, Settings)
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._switch_panel(0))  # Scan
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._switch_panel(1))  # Results
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self._switch_panel(2))  # Checks
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self._switch_panel(3))  # Watch
        QShortcut(QKeySequence("Ctrl+5"), self, lambda: self._switch_panel(4))  # Settings
        
        # Actions
        QShortcut(QKeySequence("Ctrl+O"), self, self._shortcut_browse)
        QShortcut(QKeySequence("F5"), self, self._shortcut_scan)
        QShortcut(QKeySequence("Ctrl+E"), self, self._shortcut_export)
        QShortcut(QKeySequence("Ctrl+Q"), self, self._tray_quit)

        # --- System Tray (minimize to tray when auto-scan active) ---
        self._tray_icon = None
        self._tray_scan_count = 0  # Files scanned while minimized
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon = QSystemTrayIcon(self)
            icon_path = _resource_path(os.path.join("assets", "frameguard.ico"))
            if os.path.isfile(icon_path):
                self._tray_icon.setIcon(QIcon(icon_path))
            self._tray_icon.setToolTip("FrameGuard Desktop — Idle")
            
            # Tray context menu (richer controls while minimized)
            from PySide6.QtWidgets import QMenu
            tray_menu = QMenu()
            self._tray_status_action = tray_menu.addAction("Status: Idle")
            self._tray_status_action.setEnabled(False)
            tray_menu.addSeparator()
            tray_menu.addAction("Show FrameGuard", self._tray_show)
            self._tray_pause_action = tray_menu.addAction("Pause Watch", self._tray_toggle_watch)
            self._tray_sync_action = tray_menu.addAction("Sync Now", self._tray_sync)
            tray_menu.addSeparator()
            tray_menu.addAction("Report a Problem...", self._report_problem)
            tray_menu.addAction("About", self._show_about)
            tray_menu.addAction("Quit", self._tray_quit)
            self._tray_icon.setContextMenu(tray_menu)
            self._tray_icon.activated.connect(self._on_tray_activated)
            
            # Heartbeat timer: update tray tooltip every 30s while minimized
            self._tray_heartbeat = QTimer(self)
            self._tray_heartbeat.timeout.connect(self._update_tray_status)
            self._tray_heartbeat.setInterval(30000)



        self.settings_panel.theme_changed.connect(self._apply_theme)
        self.settings_panel.preset_imported.connect(self._on_preset_imported)
        self.settings_panel.install_update_requested.connect(self._confirm_install_update)
        self.settings_panel.report_problem_requested.connect(self._report_problem)
        self.settings_panel.delta_update_requested.connect(self._apply_differential_update)
        self.settings_panel.settings_changed.connect(self._sync_preset_from_settings)
        self.scan_panel.preset_combo.currentIndexChanged.connect(self._sync_settings_from_preset)


        main_layout.addWidget(self.stack)

        outer_layout.addWidget(content_row)

        # Resize grip, bottom-right. Only needed with custom chrome; macOS
        # resizes from any edge natively and the grip looks foreign there.
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.setStyleSheet("background: transparent; border: none;")
        self._size_grip.setVisible(USE_CUSTOM_CHROME)

        # Set window icon
        if os.path.isfile(_ICON_PATH):
            self.setWindowIcon(QIcon(_ICON_PATH))
        elif os.path.isfile(_LOGO_PATH):
            self.setWindowIcon(QIcon(_LOGO_PATH))



    def _refresh_nav_icons(self, theme=None):
        """Re-render the sidebar icons in the current theme colour.

        The active row uses the accent colour and inactive rows the secondary
        text colour, so the icon carries the same emphasis as its label.
        """
        from frameguard import icons as _icons

        t = theme or get_theme()
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0

        for idx, btn in enumerate(getattr(self, "nav_buttons", [])):
            name = self._nav_icon_names[idx]
            active = bool(btn.property("active"))
            color = t.accent if active else t.text_secondary
            btn.setIcon(_icons.make_icon(name, 20, color, dpr))

        if getattr(self, "_sidebar_import_btn", None) is not None:
            self._sidebar_import_btn.setIcon(
                _icons.make_icon("import", 18, t.text_secondary, dpr))

    def _toggle_sidebar(self):
        """Smoothly collapse/expand the entire sidebar widget to/from 0 width."""
        self._sidebar_collapsed = not self._sidebar_collapsed

        start_w = self._sidebar.width()
        end_w = 0 if self._sidebar_collapsed else self._sidebar_expanded_width

        # Animate both maximumWidth AND fixedWidth for true collapse
        self._sidebar_anim = QPropertyAnimation(self._sidebar, b"maximumWidth")
        self._sidebar_anim.setDuration(250)
        self._sidebar_anim.setStartValue(start_w)
        self._sidebar_anim.setEndValue(end_w)
        self._sidebar_anim.setEasingCurve(QEasingCurve.InOutCubic)

        # Also animate minimumWidth to allow true 0-width collapse
        self._sidebar_anim2 = QPropertyAnimation(self._sidebar, b"minimumWidth")
        self._sidebar_anim2.setDuration(250)
        self._sidebar_anim2.setStartValue(start_w)
        self._sidebar_anim2.setEndValue(end_w)
        self._sidebar_anim2.setEasingCurve(QEasingCurve.InOutCubic)

        if not self._sidebar_collapsed:
            # Expanding — show widget first
            self._sidebar.setVisible(True)

        def _on_finished():
            if self._sidebar_collapsed:
                self._sidebar.setVisible(False)
            self._sidebar_toggle_btn.setText(
                "\u25B6" if self._sidebar_collapsed else "\u25C0"
            )
            self._sidebar_toggle_btn.setToolTip(
                "Show sidebar" if self._sidebar_collapsed else "Hide sidebar"
            )

        self._sidebar_anim.finished.connect(_on_finished)
        self._sidebar_anim.start()
        self._sidebar_anim2.start()


    def _shortcut_browse(self):
        """Ctrl+O: Open file browser on Scan tab."""
        self._switch_panel(0)
        self.scan_panel._browse_combined()
    
    def _shortcut_scan(self):
        """F5: Start scan if files are loaded."""
        if self.scan_panel._files:
            self.scan_panel.scan_btn.click()
    
    def _shortcut_export(self):
        """Ctrl+E: Export PDF if results exist."""
        if self.results_panel._results:
            self._export_pdf()

    def _switch_panel(self, index: int):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setProperty("active", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        # The icon carries the same emphasis as the label, so it has to follow
        # the active state too.
        self._refresh_nav_icons()

        # Clear Results badge when user views Results tab
        if index == PANEL_RESULTS and self._results_badge_count > 0:
            self._results_badge_count = 0
            self._update_results_badge()

        # Navigating away from Scan dismisses clean verdicts and transient
        # notes so neither goes stale; failures stay until the next scan or
        # a file change.
        if index != PANEL_SCAN and hasattr(self.scan_panel, "_banner_state"):
            if self.scan_panel._banner_state in ("info",):
                self.scan_panel.hide_completion("navigated")
            elif (self.scan_panel._banner_state == "done"
                    and self.scan_panel._banner_counts[1] == 0):
                self.scan_panel.hide_completion("navigated")

    def _show_profile_editor(self):
        """Jump to the delivery-spec controls from the Scan page.

        Operators asked for a way to enter custom profile values without
        having to author and import a preset file. The controls already
        existed under Settings, but nothing led there, and the deep-scan and
        audio settings were collapsed behind "Show Advanced Settings" with no
        indication they were there at all.
        """
        self._switch_panel(PANEL_SETTINGS)
        sp = self.settings_panel
        if not getattr(sp, "_advanced_mode", False):
            sp._toggle_simple_mode()
        # Selecting Custom is what makes the Settings values authoritative;
        # leaving a built-in preset selected would override them at scan time.
        combo = self.scan_panel.preset_combo
        for i in range(combo.count()):
            if combo.itemData(i) == "custom":
                combo.setCurrentIndex(i)
                break

    def _apply_theme(self, theme_name: str):
        """Apply a new color theme live to every visible pixel, recolor logo, persist."""
        set_current_theme(theme_name)
        theme = get_theme(theme_name)
        stylesheet = generate_stylesheet(theme)
        QApplication.instance().setStyleSheet(stylesheet)

        t = theme  # shorthand

        # === Recolor logo ===
        if self._logo_pixmap_original:
            accent_color = QColor(t.accent)
            tinted = _tint_pixmap(self._logo_pixmap_original, accent_color)

            if self._sidebar_logo_small:
                pix = tinted.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._sidebar_logo_small.setPixmap(pix)

            if self._sidebar_logo_label:
                pix = tinted.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self._sidebar_logo_label.setPixmap(pix)

        # === Sidebar inline styles ===
        self._dss_label.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 10px; padding: 4px 8px; "
            f"border: 1px solid {t.border}; border-radius: 4px;"
        )
        self._version_footer.setStyleSheet(
            f"color: {t.text_tertiary}; font-size: 9px; padding: 0px;"
        )
        self._sidebar_toggle_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {t.text_secondary}; "
            f"font-size: 16px; padding: 0px; }}"
            f"QPushButton:hover {{ color: {t.accent}; }}"
        )

        # === Cascade to title bar and all panels ===
        self._title_bar.apply_theme(t)
        self.scan_panel.apply_theme(t)
        self.results_panel.apply_theme(t)
        self.checks_panel.apply_theme(t)
        self.settings_panel.apply_theme(t)
        if hasattr(self, "watch_panel"): self.watch_panel.apply_theme(t)

        # Keep the Settings page's own idea of the active theme in sync,
        # otherwise its highlight and _select_theme's early-return guard drift
        # out of step with what is actually rendered.
        self.settings_panel._current_theme_name = theme_name
        self.settings_panel._highlight_active_theme(theme_name)

        # === Persist theme choice ===
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue("theme", theme_name)


        # Force theme colors on all QComboBox popups (Windows fix)
        from PySide6.QtWidgets import QComboBox
        for combo in self.findChildren(QComboBox):
            _style_combobox_popup(combo, t)

        # Recolor everything the OS draws for us, plus the tray badge.
        self._apply_window_chrome_theme(t)
        self._title_bar.set_icon_pixmap(self._themed_app_pixmap(t, 32))
        self.setWindowIcon(QIcon(self._themed_app_pixmap(t, 64)))
        self._update_tray_icon_theme(t)

        # Vector nav icons are baked at a specific colour, so they have to be
        # re-rendered rather than restyled.
        from frameguard import icons as _icons
        _icons.clear_cache()
        self._refresh_nav_icons(t)

    def _check_ffmpeg(self):
        ok, msg = check_ffmpeg_available()
        if not ok:
            QMessageBox.critical(
                self, "FFmpeg Not Found",
                f"FrameGuard requires FFmpeg and FFprobe to function.\n\n{msg}\n\n"
                "Please install FFmpeg and ensure it is in your system PATH."
            )


    def _sync_preset_from_settings(self):
        """When settings change, check if they match a known preset and update the scan panel dropdown."""
        if self._syncing_preset:
            return

        cfg = self.settings_panel.get_config_overrides()
        current_width = cfg.get("target_width", 0)
        current_height = cfg.get("target_height", 0)
        current_fps = cfg.get("target_fps", 0)
        current_bitrate = cfg.get("min_bitrate_mbps", 0)

        matched_preset = None
        for key, preset in PRESETS.items():
            if (preset["width"] == current_width and
                preset["height"] == current_height and
                abs(preset["fps"] - current_fps) < 0.01 and
                preset["min_bitrate_mbps"] == current_bitrate):
                matched_preset = key
                break

        combo = self.scan_panel.preset_combo
        was_blocked = combo.blockSignals(True)
        try:
            if matched_preset:
                for i in range(combo.count()):
                    if combo.itemData(i) == matched_preset:
                        combo.setCurrentIndex(i)
                        break
            elif not str(combo.currentData() or "").startswith("imported:"):
                # Don't yank the user out of an imported preset just because it
                # doesn't correspond to one of the nine built-ins.
                combo.setCurrentIndex(0)  # "Custom (config defaults)" is first item
        finally:
            combo.blockSignals(was_blocked)

    def _sync_settings_from_preset(self, index):
        """When scan panel preset changes, update settings panel to match."""
        preset_key = self.scan_panel.preset_combo.currentData()

        if isinstance(preset_key, str) and preset_key.startswith("imported:"):
            config = self._imported_presets.get(preset_key[len("imported:"):])
            if not config:
                return
            self._syncing_preset = True
            try:
                self.settings_panel._apply_preset_config(config)
            finally:
                self._syncing_preset = False
            return

        if preset_key == "custom" or preset_key not in PRESETS:
            return  # Don't change settings for custom

        preset = PRESETS[preset_key]
        sp = self.settings_panel

        # blockSignals() on `sp` would NOT block its child widgets' signals, so
        # guard the re-entrant sync with an explicit flag instead.
        self._syncing_preset = True
        try:
            sp._set_resolution_combo(int(preset["width"]), int(preset["height"]))
            sp._set_combo_by_text(sp.fps_combo, _fps_text(preset["fps"]))
            sp._set_float_slider(sp.fps_tol_slider, 0.1, 2.0, 0.1, float(preset["fps_tolerance"]))
            sp._set_int_slider(sp.min_bitrate_slider, int(preset["min_bitrate_mbps"]))
        finally:
            self._syncing_preset = False


    def _on_watch_files_detected(self, files: list):
        """Handle new files detected by watch folder monitor."""
        count = len(files)
        
        # If a scan is already running, queue files for later
        if self._worker_thread and self._worker_thread.isRunning():
            self._watch_pending_queue.extend(files)
            self.scan_panel.progress_label.setText(
                f"Watch folder: {count} file{'s' if count != 1 else ''} queued (scan in progress)"
            )
            return
        
        # No scan running — process immediately
        self.scan_panel._set_files(files)
        self.scan_panel.progress_label.setText(
            f"Watch folder: {count} new file{'s' if count != 1 else ''} detected"
        )
        self._watch_batch_in_progress = True
        self.scan_panel.scan_requested.emit(
            files, self.scan_panel.engine_preset() or "", True, False  # quick=True for auto-scan
        )
    
    def _on_preset_dropped(self, filepath: str):
        """Handle a .fg preset file being dropped onto the app."""
        preset = save_imported_preset(filepath)
        if preset:
            self._add_preset_to_combo(preset["name"], preset["config"])
            self._report_idle_status(f"Preset loaded: {preset['name']}")
        else:
            self._report_idle_status(
                f"Not a valid FrameGuard preset: {os.path.basename(filepath)}"
            )

    def _on_preset_imported(self, name: str, config: dict):
        """Handle a preset being imported from the settings panel."""
        self._add_preset_to_combo(name, config)

    def _add_preset_to_combo(self, name: str, config: dict):
        """Add an imported preset to the scan page combo box, select it, and apply it."""
        self._imported_presets[name] = config

        combo = self.scan_panel.preset_combo
        data = f"imported:{name}"
        for i in range(combo.count()):
            if combo.itemData(i) == data:
                combo.setCurrentIndex(i)
                break
        else:
            combo.addItem(name, data)
            combo.setCurrentIndex(combo.count() - 1)

        # currentIndexChanged only fires when the index actually changes, so
        # apply explicitly to cover re-selecting the already-current preset.
        self._syncing_preset = True
        try:
            self.settings_panel._apply_preset_config(config)
        finally:
            self._syncing_preset = False
    
    def _process_watch_queue(self):
        """Process any queued watch folder files after a scan finishes."""
        if not self._watch_pending_queue:
            return
        
        queued = list(self._watch_pending_queue)
        self._watch_pending_queue.clear()
        
        count = len(queued)
        self.scan_panel._set_files(queued)
        self.scan_panel.progress_label.setText(
            f"Watch folder: scanning {count} queued file{'s' if count != 1 else ''}"
        )
        self._watch_batch_in_progress = True
        self.scan_panel.scan_requested.emit(
            queued, self.scan_panel.engine_preset() or "", True, False  # quick=True for auto-scan
        )
    
    def _on_watch_status_changed(self, status: str):
        """Mirror the monitor's status into the Watch panel."""
        # WatchFolderMonitor emits free-form English ("Watching: C:\\in",
        # "Paused (scan in progress)"), so display it verbatim rather than
        # mapping against keys it never emits.
        if hasattr(self.watch_panel, "_update_status"):
            self.watch_panel._update_status(status)

    def _on_watch_error(self, msg: str):
        """Surface a watch-folder error where the user can actually see it."""
        if hasattr(self.watch_panel, "_update_status"):
            self.watch_panel._update_status(f"Error: {msg}")
        self._report_idle_status(f"Watch folder error: {msg}")


    def _report_idle_status(self, text: str):
        """Surface a status line where the operator can see it when idle.

        progress_label lives inside progress_frame, which is hidden whenever
        no scan is running — writes there (auto-export, watch notices) were
        silently invisible. Keep progress_label in sync (existing tests and
        scan-start flows read it) AND route to a transient info note when
        idle so the message is actually visible without becoming permanent.
        """
        try:
            self.scan_panel.progress_label.setText(text)
        except Exception:
            pass
        try:
            if self.scan_panel.progress_frame.isVisible():
                return
        except Exception:
            pass
        try:
            self.scan_panel.show_info_note(text)
        except Exception:
            pass

    def _auto_export_report(self, results):
        """Automatically export a PDF report after scan completion.

        Runs on the thread pool: generating a report for a large batch takes
        seconds, and doing it inline froze the whole UI right after a scan.
        """
        if not results:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(
            self._docs_folder,
            f"FrameGuard_Report_{timestamp}_{len(results)}files.pdf"
        )
        preset = self.scan_panel.preset_combo.currentText()

        runner = _PdfExportRunner(list(results), output_path, preset)
        self._export_runners.append(runner)
        runner.signals.finished.connect(
            lambda path: self._report_idle_status(
                f"Report saved: {os.path.basename(path)}")
        )
        runner.signals.error.connect(
            lambda err: self._report_idle_status(f"Auto-export failed: {err}")
        )
        QThreadPool.globalInstance().start(runner)


    def _install_update(self, update_info):
        """Apply update using the UpdateManager (copies installer, launches /SILENT, quits app)."""
        self._update_manager.apply_update(update_info)

    def _on_scan_requested(self, files: list, preset: str, quick: bool, naming: bool):
        """Start a scan in a background thread."""
        # Re-entrancy guard. Without it a second scan overwrites self._worker
        # and self._worker_thread, orphaning the first thread: both then emit
        # into the same slots, interleaving results, double-firing auto-export
        # and double-draining the watch queue.
        if self._worker_thread is not None and self._worker_thread.isRunning():
            print("[FrameGuard] Scan already running; ignoring duplicate request.")
            return

        # Disk-space preflight: PDF auto-export and crash logs need headroom.
        # A batch that fills the disk mid-scan used to die with an opaque
        # OSError deep inside ffmpeg/report code. Watch-folder batches run
        # unattended, so they log instead of popping a modal.
        if files:
            try:
                import shutil as _shutil
                _free = _shutil.disk_usage(
                    os.path.dirname(os.path.abspath(files[0]))).free
                if _free < 256 * 1024 * 1024:
                    _msg = (f"Only {_free // (1024*1024)} MB free on the drive holding "
                            f"the queued files. Free at least 256 MB before scanning.")
                    if self._watch_batch_in_progress:
                        print(f"[FrameGuard] {_msg}")
                    else:
                        QMessageBox.warning(self, "Low Disk Space", _msg)
                    return
            except OSError:
                pass

        # A new batch gets a fresh Results page unless the operator opted into
        # accumulating. Watch-folder batches are exempt: they are one
        # continuous session and clearing between them would destroy the log.
        if (not self._watch_batch_in_progress
                and self.settings_panel.resets_results_per_scan()):
            self.results_panel.clear_results()
            self._results_badge_count = 0
            self._update_results_badge()

        self.scan_panel.set_scanning(True)

        self._worker = ScanWorker()
        cfg_overrides = self.settings_panel.get_config_overrides()
        try:
            self._worker.configure(
                files=files,
                preset=preset if preset else None,
                quick=quick,
                naming=naming,
                strict=self.settings_panel.is_strict_mode(),
                cfg_overrides=cfg_overrides
            )

            self._worker_thread = QThread()
            self._worker.moveToThread(self._worker_thread)
            self._worker_thread.started.connect(self._worker.run)

            total_files = len(files)
            self._worker.scan_started.connect(
                lambda n: self.scan_panel.progress_label.setText(f"Scanning {n} file{'s' if n != 1 else ''}...")
            )
            self._worker.file_started.connect(
                lambda idx, name: self.scan_panel.progress_label.setText(f"File {idx+1}/{total_files}: {name}")
            )
            self._worker.file_progress.connect(
                lambda idx, phase, cur, tot, lbl: self.scan_panel.update_progress(idx, total_files, phase, cur, tot, lbl)
            )
            self._worker.file_finished.connect(self._on_file_finished)
            self._worker.scan_finished.connect(self._on_scan_finished)
            self._worker.scan_error.connect(self._on_scan_error)
            self._worker.scan_cancelled.connect(self._on_scan_cancelled)

            if self._cancel_connected:
                try:
                    self.scan_panel.cancel_btn.clicked.disconnect(self._cancel_scan)
                except RuntimeError:
                    pass
            self.scan_panel.cancel_btn.clicked.connect(self._cancel_scan)
            self._cancel_connected = True

            # Pause watch folder during scan to prevent mid-scan emissions
            if hasattr(self, '_watch_monitor') and self._watch_monitor.is_active:
                self._watch_monitor.pause()
            
            self._worker_thread.start()
        except Exception as e:
            self.scan_panel.set_scanning(False)
            # The watcher is paused inside the try above; without this it stays
            # paused for the rest of the session after a failed start.
            self._resume_watch_monitor()
            QMessageBox.critical(self, "Scan Error", f"Failed to start scan: {e}")
            return

    def _on_file_finished(self, idx: int, result: QAResult):
        """Stream each finished result into the table as soon as it lands.

        Without this the user watches a progress bar for the whole batch and
        only sees anything at the very end.
        """
        self.results_panel.set_results([result], append=True)
        self.checks_panel.set_results(self.results_panel._results)

    def _on_scan_finished(self, results: list):
        self.scan_panel.set_scanning(False)
        self._watch_batch_in_progress = False

        total = len(results)
        failed = sum(1 for r in results if _result_status(r) == "MISMATCH")
        warned = sum(1 for r in results if _result_status(r) == "PASS_WITH_WARNINGS")
        self.scan_panel.show_completion(total, failed, warned)

        # Auto-export PDF if enabled in settings
        if hasattr(self.watch_panel, '_auto_export_check') and self.watch_panel._auto_export_check.isChecked():
            self._auto_export_report(results)

        # Rows were already streamed in by _on_file_finished; just refresh the
        # badges/summary rather than appending the whole batch a second time.
        self.results_panel.set_results([], append=True)
        self.checks_panel.set_results(self.results_panel._results)
        self._cleanup_worker()

        # If user is NOT on Results tab, show badge instead of auto-switching
        current_tab = self.stack.currentIndex()
        if current_tab != PANEL_RESULTS:
            # Badge: update Results button text with count
            self._results_badge_count += len(results)
            self._update_results_badge()
            
            # Tray notification for failures (Feature 2)
            failed = [r for r in results if _result_status(r) == "MISMATCH"]
            # Track scans completed while minimized
            if self._is_in_background():
                self._tray_scan_count += len(results)
                self._update_tray_status()
            if failed and self._tray_icon and self._is_in_background():
                names = ", ".join(os.path.basename(r.filepath) for r in failed[:2])
                extra = f" (+{len(failed)-2} more)" if len(failed) > 2 else ""
                self._tray_icon.showMessage(
                    "FrameGuard - Issues Found",
                    f"\u26a0\ufe0f {len(failed)} file(s) FAILED: {names}{extra}",
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000
                )
            elif self._tray_icon and self._is_in_background():
                self._tray_icon.showMessage(
                    "FrameGuard - Scan Complete",
                    f"\u2705 {len(results)} file(s) scanned - all passed",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000
                )
            
            # Update Watch panel activity log (Feature 3)
            if hasattr(self, 'watch_panel'):
                self._update_watch_log(results)
        else:
            QTimer.singleShot(1000, lambda: self._switch_panel(PANEL_RESULTS))

        # Resume watch folder monitor and process any queued files
        self._resume_watch_monitor()
        if self._watch_pending_queue:
            QTimer.singleShot(500, self._process_watch_queue)


    def _update_results_badge(self):
        """Show a count of unseen results on the Results nav row.

        Text only: the row already carries a vector icon, so re-injecting the
        old U+2611 glyph here rendered two icons side by side.
        """
        if self._results_badge_count > 0:
            self.nav_buttons[PANEL_RESULTS].setText(
                f"   Results ({self._results_badge_count} new)")
        else:
            self.nav_buttons[PANEL_RESULTS].setText("   Results")
    
    def _update_watch_log(self, results: list):
        """Update the Watch panel activity log with latest scan results."""
        if not hasattr(self, 'watch_panel') or not hasattr(self.watch_panel, '_log'):
            return
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        entries = []
        for r in results:
            status = _result_status(r)
            name = os.path.basename(r.filepath)
            if status == "PASS":
                entries.append(f"\u2705 {name} - PASS")
            elif status == "PASS_WITH_WARNINGS":
                entries.append(f"\u26a0\ufe0f {name} - PASS (warnings)")
            else:
                entries.append(f"\u274c {name} - FAIL")
        
        log_text = f"[{timestamp}] Scanned {len(results)} file(s):\n" + "\n".join(entries)
        
        # Prepend to existing log (keep last 10 entries)
        existing = self.watch_panel._log.text()
        if existing == "No activity yet":
            existing = ""
        combined = log_text + ("\n\n" + existing if existing else "")
        # Trim to ~500 chars
        if len(combined) > 500:
            combined = combined[:500] + "..."
        self.watch_panel._log.setText(combined)

    def _is_in_background(self) -> bool:
        """True when the user is not looking at the window.

        Qt reports a MINIMISED window as isVisible() == True, so gating
        notifications on `not isVisible()` suppressed every tray message unless
        the window had been closed to tray specifically.
        """
        return (not self.isVisible()) or self.isMinimized() or not self.isActiveWindow()

    def _resume_watch_monitor(self):
        """Restart polling after a scan ends, however it ended."""
        if hasattr(self, '_watch_monitor') and self._watch_monitor.is_paused:
            self._watch_monitor.resume()

    def _on_scan_error(self, msg: str):
        self.scan_panel.set_scanning(False)
        self._watch_batch_in_progress = False
        # Replace any stale PASS/FAIL verdict: leaving it in place announces
        # the previous scan's outcome after a failure.
        try:
            self.scan_panel.show_error(msg)
        except Exception:
            pass
        self._cleanup_worker()
        self._resume_watch_monitor()
        # Watch-detected files are already marked emitted, so if the queue is
        # not drained here they are never rediscovered by sync() either - they
        # are simply lost.
        self._process_watch_queue()
        QMessageBox.critical(self, "Scan Error", msg)

    def _on_scan_cancelled(self):
        self.scan_panel.set_scanning(False)
        self._watch_batch_in_progress = False
        self.scan_panel.show_completion(0, 0, 0, cancelled=True)
        self.scan_panel.progress_label.setText("Scan cancelled.")
        self._cleanup_worker()
        self._resume_watch_monitor()
        # Cancelling one scan should not silently discard a pending watch batch.
        if self._watch_pending_queue:
            count = len(self._watch_pending_queue)
            # progress_label is hidden with the progress frame; the banner is
            # what the operator actually sees.
            self._report_idle_status(
                f"Scan cancelled. {count} watched file(s) still queued.")
            QTimer.singleShot(1500, self._process_watch_queue)

    def _on_watch_folder_lost(self, path: str):
        """The watched folder became unreachable and watching has stopped.

        This used to fail silently: the poll returned early, the UI kept
        reporting "Active - monitoring ...", and the operator believed
        unattended QA was running when nothing was.
        """
        panel = self.watch_panel
        was_blocked = panel._enable_check.blockSignals(True)
        panel._enable_check.setChecked(False)
        panel._enable_check.blockSignals(was_blocked)
        panel._sync_btn.setEnabled(False)
        panel._backlog_btn.setEnabled(False)

        note = f"\u26A0 Watching stopped \u2014 folder unavailable: {path}"
        panel._log.setText(note)

        if self._tray_icon and self._tray_icon.isVisible():
            self._tray_icon.showMessage(
                "FrameGuard \u2014 watch folder lost",
                f"{path}\nWatching has stopped.",
                QSystemTrayIcon.Warning, 10000)
        elif self.isVisible():
            QMessageBox.warning(
                self, "Watch Folder Unavailable",
                f"Watching has stopped.\n\nThis folder is no longer reachable:\n{path}\n\n"
                f"Reconnect the drive or choose another folder, then switch "
                f"watching back on.")

    def _cancel_scan(self):
        if self._worker:
            self._worker.cancel()

    def _cleanup_worker(self):
        """Safely stop and dispose the worker thread.

        Never calls QThread.terminate() on a running scan: killing a thread
        that holds the GIL / Qt locks segfaults or deadlocks Windows on
        deep scans. quit() + wait() lets run() return via its cancel_event
        checks; the worker was already asked to cancel by the caller.
        """
        if self._worker_thread:
            if self._worker:
                try:
                    self._worker.cancel()
                except Exception:
                    pass
            self._worker_thread.quit()
            if not self._worker_thread.wait(8000):
                # Last resort: the thread is wedged inside a blocking
                # communicate()/wait() that ignores cancel. Log loudly;
                # terminate() here can still crash, but hanging forever
                # is worse than a risky kill at shutdown.
                print("[FrameGuard] WARNING: worker thread did not exit in 8s; "
                      "leaving it to finish instead of terminate() to avoid crash.")
                # Do NOT terminate: orphan the thread object rather than
                # segfault. It will exit when its ffmpeg call times out.
                self._worker_thread = None
                if self._worker:
                    try:
                        self._worker.deleteLater()
                    except Exception:
                        pass
                    self._worker = None
                return
            try:
                self._worker_thread.deleteLater()
            except Exception:
                pass
            self._worker_thread = None
        if self._worker:
            try:
                self._worker.deleteLater()
            except Exception:
                pass
            self._worker = None

    def _export_pdf(self, output_path: str = None, preset_label: str = None):
        """Export results to PDF on a background thread."""
        export_results = self.results_panel.get_selected_results()
        if not export_results:
            QMessageBox.warning(self, "No Results", "No scan results to export.")
            return

        if not output_path:
            from PySide6.QtWidgets import QFileDialog
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            default_path = os.path.join(
                self._docs_folder, f"FrameGuard_Report_{timestamp}.pdf"
            )
            output_path, _ = QFileDialog.getSaveFileName(
                self, "Save PDF Report", default_path, "PDF Files (*.pdf)")
            if not output_path:
                return
            if not output_path.lower().endswith(".pdf"):
                output_path += ".pdf"

        if preset_label is None:
            preset_label = self.scan_panel.preset_combo.currentText()

        self.results_panel.export_btn.setEnabled(False)
        self.results_panel.export_btn.setText("Exporting...")

        runner = _PdfExportRunner(export_results, output_path, preset_label)
        # QThreadPool auto-deletes the runnable as soon as run() returns, which
        # can destroy its signal object before the queued completion signal has
        # been delivered on the GUI thread — the dialog would silently never appear.
        self._export_runners.append(runner)
        runner.signals.finished.connect(self._on_export_finished)
        runner.signals.error.connect(self._on_export_error)
        QThreadPool.globalInstance().start(runner)

    def _reset_export_button(self):
        self.results_panel.export_btn.setText("Export PDF")
        self.results_panel.export_btn.setEnabled(bool(self.results_panel._results))
        self._export_runners.clear()

    def _on_export_finished(self, path: str):
        self._reset_export_button()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Export Complete")
        box.setText("PDF report saved.")
        box.setInformativeText(path)
        open_btn = box.addButton("Open PDF", QMessageBox.AcceptRole)
        folder_btn = box.addButton("Show in Folder", QMessageBox.ActionRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is open_btn:
            _open_path(path)
        elif clicked is folder_btn:
            _reveal_in_file_manager(path)

    def _on_export_error(self, err: str):
        self._reset_export_button()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Export Failed")
        box.setText("Could not generate the PDF report.")
        box.setDetailedText(err)
        box.exec()

    def resizeEvent(self, event):
        """Keep the resize grip anchored, and re-flow panels for the new size."""
        super().resizeEvent(event)
        if hasattr(self, '_size_grip'):
            grip_geo = self._size_grip.geometry()
            self._size_grip.move(
                self.width() - grip_geo.width(),
                self.height() - grip_geo.height()
            )
        self._apply_responsive_layout()

    def _apply_responsive_layout(self):
        """Adapt spacing and chrome to the current window size.

        Everything below was tuned for a maximized window; at the 1000x700
        minimum the fixed gutters and the badge strip left the table with barely
        six visible rows.
        """
        if not hasattr(self, "stack"):
            return

        width = self.width()

        for panel in (getattr(self, name, None) for name in
                      ("scan_panel", "results_panel", "checks_panel",
                       "watch_panel", "settings_panel")):
            if panel is not None:
                apply_responsive_margins(panel, width)

        # The sidebar is a fixed 200px; on a narrow window that is a fifth of
        # the screen spent on five nav buttons.
        if hasattr(self, "_sidebar") and not self._sidebar_collapsed:
            self._sidebar.setMaximumWidth(D.sidebar_width(width))

        # Badge strip. The measured content height is 96px; anything less
        # clips the count numerals, so this is a floor, not a preference.
        rp = getattr(self, "results_panel", None)
        if rp is not None and hasattr(rp, "_summary_scroll"):
            rp._summary_scroll.setFixedHeight(104)

    # --- Window-wide drag & drop -------------------------------------------
    # The Scan page has its own drop zone, but a preset dropped while the user
    # is on Results/Watch/Settings should import too, rather than doing nothing.

    @staticmethod
    def _drag_paths(mime) -> List[str]:
        if not mime.hasUrls():
            return []
        return [os.path.normpath(u.toLocalFile())
                for u in mime.urls() if u.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._drag_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._drag_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._drag_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.open_paths(paths)

    def open_paths(self, paths: List[str]):
        """Import any presets and queue any videos from a list of paths.

        Shared by window-wide drop and the command line, so double-clicking a
        .fg file (or "Open with FrameGuard") behaves like dropping it.
        """
        presets = [p for p in paths if is_preset_file(p)]
        others = [p for p in paths if not is_preset_file(p)]

        for pf in presets:
            self._on_preset_dropped(pf)

        if others:
            self._switch_panel(PANEL_SCAN)
            self.scan_panel._on_files_dropped(others)
        elif presets:
            self._switch_panel(PANEL_SETTINGS)  # show the preset that just landed


    # --- Edge resize + Aero Snap for the frameless window (Windows) ---
    _BORDER_WIDTH = 8  # pixels from edge that triggers resize

    def nativeEvent(self, event_type, message):
        """Native Windows hit-testing for edge resize and Aero Snap.

        Reporting HTCAPTION for the title bar hands the drag to Windows, which
        is what enables snap-to-top (maximize), snap-to-side (half tile) and
        Win+Arrow. A frameless window that only ever reports HTCLIENT gets none
        of that, no matter what Qt-side move helper is used.
        """
        if event_type not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return super().nativeEvent(event_type, message)

        try:
            import ctypes
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))

            if msg.message == 0x0024:  # WM_GETMINMAXINFO
                # Without this a WS_THICKFRAME frameless window maximizes to
                # the full monitor and hides the taskbar.
                if self._apply_maximize_bounds(msg):
                    return True, 0

            if msg.message == 0x0084:  # WM_NCHITTEST
                pos = self.mapFromGlobal(QCursor.pos())
                x, y = pos.x(), pos.y()
                w, h = self.width(), self.height()
                bw = self._BORDER_WIDTH

                # Resize borders are meaningless while maximized.
                if not self.isMaximized():
                    if x < bw and y < bw:
                        return True, 13  # HTTOPLEFT
                    if x > w - bw and y < bw:
                        return True, 14  # HTTOPRIGHT
                    if x < bw and y > h - bw:
                        return True, 16  # HTBOTTOMLEFT
                    if x > w - bw and y > h - bw:
                        return True, 17  # HTBOTTOMRIGHT
                    if x < bw:
                        return True, 10  # HTLEFT
                    if x > w - bw:
                        return True, 11  # HTRIGHT
                    if y < bw:
                        return True, 12  # HTTOP
                    if y > h - bw:
                        return True, 15  # HTBOTTOM

                if self._title_bar.is_draggable_point(self.mapTo(self, pos)):
                    return True, 2  # HTCAPTION
        except Exception:
            pass

        return super().nativeEvent(event_type, message)

    def _apply_maximize_bounds(self, msg) -> bool:
        """Clamp the maximized size to the monitor work area (taskbar-safe)."""
        import ctypes
        import ctypes.wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [
                ("ptReserved", POINT), ("ptMaxSize", POINT),
                ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT),
                ("ptMaxTrackSize", POINT),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        MONITOR_DEFAULTTONEAREST = 2
        monitor = ctypes.windll.user32.MonitorFromWindow(
            int(msg.hWnd), MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return False

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False

        mmi = MINMAXINFO.from_address(msg.lParam)
        work, screen = info.rcWork, info.rcMonitor
        mmi.ptMaxPosition.x = work.left - screen.left
        mmi.ptMaxPosition.y = work.top - screen.top
        mmi.ptMaxSize.x = work.right - work.left
        mmi.ptMaxSize.y = work.bottom - work.top
        mmi.ptMaxTrackSize.x = work.right - work.left
        mmi.ptMaxTrackSize.y = work.bottom - work.top
        return True

    def changeEvent(self, event):
        """Keep the custom maximize button in sync with OS-driven state changes.

        Snap, Win+Arrow and double-click all change the window state without
        going through our button, so the glyph would otherwise go stale.
        """
        if event.type() == QEvent.WindowStateChange and hasattr(self, "_title_bar"):
            self._title_bar.sync_maximize_button()
        super().changeEvent(event)


    def _tray_show(self):
        """Restore window from system tray."""
        self.showNormal()
        self.activateWindow()
        self.raise_()
        if self._tray_icon:
            self._tray_icon.hide()
        if hasattr(self, '_tray_heartbeat'):
            self._tray_heartbeat.stop()
        self._tray_scan_count = 0
    
    def _tray_quit(self):
        """Actually quit the application (from tray menu)."""
        # Route through close() rather than quitting outright: the shutdown
        # path in closeEvent is what persists settings and watch config, so
        # quitting straight from the tray used to discard the whole session.
        self._force_quit = True
        self.close()
    
    def _on_tray_activated(self, reason):
        """Handle tray icon single-click or double-click."""
        from PySide6.QtWidgets import QSystemTrayIcon
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Single-click: also show (more intuitive on Windows)
            self._tray_show()
    
    def _on_update_available(self, info):
        """Handle update found - surface it without interrupting a scan."""
        self._pending_update = info
        self.settings_panel.show_available_update(info)

        if self._tray_icon and self._is_in_background():
            self._tray_icon.showMessage(
                "FrameGuard Update Available",
                f"v{info.version} is ready to install.\nOpen FrameGuard → Settings to install.",
                QSystemTrayIcon.MessageIcon.Information, 5000
            )
        elif hasattr(self.scan_panel, 'progress_label'):
            self.scan_panel.progress_label.setText(
                f"Update available: v{info.version} — Settings › Install Update"
            )

    def _confirm_install_update(self):
        """Ask before handing off to the installer (the app will close)."""
        info = getattr(self, "_pending_update", None)
        if info is None:
            QMessageBox.information(self, "No Update", "No update is currently available.")
            return
        if self._worker_thread and self._worker_thread.isRunning():
            QMessageBox.warning(
                self, "Scan In Progress",
                "Finish or cancel the current scan before installing an update.")
            return

        confirmed = QMessageBox.question(
            self, "Install Update",
            f"Install FrameGuard v{info.version}?\n\n"
            f"FrameGuard will close and the installer will run.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if confirmed == QMessageBox.Yes:
            self._install_update(info)

    def _on_delta_update_available(self, channel: str, version: str):
        """A newer build is on the update channel.

        Deliberately a prompt rather than a silent auto-apply: applying an
        update restarts the app, and doing that under an unattended overnight
        watch run would abandon the queue.
        """
        self._pending_delta_channel = channel
        if self._worker_thread and self._worker_thread.isRunning():
            return  # ask again after the current scan; never interrupt one

        if self._tray_icon and self._tray_icon.isVisible() and self._is_in_background():
            self._tray_icon.showMessage(
                "FrameGuard update available",
                f"v{version} is ready. Open FrameGuard to install it.",
                QSystemTrayIcon.Information, 8000)
            return

        reply = QMessageBox.question(
            self, "Update Available",
            f"FrameGuard v{version} is available.\n\n"
            f"Only the changed files will be downloaded. FrameGuard will "
            f"close, update and reopen.\n\nInstall it now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.Yes:
            self._apply_differential_update(channel)

    def _apply_differential_update(self, release_dir: str):
        """Update in place by transferring only the files that changed.

        The full build is ~90MB but is almost entirely Python, Qt and FFmpeg;
        a typical release changes a few hundred kilobytes of application code.
        """
        from frameguard.deltaupdate import DeltaUpdater, find_install_root

        if self._worker_thread and self._worker_thread.isRunning():
            QMessageBox.warning(
                self, "Scan In Progress",
                "Finish or cancel the current scan before updating.")
            return

        if find_install_root() is None:
            QMessageBox.information(
                self, "Not an Installed Copy",
                "Differential updates apply to an installed copy of FrameGuard.\n"
                "This looks like a source checkout, so there is nothing to update.")
            return

        updater = DeltaUpdater(self)
        self._delta_updater = updater   # keep alive for the duration

        failures = []
        updater.failed.connect(failures.append)
        plan = updater.inspect(release_dir)
        if plan is None:
            QMessageBox.warning(self, "Update Failed",
                                failures[0] if failures else "Could not read that release.")
            return
        if plan.is_empty:
            QMessageBox.information(self, "Up To Date",
                                    "This installation already matches that release.")
            return

        confirmed = QMessageBox.question(
            self, "Update FrameGuard",
            f"Update to v{plan.app_version}?\n\n{plan.summary()}\n\n"
            f"{plan.changed_count} file(s) will change. FrameGuard will close, "
            f"apply the update and reopen.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if confirmed != QMessageBox.Yes:
            return

        progress = QProgressDialog("Preparing update...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Updating FrameGuard")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)

        def _on_progress(pct, name):
            progress.setValue(pct)
            progress.setLabelText(f"Downloading {name}")

        def _on_failed(message):
            progress.close()
            QMessageBox.critical(
                self, "Update Failed",
                f"{message}\n\nYour installation has not been changed.")

        def _on_ready(_staging):
            progress.close()
            # Nothing has touched the installation yet; the swap happens after
            # this process exits, because a running exe cannot replace itself.
            if updater.apply_and_restart():
                self._force_quit = True
                # close() rather than quit(): the shutdown path persists
                # settings and stops the watch monitor before we exit.
                self.close()

        updater.progress.connect(_on_progress)
        updater.failed.connect(_on_failed)
        updater.cancelled.connect(progress.close)
        updater.ready_to_swap.connect(_on_ready)
        progress.canceled.connect(updater.cancel)
        updater.stage()
    
    def _tray_toggle_watch(self):
        """Pause/resume watch folder from tray menu."""
        if not hasattr(self, '_watch_monitor'):
            return
        if self._watch_monitor.is_polling:
            self._watch_monitor.pause()
            self._tray_pause_action.setText("Resume Watch")
        else:
            self._watch_monitor.resume()
            self._tray_pause_action.setText("Pause Watch")
        self._update_tray_status()
    
    def _tray_sync(self):
        """Trigger a manual sync from tray menu."""
        if hasattr(self, '_watch_monitor') and self._watch_monitor.watch_path:
            self._watch_monitor.sync()
    
    def _update_tray_status(self):
        """Update tray icon tooltip and status menu item."""
        if not self._tray_icon:
            return
        
        # Determine current state
        if hasattr(self, '_watch_monitor') and self._watch_monitor.is_active:
            path = os.path.basename(self._watch_monitor.watch_path or "")
            status = f"Watching: {path}"
            if self._worker_thread and self._worker_thread.isRunning():
                status += " (scanning...)"
            elif self._tray_scan_count > 0:
                status += f" | {self._tray_scan_count} scanned"
        else:
            status = "Idle"
        
        tooltip = f"FrameGuard Desktop — {status}"
        self._tray_icon.setToolTip(tooltip)
        
        if hasattr(self, '_tray_status_action'):
            self._tray_status_action.setText(f"Status: {status}")
    
    def _themed_app_pixmap(self, theme, size: int = 64) -> QPixmap:
        """Return the FrameGuard mark tinted to the theme accent.

        Prefers the real logo so the tray matches the app; falls back to a
        drawn "FG" badge only when the asset is missing.
        """
        if self._logo_pixmap_original and not self._logo_pixmap_original.isNull():
            tinted = _tint_pixmap(self._logo_pixmap_original, QColor(theme.accent))
            return tinted.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(QColor(theme.accent)))
        painter.setPen(QPen(QColor(theme.accent_hover), 2))
        painter.drawRoundedRect(QRect(2, 2, size - 4, size - 4), 14, 14)
        painter.setFont(ui_font(int(size * 0.34), QFont.Bold))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, "FG")
        painter.end()
        return pixmap

    def _update_tray_icon_theme(self, theme=None):
        """Recolor the system tray icon to match the active theme."""
        if not self._tray_icon:
            return
        t = theme or get_theme()
        self._tray_icon.setIcon(QIcon(self._themed_app_pixmap(t, 64)))

    def _apply_window_chrome_theme(self, theme):
        """Tint the OS-drawn window chrome to match the theme (Windows 11+).

        Covers the border and the caption strip Windows still paints around a
        frameless window, plus the taskbar thumbnail. Silently does nothing on
        Windows 10 and other platforms, where these attributes don't exist.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())

            def _colorref(hex_color: str) -> int:
                h = hex_color.lstrip("#")
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return (b << 16) | (g << 8) | r  # DWM wants 0x00BBGGRR

            dwm = ctypes.windll.dwmapi
            for attribute, color in (
                (35, theme.bg_secondary),   # DWMWA_CAPTION_COLOR
                (34, theme.border),         # DWMWA_BORDER_COLOR
                (36, theme.text_primary),   # DWMWA_TEXT_COLOR
            ):
                value = ctypes.c_int(_colorref(color))
                dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), ctypes.c_int(attribute),
                    ctypes.byref(value), ctypes.sizeof(value))
        except (OSError, AttributeError, ValueError):
            pass  # Pre-Windows-11 or DWM unavailable — the custom bar still themes

    def _collect_diagnostics(self, user_note: str = "") -> str:
        """Assemble everything needed to triage a report, as plain text.

        Deliberately local-only: the app promises that nothing leaves the
        machine, so this produces a report the user can read and choose to send
        rather than uploading anything on their behalf.
        """
        import platform
        from frameguard import __version__
        from frameguard.tools import get_ffmpeg_path, get_ffprobe_path

        lines = [
            "FrameGuard Problem Report",
            "=" * 46,
            f"Reported     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Version      : {__version__}",
            f"OS           : {platform.platform()}",
            f"Python       : {platform.python_version()}",
            f"Frozen build : {getattr(sys, 'frozen', False)}",
        ]

        stamp = Path(__file__).parent / "_build_stamp.json"
        if stamp.is_file():
            try:
                data = json.loads(stamp.read_text(encoding="utf-8"))
                lines.append(f"Built        : {data.get('built', '?')}")
            except (OSError, ValueError):
                pass

        lines += [
            "",
            "--- Tools ---",
            f"ffmpeg  : {get_ffmpeg_path() or 'NOT FOUND'}",
            f"ffprobe : {get_ffprobe_path() or 'NOT FOUND'}",
            "",
            "--- Session ---",
            f"Theme         : {get_theme().name}",
            f"Preset        : {self.scan_panel.preset_combo.currentText()}",
            f"Results held  : {len(self.results_panel._results)}",
            f"Watch active  : {getattr(self._watch_monitor, 'is_active', False)}",
            f"Watch folder  : {getattr(self._watch_monitor, 'watch_path', None) or '-'}",
        ]

        # The last few verdicts give a triager the shape of the problem without
        # needing the footage.
        recent = self.results_panel._results[-5:]
        if recent:
            lines += ["", "--- Last results ---"]
            for r in recent:
                lines.append(f"  {_result_status(r):<20} {os.path.basename(r.filename)}")

        cfg = self.settings_panel.get_config_overrides()
        if cfg:
            lines += ["", "--- Delivery spec ---"]
            for key in sorted(cfg):
                lines.append(f"  {key} = {cfg[key]}")

        crash = Path(_crash_log_dir()) / "frameguard_crash.log"
        if crash.is_file():
            try:
                tail = crash.read_text(encoding="utf-8", errors="replace")[-3000:]
                lines += ["", "--- Recent crash log (tail) ---", tail]
            except OSError:
                pass
        else:
            lines += ["", "--- Recent crash log ---", "(none)"]

        if user_note.strip():
            lines = lines[:2] + ["", "--- What happened ---", user_note.strip(), ""] + lines[2:]

        return "\n".join(lines)

    def _report_problem(self):
        """Gather diagnostics and let the user send them."""
        from PySide6.QtWidgets import QDialog, QPlainTextEdit, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowTitle("Report a Problem")
        dlg.resize(640, 520)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(D.SPACE_5, D.SPACE_5, D.SPACE_5, D.SPACE_5)
        layout.setSpacing(D.SPACE_3)

        intro = QLabel(
            "Describe what happened. FrameGuard will attach version, tool and "
            "session details below.\n\nNothing is sent automatically — you "
            "choose where this goes."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        note = QPlainTextEdit()
        note.setPlaceholderText(
            "What were you doing? What did you expect, and what happened instead?")
        note.setMaximumHeight(110)
        layout.addWidget(note)

        preview_label = QLabel("Report contents:")
        layout.addWidget(preview_label)
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText(self._collect_diagnostics())
        layout.addWidget(preview, 1)

        note.textChanged.connect(
            lambda: preview.setPlainText(self._collect_diagnostics(note.toPlainText())))

        buttons = QDialogButtonBox()
        copy_btn = buttons.addButton("Copy to Clipboard", QDialogButtonBox.ActionRole)
        save_btn = buttons.addButton("Save to File...", QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        def _copy():
            QApplication.clipboard().setText(preview.toPlainText())
            copy_btn.setText("Copied")
            QTimer.singleShot(1500, lambda: copy_btn.setText("Copy to Clipboard"))

        def _save():
            default = os.path.join(
                self._docs_folder,
                f"FrameGuard_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Save Problem Report", default, "Text Files (*.txt)")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(preview.toPlainText())
                _reveal_in_file_manager(path)
            except OSError as e:
                QMessageBox.warning(dlg, "Could Not Save", str(e))

        copy_btn.clicked.connect(_copy)
        save_btn.clicked.connect(_save)
        dlg.exec()

    def _show_about(self):
        """Show About dialog with version and build info."""
        from frameguard import __version__
        from PySide6.QtWidgets import QMessageBox
        
        build_info = ""
        try:
            import json
            from pathlib import Path
            stamp_path = Path(__file__).parent / "_build_stamp.json"
            if stamp_path.exists():
                stamp = json.loads(stamp_path.read_text())
                build_info = f"\nBuilt: {stamp.get('built', 'Unknown')[:10]}"
        except Exception:
            pass
        
        QMessageBox.about(
            self,
            "About FrameGuard Desktop",
            f"<h3>FrameGuard Desktop v{__version__}</h3>"
            f"<p>Professional Video QA for XR Production</p>"
            f"<p>Automates delivery spec compliance checks for<br>"
            f"resolution, codec, bitrate, frame rate, and audio.</p>"
            f"<p><b>Publisher:</b> Capri la Ria<br>"
            f"<b>Lead Developer:</b> Cassian Aléria{build_info}</p>"
        )

    def closeEvent(self, event):
        """Override close: minimize to tray if watch folder is active."""
        if not getattr(self, '_force_quit', False):
            # If watch folder is actively monitoring, minimize to tray instead
            if (hasattr(self, '_watch_monitor') and self._watch_monitor.is_active
                    and self._tray_icon is not None):
                event.ignore()
                self.hide()
                self._tray_icon.show()
                self._tray_icon.showMessage(
                    "FrameGuard",
                    "Minimized to tray. Watch folder still active.\n"
                    "Right-click tray icon for controls.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000
                )
                # Save here too: this path returns without reaching the
                # shutdown block below, so a reboot while tray-resident used to
                # lose the whole session's configuration.
                self.settings_panel.save_settings()
                self.watch_panel.save_watch_settings()
                # Start heartbeat for periodic status updates
                if hasattr(self, '_tray_heartbeat'):
                    self._tray_heartbeat.start()
                self._update_tray_status()
                return

        # Full shutdown: stop all services
        self.settings_panel.save_settings()
        self.watch_panel.save_watch_settings()
        if hasattr(self, '_watch_monitor') and self._watch_monitor.is_active:
            self._watch_monitor.stop()
        self._cleanup_worker()
        if hasattr(self, '_tray_heartbeat'):
            self._tray_heartbeat.stop()
        if self._tray_icon:
            self._tray_icon.hide()
        
        # Ensure worker thread is properly stopped
        if self._worker_thread and self._worker_thread.isRunning():
            self._cleanup_worker()
            if self._worker_thread and self._worker_thread.isRunning():
                event.ignore()
                QTimer.singleShot(500, self.close)
                return
        self._cleanup_worker()
        event.accept()
        # quitOnLastWindowClosed is off (see main()), so the event loop has to
        # be told to stop explicitly or the process lingers with no window.
        QApplication.quit()



# ============================================================================
# SPLASH SCREEN (FL Studio-style loading animation)
# ============================================================================

def get_saved_theme_name() -> str:
    """The theme the user last chose, or the default.

    Needed before any window exists: the splash is constructed at startup, and
    without this it always painted in the default palette no matter which
    theme the user had selected.
    """
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    name = settings.value("theme", "Meta Dark")
    return name if name in THEMES else "Meta Dark"


class SplashScreen(QWidget):
    """Startup splash built around the real FrameGuard mark.

    The previous version drew a blue rounded square with the letters "FG" in
    it, hardcoded to one palette, while the actual 2048px logo shipped in
    assets/ went unused. This renders the real artwork, follows the active
    theme, and uses a slow sweep arc - a nod to the scanning the app does -
    instead of a pulsing blob.
    """

    # Generous margin so the drop shadow has somewhere to fall.
    SHADOW = 24
    CARD_W, CARD_H = 460, 300

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(self.CARD_W + self.SHADOW * 2, self.CARD_H + self.SHADOW * 2)

        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2,
                  (screen.height() - self.height()) // 2)

        # Paint in the theme the user actually chose. get_theme() alone returns
        # whatever is loaded at import time (the default), and the saved theme
        # is not applied until MainWindow is built - which is after the splash
        # has already been on screen for most of startup.
        self._theme = get_theme(get_saved_theme_name())
        self._status_text = "Starting..."
        self._progress = 0.0          # eased value actually drawn
        self._progress_target = 0.0   # value most recently requested
        self._sweep = 0.0             # rotating arc, degrees
        self._intro = 0.0             # 0..1 entrance progress

        # Load the real mark once, then keep a theme-tinted copy alongside it.
        self._logo_source = self._load_logo(112)
        self._logo = None
        self._retint_logo()
        # Pre-render the drop shadow. Stacking translucent rounded rects
        # produces visible concentric banding; a real Gaussian blur does not,
        # and doing it once at startup costs nothing per frame.
        self._shadow = self._build_shadow()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(16)   # ~60fps; the sweep is visible so it must be smooth

    def _build_shadow(self) -> Optional[QPixmap]:
        """Render the card silhouette through a Gaussian blur, once."""
        try:
            from PySide6.QtWidgets import (QGraphicsScene, QGraphicsPixmapItem,
                                           QGraphicsBlurEffect)

            silhouette = QPixmap(self.size())
            silhouette.fill(Qt.transparent)
            painter = QPainter(silhouette)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawRoundedRect(
                QRectF(self.SHADOW, self.SHADOW + 4, self.CARD_W, self.CARD_H),
                18, 18)
            painter.end()

            item = QGraphicsPixmapItem(silhouette)
            blur = QGraphicsBlurEffect()
            blur.setBlurRadius(18)
            item.setGraphicsEffect(blur)

            scene = QGraphicsScene()
            scene.addItem(item)

            out = QPixmap(self.size())
            out.fill(Qt.transparent)
            out_painter = QPainter(out)
            out_painter.setRenderHint(QPainter.Antialiasing)
            scene.render(out_painter, QRectF(out.rect()), QRectF(out.rect()))
            out_painter.end()
            return out
        except Exception:
            # A missing effects backend must not stop the app from starting.
            return None

    @staticmethod
    def _load_logo(size: int) -> Optional[QPixmap]:
        for candidate in (_LOGO_PATH, os.path.join(_ASSET_DIR, "icon_64.png")):
            if candidate and os.path.isfile(candidate):
                pixmap = QPixmap(candidate)
                if not pixmap.isNull():
                    return pixmap.scaled(size, size, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
        return None

    def _retint_logo(self):
        """Recolour the mark to the active accent.

        Without this the shield stayed hard blue in every theme, clashing with
        the teal, amber and purple palettes.
        """
        if self._logo_source is None or self._logo_source.isNull():
            self._logo = None
            return
        self._logo = _colorize_pixmap(self._logo_source, QColor(self._theme.accent))

    def set_theme(self, theme):
        """Repaint in a different theme (accepts a name or a ThemeColors)."""
        self._theme = get_theme(theme) if isinstance(theme, str) else theme
        self._retint_logo()
        self.update()

    def set_status(self, text: str, progress: int = None):
        """Update the loading status text and optional progress value.

        The bar is eased toward its target on the 16ms timer, but the timer
        never fires while MainWindow() blocks the event loop — so snap the
        drawn value most of the way immediately. Without this the splash
        sits at 0% saying "Building interface…" for the whole build.
        """
        self._status_text = text
        if progress is not None:
            self._progress_target = min(100.0, max(0.0, float(progress)))
            # Drive the drawn value to the target synchronously. The 16ms
            # ease in _animate only runs when the event loop is free, which
            # it is not while MainWindow() builds — without this the bar
            # sits near 0% saying "Building interface…" for the whole build.
            # The sweep arc still animates via the timer whenever the loop
            # is free, so the splash keeps its motion.
            self._progress = self._progress_target
        self.update()
        QApplication.processEvents()

    def fade_out(self):
        """Start fade-out animation before closing."""
        self._timer.stop()
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(400)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.InCubic)
        self._fade_anim.finished.connect(self.close)
        self._fade_anim.start()

    def _animate(self):
        self._sweep = (self._sweep + 1.6) % 360
        if self._intro < 1.0:
            self._intro = min(1.0, self._intro + 0.04)
        # Ease the bar toward its target so loading steps glide instead of
        # snapping between values.
        self._progress += (self._progress_target - self._progress) * 0.18
        self.update()

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event):
        from PySide6.QtGui import QRadialGradient, QLinearGradient
        from PySide6.QtCore import QRectF, QPointF

        t = self._theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        card = QRectF(self.SHADOW, self.SHADOW, self.CARD_W, self.CARD_H)
        radius = 18

        # --- drop shadow (pre-blurred; see _build_shadow) ---
        if self._shadow is not None:
            p.drawPixmap(0, 0, self._shadow)

        # --- card: vertical gradient from secondary to primary ---
        body = QLinearGradient(card.topLeft(), card.bottomLeft())
        body.setColorAt(0.0, QColor(t.bg_secondary))
        body.setColorAt(1.0, QColor(t.bg_primary))
        p.setBrush(QBrush(body))
        p.setPen(QPen(QColor(t.border), 1))
        p.drawRoundedRect(card, radius, radius)

        # A single accent hairline along the top edge reads as deliberate
        # product design rather than decoration.
        p.setClipPath(self._rounded_path(card, radius))
        accent_wash = QLinearGradient(card.topLeft(), card.topRight())
        accent_wash.setColorAt(0.0, QColor(0, 0, 0, 0))
        accent_wash.setColorAt(0.5, QColor(t.accent))
        accent_wash.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(accent_wash))
        p.drawRect(QRectF(card.left(), card.top(), card.width(), 2))
        p.setClipping(False)

        cx = card.center().x()
        logo_cy = card.top() + 108

        # --- soft accent glow behind the mark ---
        glow = QRadialGradient(QPointF(cx, logo_cy), 92)
        halo = QColor(t.accent)
        halo.setAlpha(int(52 * self._intro))
        glow.setColorAt(0.0, halo)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, logo_cy), 92, 92)

        # --- sweep arc: a slow rotating arc, echoing a scan pass ---
        ring = QRectF(cx - 74, logo_cy - 74, 148, 148)
        track = QColor(t.border)
        track.setAlpha(90)
        p.setPen(QPen(track, 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(ring)

        sweep_pen = QPen(QColor(t.accent), 3)
        sweep_pen.setCapStyle(Qt.RoundCap)
        p.setPen(sweep_pen)
        # Qt angles are in 1/16th degrees, counter-clockwise from 3 o'clock.
        p.drawArc(ring, int(-self._sweep * 16), int(-100 * 16))

        # --- the real mark, scaling and fading in ---
        if self._logo is not None and not self._logo.isNull():
            scale = 0.86 + 0.14 * self._ease_out(self._intro)
            size = self._logo.width() * scale
            p.setOpacity(self._intro)
            p.drawPixmap(QRectF(cx - size / 2, logo_cy - size / 2, size, size),
                         self._logo, QRectF(self._logo.rect()))
            p.setOpacity(1.0)
        else:
            # Only if the artwork is genuinely missing from the build.
            p.setPen(QColor(t.accent))
            p.setFont(ui_font(34, QFont.Bold))
            p.drawText(QRectF(cx - 80, logo_cy - 30, 160, 60), Qt.AlignCenter, "FG")

        # --- wordmark ---
        p.setPen(QColor(t.text_primary))
        p.setFont(ui_font(19, QFont.DemiBold))
        p.drawText(QRectF(card.left(), logo_cy + 78, card.width(), 28),
                   Qt.AlignCenter, "FrameGuard")

        p.setPen(QColor(t.text_tertiary))
        p.setFont(ui_font(11))
        p.drawText(QRectF(card.left(), logo_cy + 102, card.width(), 18),
                   Qt.AlignCenter, f"Video QA for XR Deliverables  \u00b7  v{APP_VERSION}")

        # --- progress ---
        bar_margin = 56
        bar_w = card.width() - bar_margin * 2
        bar_x = card.left() + bar_margin
        bar_y = card.bottom() - 44
        bar_h = 4

        p.setPen(Qt.NoPen)
        rail = QColor(t.border)
        rail.setAlpha(120)
        p.setBrush(QBrush(rail))
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._progress > 0.5:
            fill_w = bar_w * (self._progress / 100.0)
            fill = QLinearGradient(bar_x, 0, bar_x + fill_w, 0)
            fill.setColorAt(0.0, QColor(t.accent_hover))
            fill.setColorAt(1.0, QColor(t.accent))
            p.setBrush(QBrush(fill))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setPen(QColor(t.text_secondary))
        p.setFont(ui_font(10))
        p.drawText(QRectF(bar_x, bar_y + 12, bar_w, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, self._status_text)
        p.setPen(QColor(t.text_tertiary))
        p.drawText(QRectF(bar_x, bar_y + 12, bar_w, 18),
                   Qt.AlignRight | Qt.AlignVCenter, f"{int(self._progress)}%")

        p.end()

    @staticmethod
    def _rounded_path(rect, radius):
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    @staticmethod
    def _ease_out(x: float) -> float:
        return 1 - (1 - x) ** 3


class _FileOpenFilter(QObject):
    """Routes macOS Apple-Event file opens into MainWindow.open_paths().

    Finder does not pass document paths in argv on macOS; it sends
    kAEOpenDocuments, which Qt delivers to the QApplication as QEvent.FileOpen.
    Without this, double-clicking a .fg launches FrameGuard with an empty queue
    and the preset is silently ignored.
    """

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FileOpen:
            path = event.file()
            if path and os.path.exists(path):
                self._window.open_paths([path])
                self._window.show()
                self._window.raise_()
                self._window.activateWindow()
                return True
        return super().eventFilter(obj, event)


def main(dev_mode: bool = False):
    """Launch FrameGuard Desktop application.
    
    Args:
        dev_mode: If True, watches theme.py for changes and hot-reloads stylesheet.
                  Start with: python main.py --dev
    """
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from PySide6.QtCore import QFileSystemWatcher
    import sys
    import os

    # High-DPI scaling (Qt6 enables this by default, but be explicit)
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("FrameGuard Desktop")

    # FrameGuard outlives its main window: closing it while a watch folder is
    # active hides it to the tray. With Qt's default of quitting once the last
    # window closes, dismissing any dialog while the main window was hidden
    # terminated the whole app — reported as "app unexpectedly quit when
    # switching windows". Shutdown is now explicit (MainWindow.closeEvent and
    # _tray_quit) rather than a side effect of window bookkeeping.
    app.setQuitOnLastWindowClosed(False)
    
    # Show splash screen immediately (before heavy imports)
    splash = SplashScreen()
    splash.show()
    splash.set_status("Initializing...", 5)

    # --- Stylesheet parse error diagnostic ---
    from PySide6.QtCore import qInstallMessageHandler, QtMsgType

    def _qt_message_handler(mode, context, message):
        # In a frozen --windowed build sys.stderr is None; printing would raise
        # inside the message handler and Qt would recurse on the failure.
        if sys.stderr is None:
            return
        if "Could not parse" in message:
            sys.stderr.write(f"[QSS ERROR] {message}\n")
        elif mode == QtMsgType.QtWarningMsg and "stylesheet" in message.lower():
            sys.stderr.write(f"[QSS WARNING] {message}\n")

    qInstallMessageHandler(_qt_message_handler)
    try:
        from frameguard import __version__ as _ver
        app.setApplicationVersion(_ver)
    except ImportError:
        app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(_SETTINGS_ORG)
    app.setOrganizationDomain("caprilaria.com")

    # Set app icon
    icon_paths = [
        # Frozen mode: icon at app root (copied by installer)
        os.path.join(os.path.dirname(sys.executable), "frameguard.ico") if getattr(sys, 'frozen', False) else "",
        # Frozen mode: icon in _internal/assets (bundled by PyInstaller)
        os.path.join(getattr(sys, '_MEIPASS', ''), "assets", "frameguard.ico") if getattr(sys, 'frozen', False) else "",
        # Dev mode paths
        os.path.join(os.path.dirname(__file__), "..", "assets", "frameguard.ico"),
        os.path.join(os.path.dirname(__file__), "assets", "frameguard.ico"),
    ]
    for icon_path in icon_paths:
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
            break

    # Apply theme stylesheet
    splash.set_status("Loading themes...", 30)
    from frameguard.theme import generate_stylesheet
    set_current_theme(get_saved_theme_name())
    app.setStyleSheet(generate_stylesheet())
    # Keep the splash in step in case the saved theme changed since it was built.
    splash.set_theme(get_saved_theme_name())
    splash.set_status("Applying stylesheet...", 50)


    # Create and show main window
    splash.set_status("Building interface...", 70)
    window = MainWindow()
    splash.set_status("Ready!", 100)

    # Fade out splash and show main window
    from PySide6.QtCore import QTimer as _QT
    _QT.singleShot(300, lambda: (splash.fade_out(), window.show()))

    # Files passed on the command line (double-click a .fg, "Open with",
    # or drag onto the exe). Deferred so the window exists first.
    cli_paths = [a for a in sys.argv[1:]
                 if not a.startswith("-") and os.path.exists(a)]
    if cli_paths:
        _QT.singleShot(400, lambda: window.open_paths(cli_paths))

    # macOS delivers "Open With" / double-click as an Apple Event, which Qt
    # turns into QEvent.FileOpen — it never appears in argv. This also covers
    # opening a second .fg while FrameGuard is already running, which
    # argv_emulation alone would miss.
    app.installEventFilter(_FileOpenFilter(window, app))

    # Initial combo popup styling (Windows needs QPalette, not just CSS)
    from PySide6.QtWidgets import QComboBox
    from frameguard.theme import get_theme
    t = get_theme()
    for combo in window.findChildren(QComboBox):
        _style_combobox_popup(combo, t)

    # --- DEV MODE: Hot-reload stylesheet on theme.py changes ---
    if dev_mode:
        theme_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme.py")

        if os.path.exists(theme_file):
            watcher = QFileSystemWatcher([theme_file])
            
            def _reload_stylesheet(path):
                """Hot-reload the stylesheet when theme.py changes."""
                try:
                    import importlib
                    import frameguard.theme as theme_mod
                    importlib.reload(theme_mod)
                    new_css = theme_mod.generate_stylesheet()
                    app.setStyleSheet(new_css)
                    print(f"[DEV] Stylesheet reloaded ({len(new_css):,} chars)")
                except Exception as e:
                    print(f"[DEV] Reload failed: {e}")
            
            watcher.fileChanged.connect(_reload_stylesheet)
            # Keep reference so it's not garbage collected
            window._dev_watcher = watcher
            print(f"[DEV] Watching: {theme_file}")
            print("[DEV] Edit theme.py and save — changes appear instantly")
        else:
            print("[DEV] WARNING: Could not find theme.py to watch")

    sys.exit(app.exec())
