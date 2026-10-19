"""Regression tests for v0.8.2: theme rename, chrome and spacing fixes,
dropdown/menu audit, Open With players, reliable quitting, deep-scan region
closing and the PDF layout audit."""

import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from frameguard import core
from frameguard.core import DEFAULT_CONFIG, QAResult

SRC = Path(__file__).resolve().parents[1]


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


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


def _settle(qapp, n=4):
    for _ in range(n):
        qapp.processEvents()


# ---------------------------------------------------------------------------
# Theme rename and badge removal
# ---------------------------------------------------------------------------

class TestBlueHour:
    def test_meta_dark_is_now_blue_hour(self):
        from frameguard.theme import THEMES, get_theme
        assert "Blue Hour" in THEMES and "Meta Dark" not in THEMES
        assert get_theme("Meta Dark").name == "Blue Hour"

    def test_saved_meta_dark_preference_migrates(self, qapp, tmp_path):
        from PySide6.QtCore import QSettings
        from frameguard import gui
        settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
        settings.setValue("appearance/theme", "Meta Dark")
        assert gui.resolve_startup_theme(settings, prefers_dark=False) == ("Blue Hour", True)
        assert settings.value("appearance/theme") == "Blue Hour"

    def test_no_meta_branding_in_user_visible_sources(self):
        for path in ("frameguard/gui.py", "frameguard/theme.py", "frameguard/report.py"):
            text = (SRC / path).read_text(encoding="utf-8")
            visible = [line for line in text.splitlines()
                       if "Meta Dark" in line and "THEME_ALIASES" not in line
                       and not line.strip().startswith("#")
                       and "renamed" not in line]
            assert not visible, (path, visible)


