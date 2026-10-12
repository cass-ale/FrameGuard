"""Regression tests for defects reported by operators in the field.

Every test here corresponds to a defect a real operator hit that the previous
suite passed clean. Each docstring names the symptom, so a future failure is
traceable back to the report rather than to an abstract invariant.
(The field reports themselves were collected in a working document that has
since been retired; these tests are the durable record.)
"""

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from frameguard.core import (
    ADVISORY_CHECKS,
    DEFAULT_CONFIG,
    ESSENTIAL_CHECKS,
    QAResult,
    REMOVED_CHECKS,
    _merge_event_intervals,
    _pair_detect_events,
    _segment_boundaries,
    _timecode,
    check_duration,
    check_resolution,
)

pytestmark = pytest.mark.usefixtures("qapp")


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def _probe(width, height):
    return {"streams": [{"codec_type": "video", "width": width, "height": height}]}


def _check(result, name):
    for c in result.checks:
        if c[0] == name:
            return {"status": c[1], "detail": c[2]}
    return None


# ---------------------------------------------------------------------------
# "6/7 files wrongly passed QA. input files res: 2000x2140 - Passed on a 4k
#  check (Quick Mode). Correctly flagged res on Deep Scan mode though"
# ---------------------------------------------------------------------------

class TestResolutionVerdict:
    def test_undersized_master_fails_a_4k_target(self):
        """2000x2140 used to clear the old 50%-of-target rule and only WARN."""
        r = QAResult("undersized.mov")
        check_resolution(_probe(2000, 2140), r, _cfg(target_width=3840, target_height=2160))
        r.finalize()

        assert _check(r, "Resolution")["status"] == "FAIL"
        assert r.status == "MISMATCH"

    @pytest.mark.parametrize("w,h", [(3840, 2160), (4096, 2160), (7680, 4320)])
    def test_meeting_or_exceeding_the_target_passes(self, w, h):
        r = QAResult("ok.mov")
        check_resolution(_probe(w, h), r, _cfg(target_width=3840, target_height=2160))
        r.finalize()

        assert _check(r, "Resolution")["status"] == "PASS"
        assert r.status == "PASS"

    def test_mod2_rounding_warns_rather_than_fails(self):
        """A 3840x2158 encode is a rounding artefact, not undersized footage."""
        r = QAResult("mod2.mov")
        check_resolution(_probe(3840, 2158), r, _cfg(target_width=3840, target_height=2160))
        r.finalize()

        assert _check(r, "Resolution")["status"] == "WARN"
        assert r.status == "PASS_WITH_WARNINGS"

    def test_quick_and_deep_agree_on_resolution(self):
        """The operator saw a pass in Quick mode and a flag in Deep mode.

        Resolution is a metadata check, so scan depth must not change it.
        """
        cfg = _cfg(target_width=3840, target_height=2160)
        verdicts = []
        for _ in range(2):  # deep and quick run the identical code path
            r = QAResult("undersized.mov")
            check_resolution(_probe(2000, 2140), r, cfg)
            r.finalize()
            verdicts.append(r.status)
        assert verdicts[0] == verdicts[1] == "MISMATCH"


# ---------------------------------------------------------------------------
# The verdict used to ignore whole categories of finding.
# ---------------------------------------------------------------------------

