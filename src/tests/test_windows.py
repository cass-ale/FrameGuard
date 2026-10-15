"""Windows chrome contracts that are safe to exercise on every CI host."""

import ctypes
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QMainWindow

pytestmark = pytest.mark.usefixtures("qapp")
REPO = Path(__file__).resolve().parents[1]


class TestWindowsPackageGate:
    def test_frozen_startup_gate_runs_before_installer_compilation(self):
        builder = (REPO / "build_installer.bat").read_text(encoding="utf-8")
        frozen_gate = builder.index("--expect-frozen")
        installer = builder.index('"%ISCC%" installer.iss')
        assert frozen_gate < installer
        assert "--qpa windows" in builder
        assert "--timeout 30" in builder

    def test_builder_smokes_final_installer_before_success(self):
        builder = (REPO / "build_installer.bat").read_text(encoding="utf-8")
        compiled = builder.index('"%ISCC%" installer.iss')
        smoke = builder.index("tools_dev\\windows_installer_smoke.ps1")
        success = builder.index("BUILD SUCCESSFUL")
        assert compiled < smoke < success
        assert '-Installer "Output\\FrameGuardInstaller.exe"' in builder
        assert '-Python "%PY%"' in builder
        assert "--skip-installer-smoke" not in builder

    def test_installer_smoke_has_bounded_processes_and_unconditional_cleanup(self):
        smoke = (REPO / "tools_dev" / "windows_installer_smoke.ps1").read_text(
            encoding="utf-8")
        helper = smoke.index("function Wait-FrameGuardProcess")
        install_attempt = smoke.index("$summary.install_attempted = $true")
        install_start = smoke.index("Start-Process -FilePath $installerPath")
        install_wait = smoke.index(
            'Wait-FrameGuardProcess -Process $setup -Label "installer"')
        outer_final = smoke.index("finally {")
        partial_probe = smoke.index("$partialInstallExists = Test-Path")
        uninstall_attempt = smoke.index("$summary.uninstall_attempted = $true")
        uninstall_start = smoke.index("Start-Process -FilePath $uninstaller")
        uninstall_wait = smoke.index(
            'Wait-FrameGuardProcess -Process $uninstall -Label "uninstaller"')
        cleanup = smoke.index("Remove-Item -LiteralPath $root -Recurse -Force")
        evidence = smoke.index("windows-installer-smoke.json")
        terminal_failure = smoke.rindex("Windows installer smoke failed")

        assert helper < install_attempt < install_start < install_wait < outer_final
        assert outer_final < partial_probe < uninstall_attempt < uninstall_start
        assert uninstall_start < uninstall_wait < cleanup < evidence < terminal_failure

        assert "-PassThru -Wait" not in smoke
        assert "$Process.WaitForExit($ProcessTimeoutSeconds * 1000)" in smoke
        assert "$Process.WaitForExit(10000)" in smoke
        assert "Stop-Process -Id $Process.Id -Force" in smoke
        assert "Partial installation has no uninstaller" in smoke
        assert "install_completed = $false" in smoke
        assert "$summary.uninstall_completed = $false" in smoke
        assert "$summary.temp_root_removed = $false" in smoke
        assert '$summary.status = "failed"' in smoke
        assert "-ErrorAction Stop" in smoke
        assert "startup_smoke.py" in smoke