class TestDssBadgeRemoved:
    def test_sidebar_has_no_compliance_badge(self, window):
        from PySide6.QtWidgets import QLabel
        texts = [label.text() for label in window._sidebar.findChildren(QLabel)]
        assert not any("DSS" in text for text in texts), texts
        assert not hasattr(window, "_dss_label")

    def test_report_footer_has_no_compliance_claim(self):
        assert "DSS" not in (SRC / "frameguard/report.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Spacing fixes
# ---------------------------------------------------------------------------

class TestSidebarAlignment:
    def test_active_row_text_starts_on_the_same_column(self):
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Clean Light"))
        # inactive: 1px border + 12px padding; active: 3px bar + 10px padding
        assert "#sidebar QPushButton { background: transparent; border-color: transparent;" in css
        assert "padding: 8px 12px;" in css
        assert "border-left: 3px solid" in css and "padding-left: 10px;" in css

    def test_import_preset_is_centred(self, window):
        button = window._sidebar_import_btn
        assert button.property("fgSidebarAction") is True
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Clean Light"))
        assert '#sidebar QPushButton[fgSidebarAction="true"] { text-align: center; }' in css


class TestSettingsGroups:
    def test_preset_buttons_share_one_row_of_equal_width(self, window, qapp):
        from frameguard import gui
        window._switch_panel(gui.PANEL_SETTINGS)
        _settle(qapp)
        sp = window.settings_panel
        buttons = (sp.export_preset_btn, sp.import_preset_btn, sp.open_presets_btn)
        assert len({b.mapTo(sp, b.rect().topLeft()).y() for b in buttons}) == 1
        assert max(b.width() for b in buttons) - min(b.width() for b in buttons) <= 1

    def test_update_buttons_share_one_row_and_field_is_labelled_above(self, window, qapp):
        from frameguard import gui
        window._switch_panel(gui.PANEL_SETTINGS)
        _settle(qapp)
        sp = window.settings_panel
        buttons = (sp.check_update_btn, sp.delta_update_btn, sp.report_problem_btn)
        tops = {b.mapTo(sp, b.rect().topLeft()).y() for b in buttons}
        assert len(tops) == 1
        assert max(b.width() for b in buttons) - min(b.width() for b in buttons) <= 1
        field_left = sp.channel_edit.mapTo(sp, sp.channel_edit.rect().topLeft()).x()
        button_left = sp.check_update_btn.mapTo(sp, sp.check_update_btn.rect().topLeft()).x()
        assert field_left == button_left


class TestResultsToolbar:
    def test_filter_and_buttons_are_grouped_evenly(self, window, qapp):
        from frameguard import gui
        window._switch_panel(gui.PANEL_RESULTS)
        _settle(qapp)
        rp = window.results_panel
        def left(w):
            return w.mapTo(rp, w.rect().topLeft()).x()
        def right(w):
            return left(w) + w.width()
        between_buttons = left(rp._more_btn) - right(rp.clear_btn)
        assert between_buttons == 8
        assert left(rp.export_btn) - right(rp._more_btn) == 8
        assert left(rp.clear_btn) - right(rp._issues_only_cb) >= 12


# ---------------------------------------------------------------------------
# Dropdowns and menus
# ---------------------------------------------------------------------------

class TestDropdowns:
    def test_popups_are_styled_lists_on_every_platform(self):
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Clean Light"))
        assert "QComboBox { combobox-popup: 0; }" in css
        assert "QComboBox QAbstractItemView::item:focus { border: none; }" in css

    def test_popup_is_wide_enough_for_the_longest_entry(self, window, qapp):
        combo = window.scan_panel.preset_combo
        combo.addItem("An exceptionally long custom preset name for a VR deliverable")
        _settle(qapp)
        assert combo.view().minimumWidth() >= combo.view().sizeHintForColumn(0)


class TestMenus:
    def test_overflow_menu_items_align_and_do_not_repeat_the_toolbar(self, window):
        menu, actions = window.results_panel._build_actions_menu()
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        assert not any("\u25B6" in text for text in texts), texts
        assert "Clear History" not in texts
        assert any(text.startswith("Open Selected With") for text in texts)

    def test_row_menu_offers_open_with(self, window):
        from PySide6.QtWidgets import QMenu
        result = QAResult("/nope/a.mov")
        menu = window.results_panel._build_row_menu([result])[0]
        sub = menu.findChild(QMenu, "open_with_menu")
        assert sub is not None
        assert any("Choose Another App" in a.text() for a in sub.actions())


# ---------------------------------------------------------------------------
# Open With
# ---------------------------------------------------------------------------

class TestPlayers:
    def test_default_player_sorts_first(self, monkeypatch):
        from frameguard import players as P
        monkeypatch.setattr(P, "_cache", {})
        fake = [P.Player("VLC", "/A/VLC.app", "/A/VLC.app"),
                P.Player("QuickTime Player", "/S/QT.app", "/S/QT.app", is_default=True),
                P.Player("IINA", "/A/IINA.app", "/A/IINA.app")]
        monkeypatch.setattr(P, "_discover_macos", lambda sample: list(fake))
        monkeypatch.setattr(P, "_discover_windows", lambda sample: list(fake))
        monkeypatch.setattr(P, "_discover_linux", lambda sample: list(fake))
        names = [p.name for p in P.discover_players("x.mov", refresh=True)]
        assert names == ["QuickTime Player", "IINA", "VLC"]

    def test_discovery_failure_degrades_to_no_list(self, monkeypatch):
        from frameguard import players as P
        monkeypatch.setattr(P, "_cache", {})
        for name in ("_discover_macos", "_discover_windows", "_discover_linux"):
            monkeypatch.setattr(P, name, mock.Mock(side_effect=OSError("boom")))
        assert P.discover_players("x.mkv", refresh=True) == []

    @pytest.mark.parametrize("platform, expected", [
        ("darwin", ["/usr/bin/open", "-a", "/Applications/VLC.app"]),
        ("win32", ["C:/VLC/vlc.exe"]),
        ("linux", ["/usr/bin/vlc"]),
    ])
    def test_launch_commands_pass_every_file(self, monkeypatch, platform, expected):
        from frameguard import players as P
        monkeypatch.setattr(P.sys, "platform", platform)
        path = {"darwin": "/Applications/VLC.app", "win32": "C:/VLC/vlc.exe",
                "linux": "/usr/bin/vlc"}[platform]
        command = P.build_command(P.Player("VLC", path, path), ["/a b.mov", "/c.mp4"])
        assert command[:len(expected)] == expected
        assert command[-2:] == [os.path.abspath("/a b.mov"), os.path.abspath("/c.mp4")]

    def test_missing_files_and_removed_apps_raise_clear_errors(self, tmp_path):
        from frameguard import players as P
        clip = tmp_path / "a.mov"
        with pytest.raises(P.LaunchError, match="exist"):
            P.open_files(P.Player("X", "/x", "/x"), [str(clip)])
        clip.write_bytes(b"0")
        with pytest.raises(P.LaunchError, match="no longer installed"):
            P.open_files(P.Player("Ghost", "/nope/Ghost.app", "/nope/Ghost.app"), [str(clip)])

    def test_launch_returns_immediately_and_reports_late_failure(self, monkeypatch, tmp_path):
        from frameguard import players as P
        import threading
        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        app = tmp_path / "Player.app"
        app.mkdir()

        class _Proc:
            stderr = None
            def wait(self, timeout=None):
                return 1

        monkeypatch.setattr(P, "_spawn", lambda command, capture_errors=False: _Proc())
        errors, done = [], threading.Event()
        def on_error(message):
            errors.append(message)
            done.set()
        opened = P.open_files(P.Player("Player", str(app), str(app)), [str(clip)], on_error)
        assert opened == [str(clip)]
        assert done.wait(2)
        assert "Player could not open the file" in errors[0]

    def test_batches_are_capped(self, monkeypatch, tmp_path):
        from frameguard import players as P
        clips = []
        for i in range(P.MAX_FILES_PER_LAUNCH + 5):
            clip = tmp_path / f"{i}.mov"
            clip.write_bytes(b"0")
            clips.append(str(clip))
        launched = []
        monkeypatch.setattr(P, "_spawn", lambda command, capture_errors=False:
                            launched.append(command) or mock.Mock(wait=lambda timeout=None: 0, stderr=None))
        monkeypatch.setattr(P.sys, "platform", "linux")
        exe = tmp_path / "mpv"
        exe.write_bytes(b"")
        opened = P.open_files(P.Player("mpv", str(exe), str(exe)), clips)
        assert len(opened) == P.MAX_FILES_PER_LAUNCH

    @pytest.mark.parametrize("command, exe", [
        ('"C:\\Program Files\\MPC-HC\\mpc-hc64.exe" "%1"', "C:\\Program Files\\MPC-HC\\mpc-hc64.exe"),
        ("C:\\Tools\\mpv.exe %1", "C:\\Tools\\mpv.exe"),
        ("rundll32.dll something", None),
        ("", None),
    ])
    def test_registry_commands_are_parsed(self, command, exe):
        from frameguard import players as P
        assert P._exe_from_command(command) == exe

    def test_windows_discovery_with_a_fake_registry(self, monkeypatch, tmp_path):
        """Exercises the registry walk on every CI runner."""
        from frameguard import players as P
        vlc = tmp_path / "vlc.exe"
        vlc.write_bytes(b"MZ")
        mpc = tmp_path / "mpc-hc64.exe"
        mpc.write_bytes(b"MZ")
        fake = types.SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, HKEY_CLASSES_ROOT=3)
        monkeypatch.setitem(sys.modules, "winreg", fake)
        values = {
            (1, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vlc.exe", ""): str(vlc),
            (3, r"MPC.mp4\shell\open\command", ""): f'"{mpc}" "%1"',
        }
        monkeypatch.setattr(P, "_win_reg_value",
                            lambda hive, sub, name="": values.get((hive, sub, name)))
        monkeypatch.setattr(P, "_win_reg_subkeys_and_values",
                            lambda hive, sub: ["MPC.mp4", "AppX123"] if sub.endswith("OpenWithProgids") else [])
        monkeypatch.setattr(P.shutil, "which", lambda name: None)
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432"):
            monkeypatch.setenv(var, str(tmp_path / "empty"))
        players = P._discover_windows(str(tmp_path / "clip.mp4"))
        names = sorted(p.name for p in players)
        assert names == ["VLC media player", "mpc-hc64"]


class TestOpenTargets:
    def _results(self, window, tmp_path, n=3):
        from frameguard import gui
        results = []
        for i in range(n):
            clip = tmp_path / f"c{i}.mov"
            clip.write_bytes(b"0")
            r = QAResult(str(clip))
            r.add_pass("Container Format", "ok")
            r.finalize()
            results.append(r)
        window.results_panel.set_results(results, append=False)
        return gui, results

    def test_checked_rows_win_over_the_highlighted_row(self, window, qapp, tmp_path):
        from PySide6.QtCore import Qt
        _gui, results = self._results(window, tmp_path)
        rp = window.results_panel
        rp.table.item(0, 0).setCheckState(Qt.Checked)
        rp.table.item(2, 0).setCheckState(Qt.Checked)
        rp.table.setCurrentCell(1, 2)
        picked = {r.filepath for r in rp._results_to_open()}
        assert picked == {rp._results[0].filepath, rp._results[2].filepath}
        assert [r.filepath for r in rp._results_to_open(clicked_row=1)] == [rp._results[1].filepath]

    def test_open_uses_the_remembered_player(self, window, qapp, tmp_path, monkeypatch):
        from frameguard import players as P
        _gui, results = self._results(window, tmp_path, n=1)
        rp = window.results_panel
        app = tmp_path / "IINA.app"
        app.mkdir()
        chosen = P.Player("IINA", str(app), str(app))
        monkeypatch.setattr(P, "discover_players", lambda *a, **k: [chosen])
        launched = []
        monkeypatch.setattr(P, "open_files",
                            lambda player, paths, on_error=None: launched.append((player, paths)) or paths)
        assert rp._open_targets(chosen, rp._results[:1], remember=True)
        rp.table.setCurrentCell(0, 2)
        assert rp._play_selected()
        assert launched[-1][0].key == chosen.key

    def test_launch_failure_from_a_thread_reaches_the_gui(self, window, qapp, monkeypatch):
        shown = []
        from frameguard import gui
        monkeypatch.setattr(gui.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: shown.append(a[2])))
        import threading
        t = threading.Thread(target=lambda: window.results_panel.player_launch_failed.emit("boom"))
        t.start(); t.join()
        _settle(qapp)
        assert shown == ["boom"]