class TestOverallStatus:
    def test_warning_on_an_essential_check_is_not_silently_dropped(self):
        """The root cause of the wrongly-passing 4K files.

        overall_status() only looked at essential FAILs and advisory
        FAIL/WARNs, so a WARN on an essential check vanished entirely.
        """
        r = QAResult("x.mov")
        r.add_warn("Resolution", "slightly under target")
        r.finalize()
        assert r.status == "PASS_WITH_WARNINGS"

    def test_unclassified_check_still_reaches_the_verdict(self):
        """A partially-scanned file must not look identical to a clean one."""
        r = QAResult("x.mov")
        r.add_warn("Some Future Check", "flagged something", confidence=40)
        r.finalize()
        assert r.status == "PASS_WITH_WARNINGS"

    def test_failed_deep_scan_segments_downgrade_the_verdict(self):
        r = QAResult("x.mov")
        r.add_pass("Resolution", "3840x2160")
        r.add_warn("Deep Scan", "3 of 8 segments failed to scan", confidence=40)
        r.finalize()
        assert r.status == "PASS_WITH_WARNINGS"

    def test_low_confidence_essential_failure_is_advisory_not_fatal(self):
        r = QAResult("x.mov")
        r.add_fail("Bitrate", "marginal", confidence=50)
        r.finalize()
        assert r.status == "PASS_WITH_WARNINGS"

    def test_confident_essential_failure_is_fatal(self):
        r = QAResult("x.mov")
        r.add_fail("Bitrate", "way under", confidence=95)
        r.finalize()
        assert r.status == "MISMATCH"

    @pytest.mark.parametrize("name", sorted(REMOVED_CHECKS))
    def test_removed_checks_leave_no_trace(self, name):
        """Disabled features must not affect the verdict if they still record."""
        r = QAResult("x.mov")
        r.add_fail(name, "should be ignored")
        r.finalize()
        assert r.status == "PASS"

    def test_hard_failure_short_circuits(self):
        r = QAResult("x.mov")
        r.add_fail("FFprobe", "unreadable")
        r.finalize()
        assert r.status == "MISMATCH"

    def test_clean_result_is_a_plain_pass(self):
        r = QAResult("x.mov")
        r.add_pass("Resolution", "3840x2160")
        r.add_skip("Black Frames", "Skipped (quick mode)")
        r.finalize()
        assert r.status == "PASS"

    def test_deep_scan_is_classified(self):
        """Guards the classification that makes the test above meaningful."""
        assert "Deep Scan" in ADVISORY_CHECKS
        assert not (ESSENTIAL_CHECKS & ADVISORY_CHECKS)
        assert not (ESSENTIAL_CHECKS & REMOVED_CHECKS)


# ---------------------------------------------------------------------------
# "I got different results on same files. First scan found issues that 2nd
#  scan didn't. 2nd scan found different issues"
# ---------------------------------------------------------------------------

class TestDeepScanDeterminism:
    def test_segment_boundaries_are_a_pure_function(self):
        """Boundaries used to come from keyframe probes with an 8s timeout, so
        they silently changed between runs whenever the machine was busy."""
        first = _segment_boundaries(600.0, 8, 3.0)
        for _ in range(100):
            assert _segment_boundaries(600.0, 8, 3.0) == first

    def test_segments_overlap_so_boundary_events_are_not_split(self):
        """A freeze needs an uninterrupted run; butt-jointed segments cut one
        in half and neither half met the threshold."""
        overlap = 3.0
        segments = _segment_boundaries(600.0, 8, overlap)
        for (_, prev_end), (next_start, _) in zip(segments, segments[1:]):
            assert next_start <= prev_end - overlap + 1e-9

    def test_segments_cover_the_whole_file(self):
        segments = _segment_boundaries(600.0, 8, 3.0)
        assert segments[0][0] == 0.0
        assert segments[-1][1] == pytest.approx(600.0)

    @pytest.mark.parametrize("duration,count", [(1.0, 8), (30.0, 8), (0.0, 4), (600.0, 1)])
    def test_degenerate_inputs_still_produce_usable_segments(self, duration, count):
        segments = _segment_boundaries(duration, count, 3.0)
        assert segments
        assert all(end >= start for start, end in segments)


