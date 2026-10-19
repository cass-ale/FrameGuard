"""Regression tests for the v0.8.1 bug sweep.

Each test pins one defect that shipped in v0.8.0: deep-scan verdict errors,
preset injection, updater hand-off and trust, tray/close crashes, and the
visual seams in the sidebar and controls.
"""

import hashlib
import os
import ssl
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from frameguard import core
from frameguard.core import DEFAULT_CONFIG, QAResult


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


SRC = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Deep-scan verdicts
# ---------------------------------------------------------------------------

class TestDetectionEventsSurviveCapture:
    def _capture(self, lines, **cfg):
        capture = core._BoundedStderrCapture(_cfg(**cfg))
        for line in lines:
            capture.add(line)
        return str(capture.result())

    def test_three_silences_are_not_fused_into_one_long_silence(self):
        lines = []
        for start in (60, 300, 540):
            lines += [f"[silencedetect @ 0x1] silence_start: {start}",
                      f"[silencedetect @ 0x1] silence_end: {start + 3} | silence_duration: 3"]
        text = self._capture(lines)
        events = core._pair_detect_events(text, "silence")
        assert events == [(60.0, 3.0), (300.0, 3.0), (540.0, 3.0)]

    def test_three_line_freeze_events_keep_their_own_ends(self):
        lines = []
        for start in (100, 400):
            lines += [f"[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: {start}",
                      "[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 2.5",
                      f"[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: {start + 2.5}"]
        text = self._capture(lines, deep_scan_max_stderr_lines=8,
                             deep_scan_max_stderr_chars=600)
        assert core._pair_detect_events(text, "freeze") == [(100.0, 2.5), (400.0, 2.5)]

    def test_event_ceiling_bounds_pathological_streams(self):
        lines = [f"black_start:{i} black_end:{i + 1} black_duration:1" for i in range(50)]
        capture = core._BoundedStderrCapture(_cfg(deep_scan_max_event_lines=10))
        for line in lines:
            capture.add(line)
        result = capture.result()
        assert str(result).count("black_start:") == 10
        assert result.truncated_records >= 40


class TestSegmentOffsets:
    def test_negative_segment_start_is_shifted_onto_the_file_timeline(self):
        text = ("[silencedetect @ 0x1] silence_start: -0.0213\n"
                "[silencedetect @ 0x1] silence_end: 12.5 | silence_duration: 12.52")
        shifted = core._offset_event_times(text, 300.0)
        events = core._pair_detect_events(shifted, "silence")
        assert len(events) == 1
        start, duration = events[0]
        assert start == pytest.approx(299.979, abs=0.01)
        assert duration < 13


class TestDecodeErrorMatching:
    def test_input_banner_with_a_suspicious_path_is_not_a_decode_error(self):
        text = ("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/Volumes/Shoot/not_corrupt_take.mp4':\n"
                "  Metadata:\n"
                "    title           : corrupt_take_out_of_range\n"
                "  Duration: 00:05:00.00, start: 0.000000, bitrate: 50000 kb/s\n")
        assert core._extract_decode_errors(text) == []

    def test_real_decoder_errors_are_still_counted_individually(self):
        text = "\n".join(["[h264 @ 0x7f] Invalid NAL unit size (-1 > 100)."] * 3)
        assert len(core._extract_decode_errors(text)) == 3


class TestFrameRateAndContainers:
    def test_unknown_frame_rate_is_a_low_confidence_warning(self):
        result = QAResult("clip.mov")
        probe = {"streams": [{"codec_type": "video", "r_frame_rate": "0/0",
                              "avg_frame_rate": "0/0"}]}
        core.check_frame_rate(probe, result, _cfg())
        name, status, _detail, confidence = result.checks[-1][:4]
        assert (name, status) == ("Frame Rate", "WARN")
        assert confidence <= 50

    def test_matroska_duration_tags_feed_the_av_duration_check(self):
        result = QAResult("clip.mkv")
        probe = {"streams": [
            {"codec_type": "video", "tags": {"DURATION": "00:01:00.000000000"}},
            {"codec_type": "audio", "tags": {"DURATION-eng": "00:01:00.020000000"}},
        ], "format": {"format_name": "matroska,webm"}}
        core.check_av_duration_mismatch(probe, result, _cfg())
        assert result.checks[-1][1] == "PASS", result.checks

    def test_matroska_frame_count_absence_is_not_a_warning(self):
        result = QAResult("clip.mkv")
        probe = {"streams": [{"codec_type": "video", "r_frame_rate": "30/1",
                              "avg_frame_rate": "30/1"}],
                 "format": {"format_name": "matroska,webm", "duration": "60"}}
        core.check_frame_count(probe, result, _cfg())
        assert result.checks[-1][0] == "Frame Count"
        assert result.checks[-1][1] == "SKIP"