# ---------------------------------------------------------------------------
# Quitting
# ---------------------------------------------------------------------------

class TestQuit:
    def test_quit_event_forces_a_real_quit(self, window, qapp, monkeypatch):
        from PySide6.QtCore import QEvent
        from frameguard import gui
        filt = gui._FileOpenFilter.__new__(gui._FileOpenFilter)
        gui.QObject.__init__(filt)
        filt._window = window
        filt._pending_paths = []
        armed = []
        monkeypatch.setattr(gui, "_arm_exit_watchdog", lambda *a: armed.append(True))
        assert filt.eventFilter(qapp, QEvent(QEvent.Quit)) is False
        assert window._force_quit is True and armed

    def test_forced_close_while_watching_stops_services(self, window, monkeypatch):
        from frameguard import gui
        monkeypatch.setattr(gui.QTimer, "singleShot", staticmethod(lambda *a: None))
        monitor = mock.Mock(is_active=True)
        monkeypatch.setattr(window, "_watch_monitor", monitor)
        window._force_quit = True
        from PySide6.QtGui import QCloseEvent
        event = QCloseEvent()
        window.closeEvent(event)
        assert event.isAccepted()
        monitor.stop.assert_called_once()
        assert window._services_stopped

    def test_watchdog_never_arms_inside_tests(self, monkeypatch):
        from frameguard import gui
        started = []
        monkeypatch.setattr("threading.Timer", lambda *a, **k: started.append(a))
        gui._arm_exit_watchdog(0.01)
        assert gui._EXIT_WATCHDOG_ENABLED is False and started == []

    def test_entry_point_hard_exits_after_the_event_loop(self):
        text = (SRC / "main.py").read_text(encoding="utf-8")
        assert "_hard_exit(exit_code)" in text

    def test_hard_exit_flushes_settings_before_exiting(self, monkeypatch):
        from frameguard import gui
        calls = []
        monkeypatch.setattr(gui, "_sync_settings", lambda: calls.append("sync"))
        monkeypatch.setattr(gui.os, "_exit", lambda code: calls.append(("exit", code)))
        gui._hard_exit(3)
        assert calls == ["sync", ("exit", 3)]


