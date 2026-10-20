"""Regression tests for v0.8.3: menu-bar icon crash on macOS 27, sidebar
toggle hover, and the reviewed GitHub Actions pins."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]


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


class TestMenuBarIconCrash:
    def test_guard_is_a_noop_off_cocoa(self, qapp):
        from frameguard import gui
        if qapp.platformName() == "cocoa":
            pytest.skip("exercised natively by the subprocess probe")
        assert gui._install_macos_status_item_guard() is False

    def test_guard_is_installed_before_the_menu_is_attached(self):
        text = (SRC / "frameguard/gui.py").read_text(encoding="utf-8")
        guard = text.index("_install_macos_status_item_guard()\n            self._tray_icon.setContextMenu(tray_menu)")
        assert guard > 0

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS status items only")
    def test_menu_opened_by_a_scene_action_does_not_abort(self, tmp_path):
        env = dict(os.environ, QT_QPA_PLATFORM="cocoa", HOME=str(tmp_path))
        env.pop("FRAMEGUARD_TEST_MODE", None)
        probe = SRC / "tests" / "status_item_probe.py"
        completed = subprocess.run([sys.executable, str(probe)], cwd=str(SRC), env=env,
                                   capture_output=True, text=True, timeout=90)
        if completed.returncode == 3:
            pytest.skip("no menu bar on this runner: " + completed.stdout.strip()[-200:])
        assert completed.returncode == 0, completed.stdout + completed.stderr[-2000:]
        assert "SURVIVED" in completed.stdout
        assert "guard: True" in completed.stdout


class TestSidebarToggle:
    def test_toggle_is_a_square_that_fits_its_column(self, window, qapp):
        button = window._sidebar_toggle_btn
        column = window._toggle_container
        assert button.width() == button.height()
        assert column.rect().contains(button.geometry()), (button.geometry(), column.rect())
        left = button.geometry().left()
        right = column.width() - button.geometry().right() - 1
        assert left == right, (left, right)

    def test_toggle_is_not_covered_by_the_page(self, window, qapp):
        button = window._sidebar_toggle_btn
        visible = button.visibleRegion().boundingRect()
        assert visible.width() == button.width() and visible.height() == button.height()

    def test_toggle_stays_square_when_the_sidebar_is_collapsed(self, window, qapp):
        window._toggle_sidebar()
        for _ in range(30):
            qapp.processEvents()
        import time
        time.sleep(0.4)
        for _ in range(10):
            qapp.processEvents()
        button = window._sidebar_toggle_btn
        assert button.width() == button.height()
        assert window._toggle_container.rect().contains(button.geometry())


class TestFileNameElision:
    def test_results_and_checks_elide_file_names_in_the_middle(self, window):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QStyleOptionViewItem
        for table, column in ((window.results_panel.table, 2), (window.checks_panel.table, 0)):
            delegate = table.itemDelegateForColumn(column)
            assert delegate is not None
            option = QStyleOptionViewItem()
            table.setRowCount(max(1, table.rowCount()))
            delegate.initStyleOption(option, table.model().index(0, column))
            assert option.textElideMode == Qt.ElideMiddle


def test_release_manifest_is_valid_json_with_this_version():
    import json
    from frameguard import __version__
    data = json.loads((SRC / "version.json").read_text(encoding="utf-8"))
    assert data["version"] == __version__
    assert data["changelog"].startswith(f"v{__version__} - ")