class TestBlackFrameMargins:
    def _run(self, events_text, duration=600.0):
        result = QAResult("clip.mp4")
        cfg = _cfg(segment_enabled=False)

        class _Proc:
            pid = 4242
            returncode = 0

            def __init__(self, *args, **kwargs):
                import io
                self.stderr = io.BytesIO(events_text.encode())
                self.stdout = io.BytesIO(b"out_time_us=1\nprogress=end\n")

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        with mock.patch("frameguard.core.get_file_duration", return_value=duration), \
             mock.patch("frameguard.core.os.path.getsize", return_value=1024), \
             mock.patch("frameguard.core.get_ffmpeg_path", return_value="ffmpeg"), \
             mock.patch("frameguard.core._get_optimal_hwaccel", return_value=[]), \
             mock.patch("frameguard.core.subprocess.Popen", _Proc):
            core.run_deep_scan("clip.mp4", result, cfg,
                               probe_data={"streams": [{"codec_type": "video"}]})
        return next(c for c in result.checks if c[0] == "Black Frames")

    def test_entirely_black_file_fails(self):
        check = self._run("[blackdetect @ 0x1] black_start:0 black_end:600 black_duration:600\n")
        assert check[1] == "FAIL", check

    def test_opening_black_beyond_the_fade_margin_is_reported(self):
        check = self._run("[blackdetect @ 0x1] black_start:0 black_end:20 black_duration:20\n")
        assert check[1] in ("WARN", "FAIL"), check

    def test_a_short_fade_in_is_still_ignored(self):
        check = self._run("[blackdetect @ 0x1] black_start:0 black_end:0.8 black_duration:0.8\n")
        assert check[1] == "PASS", check


class TestQuickModeSkipsHardwareProbes:
    def test_quick_mode_never_probes_hardware_decoders(self):
        result = QAResult("clip.mp4")
        with mock.patch("frameguard.core.get_file_duration", return_value=60.0), \
             mock.patch("frameguard.core.os.path.getsize", return_value=1024), \
             mock.patch("frameguard.core._get_optimal_hwaccel",
                        side_effect=AssertionError("probed in quick mode")):
            core.run_deep_scan("clip.mp4", result, _cfg(), quick=True,
                               probe_data={"streams": [{"codec_type": "video"}]})
        assert any(c[0] == "Black Frames" and c[1] == "SKIP" for c in result.checks)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

class TestPresetValuesAreTyped:
    def test_filter_injection_string_is_dropped(self):
        from frameguard.presets import normalize_preset_config
        config = normalize_preset_config({
            "silence_noise_db": "-50dB:d=2,ametadata=mode=print:file=/tmp/x,silencedetect=n=-50",
            "target_fps": "30",
            "segment_count": 99,
        })
        assert "silence_noise_db" not in config
        assert config["target_fps"] == 30.0
        assert config["segment_count"] == 16

    def test_codec_and_container_lists_are_normalised(self):
        from frameguard.presets import normalize_preset_config
        config = normalize_preset_config({
            "accepted_codecs": ["H264", " HEVC "],
            "accepted_containers": ["MP4", ".MOV"],
            "audio_required": "false",
        })
        assert config["accepted_codecs"] == ["h264", "hevc"]
        assert config["accepted_containers"] == [".mp4", ".mov"]
        assert config["audio_required"] is False

    def test_wrong_types_are_dropped_not_passed_through(self):
        from frameguard.presets import normalize_preset_config
        config = normalize_preset_config({
            "target_width": "wide", "require_cfr": "maybe", "accepted_codecs": "h264",
        })
        assert config == {}


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------

