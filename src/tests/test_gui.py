"""GUI behaviour tests: drag and drop, results accumulation, PDF export wiring.

All run headless via the offscreen Qt platform plugin.
"""

import os

import pytest
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QThreadPool, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent

from frameguard.presets import export_preset

from conftest import make_result

pytestmark = pytest.mark.usefixtures("qapp")


def _url_mime(paths):
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(p) for p in paths])
    return m


def _drop(widget, paths):
    # QDropEvent does not take ownership of the QMimeData; if it is collected
    # before the handler runs, PySide hands back a bare QObject (or crashes).
    mime = _url_mime(paths)
    e = QDropEvent(QPoint(10, 10), Qt.CopyAction, mime,
                   Qt.LeftButton, Qt.NoModifier)
    widget.dropEvent(e)
    assert mime is not None  # keep alive until after the call
    return e


def _drag_enter(widget, mime):
    e = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime,
                        Qt.LeftButton, Qt.NoModifier)
    widget.dragEnterEvent(e)
    return e


@pytest.fixture
def preset_file(tmp_path):
    return export_preset(
        "Team 1080p24",
        {"target_width": 1920, "target_height": 1080, "target_fps": 24.0,
         "min_bitrate_mbps": 6, "fps_tolerance": 0.2, "use_hwaccel": False},
        str(tmp_path / "Team 1080p24.fg"),
    )


