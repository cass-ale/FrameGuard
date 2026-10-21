"""Regression tests for v0.9.0: the Monogram mark and theme-aware app icons.

Before 0.9.0 the Dock always showed the old icon: startup set the .ico as the
application icon and theme changes only retinted the window icon, which macOS
never displays. Qt 6.9 also hands AppKit a 32 px rendition of the application
icon, so the Dock upscaled it. The menu-bar icon was accent-tinted artwork
filling about 60% of its slot instead of a template glyph.
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]


def _alpha_bbox(image, threshold=40):
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() > threshold:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


@pytest.fixture
def window(qapp):
    from frameguard.gui import MainWindow
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    for _ in range(4):
        qapp.processEvents()
    yield w
    w._cleanup_worker()
    if getattr(w, "_watch_monitor", None) is not None:
        w._watch_monitor.stop()
    w.hide()
    w.deleteLater()


class TestMarkGeometry:
    def test_both_optical_masters_render(self, qapp):
        from frameguard import brand
        for small in (False, True):
            stem, tick = brand.glyph_paths(small)
            assert not stem.isEmpty() and not tick.isEmpty()
            ink = brand.silhouette(small).boundingRect()
            assert 0 <= ink.left() and ink.right() <= 100 and 0 <= ink.top() and ink.bottom() <= 100

    def test_small_master_keeps_two_pixel_strokes_at_16px(self):
        from frameguard import brand
        thinnest = min(brand._SMALL["stem_width"], brand._SMALL["tick_width"])
        assert thinnest * 16 / 100 >= 2.0

    def test_mac_icon_follows_apples_grid(self, qapp):
        """824 px body on 1024 with transparent margin: macOS 26+ shows
        conforming icons full size, and shrinks others onto a grey tile."""
        from frameguard import brand
        image = brand.app_icon_image(256, brand.BRAND, "mac")
        left, top, right, bottom = _alpha_bbox(image)
        expected = 256 * brand.MAC_BODY / brand.MAC_GRID
        assert abs((right - left + 1) - expected) <= 2
        assert abs((bottom - top + 1) - expected) <= 2
        assert image.pixelColor(2, 2).alpha() == 0
        assert image.pixelColor(128, 128 + 90).alpha() == 255

    def test_full_grid_icon_fills_the_canvas_for_windows(self, qapp):
        from frameguard import brand
        image = brand.app_icon_image(32, brand.BRAND, "full")
        left, top, right, bottom = _alpha_bbox(image)
        assert right - left + 1 >= 29 and bottom - top + 1 >= 29

    def test_every_theme_icon_is_legible(self):
        from frameguard import brand
        from frameguard.theme import THEMES, contrast_ratio
        for name, theme in THEMES.items():
            palette = brand.theme_palette(theme)
            for plate in (palette.plate_top, palette.plate_bottom):
                assert contrast_ratio(palette.stem, plate) >= 4.5, (name, plate)
                assert contrast_ratio(palette.tick, plate) >= 3.0, (name, plate)
            for page in (theme.bg_primary, theme.bg_secondary):
                assert contrast_ratio(theme.accent, page) >= 3.0, (name, page)


class TestMenuBarIcon:
    def test_template_glyph_is_a_mask(self, qapp):
        from frameguard import brand
        assert brand.menubar_template_icon().isMask()

    def test_template_glyph_fills_the_slot(self, qapp):
        from frameguard import brand
        image = brand.menubar_template_image(18, 2)
        left, top, right, bottom = _alpha_bbox(image)
        assert max(right - left + 1, bottom - top + 1) >= 34  # of 36 px

    def test_tray_uses_the_template_on_macos(self, qapp, monkeypatch):
        from frameguard import gui
        from frameguard.theme import get_theme
        monkeypatch.setattr(gui.sys, "platform", "darwin")
        assert gui._tray_icon_for(get_theme("Blue Hour")).isMask()
        monkeypatch.setattr(gui.sys, "platform", "win32")
        assert not gui._tray_icon_for(get_theme("Blue Hour")).isMask()


class TestThemedAppIcon:
    @staticmethod
    def _plate(icon):
        from PySide6.QtCore import QSize
        image = icon.pixmap(QSize(128, 128)).toImage()
        return image.pixelColor(int(image.width() * 0.25), int(image.height() * 0.80))

    def test_theme_change_recolours_application_and_window_icons(self, window, qapp):
        from PySide6.QtGui import QColor
        from frameguard.theme import get_theme
        for name in ("Blue Hour", "Clean Light", "Monokai Pro"):
            window._apply_theme(name)
            qapp.processEvents()
            theme = get_theme(name)
            for icon in (qapp.windowIcon(), window.windowIcon()):
                plate = self._plate(icon)
                bgs = [QColor(theme.bg_primary), QColor(theme.bg_secondary), QColor(theme.bg_tertiary)]
                assert min(abs(plate.lightness() - c.lightness()) for c in bgs) <= 24, (name, plate.name())

    def test_sidebar_mark_is_two_tone_and_retina_ready(self, window, qapp):
        from PySide6.QtGui import QColor
        from frameguard.theme import get_theme
        window._apply_theme("Abyss Teal")
        pixmap = window._sidebar_logo_small.pixmap()
        assert pixmap.deviceIndependentSize().width() == 28
        image = pixmap.toImage()
        theme = get_theme("Abyss Teal")
        colours = {image.pixelColor(x, y).name() for y in range(image.height())
                   for x in range(image.width()) if image.pixelColor(x, y).alpha() == 255}
        def has(ref):
            r = QColor(ref)
            return any(sum(abs(a - b) for a, b in zip(QColor(c).getRgb()[:3], r.getRgb()[:3])) <= 30
                       for c in colours)
        assert has(theme.text_primary) and has(theme.accent)

    def test_dock_setter_is_a_noop_off_cocoa(self, qapp):
        from frameguard import brand, gui
        if qapp.platformName() == "cocoa":
            pytest.skip("exercised natively by the subprocess probe")
        assert gui._set_macos_dock_icon(brand.app_icon_image(64)) is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Dock only")
    def test_dock_shows_the_themed_icon_at_full_resolution(self, tmp_path):
        env = dict(os.environ, QT_QPA_PLATFORM="cocoa", HOME=str(tmp_path))
        env.pop("FRAMEGUARD_TEST_MODE", None)
        completed = subprocess.run([sys.executable, str(SRC / "tests" / "dock_icon_probe.py")],
                                   cwd=str(SRC), env=env, capture_output=True, text=True, timeout=90)
        if completed.returncode == 3:
            pytest.skip("not running on Cocoa: " + completed.stdout.strip()[-200:])
        assert completed.returncode == 0, completed.stdout + completed.stderr[-2000:]
        rows = {parts[1]: parts for parts in (line.split() for line in completed.stdout.splitlines())
                if parts and parts[0] == "ICON"}
        from PySide6.QtGui import QColor
        # AppKit keeps its own copy at the screen's scale: 1024 px on Retina,
        # 512 px on a 1x display (CI). Qt alone handed it 32 px.
        assert int(rows["Blue_Hour"][2]) >= 512 and int(rows["Clean_Light"][2]) >= 512
        assert QColor(rows["Blue_Hour"][3]).lightness() < 80
        assert QColor(rows["Clean_Light"][3]).lightness() > 200


class TestShippedIcons:
    def test_committed_brand_assets_match_the_geometry(self, qapp):
        sys.path.insert(0, str(SRC / "tools_dev"))
        try:
            import make_brand_assets
        finally:
            sys.path.pop(0)
        assert make_brand_assets.main(["--check"]) == 0

    def test_icns_packs_every_size_from_branding(self, tmp_path):
        out = tmp_path / "fg.icns"
        completed = subprocess.run([sys.executable, str(SRC / "assets" / "make_icns.py"), "--out", str(out)],
                                   capture_output=True, text=True, timeout=60)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        data = out.read_bytes()
        assert data[:4] == b"icns" and struct.unpack(">I", data[4:8])[0] == len(data)
        sizes, offset = set(), 8
        while offset < len(data):
            length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            payload = data[offset + 8:offset + length]
            sizes.add(struct.unpack(">I", payload[16:20])[0])
            offset += length
        assert sizes == {16, 32, 64, 128, 256, 512, 1024}

    def test_ico_has_every_windows_size(self):
        data = (SRC / "assets" / "frameguard.ico").read_bytes()
        count = struct.unpack("<H", data[4:6])[0]
        sizes = set()
        for i in range(count):
            width = data[6 + 16 * i] or 256
            offset = struct.unpack("<I", data[18 + 16 * i:22 + 16 * i])[0]
            assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
            sizes.add(width)
        assert {16, 20, 24, 32, 40, 48, 64, 256} <= sizes

    def test_no_reference_to_deleted_artwork(self):
        gone = ("frameguard_logo.png", "icon_64.png", "theme_icons.json", "decode_icons")
        roots = [SRC / "frameguard", SRC / "tools_dev", SRC / "assets"]
        files = [p for root in roots for p in root.rglob("*") if p.suffix in (".py", ".json")]
        files += [SRC / n for n in ("frameguard.spec", "installer.iss", "build_mac.sh",
                                    "BUILD_README.md", "README.md") if (SRC / n).exists()]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in gone:
                assert name not in text, f"{path.relative_to(SRC)} still references {name}"

    def test_brand_module_is_frozen_into_the_app(self):
        assert "'frameguard.brand'" in (SRC / "frameguard.spec").read_text(encoding="utf-8")


def test_installer_shows_the_same_licence_as_the_repository():
    """installer.iss displays src/LICENSE; it must not drift from the
    repository's canonical MIT text (GitHub's licence detection reads the root)."""
    root = (SRC.parent / "LICENSE").read_text(encoding="utf-8")
    assert (SRC / "LICENSE").read_text(encoding="utf-8") == root


# ---------------------------------------------------------------------------
# App-shell audit fixes
# ---------------------------------------------------------------------------

def _wait_for(qapp, predicate, timeout_ms=6000):
    """Pump events until predicate() is true (CI runners can be slow)."""
    import time
    end = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _pump(qapp, ms):
    import time
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


class TestWorkerSignalsStayOnTheGuiThread:
    def test_progress_updates_run_on_the_gui_thread(self, window, qapp, tmp_path, monkeypatch):
        """Lambdas connected to worker signals ran on the worker thread and
        touched widgets there; every progress update must be on the GUI thread."""
        from PySide6.QtCore import QThread
        from frameguard import gui

        def fake_run(worker):
            worker.scan_started.emit(1)
            worker.file_started.emit(0, "clip.mov")
            for i in range(20):
                worker.file_progress.emit(0, "decode", i, 20, "Decoding")
            worker.scan_finished.emit([])

        monkeypatch.setattr(gui.ScanWorker, "configure", lambda self, **kw: None)
        monkeypatch.setattr(gui.ScanWorker, "run", fake_run)
        threads = []
        panel = window.scan_panel
        original_set, original_update = panel.progress_label.setText, panel.update_progress
        monkeypatch.setattr(panel.progress_label, "setText",
                            lambda text: (threads.append(QThread.currentThread()), original_set(text)))
        monkeypatch.setattr(panel, "update_progress",
                            lambda *a: (threads.append(QThread.currentThread()), original_update(*a)))
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"\0" * 16)
        window._on_scan_requested([str(clip)], "custom", True, False)
        assert _wait_for(qapp, lambda: len(threads) >= 21)
        assert all(t is qapp.thread() for t in threads)

    def test_no_lambda_slots_on_background_signals(self):
        import re
        text = (SRC / "frameguard/gui.py").read_text(encoding="utf-8")
        offenders = re.findall(r"(self\._worker\.\w+|runner\.signals\.\w+)\.connect\(\s*lambda", text)
        assert not offenders, offenders


class TestFolderSearchRace:
    def test_slow_old_search_cannot_replace_newer_queue_or_refill_a_clear(
            self, window, qapp, tmp_path, monkeypatch):
        import time
        from frameguard import core
        slow, fast = tmp_path / "slow", tmp_path / "fast"
        slow.mkdir()
        fast.mkdir()

        def fake_discover(path):
            if str(path) == str(slow):
                time.sleep(0.6)
                return [str(slow / "old_take.mov")]
            return [str(fast / "new_take.mov")]

        monkeypatch.setattr(core, "discover_video_files", fake_discover)
        panel = window.scan_panel
        panel._discover_files_async([str(slow)])
        panel._discover_files_async([str(fast)])
        assert _wait_for(qapp, lambda: not panel._discovery_runners)
        _pump(qapp, 100)
        assert panel._files == [str(fast / "new_take.mov")]

        panel._discover_files_async([str(slow)])
        panel._on_files_dropped([])  # Clear while the search runs
        assert _wait_for(qapp, lambda: not panel._discovery_runners)
        _pump(qapp, 100)
        assert panel._files == []


class TestUnattendedErrors:
    def test_watch_errors_never_open_modals(self, window, qapp, monkeypatch):
        from frameguard import gui
        modals = []
        monkeypatch.setattr(gui.QMessageBox, "critical", lambda *a, **k: modals.append(a))
        window.hide()
        for _ in range(3):
            window._watch_batch_in_progress = True
            window._on_scan_error("FFmpeg not found")
        assert modals == []
        assert "Scan error: FFmpeg not found" in window.watch_panel._log.toPlainText()


class TestAutosave:
    def test_settings_are_saved_without_quitting(self, window, qapp):
        from frameguard import gui
        target = not window.settings_panel.strict_cb.isChecked()
        window.settings_panel.strict_cb.setChecked(target)
        assert _wait_for(qapp, lambda: gui.QSettings(gui._SETTINGS_ORG, gui._SETTINGS_APP).value(
            "scan/strict", not target, type=bool) is target)

    def test_sidebar_collapse_survives_a_restart(self, window, qapp):
        from frameguard import gui
        window._toggle_sidebar()
        assert _wait_for(qapp, lambda: gui.QSettings(gui._SETTINGS_ORG, gui._SETTINGS_APP).value(
            gui._SIDEBAR_COLLAPSED_KEY, False, type=bool) is True)
        second = gui.MainWindow()
        try:
            assert second._sidebar_collapsed and not second._sidebar.isVisible()
        finally:
            second._cleanup_worker()
            second.deleteLater()


class TestLayoutAtSmallSizes:
    def test_minimum_size_fits_the_pages(self, window, qapp):
        from frameguard import gui
        screen = gui._active_available_geometry()
        assert window.minimumWidth() == min(gui.MIN_WINDOW_W, screen.width())
        assert window.minimumHeight() == min(gui.MIN_WINDOW_H, screen.height())

    def test_filename_keeps_its_width_and_the_table_its_rows(self, window, qapp):
        from tests.conftest import make_result
        window.resize(800, 600)
        window._switch_panel(1)
        results = [make_result(name=f"PROJ_CAM0{i}_TAKE_20261018_C00{i}_LONG_DESCRIPTIVE_NAME.mov")
                   for i in range(8)]
        window.results_panel.set_results(results)
        _pump(qapp, 100)
        table = window.results_panel.table
        assert table.columnWidth(2) >= 180, table.columnWidth(2)
        rows_visible = table.viewport().height() // max(1, table.rowHeight(0))
        assert rows_visible >= 3, rows_visible


class TestThemeAndAccessibility:
    def test_control_outlines_meet_three_to_one(self):
        from frameguard.theme import THEMES, contrast_ratio, generate_stylesheet
        for name, theme in THEMES.items():
            for bg in (theme.bg_input, theme.bg_primary):
                assert contrast_ratio(theme.control_border, bg) >= 3.0, (name, bg)
        css = generate_stylesheet(THEMES["Clean Light"])
        assert f"border: 2px solid {THEMES['Clean Light'].control_border}" in css

    def test_explicit_theme_pins_the_colour_scheme_for_dialogs(self, window, qapp, monkeypatch):
        from PySide6.QtCore import Qt
        hints = qapp.styleHints()
        if not hasattr(hints, "setColorScheme"):
            pytest.skip("Qt < 6.8")
        calls = []
        # Offscreen has no colour-scheme backend (colorScheme() stays
        # Unknown), so record what the app asks Qt for.
        monkeypatch.setattr(hints, "setColorScheme", lambda scheme: calls.append(scheme))
        monkeypatch.setattr(hints, "unsetColorScheme", lambda: calls.append("unset"))
        monkeypatch.setattr(hints, "colorScheme", lambda: Qt.ColorScheme.Unknown)
        window._theme_is_explicit = True
        window._apply_theme("Blue Hour")
        window._apply_theme("Clean Light")
        window._theme_is_explicit = False
        window._apply_theme("Clean Light")  # following the OS again
        assert calls[-3:] == [Qt.ColorScheme.Dark, Qt.ColorScheme.Light, "unset"]

    def test_every_slider_and_spin_box_has_a_name(self, window, qapp):
        from PySide6.QtWidgets import QDoubleSpinBox, QSlider, QSpinBox
        unnamed = [w for cls in (QSlider, QSpinBox, QDoubleSpinBox)
                   for w in window.findChildren(cls) if not w.accessibleName()]
        assert not unnamed

    def test_summary_badges_are_keyboard_operable(self, window, qapp):
        from PySide6.QtCore import Qt
        panel = window.results_panel
        for key, frame in panel._badge_frames.items():
            assert frame.focusPolicy() & Qt.TabFocus
            assert frame.accessibleName().endswith("show details")
        panel._update_badge_count("mismatched", "3")
        assert panel._badge_frames["mismatched"].accessibleName() == "Failed: 3, show details"

    def test_queue_can_be_trimmed_by_keyboard(self, window, qapp, tmp_path):
        from PySide6.QtCore import Qt
        files = [str(tmp_path / f"{n}.mov") for n in "abc"]
        panel = window.scan_panel
        panel._set_files(files)
        queue = panel.drop_zone._file_list
        assert queue.focusPolicy() & Qt.TabFocus
        queue.remove_requested.emit(1)
        assert panel._files == [files[0], files[2]]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS menu bar")
class TestMacMenuBar:
    def test_menu_bar_has_the_standard_items(self, window):
        from PySide6.QtGui import QAction, QKeySequence
        bar = window._mac_menu_bar
        actions = {a.text(): a for m in bar.findChildren(QAction) for a in [m]}
        assert actions["Settings\u2026"].menuRole() == QAction.MenuRole.PreferencesRole
        assert actions["Settings\u2026"].shortcut() == QKeySequence("Ctrl+,")
        assert actions["About FrameGuard"].menuRole() == QAction.MenuRole.AboutRole
        for text, key in (("Close Window", "Ctrl+W"), ("Minimize", "Ctrl+M"),
                          ("Add Videos\u2026", "Ctrl+O"), ("Export Report\u2026", "Ctrl+E")):
            assert actions[text].shortcut() == QKeySequence(key), text


def test_finder_open_with_lists_frameguard_for_videos():
    spec = (SRC / "frameguard.spec").read_text(encoding="utf-8")
    assert '"CFBundleTypeRole": "Viewer"' in spec and '"public.movie"' in spec
    assert '"LSHandlerRank": "Alternate"' in spec


class TestSingleInstance:
    def test_second_launch_hands_its_files_to_the_running_app(self, qapp, tmp_path, monkeypatch):
        import uuid
        from frameguard import gui
        name = f"fg-test-{uuid.uuid4().hex[:12]}"
        monkeypatch.setattr(gui, "_instance_server_name", lambda: name)
        received = []

        class Receiver:
            def accept_paths(self, paths):
                received.extend(paths)

        server = gui._SingleInstanceServer(Receiver())
        try:
            assert server.is_listening()
            clip = tmp_path / "take one.mov"
            clip.write_bytes(b"\0")
            # The second launch is another process; run the client off the GUI
            # thread so this event loop can accept and read (Windows pipes
            # have no buffer, so a same-thread write would never complete).
            import threading
            outcome = {}
            client = threading.Thread(target=lambda: outcome.update(
                ok=gui._forward_to_running_instance([str(clip)], timeout_ms=5000)))
            client.start()
            assert _wait_for(qapp, lambda: received == [str(clip)] and not client.is_alive())
            client.join(5)
            assert outcome.get("ok") is True
        finally:
            server.deleteLater()

    def test_no_running_instance_means_start_normally(self, qapp, monkeypatch):
        import uuid
        from frameguard import gui
        monkeypatch.setattr(gui, "_instance_server_name", lambda: f"fg-none-{uuid.uuid4().hex[:12]}")
        assert gui._forward_to_running_instance([], timeout_ms=200) is False


def test_windows_open_with_registers_videos_without_taking_defaults():
    iss = (SRC / "installer.iss").read_text(encoding="utf-8")
    assert 'Classes\\FrameGuard.Video\\shell\\open\\command' in iss
    for ext in ("mp4", "mov", "mkv", "mxf"):
        assert f'Classes\\.{ext}\\OpenWithProgids"; ValueType: string; ValueName: "FrameGuard.Video"' in iss
    # Never the default handler for a video extension.
    assert 'Classes\\.mp4"; ValueType: string; ValueName: ""' not in iss


def test_lazily_imported_modules_are_frozen_into_the_app():
    spec = (SRC / "frameguard.spec").read_text(encoding="utf-8")
    for module in ("'PySide6.QtNetwork'", "'truststore'", "'truststore._api'"):
        assert module in spec, module


def test_checks_matrix_puts_problem_columns_first(qapp):
    from frameguard.gui import ChecksPanel
    from tests.conftest import make_result
    clean = make_result(name="a.mov", outcome="pass")
    failing = make_result(name="b.mov", outcome="fail")
    panel = ChecksPanel()
    panel.set_results([clean, failing])
    worst = {}
    for r in (clean, failing):
        for name, status, *_ in r.checks:
            worst[name] = min(worst.get(name, 2), {"FAIL": 0, "WARN": 1}.get(status, 2))
    ranks = [worst[name] for name in panel._columns]
    assert ranks == sorted(ranks), ranks
    assert ranks and ranks[0] == 0


class TestReviewFollowUps:
    def test_strictness_rules_have_settings_controls(self, window):
        """Every 0.9 strict rule can be relaxed from Settings, not only by
        hand-editing a preset, and round-trips through the config."""
        panel = window.settings_panel
        cfg = panel.get_config_overrides()
        for key in ("allow_larger_resolution", "allow_fps_multiples", "require_progressive",
                    "min_audio_sample_rate", "loudness_mode", "max_decode_errors"):
            assert key in cfg, key
        panel._apply_preset_config({**cfg, "allow_larger_resolution": True,
                                    "loudness_mode": "off", "max_decode_errors": 5,
                                    "min_audio_sample_rate": 48000})
        after = panel.get_config_overrides()
        assert after["allow_larger_resolution"] is True
        assert after["loudness_mode"] == "off"
        assert after["max_decode_errors"] == 5
        assert after["min_audio_sample_rate"] == 48000
        assert "loudness_mode" not in panel._preset_extras
        for control in panel._rule_controls.values():
            assert control.accessibleName()

    def test_shutdown_stops_the_single_instance_server_first(self, window, monkeypatch, qapp):
        closed = []

        class Server:
            def close(self):
                closed.append(True)

        monkeypatch.setattr(qapp, "_fg_instance_server", Server(), raising=False)
        window._shutdown_services()
        assert closed == [True]
        window._services_stopped = False

    def test_closing_during_a_scan_asks_first(self, window, monkeypatch, qapp):
        from frameguard import gui
        asked = []
        monkeypatch.setattr(gui.QApplication, "platformName", staticmethod(lambda: "cocoa"))
        monkeypatch.setattr(gui.QMessageBox, "question",
                            lambda *a, **k: (asked.append(a), gui.QMessageBox.No)[1])

        class Thread:
            def isRunning(self):
                return True

        monkeypatch.setattr(window, "_worker_thread", Thread())
        assert window._confirm_close_during_scan() is False
        assert len(asked) == 1
        monkeypatch.setattr(window, "_worker_thread", None)
        assert window._confirm_close_during_scan() is True


    def test_preset_rule_values_are_never_substituted_or_inherited(self, window):
        panel = window.settings_panel
        base = panel.get_config_overrides()
        panel._apply_preset_config({**base, "min_audio_sample_rate": 22050,
                                    "max_decode_errors": 5000, "loudness_mode": "essential",
                                    "allow_fps_multiples": True})
        cfg = panel.get_config_overrides()
        assert cfg["min_audio_sample_rate"] == 22050
        assert cfg["max_decode_errors"] == 5000
        # A second preset that does not mention the rules gets the defaults back.
        plain = {k: v for k, v in base.items() if k not in panel._rule_controls}
        panel._apply_preset_config(plain)
        cfg = panel.get_config_overrides()
        assert cfg["loudness_mode"] == panel._defaults["loudness_mode"]
        assert cfg["allow_fps_multiples"] is bool(panel._defaults["allow_fps_multiples"])