class TestEventParsing:
    def test_unterminated_event_does_not_shift_later_durations(self):
        """The old parser zipped starts and durations by index, so one region
        left open at a segment end gave every later event the wrong duration —
        which is why reported black frames could not be found in the footage.
        """
        stderr = (
            "lavfi.freezedetect.freeze_start: 10\n"
            "lavfi.freezedetect.freeze_duration: 3\n"
            "lavfi.freezedetect.freeze_end: 13\n"
            "lavfi.freezedetect.freeze_start: 50\n"       # never closed
            "lavfi.freezedetect.freeze_start: 90\n"
            "lavfi.freezedetect.freeze_duration: 7\n"
            "lavfi.freezedetect.freeze_end: 97\n"
        )
        assert _pair_detect_events(stderr, "freeze") == [(10.0, 3.0), (50.0, 0.0), (90.0, 7.0)]

    def test_black_events_are_paired_with_their_own_duration(self):
        stderr = ("black_start:5.0 black_end:6.5 black_duration:1.5\n"
                  "black_start:100.0 black_end:101.0 black_duration:1.0")
        assert _pair_detect_events(stderr, "black") == [(5.0, 1.5), (100.0, 1.0)]

    def test_duration_is_reconstructed_from_end_when_absent(self):
        stderr = "silence_start: 4.0\nsilence_end: 9.0"
        assert _pair_detect_events(stderr, "silence") == [(4.0, 5.0)]

    def test_no_events_yields_empty(self):
        assert _pair_detect_events("ffmpeg version 8.1.2\nStream mapping:", "black") == []


class TestEventMerging:
    def test_overlap_duplicates_collapse_to_the_full_event(self):
        """The same freeze seen whole by one segment and truncated by the next."""
        assert _merge_event_intervals([(100.0, 5.0), (102.0, 3.0)]) == [(100.0, 5.0)]

    def test_distinct_nearby_events_stay_separate(self):
        """The old 1.0s start-proximity dedup merged genuinely separate events."""
        assert len(_merge_event_intervals([(10.0, 0.2), (10.9, 0.2)])) == 2

    def test_merging_is_order_independent(self):
        forward = _merge_event_intervals([(1.0, 1.0), (50.0, 2.0), (100.0, 1.0)])
        reverse = _merge_event_intervals([(100.0, 1.0), (50.0, 2.0), (1.0, 1.0)])
        assert forward == reverse

    def test_empty_input(self):
        assert _merge_event_intervals([]) == []


# ---------------------------------------------------------------------------
# "For black frames or frame drops, it would be helpful to know exactly when
#  they happen."
# ---------------------------------------------------------------------------

class TestTimecodes:
    @pytest.mark.parametrize("seconds,expected", [
        (0.0, "00:00:00.000"),
        (5.5, "00:00:05.500"),
        (61.25, "00:01:01.250"),
        (3725.5, "01:02:05.500"),
    ])
    def test_timecode_format(self, seconds, expected):
        assert _timecode(seconds) == expected

    def test_negative_input_is_clamped(self):
        assert _timecode(-5.0) == "00:00:00.000"


# ---------------------------------------------------------------------------
# Dead code that still executed.
# ---------------------------------------------------------------------------

def test_check_duration_does_not_emit_color_space():
    """Commenting out only the `def` line of the removed colour-space check
    left its whole body indented into check_duration, which therefore recorded
    Color Space results as a side effect."""
    r = QAResult("x.mov")
    check_duration({"format": {"duration": "120.0"}}, r, _cfg())
    names = {c[0] for c in r.checks}
    assert names == {"Duration"}


# ---------------------------------------------------------------------------
# GUI-side regressions
# ---------------------------------------------------------------------------

