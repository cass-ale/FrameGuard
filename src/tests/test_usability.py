"""Tests for the v0.5.2 usability additions: settings persistence, row
filtering, and the result context menu.
"""

import os

import pytest

from conftest import make_result

pytestmark = pytest.mark.usefixtures("qapp")


class TestSettingsPersistence:
    def test_config_survives_a_restart(self, tmp_path, monkeypatch):
        """Only the theme used to persist; the delivery spec was lost."""
        from PySide6.QtCore import QSettings
        from frameguard.gui import SettingsPanel, _SETTINGS_APP, _SETTINGS_ORG

        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).clear()

        panel = SettingsPanel()
        panel._apply_preset_config({
            "target_width": 1280, "target_height": 720, "target_fps": 60.0,
            "min_bitrate_mbps": 4, "use_hwaccel": False, "segment_enabled": False,
        })
        saved = panel.get_config_overrides()
        panel.save_settings()

        restored_panel = SettingsPanel()
        restored_panel.restore_settings()
        restored = restored_panel.get_config_overrides()

        for key in ("target_width", "target_height", "target_fps",
                    "min_bitrate_mbps", "use_hwaccel", "segment_enabled"):
            assert restored[key] == saved[key], f"{key} was not restored"

        QSettings(_SETTINGS_ORG, _SETTINGS_APP).clear()

    def test_corrupt_saved_config_is_ignored(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import SettingsPanel, _SETTINGS_APP, _SETTINGS_ORG

        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue("scan/config", "{not json")
        panel = SettingsPanel()
        panel.restore_settings()  # must not raise
        assert panel.get_config_overrides()["target_width"] > 0
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).clear()

    def test_stale_keys_in_saved_config_are_dropped(self):
        """An old blob must not reintroduce the delivery_*/deep_* mismatch."""
        import json
        from PySide6.QtCore import QSettings
        from frameguard.core import DEFAULT_CONFIG
        from frameguard.gui import SettingsPanel, _SETTINGS_APP, _SETTINGS_ORG

        QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(
            "scan/config",
            json.dumps({
                "delivery_width": 1920,
                "delivery_height": 1080,
                "obsolete_key": 1,
                "target_fps": 24.0,
            }),
        )
        panel = SettingsPanel()
        panel.restore_settings()
        cfg = panel.get_config_overrides()

        assert not set(cfg) - set(DEFAULT_CONFIG)
        assert cfg["target_width"] == 1920   # migrated from delivery_width
        assert cfg["target_height"] == 1080  # migrated from delivery_height
        assert cfg["target_fps"] == 24.0
        QSettings(_SETTINGS_ORG, _SETTINGS_APP).clear()


class TestResultsFiltering:
    def test_issues_only_hides_clean_rows(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("pass.mov", "pass"),
                        make_result("warn.mov", "warn"),
                        make_result("fail.mov", "fail")], append=True)

        assert not any(rp.table.isRowHidden(r) for r in range(3))

        # Rows are sorted failures-first, so locate by filename rather than
        # assuming the original scan order.
        row_of = {rp.table.item(r, 2).text(): r for r in range(rp.table.rowCount())}

        rp._issues_only_cb.setChecked(True)
        assert rp.table.isRowHidden(row_of["pass.mov"])
        assert not rp.table.isRowHidden(row_of["warn.mov"])
        assert not rp.table.isRowHidden(row_of["fail.mov"])

        rp._issues_only_cb.setChecked(False)
        assert not any(rp.table.isRowHidden(r) for r in range(3))

    def test_failures_sort_to_the_top(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("a_pass.mov", "pass"),
                        make_result("b_fail.mov", "fail"),
                        make_result("c_warn.mov", "warn")], append=True)

        order = [rp.table.item(r, 2).text() for r in range(rp.table.rowCount())]
        assert order == ["b_fail.mov", "c_warn.mov", "a_pass.mov"]

        rp.set_failures_first(False)
        order = [rp.table.item(r, 2).text() for r in range(rp.table.rowCount())]
        assert order == ["a_pass.mov", "b_fail.mov", "c_warn.mov"]

    def test_status_column_says_fail_not_mismatch(self):
        """Leadership found "MISMATCH" too soft for a sign-off tool."""
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("f.mov", "fail"),
                        make_result("w.mov", "warn"),
                        make_result("p.mov", "pass")], append=True)
        labels = {rp.table.item(r, 2).text(): rp.table.item(r, 1).text()
                  for r in range(rp.table.rowCount())}
        assert labels == {"f.mov": "FAIL", "w.mov": "WARNING", "p.mov": "PASS"}

    def test_attention_banner_reports_failures(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("f.mov", "fail")] +
                       [make_result(f"p{i}.mov", "pass") for i in range(3)], append=True)
        assert "1 of 4" in rp._attention_banner.text()
        assert "FAILED" in rp._attention_banner.text()

        rp._clear_all_results()
        rp.set_results([make_result("p.mov", "pass")], append=True)
        assert "passed" in rp._attention_banner.text().lower()

    def test_filtering_does_not_disturb_selection_indexes(self):
        from PySide6.QtCore import Qt
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result("pass.mov", "pass"),
                        make_result("fail.mov", "fail")], append=True)
        row_of = {rp.table.item(r, 2).text(): r for r in range(rp.table.rowCount())}
        rp.table.item(row_of["fail.mov"], 0).setCheckState(Qt.Checked)
        rp._issues_only_cb.setChecked(True)

        selected = rp.get_selected_results()
        assert len(selected) == 1
        assert selected[0].filename == "fail.mov"

    def test_hint_reports_how_many_are_hidden(self):
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp.set_results([make_result(f"p{i}.mov", "pass") for i in range(4)]
                       + [make_result("f.mov", "fail")], append=True)
        rp._issues_only_cb.setChecked(True)
        assert "4 clean file(s) hidden" in rp._summary_hint.text()


class TestResultContextMenu:
    def test_handler_ignores_clicks_outside_any_row(self):
        from PySide6.QtCore import QPoint
        from frameguard.gui import ResultsPanel

        rp = ResultsPanel()
        rp._show_row_context_menu(QPoint(5, 5))  # empty table, must not raise

    def test_reveal_helpers_do_not_raise_on_missing_paths(self):
        from frameguard.gui import _open_path, _reveal_in_file_manager

        _reveal_in_file_manager(os.path.join(os.sep, "definitely", "missing.mov"))
        _open_path(os.path.join(os.sep, "definitely", "missing.mov"))