class TestUpdaterTrustAndFailures:
    def test_https_context_verifies_certificates_and_hostnames(self):
        from frameguard.updater import https_context
        context = https_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS keychain only")
    def test_macos_context_loads_system_keychain_roots(self):
        from frameguard import updater as U
        U._macos_pem_cache = None
        assert U._macos_keychain_certificates(), "no roots read from the system keychains"
        assert U.https_context().cert_store_stats()["x509_ca"] > 0

    def test_keychain_discovery_failure_never_breaks_a_check(self, monkeypatch):
        from frameguard import updater as U
        monkeypatch.setattr(U, "_macos_pem_cache", None)
        monkeypatch.setattr(U.os.path, "exists", lambda _p: True)
        monkeypatch.setattr(U.subprocess, "run",
                            mock.Mock(side_effect=TypeError("patched Popen")))
        assert U._macos_keychain_certificates() == []
        assert U._macos_pem_cache is None   # retried next time, not cached

    def test_unreachable_github_reports_failure_not_up_to_date(self, qapp, monkeypatch):
        from frameguard import updater as U
        import urllib.error

        def _boom(*_a, **_k):
            raise urllib.error.URLError("certificate verify failed")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        monkeypatch.setattr(U, "get_default_search_paths", lambda: [])
        manager = U.UpdateManager()
        monkeypatch.setattr(manager, "check_channel_for_delta", lambda: False)
        monkeypatch.setattr(manager, "get_update_source", lambda: "")
        failures, none = [], []
        manager.check_failed.connect(failures.append)
        manager.update_not_found.connect(lambda: none.append(True))
        manager._settings.remove("update/last_check")

        manager._do_check()
        qapp.processEvents()

        assert failures and "certificate verify failed" in failures[-1]
        assert none == []
        assert not manager._settings.value("update/last_check", "")

    def test_mac_handoff_is_signalled_instead_of_quitting_from_a_thread(
            self, qapp, monkeypatch, tmp_path):
        from frameguard import updater as U
        body = b"installer"
        digest = hashlib.sha256(body).hexdigest()
        stage = tmp_path / "stage"
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_k: str(stage))
        monkeypatch.setattr(U.sys, "platform", "darwin")
        monkeypatch.setattr(U.subprocess, "Popen", lambda command: None)

        class _Response:
            status = 200
            headers = {"Content-Length": str(len(body))}

            def __init__(self):
                import io
                self._io = io.BytesIO(body)

            def read(self, size=-1):
                return self._io.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response())
        manager = U.UpdateManager()
        handoffs = []
        manager.install_handoff.connect(handoffs.append)
        info = U.UpdateInfo(version="9.9.9",
                            installer_path="https://example.test/FrameGuard-arm64.dmg",
                            sha256=digest, size_bytes=len(body),
                            asset_name="FrameGuard-arm64.dmg")
        manager._do_apply(info)
        qapp.processEvents()
        assert handoffs == [str(stage / "FrameGuard-arm64.dmg")]

    def test_apply_path_no_longer_schedules_a_quit_from_the_worker(self):
        source = (SRC / "frameguard" / "updater.py").read_text(encoding="utf-8")
        assert "QTimer.singleShot(0, app.quit)" not in source

    def test_stale_staging_dirs_are_removed(self, tmp_path, monkeypatch):
        from frameguard import updater as U
        monkeypatch.setattr(U.tempfile, "gettempdir", lambda: str(tmp_path))
        old = tmp_path / "frameguard_update_old"
        fresh = tmp_path / "frameguard_update_new"
        other = tmp_path / "unrelated"
        for folder in (old, fresh, other):
            folder.mkdir()
        past = time.time() - 3 * 24 * 3600
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        assert U.remove_stale_staging_dirs() == 1
        assert not old.exists() and fresh.exists() and other.exists()


class TestInstallerScript:
    def _iss(self):
        return (SRC / "installer.iss").read_text(encoding="utf-8-sig")

    def test_silent_update_is_not_blocked_by_an_app_mutex_prompt(self):
        iss = self._iss()
        assert "\nAppMutex=" not in iss
        assert "CheckForMutexes('FrameGuardDesktopApp')" in iss

    def test_silent_in_app_update_relaunches_frameguard(self):
        iss = self._iss()
        assert "Check: ShouldRelaunchAfterUpdate" in iss
        assert "/RESTARTAPPLICATIONS" in iss


# ---------------------------------------------------------------------------
# GUI: crashes and seams
# ---------------------------------------------------------------------------

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