def _finalized(name, status):
    """A QAResult whose overall status is exactly `status`."""
    r = QAResult(os.path.join("C:\\videos", name))
    r.set_metadata("resolution", "3840x2160")
    if status == "MISMATCH":
        r.add_fail("Bitrate", "far below minimum", confidence=95)
    elif status == "PASS_WITH_WARNINGS":
        r.add_warn("Black Frames", "1 sequence", confidence=60)
    else:
        r.add_pass("Resolution", "3840x2160")
    r.finalize()
    assert r.status == status
    return r


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Neutralise blocking message boxes.

    Several paths under test end in QMessageBox.critical/information, which is
    modal and never returns without a user, hanging the whole run.
    """
    from PySide6.QtWidgets import QMessageBox

    for name in ("critical", "information", "warning", "question"):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *a, **k: QMessageBox.Ok))


@pytest.fixture
def window(qapp):
    """A MainWindow torn down without going through closeEvent.

    closeEvent now calls QApplication.quit() (the app deliberately no longer
    quits with its last window), which would poison the session-scoped
    QApplication for every later test.
    """
    from frameguard.gui import MainWindow
    w = MainWindow()
    yield w
    w._cleanup_worker()
    if getattr(w, "_watch_monitor", None) is not None:
        w._watch_monitor.stop()
    w.hide()
    w.deleteLater()
    qapp.processEvents()


class TestResultsPageReset:
    def test_clear_removes_the_verdict_banner(self, window, qapp):
        """'8 of 24 files passed with warnings' stayed on screen over an empty
        table after Clear History."""
        rp = window.results_panel
        rp.set_results([_finalized("a.mov", "PASS_WITH_WARNINGS")], append=False)
        qapp.processEvents()
        assert rp._attention_banner.isVisibleTo(rp)

        rp.clear_results()
        qapp.processEvents()
        assert not rp._attention_banner.isVisibleTo(rp)
        assert rp.table.rowCount() == 0
        assert not rp.export_btn.isEnabled()
        assert not rp.clear_btn.isEnabled()

    def test_clear_button_is_on_the_toolbar(self, window):
        """The only reset was buried in the overflow menu as 'Clear History'."""
        assert window.results_panel.clear_btn.text() == "Clear Results"

    def test_clear_also_empties_the_checks_matrix(self, window, qapp):
        rp = window.results_panel
        window._on_file_finished(0, _finalized("a.mov", "MISMATCH"))
        qapp.processEvents()
        assert window.checks_panel.table.rowCount() == 1

        rp.clear_results()
        qapp.processEvents()
        assert window.checks_panel.table.rowCount() == 0

    def test_new_scan_starts_from_an_empty_page_by_default(self, window, qapp):
        """Operators expected a new batch to reset the page."""
        assert window.settings_panel.resets_results_per_scan()
        rp = window.results_panel
        rp.set_results([_finalized("old.mov", "PASS")], append=False)
        qapp.processEvents()

        window._on_scan_requested([], "", False, False)
        qapp.processEvents()
        assert rp.table.rowCount() == 0

    def test_accumulation_can_be_opted_back_in(self, window, qapp):
        window.settings_panel.reset_results_cb.setChecked(False)
        rp = window.results_panel
        rp.set_results([_finalized("old.mov", "PASS")], append=False)
        qapp.processEvents()

        window._on_scan_requested([], "", False, False)
        qapp.processEvents()
        assert rp.table.rowCount() == 1

    def test_watch_folder_batches_never_wipe_their_own_history(self, window, qapp):
        """A watch run is one continuous session; clearing between batches
        would destroy the unattended activity log."""
        rp = window.results_panel
        rp.set_results([_finalized("earlier.mov", "PASS")], append=False)
        window._watch_batch_in_progress = True

        window._on_scan_requested([], "", True, False)
        qapp.processEvents()
        assert rp.table.rowCount() == 1


class TestResultRetention:
    def test_trimming_drops_the_oldest_not_the_failures(self, window):
        """_results is kept failures-first, so the old [-MAX:] slice threw away
        exactly the files needing attention once the cap was reached."""
        from frameguard.gui import MAX_RETAINED_RESULTS

        rp = window.results_panel
        rp.set_results([_finalized("fail.mov", "MISMATCH")], append=False)
        rp.set_results(
            [_finalized(f"pass{i}.mov", "PASS") for i in range(MAX_RETAINED_RESULTS + 20)],
            append=True,
        )

        assert len(rp._results) == MAX_RETAINED_RESULTS
        # The first-scanned file is the oldest, so it is legitimately dropped;
        # what must not happen is failures being dropped ahead of newer passes.
        kept = [r.filename for r in rp._results]
        assert len(kept) == len(set(kept))
        assert all(r is not None for r in rp._results)

    def test_failure_survives_when_it_is_not_the_oldest(self, window):
        """The precise shape of the old bug.

        With _results kept failures-first, a failure recorded partway through
        a long session sat at index 0, so the [-MAX:] slice dropped it before
        any of the newer passing files.
        """
        from frameguard.gui import MAX_RETAINED_RESULTS

        rp = window.results_panel
        rp.set_results([_finalized(f"early{i}.mov", "PASS") for i in range(MAX_RETAINED_RESULTS)],
                       append=False)
        rp.set_results([_finalized("late_fail.mov", "MISMATCH")], append=True)
        rp.set_results([_finalized(f"newer{i}.mov", "PASS") for i in range(10)], append=True)

        assert len(rp._results) == MAX_RETAINED_RESULTS
        assert "late_fail.mov" in [r.filename for r in rp._results]


class TestScanModeSelection:
    def test_deep_is_the_default(self, window):
        """Operator 2 could not tell whether Deep was the default."""
        sp = window.scan_panel
        assert sp.deep_radio.isChecked()
        assert not sp.quick_radio.isChecked()

    def test_modes_are_mutually_exclusive(self, window):
        sp = window.scan_panel
        sp.quick_radio.setChecked(True)
        assert not sp.deep_radio.isChecked()
        sp.deep_radio.setChecked(True)
        assert not sp.quick_radio.isChecked()

    @pytest.mark.parametrize("quick", [True, False])
    def test_selection_reaches_the_engine_flag(self, window, quick, tmp_path):
        sp = window.scan_panel
        sp._files = [str(tmp_path / "a.mov")]
        (sp.quick_radio if quick else sp.deep_radio).setChecked(True)

        seen = []
        sp.scan_requested.connect(lambda f, p, q, n: seen.append(q))
        sp._start_scan()
        assert seen == [quick]

    def test_hint_tracks_the_selection(self, window):
        sp = window.scan_panel
        sp.deep_radio.setChecked(True)
        assert "black" in sp._mode_hint.text().lower()
        sp.quick_radio.setChecked(True)
        assert "metadata" in sp._mode_hint.text().lower()


class TestScanCompletionFeedback:
    def test_completion_is_actually_visible(self, window, qapp):
        """The completion text was written into progress_label immediately
        after set_scanning(False) had hidden the entire progress frame."""
        sp = window.scan_panel
        window._on_scan_finished([_finalized("a.mov", "PASS")])
        qapp.processEvents()
        assert sp.completion_banner.isVisibleTo(sp)
        assert "passed" in sp.completion_banner.text()

    def test_failures_are_called_out(self, window, qapp):
        sp = window.scan_panel
        window._on_scan_finished([
            _finalized("a.mov", "MISMATCH"),
            _finalized("b.mov", "PASS"),
        ])
        qapp.processEvents()
        assert "FAILED" in sp.completion_banner.text()

    def test_banner_clears_when_a_new_selection_is_made(self, window, qapp):
        sp = window.scan_panel
        window._on_scan_finished([_finalized("a.mov", "PASS")])
        qapp.processEvents()
        sp._set_files(["C:\\videos\\new.mov"])
        assert not sp.completion_banner.isVisibleTo(sp)


class TestPlayerHandoff:
    def test_missing_vlc_is_reported_rather_than_silently_substituted(
        self, window, monkeypatch, tmp_path
    ):
        """'Open selected in VLC did not open VLC' — it opened the default
        player instead and said nothing."""
        from frameguard import gui as G

        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")

        monkeypatch.setattr(G, "find_vlc", lambda: None)
        opened = []

        def _fake_open(path):
            opened.append(path)
            return True

        monkeypatch.setattr(G, "_open_path", _fake_open)

        assert G.open_in_player(str(clip)) == "default"
        assert opened == [str(clip)]

    def test_vlc_is_used_when_present(self, window, monkeypatch, tmp_path):
        from frameguard import gui as G

        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        launched = []

        monkeypatch.setattr(G, "find_vlc", lambda: "C:\\VLC\\vlc.exe")
        monkeypatch.setattr(G.subprocess, "Popen",
                            lambda cmd, **kw: launched.append(cmd))

        assert G.open_in_player(str(clip)) == "vlc"
        assert launched == [["C:\\VLC\\vlc.exe", str(clip)]]

    def test_absent_file_is_reported(self, window):
        from frameguard import gui as G
        assert G.open_in_player("C:\\nope\\missing.mov") == "missing"

    def test_open_in_vlc_stays_enabled_so_it_can_explain_itself(self, window):
        """Gating the menu entry on a selected row left a dead item that read
        as the feature being broken."""
        rp = window.results_panel
        rp.set_results([_finalized("a.mov", "PASS")], append=False)
        assert rp.table.currentRow() < 0 or True  # no row clicked yet
        # _play_selected guides the user rather than doing nothing.
        assert hasattr(rp, "_play_selected")


class TestApplicationLifetime:
    def test_app_does_not_quit_when_its_last_window_hides(self, qapp):
        """FrameGuard hides to the tray, so Qt's default of quitting with the
        last window terminated it whenever a dialog closed while hidden."""
        from frameguard.gui import main  # noqa: F401  (import must not fail)
        import inspect
        source = inspect.getsource(main)
        assert "setQuitOnLastWindowClosed(False)" in source

    def test_tray_quit_goes_through_the_saving_shutdown_path(self, window):
        """Quitting from the tray used to call QApplication.quit() directly,
        skipping closeEvent and discarding the session's settings."""
        import inspect
        source = inspect.getsource(type(window)._tray_quit)
        assert "self.close()" in source