class TestDropZone:
    def test_drop_is_accepted(self, preset_file):
        """Without acceptProposedAction, Explorer shows a rejected cursor."""
        from frameguard.gui import DropZone

        e = _drop(DropZone(), [preset_file])
        assert e.isAccepted()

    def test_preset_and_video_are_routed_separately(self, tmp_path, preset_file):
        from frameguard.gui import DropZone

        video = tmp_path / "clip.mov"
        video.write_bytes(b"\x00" * 32)

        dz = DropZone()
        presets, videos = [], []
        dz.preset_dropped.connect(presets.append)
        dz.files_dropped.connect(videos.append)

        _drop(dz, [preset_file, str(video)])
        assert presets == [preset_file]
        assert videos == [[str(video)]]

    def test_drag_enter_accepts_files(self, preset_file):
        from frameguard.gui import DropZone

        mime = _url_mime([preset_file])
        assert _drag_enter(DropZone(), mime).isAccepted()

    def test_drag_enter_rejects_non_files(self):
        from frameguard.gui import DropZone

        m = QMimeData()
        m.setText("just some text")
        assert not _drag_enter(DropZone(), m).isAccepted()

    def test_empty_drop_is_ignored(self):
        from frameguard.gui import DropZone

        # QDropEvent does not take ownership of the QMimeData, so it must be
        # kept alive for the duration of the call.
        mime = QMimeData()
        e = QDropEvent(QPoint(0, 0), Qt.CopyAction, mime,
                       Qt.LeftButton, Qt.NoModifier)
        DropZone().dropEvent(e)
        assert not e.isAccepted()

    def test_queue_list_does_not_swallow_drops_or_clicks(self):
        """Once files are queued the list covers the whole drop zone.

        Asserts the behaviour, not the mechanism: the list previously set
        WA_TransparentForMouseEvents on its viewport, which preserved
        click-to-browse but also discarded wheel events, so the queue could
        only be scrolled by dragging the scrollbar.
        """
        from frameguard.gui import DropZone

        dz = DropZone()
        dz.show_file_queue(["a.mov", "b.mov"])

        # Refusing drops lets Qt propagate them up to the DropZone.
        assert not dz._file_list.acceptDrops()

        # Clicking anywhere in the list still opens the browse dialog.
        browse = []
        dz.browse_requested.connect(lambda: browse.append(True))
        press = QMouseEvent(QEvent.MouseButtonPress, QPointF(10, 10), QPointF(10, 10),
                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        dz._file_list.mousePressEvent(press)
        assert browse, "clicking the queue list no longer opens Browse"

        # ...and the wheel still reaches the list so it can be scrolled.
        assert not dz._file_list.viewport().testAttribute(
            Qt.WA_TransparentForMouseEvents), \
            "a mouse-transparent viewport discards wheel events"

    def test_queue_list_is_not_height_capped(self):
        """A 120px cap showed 3 of 6 queued files with empty space below."""
        from frameguard.gui import DropZone

        dz = DropZone()
        dz.show_file_queue([f"clip_{i}.mov" for i in range(6)])
        assert dz._file_list.count() == 6
        # QWidget's default "no maximum" sentinel.
        assert dz._file_list.maximumHeight() == 16777215

    def test_unrelated_json_is_not_treated_as_a_preset(self, tmp_path):
        from frameguard.gui import DropZone

        sidecar = tmp_path / "metadata.json"
        sidecar.write_text('{"camera": "Ronin 4D"}', encoding="utf-8")

        dz = DropZone()
        presets, videos = [], []
        dz.preset_dropped.connect(presets.append)
        dz.files_dropped.connect(videos.append)

        _drop(dz, [str(sidecar)])
        assert presets == []
        assert videos == [[str(sidecar)]]


class TestWindowLevelDrop:
    def test_window_accepts_drops(self):
        from frameguard.gui import MainWindow

        assert MainWindow().acceptDrops()

    def test_dropped_preset_reaches_the_engine_config(self, preset_file):
        """End-to-end: drop a .fg, and the next scan must use its values."""
        from frameguard.gui import MainWindow
        from frameguard.worker import ScanWorker

        w = MainWindow()
        w.open_paths([preset_file])

        cfg = w.settings_panel.get_config_overrides()
        assert cfg["target_width"] == 1920
        assert cfg["target_height"] == 1080
        assert cfg["target_fps"] == 24.0
        assert cfg["min_bitrate_mbps"] == 6
        assert cfg["fps_tolerance"] == 0.2
        assert cfg["use_hwaccel"] is False

        worker = ScanWorker()
        worker.configure(files=["x.mov"], preset=w.scan_panel.engine_preset(),
                         cfg_overrides=cfg)
        assert worker._cfg["target_width"] == 1920
        assert worker._cfg["target_fps"] == 24.0
        assert worker._cfg["min_bitrate_mbps"] == 6

    def test_imported_preset_selects_but_does_not_confuse_the_engine(self, preset_file):
        from frameguard.gui import MainWindow

        w = MainWindow()
        w.open_paths([preset_file])
        assert w.scan_panel.preset_combo.currentData() == "imported:Team 1080p24"
        # The engine only understands built-in preset keys; imported presets
        # travel via the Settings overrides instead.
        assert w.scan_panel.engine_preset() is None

    def test_invalid_preset_reports_instead_of_failing_silently(self, tmp_path):
        from frameguard.gui import MainWindow

        bad = tmp_path / "broken.fg"
        bad.write_text("not json", encoding="utf-8")

        w = MainWindow()
        w.open_paths([str(bad)])
        assert "not a valid" in w.scan_panel.progress_label.text().lower()


class TestWatchQueueOwnership:
    @staticmethod
    def _install_successful_start(monkeypatch, gui, window, starts):
        def record_start(_thread):
            starts.append(list(window._worker._files))

        monkeypatch.setattr(gui.QThread, "start", record_start)

    def test_low_disk_restores_then_retries_batch_exactly_once(
            self, tmp_path, monkeypatch):
        import shutil
        from frameguard import gui

        window = gui.MainWindow()
        retries = []
        starts = []
        free_bytes = {"value": 64 * 1024 * 1024}
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda _path: shutil._ntuple_diskusage(
                1024 * 1024 * 1024, 0, free_bytes["value"]),
        )
        monkeypatch.setattr(
            gui.QTimer, "singleShot",
            staticmethod(lambda delay, callback: retries.append((delay, callback))),
        )
        self._install_successful_start(monkeypatch, gui, window, starts)
        first = str(tmp_path / "first.mov")
        second = str(tmp_path / "second.mov")
        third = str(tmp_path / "third.mov")

        try:
            window._watch_pending_queue = [first, second, first]
            window._process_watch_queue()

            assert window._watch_pending_queue == [first, second]
            assert window._watch_dispatch_pending is None
            assert not window._watch_batch_in_progress
            assert starts == []
            assert [delay for delay, _callback in retries] == [1500]

            window._on_watch_files_detected([second, third])
            assert window._watch_pending_queue == [first, second, third]
            assert [delay for delay, _callback in retries] == [1500]

            free_bytes["value"] = 512 * 1024 * 1024
            _delay, retry = retries.pop()
            retry()

            assert starts == [[first, second, third]]
            assert window._watch_pending_queue == []
            assert window._watch_dispatch_pending is None
            assert window._watch_batch_in_progress
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_pre_signal_ui_exception_restores_batch_for_retry(
            self, tmp_path, monkeypatch):
        import shutil
        from frameguard import gui

        window = gui.MainWindow()
        retries = []
        starts = []
        original_set_files = window.scan_panel._set_files
        attempts = {"count": 0}

        def set_files_once_then_succeed(files):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("injected queue UI failure")
            return original_set_files(files)

        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda _path: shutil._ntuple_diskusage(
                1024 * 1024 * 1024, 0, 512 * 1024 * 1024),
        )
        monkeypatch.setattr(window.scan_panel, "_set_files", set_files_once_then_succeed)
        monkeypatch.setattr(
            gui.QTimer, "singleShot",
            staticmethod(lambda delay, callback: retries.append((delay, callback))),
        )
        self._install_successful_start(monkeypatch, gui, window, starts)
        filepath = str(tmp_path / "ui-setup.mov")

        try:
            window._watch_pending_queue = [filepath]
            window._process_watch_queue()

            assert window._watch_pending_queue == [filepath]
            assert not window._watch_batch_in_progress
            assert starts == []
            assert [delay for delay, _callback in retries] == [1500]

            retries.pop()[1]()

            assert attempts["count"] == 2
            assert starts == [[filepath]]
            assert window._watch_pending_queue == []
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_worker_setup_exception_restores_batch_for_retry(
            self, tmp_path, monkeypatch):
        import shutil
        from frameguard import gui

        window = gui.MainWindow()
        retries = []
        starts = []
        original_configure = gui.ScanWorker.configure
        attempts = {"count": 0}
        filepath = str(tmp_path / "setup.mov")
        arrived_during_error = str(tmp_path / "nested.mov")

        def configure_once_then_succeed(worker, *args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("injected setup failure")
            return original_configure(worker, *args, **kwargs)

        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda _path: shutil._ntuple_diskusage(
                1024 * 1024 * 1024, 0, 512 * 1024 * 1024),
        )
        monkeypatch.setattr(gui.ScanWorker, "configure", configure_once_then_succeed)
        monkeypatch.setattr(
            gui.QMessageBox, "critical",
            lambda *_args: window._on_watch_files_detected(
                [filepath, arrived_during_error, filepath]),
        )
        monkeypatch.setattr(
            gui.QTimer, "singleShot",
            staticmethod(lambda delay, callback: retries.append((delay, callback))),
        )
        self._install_successful_start(monkeypatch, gui, window, starts)

        try:
            window._watch_pending_queue = [filepath]
            window._process_watch_queue()

            assert window._watch_pending_queue == [filepath, arrived_during_error]
            assert not window._watch_batch_in_progress
            assert starts == []
            assert [delay for delay, _callback in retries] == [1500]

            retries.pop()[1]()

            assert attempts["count"] == 2
            assert starts == [[filepath, arrived_during_error]]
            assert window._watch_pending_queue == []
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_reentrant_rejection_keeps_batch_for_worker_completion(
            self, tmp_path, monkeypatch):
        import shutil
        from frameguard import gui

        class RunningThread:
            @staticmethod
            def isRunning():
                return True

        window = gui.MainWindow()
        retries = []
        starts = []
        monkeypatch.setattr(
            shutil, "disk_usage",
            lambda _path: shutil._ntuple_diskusage(
                1024 * 1024 * 1024, 0, 512 * 1024 * 1024),
        )
        monkeypatch.setattr(
            gui.QTimer, "singleShot",
            staticmethod(lambda delay, callback: retries.append((delay, callback))),
        )
        self._install_successful_start(monkeypatch, gui, window, starts)
        filepath = str(tmp_path / "busy.mov")

        try:
            window._worker_thread = RunningThread()
            window._watch_pending_queue = [filepath]
            window._process_watch_queue()

            assert window._watch_pending_queue == [filepath]
            assert not window._watch_batch_in_progress
            assert retries == []
            assert starts == []

            window._worker_thread = None
            window._process_watch_queue()

            assert starts == [[filepath]]
            assert window._watch_pending_queue == []
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()