class TestWindowsTitleBar:
    def test_caption_controls_are_accessible_and_semantic(self, qapp):
        from frameguard.gui import TitleBar

        window = QMainWindow()
        window.resize(900, 600)
        bar = TitleBar(window)
        bar.resize(900, bar.height())
        bar.show()
        qapp.processEvents()

        expected = (
            (bar._btn_minimize, "Minimize window", "icon"),
            (bar._btn_maximize, "Maximize window", "icon"),
            (bar._btn_close, "Close window", "destructive"),
        )
        for button, accessible_name, role in expected:
            assert button.accessibleName() == accessible_name
            assert button.property("fgRole") == role
            assert button.height() >= 28

        window.close()

    def test_hit_test_excludes_buttons_and_exposes_snap_layouts(self, qapp):
        from frameguard.gui import TitleBar

        window = QMainWindow()
        window.resize(900, 600)
        bar = TitleBar(window)
        bar.resize(900, bar.height())
        bar.show()
        qapp.processEvents()

        drag_point = bar.mapTo(window, QPoint(300, bar.height() // 2))
        minimize = bar._btn_minimize.mapTo(window, bar._btn_minimize.rect().center())
        maximize = bar._btn_maximize.mapTo(window, bar._btn_maximize.rect().center())
        close = bar._btn_close.mapTo(window, bar._btn_close.rect().center())

        assert bar.hit_test_code(drag_point) == 2  # HTCAPTION
        assert bar.hit_test_code(minimize) == 1   # HTCLIENT
        assert bar.hit_test_code(maximize) == 9   # HTMAXBUTTON / Snap Layouts
        assert bar.hit_test_code(close) == 1      # HTCLIENT
        window.close()

    def test_maximize_glyph_tracks_window_state(self, qapp):
        from frameguard.gui import TitleBar

        window = QMainWindow()
        bar = TitleBar(window)
        window.showMaximized()
        qapp.processEvents()
        bar.sync_maximize_button()
        assert bar._btn_maximize.toolTip() == "Restore Down"
        assert bar._btn_maximize.accessibleName() == "Restore window"
        window.showNormal()
        qapp.processEvents()
        bar.sync_maximize_button()
        assert bar._btn_maximize.toolTip() == "Maximize"
        assert bar._btn_maximize.accessibleName() == "Maximize window"
        window.close()


class TestWindowsNativeContracts:
    def test_custom_chrome_remains_windows_only(self):
        from frameguard import gui

        assert gui.USE_CUSTOM_CHROME == (gui.sys.platform == "win32")

    def test_native_event_keeps_resize_snap_and_taskbar_contracts(self):
        from frameguard.gui import MainWindow, TitleBar

        native_source = inspect.getsource(MainWindow.nativeEvent)
        for contract in ("WM_GETMINMAXINFO", "WM_NCHITTEST"):
            assert contract in native_source
        assert "HTMAXBUTTON" in inspect.getsource(TitleBar.hit_test_code)

    def test_dwm_contract_applies_dark_caption_border_and_text(self, monkeypatch):
        from frameguard import gui as G
        from frameguard.theme import get_theme

        calls = []

        class _Dwm:
            @staticmethod
            def DwmSetWindowAttribute(hwnd, attribute, value, size):
                calls.append(attribute.value)
                return 0

        window = G.MainWindow()
        try:
            monkeypatch.setattr(G.sys, "platform", "win32")
            monkeypatch.setattr(
                ctypes, "windll", SimpleNamespace(dwmapi=_Dwm()), raising=False)
            window._apply_window_chrome_theme(get_theme("Meta Dark"))
            assert calls == [20, 35, 34, 36]
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_alt_space_forwards_to_native_system_menu(self, monkeypatch):
        from frameguard import gui as G

        calls = []

        class _User32:
            @staticmethod
            def PostMessageW(*args):
                calls.append(args)
                return 1

        window = G.MainWindow()
        try:
            monkeypatch.setattr(G, "USE_CUSTOM_CHROME", True)
            monkeypatch.setattr(
                ctypes, "windll", SimpleNamespace(user32=_User32()), raising=False)
            event = QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.AltModifier)
            window.keyPressEvent(event)
            assert event.isAccepted()
            assert len(calls) == 1
            assert calls[0][1:] == (0x0112, 0xF100, ord(" "))
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_window_rect_clamps_in_logical_pixels(self):
        from frameguard.gui import _clamp_window_rect

        available = QRect(100, 50, 1200, 800)
        assert _clamp_window_rect(QRect(-500, -400, 1600, 1000), available) == available
        assert _clamp_window_rect(QRect(1250, 760, 600, 400), available) == QRect(
            700, 450, 600, 400)

    def test_resize_border_meets_practical_hit_target(self):
        from frameguard.gui import MainWindow

        window = MainWindow()
        try:
            assert window._resize_border_width() >= 8
            assert window.minimumWidth() <= 480
            assert window.minimumHeight() <= 320
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_offscreen_saved_geometry_is_recovered_and_normal_geometry_persists(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import (
            MainWindow, _SETTINGS_APP, _SETTINGS_ORG, _WINDOW_GEOMETRY_KEY,
            _active_available_geometry,
        )

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(_WINDOW_GEOMETRY_KEY, QRect(50000, 50000, 1200, 800))
        window = MainWindow()
        try:
            available = _active_available_geometry()
            assert available.contains(window.geometry())
            window._save_window_state()
            saved = settings.value(_WINDOW_GEOMETRY_KEY)
            assert isinstance(saved, QRect)
            assert available.intersects(saved)
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()