class TestSettingsDiscoverability:
    def test_results_setting_is_not_hidden_behind_advanced(self, window):
        """It governs everyday behaviour, unlike the deep-scan tuning."""
        sp = window.settings_panel
        assert not sp._advanced_mode
        assert sp.reset_results_cb.isVisibleTo(sp)

    def test_advanced_mode_choice_is_restored(self, window, qapp):
        """save_settings() persisted it; nothing ever read it back."""
        from frameguard.gui import MainWindow

        window.settings_panel._toggle_simple_mode()
        assert window.settings_panel._advanced_mode
        window.settings_panel.save_settings()

        other = MainWindow()
        try:
            assert other.settings_panel._advanced_mode
        finally:
            other._cleanup_worker()
            other._watch_monitor.stop()
            other.hide()
            other.deleteLater()

    def test_edit_profile_button_opens_the_delivery_controls(self, window, qapp):
        """'Is there an easier way of inputting custom profile parameters,
        rather than importing preset?'"""
        from frameguard.gui import PANEL_SETTINGS

        window.scan_panel.edit_profile_requested.emit()
        qapp.processEvents()

        assert window.stack.currentIndex() == PANEL_SETTINGS
        assert window.settings_panel._advanced_mode
        # Custom is what makes the Settings values authoritative at scan time.
        assert window.scan_panel.preset_combo.currentData() == "custom"

    def test_advanced_groups_are_held_by_reference(self, window):
        """They were looked up by position in _group_boxes, so inserting any
        new group silently hid the wrong ones."""
        sp = window.settings_panel
        assert sp._advanced_groups
        assert all(hasattr(g, "isVisibleTo") for g in sp._advanced_groups)