class TestTrayAndShutdownCrashes:
    def test_tray_activation_defers_instead_of_hiding_inside_the_callback(
            self, window, monkeypatch):
        from PySide6.QtWidgets import QSystemTrayIcon
        from frameguard import gui
        deferred = []
        monkeypatch.setattr(window, "_defer", deferred.append)
        monkeypatch.setattr(gui.sys, "platform", "win32")
        window._on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert deferred == [window._tray_show]

    def test_macos_click_leaves_the_status_menu_alone(self, window, monkeypatch):
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon
        from frameguard import gui
        deferred = []
        monkeypatch.setattr(window, "_defer", deferred.append)
        monkeypatch.setattr(gui.sys, "platform", "darwin")
        tray = mock.Mock()
        tray.contextMenu.return_value = QMenu()
        monkeypatch.setattr(window, "_tray_icon", tray)
        window._on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert deferred == []

    def test_close_never_quits_synchronously(self, window, monkeypatch):
        from frameguard import gui
        calls = []
        monkeypatch.setattr(gui.QApplication, "quit",
                            staticmethod(lambda: calls.append("sync-quit")))
        scheduled = []
        monkeypatch.setattr(gui.QTimer, "singleShot",
                            staticmethod(lambda delay, callback: scheduled.append(callback)))
        window._force_quit = True
        window.close()
        assert calls == []          # nothing ran inside closeEvent
        assert scheduled            # the quit is queued for after it unwinds


class TestSeamlessChrome:
    def test_sidebar_layout_containers_do_not_repaint_the_page_colour(self):
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Blue Hour"))
        assert "#logo_area, #sidebar_body, #sidebar_toggle_column { background: transparent;" in css

    def test_sidebar_body_and_toggle_are_named_for_the_rule(self, window):
        from PySide6.QtWidgets import QWidget
        assert window._sidebar.findChild(QWidget, "sidebar_body") is not None
        assert window._toggle_container.objectName() == "sidebar_toggle_column"
        assert window._sidebar_toggle_btn.text() == ""
        assert not window._sidebar_toggle_btn.icon().isNull()

    def test_results_header_does_not_light_up_on_selection(self, window):
        header = window.results_panel.table.horizontalHeader()
        assert header.highlightSections() is False

    def test_indicators_use_real_artwork_not_a_css_triangle(self):
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Clean Light"))
        assert "border-top: 6px solid" not in css
        urls = [part.split('")')[0] for part in css.split('url("')[1:]]
        assert urls, "no indicator artwork referenced"
        for path in urls:
            assert os.path.isfile(path), path
            assert Path(path).read_text(encoding="utf-8").startswith("<svg")

    def test_checked_indicator_keeps_its_unchecked_size(self):
        from frameguard.theme import generate_stylesheet, get_theme
        css = generate_stylesheet(get_theme("Blue Hour"))
        assert "QRadioButton::indicator:checked { border: 2px solid" in css
        assert "QCheckBox::indicator:checked { border: 2px solid" in css

    def test_no_placeholder_emoji_in_results_hint(self, window):
        window.results_panel._apply_row_filter()
        assert "\U0001F6C8" not in window.results_panel._summary_hint.text()

    def test_titlebar_tint_is_a_noop_off_cocoa(self, window, qapp):
        from frameguard import gui
        from frameguard.theme import get_theme
        if qapp.platformName() == "cocoa":
            pytest.skip("covered natively")
        assert gui._apply_macos_titlebar(window, get_theme()) is False

    def test_checks_matrix_spans_the_panel_when_it_fits(self, window, qapp):
        from frameguard import gui
        from PySide6.QtWidgets import QHeaderView
        from conftest import make_result
        results = [make_result(f"clip_{i}.mov", "warn") for i in range(3)]
        window.checks_panel.set_results(results)
        window._switch_panel(gui.PANEL_CHECKS)
        for _ in range(4):
            qapp.processEvents()
        header = window.checks_panel.table.horizontalHeader()
        assert header.sectionResizeMode(0) == QHeaderView.Stretch


class TestSplash:
    def test_pulse_repaints_without_running_the_event_loop(self, qapp, monkeypatch):
        from frameguard import gui
        splash = gui.SplashScreen()
        try:
            splash._timer.stop()
            monkeypatch.setattr(gui.QApplication, "processEvents",
                                staticmethod(lambda *a: pytest.fail("pulse ran the loop")))
            splash.pulse("Building results...", 78)
            assert splash._status_text == "Building results..."
            assert splash._progress == 78
        finally:
            splash.close()

    def test_sweep_follows_the_wall_clock(self, qapp):
        from frameguard import gui
        splash = gui.SplashScreen()
        try:
            splash._timer.stop()
            splash._started = time.monotonic() - 1.0
            splash.pulse()
            # One second at 96 deg/s, independent of how long the
            # constructor itself took on a slow CI runner.
            assert splash._sweep == pytest.approx(96.0, abs=10)
        finally:
            splash.close()

    def test_window_shows_before_the_splash_fades(self):
        import inspect
        from frameguard import gui
        source = inspect.getsource(gui.main)
        body = source[source.index("def _show_window"):]
        assert body.index("window.show()") < body.index("splash.fade_out()")