# ---------------------------------------------------------------------------
# Deep scan: regions open at the end of the input
# ---------------------------------------------------------------------------

class TestOpenRegions:
    def test_freeze_running_to_the_end_is_measured(self):
        text = "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 8.0"
        closed = core._close_open_regions(text, 14.0)
        assert core._pair_detect_events(closed, "freeze") == [(8.0, 6.0)]

    def test_closed_regions_are_left_alone(self):
        text = ("lavfi.freezedetect.freeze_start: 1\nlavfi.freezedetect.freeze_duration: 2\n"
                "lavfi.freezedetect.freeze_end: 3")
        assert core._close_open_regions(text, 10.0) == text

    def test_static_file_scanned_in_segments_is_one_long_freeze(self):
        segments = [(0.0, 11.0), (9.0, 21.0), (19.0, 31.0), (29.0, 40.0)]
        parts = [core._close_open_regions(core._offset_event_times(
            "lavfi.freezedetect.freeze_start: 0", start), end) for start, end in segments]
        events = core._merge_event_intervals(core._pair_detect_events("\n".join(parts), "freeze"))
        assert len(events) == 1
        assert events[0][0] == pytest.approx(0.0)
        assert events[0][1] == pytest.approx(40.0)

    @pytest.mark.parametrize("value, text", [(0.0068, "7 KB"), (40.31, "40.3 MB")])
    def test_sizes_never_read_zero(self, value, text):
        assert core._format_size(value) == text

    @pytest.mark.parametrize("value, text", [(0.05, "0.05"), (0.001, "<0.01"), (28.13, "28.1")])
    def test_bitrates_never_read_zero(self, value, text):
        assert core._format_mbps(value) == text

    def test_near_total_deficit_is_not_rounded_to_100(self):
        assert core._format_pct(99.8) == ">99%"
        assert core._format_pct(100.0) == "100%"