class TestResultsPanel:
    def test_results_accumulate_across_scans(self):
        """Previously each new batch replaced the whole history."""
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("a.mov")], append=True)
        rp.set_results([make_result("b.mov")], append=True)
        rp.set_results([make_result("c.mov")], append=True)

        assert len(rp._results) == 3
        assert rp.table.rowCount() == 3

    def test_append_false_replaces(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("a.mov"), make_result("b.mov")], append=True)
        rp.set_results([make_result("c.mov")], append=False)
        assert len(rp._results) == 1

    def test_refresh_with_empty_batch_keeps_history(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("a.mov")], append=True)
        rp.set_results([], append=True)
        assert rp.table.rowCount() == 1

    def test_export_button_tracks_result_count(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        assert not rp.export_btn.isEnabled()
        rp.set_results([make_result()], append=True)
        assert rp.export_btn.isEnabled()
        rp._clear_all_results()
        assert not rp.export_btn.isEnabled()

    def test_badges_use_finalized_status(self):
        from PySide6.QtWidgets import QLabel
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("a.mov", "pass"),
                        make_result("b.mov", "warn"),
                        make_result("c.mov", "fail")], append=True)

        def badge(name):
            return rp.findChild(QLabel, f"badge_count_{name}").text()

        assert badge("total") == "3"
        assert badge("passed") == "1"
        assert badge("warnings") == "1"
        assert badge("mismatched") == "1"

    def test_selection_indexes_line_up_with_rows(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result(f"{c}.mov") for c in "abc"], append=True)
        target = rp.table.item(1, 2).text()
        rp.table.item(1, 0).setCheckState(Qt.Checked)

        selected = rp.get_selected_results()
        assert len(selected) == 1
        assert selected[0].filename == target

    def test_no_selection_exports_everything(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result(f"{c}.mov") for c in "abc"], append=True)
        assert len(rp.get_selected_results()) == 3

    def test_retention_cap_is_enforced(self):
        from frameguard.gui import MAX_RETAINED_RESULTS, ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result(f"c{i}.mov") for i in range(MAX_RETAINED_RESULTS + 40)],
                       append=True)
        assert len(rp._results) == MAX_RETAINED_RESULTS
        assert rp.table.rowCount() == MAX_RETAINED_RESULTS