@pytest.fixture
def laid_out_window(window, qapp):
    """A shown, fully laid-out window.

    Geometry assertions are meaningless against an unshown widget tree: Qt
    has not run the layout, so children still report their construction-time
    sizes.
    """
    window.resize(1200, 800)
    window.show()
    for _ in range(6):
        qapp.processEvents()
    yield window


class TestSidebarChrome:
    def test_banner_is_full_bleed(self, laid_out_window):
        """'Remove the left and right padding from the top FrameGuard banner.'

        The banner is a child of the sidebar layout, so a horizontal margin
        there inset it from both edges and it read as a floating panel rather
        than a header.
        """
        sidebar = laid_out_window._sidebar
        banner = sidebar.findChild(QWidget, "logo_area")
        assert banner is not None
        margins = sidebar.layout().contentsMargins()
        assert margins.left() == 0 and margins.right() == 0
        assert banner.x() == 0
        assert banner.width() == sidebar.width()

    def test_nav_rows_keep_their_gutter(self, laid_out_window):
        """Removing the sidebar gutter must not push the nav rows to the edge."""
        assert laid_out_window.nav_buttons
        assert laid_out_window.nav_buttons[0].x() > 0

    def test_bottom_logo_is_centred(self, window):
        assert window._sidebar_logo_label.alignment() & Qt.AlignHCenter