# ---------------------------------------------------------------------------
# Independent-review follow-ups
# ---------------------------------------------------------------------------

class TestReviewFollowUps:
    def test_long_metadata_keys_and_continuations_are_not_decode_errors(self):
        text = ("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip.mov':\n"
                "  Metadata:\n"
                "    com.apple.quicktime.description: corrupt take, re-shoot\n"
                "    comment         : line one\n"
                "                    : corrupt frames fixed in v2\n"
                "    abcdefghijklmno : value out of range\n"
                "[h264 @ 0x7f] error while decoding MB 10 20\n")
        assert core._extract_decode_errors(text) == [
            "[h264 @ 0x7f] error while decoding MB 10 20"]

    def test_dump_line_first_after_trimming_and_glued_errors(self):
        leading = core._filter_hwaccel_noise(
            "    com.apple.quicktime.description: corrupt take\n"
            "    LRA: 4.8 LU[h264 @ 0x7f] error while decoding MB 1 2\n")
        errors = core._extract_decode_errors(leading)
        assert errors == ["LRA: 4.8 LU[h264 @ 0x7f] error while decoding MB 1 2"]
        demuxer = ("    LRA: 4.8 LU[mov,mp4,m4a,3gp,3g2,mj2 @ 0x7f] "
                   "Invalid data found when processing input")
        assert core._extract_decode_errors(demuxer) == [demuxer.strip()]

    def test_retained_evidence_has_a_character_ceiling(self):
        capture = core._BoundedStderrCapture(_cfg(deep_scan_max_event_chars=64 * 1024))
        line = "[h264 @ 0x1] corrupt " + "x" * 4000
        for i in range(2000):
            capture.add(f"{line}{i}")
        result = capture.result()
        assert len(result) <= 64 * 1024 + 2000
        assert result.truncated_records > 0

    def test_fractional_bitrate_and_noise_floor_survive_import(self):
        from frameguard.presets import normalize_preset_config
        config = normalize_preset_config({"min_bitrate_mbps": 12.5, "silence_noise_db": -45.5})
        assert config == {"min_bitrate_mbps": 12.5, "silence_noise_db": -45.5}

    def test_install_failures_have_their_own_signal(self, qapp, monkeypatch, tmp_path):
        from frameguard import updater as U
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_k: str(tmp_path / "stage"))
        monkeypatch.setattr(U.sys, "platform", "darwin")
        monkeypatch.setattr(U.subprocess, "Popen",
                            lambda *_a, **_k: (_ for _ in ()).throw(OSError("launch failed")))
        installer = tmp_path / "FrameGuard-arm64.dmg"
        installer.write_bytes(b"installer")
        manager = U.UpdateManager()
        applied = []
        manager.apply_failed.connect(applied.append)
        manager._do_apply(U.UpdateInfo(
            version="9.9.9", installer_path=str(installer),
            sha256=hashlib.sha256(b"installer").hexdigest(), size_bytes=9))
        qapp.processEvents()
        assert applied and "launch failed" in applied[-1]

    def test_no_update_check_runs_during_an_install(self, qapp, monkeypatch):
        from frameguard import updater as U
        manager = U.UpdateManager()
        started = []
        monkeypatch.setattr(U.threading, "Thread",
                            lambda *a, **k: started.append(k) or mock.Mock())
        failures = []
        manager.check_failed.connect(failures.append)
        manager._applying = True
        manager.check_now()
        qapp.processEvents()
        assert started == []
        assert failures == ["An update is already being installed."]

    def test_window_ignores_check_failures_while_installing(self, window, monkeypatch):
        shown = []
        from frameguard import gui
        monkeypatch.setattr(gui.QMessageBox, "warning",
                            staticmethod(lambda *a, **k: shown.append(a)))
        window._update_progress = mock.Mock()
        window._update_manager.check_failed.emit("hourly check failed")
        assert window._update_progress is not None and shown == []
        window._update_progress = None

    def test_indicator_artwork_is_not_kept_in_the_temp_folder(
            self, isolated_user_dirs, monkeypatch):
        from frameguard import theme
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        directory = theme._asset_dir()
        assert directory != theme._fallback_asset_dir()
        if sys.platform == "win32":
            assert directory.startswith(os.environ["LOCALAPPDATA"])
        else:
            assert directory.startswith(str(isolated_user_dirs))

    def test_worker_keeps_every_result(self):
        source = (SRC / "frameguard" / "worker.py").read_text(encoding="utf-8")
        assert ">= 2000" not in source and "_MAX_BATCH_RESULTS" not in source