class TestPdfExportRunner:
    def test_completion_signal_survives_autodelete(self, tmp_path):
        """QThreadPool deletes the runnable as soon as run() returns; the
        queued finished signal must still be delivered."""
        import gc
        from frameguard.gui import _PdfExportRunner

        out = str(tmp_path / "out.pdf")
        state = {}
        runner = _PdfExportRunner([make_result()], out, "4K30")
        keep = [runner]  # mirrors MainWindow._export_runners
        runner.signals.finished.connect(lambda p: state.update(done=p))
        runner.signals.error.connect(lambda e: state.update(err=e))

        QThreadPool.globalInstance().start(runner)
        del runner
        gc.collect()
        assert QThreadPool.globalInstance().waitForDone(30000)
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        assert state.get("err") is None
        assert state.get("done") == out
        assert os.path.exists(out)
        assert keep

    def test_failure_is_reported_not_swallowed(self, tmp_path):
        from PySide6.QtWidgets import QApplication
        from frameguard.gui import _PdfExportRunner

        state = {}
        runner = _PdfExportRunner([], str(tmp_path / "x.pdf"), "P")
        runner.signals.finished.connect(lambda p: state.update(done=p))
        runner.signals.error.connect(lambda e: state.update(err=e))

        QThreadPool.globalInstance().start(runner)
        assert QThreadPool.globalInstance().waitForDone(30000)
        QApplication.processEvents()

        assert state.get("done") is None
        assert state.get("err")


class TestThemePersistence:
    def test_settings_panel_stays_in_sync(self):
        from frameguard.gui import MainWindow
        from frameguard.theme import THEMES

        w = MainWindow()
        for name in list(THEMES)[:5]:
            w._apply_theme(name)
            assert w.settings_panel._current_theme_name == name

    def test_default_theme_is_still_selectable_after_restore(self):
        """Restoring a non-default theme used to leave the panel believing it
        was still on Meta Dark, making that button a permanent no-op."""
        from frameguard.gui import MainWindow

        w = MainWindow()
        w._apply_theme("Nord Frost")
        assert w.settings_panel._current_theme_name == "Nord Frost"

        emitted = []
        w.settings_panel.theme_changed.connect(emitted.append)
        w.settings_panel._select_theme("Meta Dark")
        assert emitted == ["Meta Dark"]

    def test_windows_combo_popup_uses_theme_selection_contrast(self, monkeypatch):
        from PySide6.QtWidgets import QComboBox
        from frameguard import gui
        from frameguard.theme import get_theme

        monkeypatch.setattr(gui.sys, "platform", "win32")
        combo = QComboBox()
        theme = get_theme("Nord Frost")
        gui._style_combobox_popup(combo, theme)

        popup_qss = combo.view().styleSheet().lower()
        assert theme.selection.lower() in popup_qss
        assert theme.selection_foreground.lower() in popup_qss
        assert "#ffffff" not in popup_qss
        assert combo.property("fgInlineStyleAllowed") is True
        assert combo.view().property("fgInlineStyleAllowed") is True


