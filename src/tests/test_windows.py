"""Windows chrome contracts that are safe to exercise on every CI host."""

import ctypes

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect

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


class TestWindowsNativeChrome:
    """0.9.1 replaced the frameless window and hand-drawn title bar with the
    native frame, themed through DWM."""

    def test_caption_attributes_follow_the_theme(self):
        from frameguard import gui as G
        from frameguard.theme import get_theme

        for name in ("Blue Hour", "Clean Light", "Warm Cream"):
            theme = get_theme(name)
            attrs = dict(G._windows_caption_attributes(theme))
            assert attrs[G._DWMWA_CAPTION_COLOR] == G._colorref(theme.bg_secondary)
            assert attrs[G._DWMWA_TEXT_COLOR] == G._colorref(theme.text_primary)
            assert attrs[G._DWMWA_BORDER_COLOR] == G._colorref(theme.border)
            assert attrs[G._DWMWA_WINDOW_CORNER_PREFERENCE] == G._DWMWCP_ROUND
            dark = G.QColor(theme.bg_primary).lightness() < 128
            assert attrs[G._DWMWA_USE_IMMERSIVE_DARK_MODE] == int(dark)
        assert G._colorref("#112233") == 0x332211

    def test_dwm_calls_dark_mode_first_and_falls_back_on_old_windows_10(self, monkeypatch):
        from frameguard import gui as G
        from frameguard.theme import get_theme

        calls = []

        class _Dwm:
            @staticmethod
            def DwmSetWindowAttribute(hwnd, attribute, value, size):
                calls.append(attribute.value)
                # Pre-20H1 Windows 10 rejects 20 and every Windows 11 attribute.
                return 0 if attribute.value == 19 else -2147024809

        monkeypatch.setattr(G.sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=_Dwm()), raising=False)
        assert G._apply_windows_caption(1234, get_theme("Blue Hour")) is False
        assert calls[:2] == [20, 19]
        assert set(calls[2:]) == {33, 35, 34, 36}

    def test_no_caption_calls_off_windows_or_without_a_window(self, monkeypatch):
        from frameguard import gui as G
        from frameguard.theme import get_theme

        monkeypatch.setattr(G.sys, "platform", "darwin")
        assert G._apply_windows_caption(1234, get_theme("Blue Hour")) is False
        monkeypatch.setattr(G.sys, "platform", "win32")
        assert G._apply_windows_caption(0, get_theme("Blue Hour")) is False


class TestWindowsShortcutIcons:
    def test_icon_file_is_a_valid_multi_size_ico(self, qapp, tmp_path):
        import struct
        from frameguard import gui as G, winshell
        from frameguard.theme import get_theme

        path = winshell.write_theme_icon(G._themed_app_icon(get_theme("Abyss Teal")),
                                         "Abyss Teal", directory=str(tmp_path))
        assert Path(path).name == "frameguard-abyss-teal.ico"
        data = Path(path).read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        assert (reserved, kind) == (0, 1) and count >= 6
        sides = {data[6 + 16 * i] or 256 for i in range(count)}
        assert {16, 32, 48, 256} <= sides
        for i in range(count):
            size, offset = struct.unpack("<II", data[6 + 16 * i + 8: 6 + 16 * i + 16])
            assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + size <= len(data)

    def test_icon_names_are_per_theme_and_filesystem_safe(self):
        from frameguard import winshell

        assert winshell.icon_file_name("Blue Hour") == "frameguard-blue-hour.ico"
        assert winshell.icon_file_name("Solarized Dark") != winshell.icon_file_name("Blue Hour")
        assert "/" not in winshell.icon_file_name("A/B:C")

    def test_retheme_is_a_no_op_off_windows_or_from_source(self, monkeypatch, tmp_path):
        from frameguard import winshell

        icon = tmp_path / "x.ico"
        icon.write_bytes(b"\0")
        monkeypatch.setattr(winshell.sys, "platform", "darwin")
        assert winshell.retheme_shortcuts(str(icon), target_exe="C:/x.exe", dirs=[str(tmp_path)]) == []
        monkeypatch.setattr(winshell.sys, "platform", "win32")
        monkeypatch.delattr(winshell.sys, "frozen", raising=False)
        assert winshell.retheme_shortcuts(str(icon), dirs=[str(tmp_path)]) == []

    @pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="needs the Windows shell")
    def test_only_shortcuts_to_this_exe_are_re_iconed(self, qapp, tmp_path):
        import shutil
        import subprocess
        import sys
        from frameguard import gui as G, winshell
        from frameguard.theme import get_theme

        exe, other = tmp_path / "FrameGuard.exe", tmp_path / "Other.exe"
        shutil.copy2(sys.executable, exe)
        shutil.copy2(sys.executable, other)
        nested = tmp_path / "Start Menu" / "FrameGuard Desktop"
        nested.mkdir(parents=True)

        def make(link, target):
            script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
                      f"$s.TargetPath='{target}';$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)

        def icon_of(link):
            script = f"(New-Object -ComObject WScript.Shell).CreateShortcut('{link}').IconLocation"
            return subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                  capture_output=True, text=True, check=True).stdout.strip()

        ours, ours_nested, theirs = tmp_path / "FG.lnk", nested / "FG.lnk", tmp_path / "Other.lnk"
        make(ours, exe)
        make(ours_nested, exe)
        make(theirs, other)
        ico = winshell.write_theme_icon(G._themed_app_icon(get_theme("Sage")), "Sage",
                                        directory=str(tmp_path / "icons"))
        changed = winshell.retheme_shortcuts(ico, target_exe=str(exe), dirs=[str(tmp_path)])
        assert sorted(map(Path, changed)) == sorted([ours, ours_nested])
        assert icon_of(ours).lower() == f"{ico},0".lower()
        assert "frameguard-sage" not in icon_of(theirs)
        assert winshell.retheme_shortcuts(ico, target_exe=str(exe), dirs=[str(tmp_path)]) == []