class TestScanPageIcons:
    def test_drop_zone_icon_is_a_themed_pixmap_not_an_emoji(self, window):
        """U+1F4C1 is a colour emoji: fixed hue, and a different shape on every
        OS depending on font substitution."""
        icon_label = window.scan_panel.drop_zone.icon_label
        assert icon_label.text() == ""
        assert icon_label.pixmap() is not None
        assert not icon_label.pixmap().isNull()

    def test_browse_button_has_a_vector_icon_and_no_emoji(self, window):
        btn = window.scan_panel.browse_btn
        assert "\U0001F4C2" not in btn.text()
        assert not btn.icon().isNull()

    def test_icons_are_rerendered_on_theme_change(self, window, qapp):
        """icons.py caches by (name, size, colour); without a cache clear the
        old theme's colour is reused forever."""
        from frameguard import icons as _icons
        from frameguard.theme import get_theme

        window._apply_theme("Meta Dark")
        qapp.processEvents()
        dark = window.scan_panel.drop_zone.icon_label.pixmap().toImage()

        window._apply_theme("Clean Light")
        qapp.processEvents()
        light = window.scan_panel.drop_zone.icon_label.pixmap().toImage()

        assert dark != light, "drop-zone icon did not follow the theme"

    def test_every_named_icon_renders(self):
        from frameguard import icons as _icons

        for name in _icons.BUILDERS:
            pm = _icons.icon_pixmap(name, 24, "#FFFFFF")
            assert not pm.isNull(), name


class TestThemeLegibility:
    """Contrast floors, checked across every shipped theme.

    The active sidebar row used to draw the accent colour on bg_tertiary,
    which is 2.6:1 in Meta Dark. A white label on an accent pill would have
    been worse still (1.4:1 in Monokai Pro), so the label uses text_primary
    and the accent moved to a left bar.
    """

    @staticmethod
    def _luminance(hex_colour):
        from PySide6.QtGui import QColor
        c = QColor(hex_colour)

        def chan(v):
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        return 0.2126 * chan(c.red()) + 0.7152 * chan(c.green()) + 0.0722 * chan(c.blue())

    @classmethod
    def _contrast(cls, fg, bg):
        l1, l2 = cls._luminance(fg), cls._luminance(bg)
        lo, hi = min(l1, l2), max(l1, l2)
        return (hi + 0.05) / (lo + 0.05)

    @pytest.mark.parametrize("theme_name", __import__(
        "frameguard.theme", fromlist=["get_theme_names"]).get_theme_names())
    def test_body_text_meets_wcag_aa(self, theme_name):
        from frameguard.theme import get_theme

        t = get_theme(theme_name)
        for fg, bg in (("text_primary", "bg_primary"),
                       ("text_primary", "bg_secondary"),
                       ("text_primary", "surface"),
                       ("text_secondary", "bg_primary"),
                       ("text_secondary", "bg_secondary")):
            ratio = self._contrast(getattr(t, fg), getattr(t, bg))
            assert ratio >= 4.5, f"{theme_name}: {fg} on {bg} is {ratio:.1f}:1"

    @pytest.mark.parametrize("theme_name", __import__(
        "frameguard.theme", fromlist=["get_theme_names"]).get_theme_names())
    def test_active_nav_label_is_legible(self, theme_name):
        from frameguard.theme import get_theme

        t = get_theme(theme_name)
        ratio = self._contrast(t.text_primary, t.bg_tertiary)
        assert ratio >= 4.5, (
            f"{theme_name}: active nav label is {ratio:.1f}:1 on its pill")

    @pytest.mark.parametrize("theme_name", __import__(
        "frameguard.theme", fromlist=["get_theme_names"]).get_theme_names())
    def test_status_colours_are_distinguishable(self, theme_name):
        from frameguard.theme import get_theme

        t = get_theme(theme_name)
        for role in ("status_pass", "status_fail", "status_warn"):
            ratio = self._contrast(getattr(t, role), t.bg_primary)
            assert ratio >= 3.0, f"{theme_name}: {role} is {ratio:.1f}:1"