# ---------------------------------------------------------------------------
# PDF layout
# ---------------------------------------------------------------------------

class TestPdfLayout:
    def _result(self, name, rows=14, status="fail"):
        r = QAResult(name)
        for i in range(rows):
            (r.add_fail if status == "fail" and i == 0 else r.add_pass)(f"Check {i}", "detail " * (1 + i % 3))
        r.finalize()
        return r

    def test_unicode_names_use_a_unicode_font_when_available(self):
        from frameguard.report import HandoffReport, _font_candidates
        pdf = HandoffReport()
        main, _ = _font_candidates()
        if all(os.path.isfile(p) for p in main.values()):
            assert pdf.ff == "FGSans"
        pdf.add_page()
        pdf.set_font(pdf.ff, "", 10)
        text = pdf._text("Cafe\u0301_\u65e5\u672c.mov")
        assert text.startswith("Caf\u00e9")  # composed
        if pdf.ff == "FGSans":
            assert "\u65e5" in text

    def test_first_break_obeys_orphan_and_widow_limits(self):
        from frameguard.report import _first_break, MIN_ORPHAN_ROWS, MIN_WIDOW_ROWS
        rows = [7.0] * 10
        assert _first_break(200, 20, rows) is None                  # fits
        assert _first_break(20 + 7 * 2, 20, rows) == 0              # 2 rows: new page
        assert _first_break(20 + 7 * 9, 20, rows) == len(rows) - MIN_WIDOW_ROWS
        cut = _first_break(20 + 7 * 5, 20, rows)
        assert cut >= MIN_ORPHAN_ROWS and len(rows) - cut >= MIN_WIDOW_ROWS

    def test_report_has_document_properties_and_page_x_of_y(self, tmp_path):
        from frameguard.report import generate_report
        out = tmp_path / "r.pdf"
        generate_report([self._result(f"clip_{i}.mov") for i in range(8)], str(out),
                        preset_label="4K", editor="Jace")
        data = out.read_bytes()
        assert b"/Title" in data and b"/Author" in data and b"/Creator" in data
        src = (SRC / "frameguard/report.py").read_text(encoding="utf-8")
        assert 'f"Page {self.page_no()} of {{nb}}"' in src

    def test_status_pills_meet_text_contrast(self):
        from frameguard.report import STATUS_COLORS
        def lum(rgb):
            def ch(v):
                v /= 255
                return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
            r, g, b = (ch(v) for v in rgb)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        for status, rgb in STATUS_COLORS.items():
            ratio = (1.05) / (lum(rgb) + 0.05)
            assert ratio >= 4.5, (status, round(ratio, 2))

    def test_long_name_wraps_inside_the_page(self, tmp_path):
        from frameguard.report import HandoffReport, USABLE_W
        pdf = HandoffReport()
        pdf.add_page()
        pdf.set_font(pdf.ff, "B", 11)
        name = "Extremely_Long_Production_Deliverable_Name_" * 3 + ".mp4"
        assert pdf.text_height(USABLE_W - 20, name, 5.2) > 5.2