class TestResetToDefaults:
    def test_reset_matches_engine_defaults(self):
        """The reset values used to come from fallback literals that disagreed
        with DEFAULT_CONFIG (fps 24 vs 30, bitrate 8 vs 20, ...)."""
        from frameguard.core import DEFAULT_CONFIG
        from frameguard.gui import SettingsPanel

        panel = SettingsPanel()
        panel._reset_to_defaults()
        for key, value in panel.get_config_overrides().items():
            assert DEFAULT_CONFIG[key] == value, (
                f"reset produced {key}={value!r}, engine default is "
                f"{DEFAULT_CONFIG[key]!r}"
            )


class TestFirstLaunchContract:
    def test_main_does_not_shadow_startup_theme_resolver(self):
        """A late local import made the packaged startup call an unbound local."""
        from frameguard.gui import main

        assert "get_theme" not in main.__code__.co_varnames

    def test_os_theme_mapping_is_not_persisted(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import (
            _SETTINGS_APP, _SETTINGS_ORG, _THEME_KEY, resolve_startup_theme,
        )

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.clear()
        assert resolve_startup_theme(settings, prefers_dark=False) == ("Clean Light", False)
        assert not settings.contains(_THEME_KEY)
        assert resolve_startup_theme(settings, prefers_dark=True) == ("Meta Dark", False)
        assert not settings.contains(_THEME_KEY)

    def test_valid_legacy_theme_is_preserved(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import _SETTINGS_APP, _SETTINGS_ORG, resolve_startup_theme

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue("theme", "Nord Frost")
        assert resolve_startup_theme(settings, prefers_dark=False) == ("Nord Frost", True)

    def test_invalid_saved_theme_heals_to_the_os_appearance(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import (
            _LEGACY_THEME_KEY, _SETTINGS_APP, _SETTINGS_ORG, _THEME_KEY,
            resolve_startup_theme,
        )

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(_THEME_KEY, "Removed Theme")
        settings.setValue(_LEGACY_THEME_KEY, "Also Invalid")
        assert resolve_startup_theme(settings, prefers_dark=False) == ("Clean Light", False)
        assert not settings.contains(_THEME_KEY)
        assert not settings.contains(_LEGACY_THEME_KEY)

    def test_explicit_selection_persists_exact_theme(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import MainWindow, _SETTINGS_APP, _SETTINGS_ORG, _THEME_KEY

        window = MainWindow()
        try:
            window._apply_theme("Glacier", persist=True)
            assert QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(_THEME_KEY) == "Glacier"
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_constructing_window_does_not_create_documents_output(self, isolated_user_dirs):
        from frameguard.gui import MainWindow

        output = isolated_user_dirs / "Documents" / "FrameGuard"
        output.rmdir()
        window = MainWindow()
        try:
            assert not output.exists()
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_runtime_os_changes_stop_after_explicit_selection(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import MainWindow, _SETTINGS_APP, _SETTINGS_ORG, _THEME_KEY
        from frameguard.theme import get_theme

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.clear()
        window = MainWindow()
        try:
            window._on_system_color_scheme_changed(Qt.ColorScheme.Light)
            assert get_theme().name == "Clean Light"
            assert not settings.contains(_THEME_KEY)

            window._apply_theme("Glacier", persist=True)
            window._on_system_color_scheme_changed(Qt.ColorScheme.Dark)
            assert get_theme().name == "Glacier"
            assert settings.value(_THEME_KEY) == "Glacier"
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()


class TestSemanticOwnership:
    def test_every_application_button_has_an_exact_fg_role(self):
        from PySide6.QtWidgets import QPushButton
        from frameguard.gui import MainWindow

        allowed = {"primary", "secondary", "quiet", "destructive", "toolbar", "icon"}
        window = MainWindow()
        try:
            offenders = [
                button.text() or button.objectName()
                for button in window.findChildren(QPushButton)
                if button.property("fgRole") not in allowed
            ]
            assert not offenders
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()