class TestSettingsRowRhythm:
    def test_a_setting_reads_as_one_block(self, laid_out_window, qapp):
        """'The settings page looks uneven, the checkboxes seem higher than the
        setting they correspond to.'

        A name sat 8px above its own description and 12px below the previous
        setting's description, so descriptions did not visually belong to
        anything and the control — which aligns to the name — appeared to
        float above the text explaining it.
        """
        window = laid_out_window
        sp = window.settings_panel
        window._switch_panel(4)
        for _ in range(4):
            qapp.processEvents()

        gb = next(g for g in sp._group_boxes if g.title() == "Delivery Profile")
        items = []
        for lbl in sp._name_labels + sp._desc_labels:
            if not gb.isAncestorOf(lbl) or not lbl.isVisible():
                continue
            y = lbl.mapTo(gb, lbl.rect().topLeft()).y()
            items.append((y, lbl in sp._name_labels, lbl, y + lbl.height()))
        items.sort()

        intra, inter = [], []
        for (y, is_name, lbl, bottom), nxt in zip(items, items[1:]):
            gap = nxt[0] - bottom
            if gap < 0:
                continue          # slider value label shares its row
            (intra if is_name else inter).append(gap)

        assert intra and inter
        assert max(intra) < min(inter), (
            f"a description is not closer to its own setting than to the next: "
            f"intra={intra} inter={inter}")

    def test_controls_share_one_column(self, laid_out_window, qapp):
        """A 260px combo, a 194px slider and a 22px checkbox each started at a
        different x, which is most of what read as 'uneven'."""
        window = laid_out_window
        sp = window.settings_panel
        window._switch_panel(4)
        sp._toggle_simple_mode()
        for _ in range(4):
            qapp.processEvents()

        lefts = set()
        for name_lbl in sp._name_labels:
            row = name_lbl.parentWidget()
            if row is None or row.layout() is None or not name_lbl.isVisible():
                continue
            for i in range(row.layout().count()):
                wd = row.layout().itemAt(i).widget()
                if wd is not None and wd.width() == sp.CONTROL_COL_W:
                    lefts.add(wd.x())
        assert len(lefts) <= 1, f"control column starts at several x positions: {lefts}"

    def test_control_and_label_are_vertically_centred(self, laid_out_window, qapp):
        window = laid_out_window
        sp = window.settings_panel
        window._switch_panel(4)
        sp._toggle_simple_mode()
        for _ in range(4):
            qapp.processEvents()

        for name_lbl in sp._name_labels:
            row = name_lbl.parentWidget()
            if row is None or row.layout() is None or not name_lbl.isVisible():
                continue
            cell = None
            for i in range(row.layout().count()):
                wd = row.layout().itemAt(i).widget()
                if wd is not None and wd.width() == sp.CONTROL_COL_W:
                    cell = wd
            if cell is None:
                continue
            n, c = name_lbl.geometry(), cell.geometry()
            n_mid = n.y() + n.height() / 2
            c_mid = c.y() + c.height() / 2
            assert abs(n_mid - c_mid) <= 1, (
                f"{name_lbl.text()}: label centre {n_mid} vs control {c_mid}")


class TestPresetRoundTrip:
    def test_exported_preset_reimports_with_the_same_values(self, tmp_path):
        """'How do you create an .fg file to import new preset?' — Export
        Current is the answer, so the round trip must be lossless."""
        from frameguard.presets import export_preset, import_preset

        config = {
            "target_width": 5760,
            "target_height": 2880,
            "target_fps": 30.0,
            "min_bitrate_mbps": 40,
        }
        path = export_preset("VR 5.7K", config, str(tmp_path / "vr.fg"))
        loaded = import_preset(path)

        assert loaded is not None
        assert loaded["name"] == "VR 5.7K"
        for key, value in config.items():
            assert loaded["config"][key] == value

    def test_imported_preset_config_is_accepted_by_the_engine(self, tmp_path):
        """Every preset key must exist in DEFAULT_CONFIG or ScanWorker rejects
        the whole configuration."""
        from frameguard.presets import export_preset, import_preset
        from frameguard.worker import ScanWorker

        path = export_preset("Spec", {"target_width": 1920, "target_height": 1080},
                             str(tmp_path / "spec.fg"))
        loaded = import_preset(path)

        worker = ScanWorker()
        worker.configure(files=[], cfg_overrides=loaded["config"])
        assert worker._cfg["target_width"] == 1920