class TestPdfFallback:
    def test_damaged_system_font_falls_back_to_builtin(self, tmp_path, monkeypatch):
        from frameguard import report as R
        bad = tmp_path / "bad.ttf"
        bad.write_bytes(b"not a font")
        monkeypatch.setattr(R, "_font_candidates",
                            lambda: ({"": str(bad), "B": str(bad), "I": str(bad)}, []))
        out = tmp_path / "r.pdf"
        r = QAResult("Caf\u00e9_\u65e5\u672c.mov"); r.add_pass("Container Format", "ok"); r.finalize()
        R.generate_report([r], str(out))
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_unicode_render_failure_retries_with_builtin_font(self, tmp_path, monkeypatch):
        from frameguard import report as R
        calls = []
        real = R._generate_report
        def flaky(*args):
            calls.append(args[-1])
            if args[-1]:
                raise KeyError("glyph table")
            return real(*args)
        monkeypatch.setattr(R, "_generate_report", flaky)
        out = tmp_path / "r.pdf"
        r = QAResult("a.mov"); r.add_pass("Container Format", "ok"); r.finalize()
        R.generate_report([r], str(out))
        assert calls == [True, False]
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_selftest_renders_a_unicode_pdf(self):
        text = (SRC / "main.py").read_text(encoding="utf-8")
        assert "pdf report     : " in text and "_generate_report(" in text

    def test_spec_bundles_players_and_font_tooling(self):
        spec = (SRC / "frameguard.spec").read_text(encoding="utf-8")
        assert "'frameguard.players'" in spec
        assert "'fontTools.subset'" in spec and "collect_submodules('fontTools.ttLib.tables')" in spec


class TestThemeSwitchKeepsLabelsLegible:
    def test_file_count_follows_the_new_theme(self, window, qapp, tmp_path):
        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        sp = window.scan_panel
        window._apply_theme("Blue Hour")
        sp._style_file_count("primary")
        window._apply_theme("Clean Light")
        _settle(qapp)
        from frameguard.theme import get_theme
        assert get_theme("Clean Light").text_primary.lower() in sp.file_count_label.styleSheet().lower()

    def test_disabled_choices_still_show_the_selection(self):
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Blue Hour"))
        assert "QRadioButton::indicator:checked:disabled" in css
        assert "QCheckBox::indicator:checked:disabled" in css

    def test_watch_log_uses_words_not_colour_emoji(self):
        text = (SRC / "frameguard/gui.py").read_text(encoding="utf-8")
        for glyph in ("\\u2705", "\\u274c", "\\u26a0\\ufe0f", "\u27f3"):
            assert glyph not in text, glyph