class TestWindowsShortcutSafety:
    def test_user_custom_icons_are_kept(self, tmp_path, monkeypatch):
        from frameguard import winshell

        exe = tmp_path / "FrameGuard Desktop" / "FrameGuard.exe"
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        cache = Path(winshell.icon_cache_dir())
        assert winshell._is_frameguard_icon("", str(exe))
        assert winshell._is_frameguard_icon(str(exe), str(exe))
        assert winshell._is_frameguard_icon(str(exe.parent / "frameguard.ico"), str(exe))
        assert winshell._is_frameguard_icon(str(cache / "frameguard-sage.ico"), str(exe))
        assert not winshell._is_frameguard_icon(r"C:\Windows\System32\imageres.dll", str(exe))
        assert not winshell._is_frameguard_icon(str(tmp_path / "mine.ico"), str(exe))

    def test_rapid_theme_switches_end_on_the_last_theme(self, monkeypatch):
        import threading
        from frameguard import winshell

        applied, gate = [], threading.Event()

        def slow_retheme(icon_path, *args, **kwargs):
            gate.wait(2)
            applied.append(icon_path)
            return []

        monkeypatch.setattr(winshell, "retheme_shortcuts", slow_retheme)
        for name in ("a.ico", "b.ico", "c.ico", "d.ico"):
            winshell.request_retheme(name)
        gate.set()
        winshell._worker.join(5)
        assert applied[-1] == "d.ico"
        assert len(applied) <= 2  # the first in flight, then only the latest

    @pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="needs the Windows shell")
    def test_a_corrupt_or_foreign_shortcut_does_not_stop_the_walk(self, qapp, tmp_path):
        import shutil
        import subprocess
        import sys
        from frameguard import gui as G, winshell
        from frameguard.theme import get_theme

        exe = tmp_path / "FrameGuard.exe"
        shutil.copy2(sys.executable, exe)
        (tmp_path / "a_corrupt.lnk").write_bytes(b"not a shortcut")
        ours = tmp_path / "z_ours.lnk"
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{ours}');"
                  f"$s.TargetPath='{exe}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
        ico = winshell.write_theme_icon(G._themed_app_icon(get_theme("Carbon")), "Carbon",
                                        directory=str(tmp_path / "icons"))
        assert winshell.retheme_shortcuts(ico, target_exe=str(exe), dirs=[str(tmp_path)]) == [str(ours)]

    def test_shortcut_folders_are_per_user_only(self, monkeypatch, tmp_path):
        from frameguard import winshell

        monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
        assert all(not d.lower().startswith(str(tmp_path).lower()) for d in winshell.shortcut_dirs())


class TestNativeFrameGeometry:
    def test_caption_is_kept_on_screen(self):
        from frameguard.gui import _frame_fitted_geometry

        available = QRect(0, 0, 1366, 728)          # 1366x768 minus the taskbar
        caption, border = 31, 1
        inner = QRect(100, 0, 1200, 800)             # client clamped to y=0 (0.9.0 save)
        frame = inner.adjusted(-border, -caption, border, border)
        fitted = _frame_fitted_geometry(inner, frame, available)
        fitted_frame = fitted.adjusted(-border, -caption, border, border)
        assert available.contains(fitted_frame), fitted_frame
        assert fitted.top() == caption

    def test_window_already_on_screen_is_untouched(self):
        from frameguard.gui import _frame_fitted_geometry

        available = QRect(0, 0, 2560, 1400)
        inner = QRect(200, 140, 1200, 800)
        frame = inner.adjusted(-1, -31, 1, 1)
        assert _frame_fitted_geometry(inner, frame, available) == inner


class TestWindowsNativeContracts:
    def test_window_rect_clamps_in_logical_pixels(self):
        from frameguard.gui import _clamp_window_rect

        available = QRect(100, 50, 1200, 800)
        assert _clamp_window_rect(QRect(-500, -400, 1600, 1000), available) == available
        assert _clamp_window_rect(QRect(1250, 760, 600, 400), available) == QRect(
            700, 450, 600, 400)

    def test_minimum_size_fits_small_laptops(self):
        from frameguard.gui import MainWindow

        window = MainWindow()
        try:
            # Small enough for 1366x768 laptops, large enough for the pages'
            # layouts (0.9.0 raised it from 480x320, which cut the table off).
            assert window.minimumWidth() <= 760
            assert window.minimumHeight() <= 520
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