class TestReviewRoundTwo:
    def test_subsetting_failure_retries_with_builtin_font(self, tmp_path, monkeypatch):
        from frameguard import report as R
        import fpdf
        real_output = fpdf.FPDF.output
        def flaky(self, *a, **k):
            if getattr(self, "ff", "") == "FGSans":
                raise KeyError("subset")
            return real_output(self, *a, **k)
        monkeypatch.setattr(fpdf.FPDF, "output", flaky)
        out = tmp_path / "r.pdf"
        r = QAResult("a.mov"); r.add_pass("Container Format", "ok"); r.finalize()
        R.generate_report([r], str(out))
        assert out.read_bytes()[:5] == b"%PDF-"
        assert not (tmp_path / "r.pdf.part").exists()

    def test_unwritable_destination_is_reported_not_retried(self, tmp_path, monkeypatch):
        from frameguard import report as R
        calls = []
        real = R._generate_report
        monkeypatch.setattr(R, "_generate_report", lambda *a: calls.append(a[-1]) or real(*a))
        blocker = tmp_path / "file"
        blocker.write_text("x")
        r = QAResult("a.mov"); r.add_pass("Container Format", "ok"); r.finalize()
        with pytest.raises(RuntimeError):
            R.generate_report([r], str(blocker / "sub" / "r.pdf"))
        assert calls == [True]

    def test_unicode_preset_label_and_editor_survive(self):
        from frameguard.report import HandoffReport
        pdf = HandoffReport(preset_label="\u65e5\u672c\u8a9e preset", editor="\u0141ukasz")
        assert pdf.preset_label == "\u65e5\u672c\u8a9e preset"
        assert pdf.editor == "\u0141ukasz"

    def test_fallback_fonts_only_when_needed(self):
        from frameguard.report import _needs_fallback_fonts
        latin = QAResult("Caf\u00e9_\u041f\u0440\u0438\u0432\u0435\u0442.mov")
        cjk = QAResult("\u65e5\u672c.mov")
        assert _needs_fallback_fonts([latin]) is False
        assert _needs_fallback_fonts([cjk]) is True
        assert _needs_fallback_fonts([latin], "\u65e5 preset") is True

    def test_return_opens_only_the_highlighted_row(self, window, qapp, tmp_path, monkeypatch):
        from PySide6.QtCore import Qt
        from frameguard import players as P
        results = []
        for i in range(3):
            clip = tmp_path / f"c{i}.mov"; clip.write_bytes(b"0")
            r = QAResult(str(clip)); r.add_pass("Container Format", "ok"); r.finalize()
            results.append(r)
        rp = window.results_panel
        rp.set_results(results, append=False)
        rp.table.item(0, 0).setCheckState(Qt.Checked)
        rp.table.item(2, 0).setCheckState(Qt.Checked)
        rp.table.setCurrentCell(1, 2)
        opened = []
        monkeypatch.setattr(P, "discover_players", lambda *a, **k: [])
        monkeypatch.setattr(P, "open_files", lambda player, paths, on_error=None: opened.append(paths) or paths)
        rp._play_current()
        assert opened == [[rp._results[1].filepath]]

    def test_windows_per_user_open_with_list_reads_value_data(self, monkeypatch, tmp_path):
        from frameguard import players as P
        exe = tmp_path / "PotPlayerMini64.exe"; exe.write_bytes(b"MZ")
        fake = types.SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, HKEY_CLASSES_ROOT=3)
        monkeypatch.setitem(sys.modules, "winreg", fake)
        monkeypatch.setattr(P, "_win_reg_value", lambda *a, **k: None)
        monkeypatch.setattr(P, "_win_reg_subkeys_and_values", lambda hive, sub: [])
        monkeypatch.setattr(P, "_win_reg_value_data",
                            lambda hive, sub: ["PotPlayerMini64.exe"] if "FileExts" in sub else [])
        monkeypatch.setattr(P, "_win_app_path",
                            lambda name: str(exe) if name == "PotPlayerMini64.exe" else None)
        monkeypatch.setattr(P.shutil, "which", lambda name: None)
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432"):
            monkeypatch.setenv(var, str(tmp_path / "empty"))
        players = P._discover_windows(str(tmp_path / "clip.mp4"))
        assert [p.path for p in players] == [str(exe)]

    def test_quit_waits_for_a_running_export(self, window, monkeypatch):
        from frameguard import gui
        waited = []
        monkeypatch.setattr(gui.QThreadPool.globalInstance(), "waitForDone",
                            lambda ms: waited.append(ms) or True, raising=False)
        window._export_runners = [object()]
        window._services_stopped = False
        window._shutdown_services()
        assert waited and waited[0] <= 10000



class TestReviewRoundThree:
    def test_em_dash_alone_does_not_pull_in_fallback_fonts(self):
        from frameguard.report import _needs_fallback_fonts, _main_font_codepoints
        if not _main_font_codepoints():
            pytest.skip("no system report font on this machine")
        r = QAResult("clip.mov")
        r.add_warn("Bitrate", "18.0 Mbps \u2014 10% below target")
        r.finalize()
        assert _needs_fallback_fonts([r]) is False

    def test_shutdown_waits_never_outlive_the_watchdog(self, monkeypatch):
        import time
        from frameguard import gui
        monkeypatch.setattr(gui, "_exit_watchdog_deadline", time.monotonic() + 3.0)
        assert gui._exit_budget_ms(8000) <= 2000
        monkeypatch.setattr(gui, "_exit_watchdog_deadline", None)
        assert gui._exit_budget_ms(8000) == 8000
