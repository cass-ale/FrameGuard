"""Bug-sweep regression tests (deep-scan stability, banner lifecycle, severity).

Covers the fixes from the FGv0.2.2 hardening pass. All run headless via the
offscreen Qt platform plugin. No test touches the network or real media files:
ffmpeg interaction is mocked at the subprocess boundary.
"""

import threading
from unittest import mock

import pytest

from frameguard.core import (
    CHECK_SEVERITY_WEIGHTS,
    DEFAULT_CONFIG,
    QAResult,
    _cap_stderr_text,
    _run_segment_filter_scan,
    check_severity,
    discover_video_files,
    result_severity,
    run_ffprobe,
)

pytestmark = pytest.mark.usefixtures("qapp")


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


def _finalized(name, outcome="fail", **kw):
    from conftest import make_result
    return make_result(name, outcome, **kw)


# ---------------------------------------------------------------------------
# Phase A: deep-scan stability
# ---------------------------------------------------------------------------

class TestSingleTerminalEmit:
    def test_cancel_emits_only_scan_cancelled(self, tmp_path):
        """run() used to emit BOTH scan_finished and scan_cancelled on cancel,
        double-running GUI cleanup, auto-export and the watch-queue drain."""
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        w.configure(files=[str(tmp_path / "a.mov")])
        w._cancel_event.set()  # cancel before the first file

        terminals = []
        w.scan_finished.connect(lambda r: terminals.append("finished"))
        w.scan_cancelled.connect(lambda: terminals.append("cancelled"))
        w.scan_error.connect(lambda m: terminals.append("error"))
        # Hermetic: the tool preflight must pass so we reach the cancel path
        # on machines with and without ffmpeg installed.
        with mock.patch("frameguard.tools.get_ffmpeg_path",
                        return_value="ffmpeg"), \
             mock.patch("frameguard.tools.get_ffprobe_path",
                        return_value="ffprobe"):
            w.run()

        assert terminals == ["cancelled"], terminals

    def test_no_files_is_an_error_not_a_cancel(self):
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        w.configure(files=[])
        terminals = []
        w.scan_finished.connect(lambda r: terminals.append("finished"))
        w.scan_cancelled.connect(lambda: terminals.append("cancelled"))
        w.scan_error.connect(lambda m: terminals.append("error"))
        w.run()
        assert terminals == ["error"]

    def test_missing_tools_is_a_clear_error(self, tmp_path):
        """Popen(None) used to raise TypeError mid-scan, surfacing as a cryptic
        per-file 'Internal Error' instead of a clear missing-tools message."""
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        w.configure(files=[str(tmp_path / "a.mov")])
        errors = []
        w.scan_error.connect(errors.append)
        with mock.patch("frameguard.tools.get_ffmpeg_path", return_value=None), \
             mock.patch("frameguard.tools.get_ffprobe_path", return_value=None):
            w.run()
        assert errors and "not found" in errors[0].lower()


class TestCancelWithoutPsutil:
    def test_cancel_kills_tracked_procs_when_psutil_missing(self):
        """Without psutil, cancel() was a no-op and a deep scan ran for up to
        two hours before noticing the cancel."""
        import builtins
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        real_import = builtins.__import__

        killed = []

        class _FakeProc:
            def terminate(self):
                killed.append("terminate")

            def poll(self):
                return 0  # already exited after terminate

            def kill(self):
                killed.append("kill")

        w.register_proc(_FakeProc())

        def _no_psutil(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("No module named 'psutil'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", _no_psutil):
            w.cancel()

        assert "terminate" in killed

    def test_cancel_reaches_core_live_handles(self):
        """Segment scans register in core's live-handle registry; the
        psutil-missing fallback must consult it, not just the worker list."""
        import builtins
        from frameguard import core as C
        from frameguard.worker import ScanWorker

        w = ScanWorker()
        real_import = builtins.__import__
        killed = []

        class _FakeProc:
            def terminate(self):
                killed.append("terminate")

            def poll(self):
                return 0

            def kill(self):
                killed.append("kill")

        proc = _FakeProc()
        C._track_proc(proc)
        try:
            def _no_psutil(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("No module named 'psutil'")
                return real_import(name, *args, **kwargs)

            with mock.patch.object(builtins, "__import__", _no_psutil):
                w.cancel()
        finally:
            C._untrack_proc(proc)

        assert "terminate" in killed


class TestStderrCap:
    def test_huge_stderr_is_bounded(self):
        text = "\n".join(f"[info] frame={i} fps=30" for i in range(100_000))
        cfg = _cfg(deep_scan_max_stderr_lines=5000,
                   deep_scan_max_stderr_chars=512 * 1024)
        capped, truncated = _cap_stderr_text(text, cfg)
        assert truncated > 0
        assert len(capped.split("\n")) <= 5000
        assert len(capped) <= 512 * 1024

    def test_small_stderr_passes_through_untouched(self):
        capped, truncated = _cap_stderr_text("a\nb\nc", _cfg())
        assert (capped, truncated) == ("a\nb\nc", 0)

    def test_segment_scan_returns_none_immediately_when_cancelled(self):
        """A pre-set cancel_event must not spawn ffmpeg at all."""
        cfg = _cfg()
        ev = threading.Event()
        ev.set()
        with mock.patch("frameguard.core.subprocess.Popen") as popen:
            out = _run_segment_filter_scan("x.mp4", 0, 10, "blackdetect", None,
                                           False, cfg, cancel_event=ev)
        assert out is None
        popen.assert_not_called()

    def test_segment_scan_never_calls_popen_with_none_tool(self):
        """get_ffmpeg_path() -> None must short-circuit, not Popen(None)."""
        with mock.patch("frameguard.core.get_ffmpeg_path", return_value=None):
            with mock.patch("frameguard.core.subprocess.Popen") as popen:
                out = _run_segment_filter_scan("x.mp4", 0, 10, "blackdetect",
                                               None, False, _cfg())
        assert out is None
        popen.assert_not_called()


class TestSafeDiscovery:
    def test_permission_error_returns_empty_instead_of_raising(self, tmp_path):
        with mock.patch("frameguard.core.os.listdir",
                        side_effect=PermissionError("denied")):
            assert discover_video_files(str(tmp_path)) == []

    def test_missing_path_returns_empty(self, tmp_path):
        assert discover_video_files(str(tmp_path / "nope")) == []

    def test_single_file_passthrough(self, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"\x00")
        assert discover_video_files(str(f)) == [str(f)]

    def test_unsupported_extension_filtered(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hi")
        assert discover_video_files(str(f)) == []


class TestToolPathGuards:
    def test_run_ffprobe_with_no_tool_returns_none(self):
        with mock.patch("frameguard.core.get_ffprobe_path", return_value=None):
            with mock.patch("frameguard.core.subprocess.run") as run:
                assert run_ffprobe("x.mp4") is None
            run.assert_not_called()


class TestDeepScanDefaults:
    def test_segment_parallelism_is_capped(self):
        """8 concurrent 4K ffmpeg decodes OOM'd Windows; cap is 4."""
        assert DEFAULT_CONFIG["segment_count"] <= 4
        assert DEFAULT_CONFIG["max_workers"] <= 4

    def test_stderr_bounds_exist(self):
        assert DEFAULT_CONFIG["deep_scan_max_stderr_lines"] > 0
        assert DEFAULT_CONFIG["deep_scan_max_stderr_chars"] > 0

    def test_size_gate_exists(self):
        assert DEFAULT_CONFIG["deep_scan_max_file_gb"] > 0
        assert DEFAULT_CONFIG["deep_scan_max_duration_s"] > 0


# ---------------------------------------------------------------------------
# Phase B: banner lifecycle
# ---------------------------------------------------------------------------

class TestBannerLifecycle:
    def _panel(self):
        from frameguard.gui import ScanPanel
        return ScanPanel()

    def test_discovery_runner_delivers_without_blocking(self, tmp_path):
        """Folder discovery must reach _set_files via the background runner
        (sync os.listdir on a network mount froze the UI). The panel must
        also hold the runner: QThreadPool auto-deletes the runnable, and
        without a Python-side holder the finished signal can be lost."""
        from PySide6.QtCore import QThreadPool
        from PySide6.QtWidgets import QApplication
        from frameguard.gui import ScanPanel

        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00" * 32)

        p = ScanPanel()
        p._discover_files_async([str(tmp_path)])
        assert p._discovery_runners, "runner was not retained"
        assert QThreadPool.globalInstance().waitForDone(30000)
        QApplication.processEvents()
        assert [f for f in p._files if f.endswith("clip.mp4")], p._files

    def test_new_files_clear_the_verdict(self):
        p = self._panel()
        p.show_completion(3, 1, 0)
        assert p.completion_banner.isVisibleTo(p)
        p._set_files(["a.mov"])
        assert p._banner_state == "idle"
        assert not p.completion_banner.isVisibleTo(p)

    def test_error_replaces_stale_verdict(self):
        p = self._panel()
        p.show_completion(3, 0, 0)  # clean pass banner
        assert "passed" in p.completion_banner.text().lower()
        p.show_error("boom")
        assert p._banner_state == "error"
        assert "boom" in p.completion_banner.text()
        assert p.completion_banner.isVisibleTo(p)

    def test_scan_error_path_sets_error_banner(self):
        """MainWindow._on_scan_error must not leave the prior verdict up."""
        from frameguard.gui import MainWindow
        w = MainWindow()
        w.scan_panel.show_completion(2, 0, 0)
        # QMessageBox.critical() would exec a modal dialog and block forever
        # on the offscreen platform.
        with mock.patch("frameguard.gui.QMessageBox"):
            w._on_scan_error("disk vanished")
        assert w.scan_panel._banner_state == "error"
        assert "disk vanished" in w.scan_panel.completion_banner.text()

    def test_empty_completion_hides(self):
        p = self._panel()
        p.show_completion(0, 0, 0)
        assert not p.completion_banner.isVisibleTo(p)
        assert p._banner_state == "idle"

    def test_theme_change_preserves_verdict(self):
        """apply_theme used to wipe the verdict banner."""
        from frameguard.theme import get_theme
        p = self._panel()
        p.show_completion(2, 1, 0)
        text = p.completion_banner.text()
        p.apply_theme(get_theme())
        assert p.completion_banner.isVisibleTo(p)
        assert p.completion_banner.text() == text

    def test_cancelled_state(self):
        p = self._panel()
        p.show_completion(0, 0, 0, cancelled=True)
        assert p._banner_state == "cancelled"
        assert "cancelled" in p.completion_banner.text().lower()


# ---------------------------------------------------------------------------
# Phase C: severity-weighted hierarchy
# ---------------------------------------------------------------------------

class TestSeverityWeights:
    def test_corruption_outranks_framerate(self):
        corrupt = QAResult("bad.mov")
        corrupt.add_fail("FFprobe", "could not read file")
        corrupt.finalize()

        fr = QAResult("offspec.mov")
        fr.add_pass("Container Format", "ok")
        fr.add_fail("Frame Rate", "24 != 30")
        fr.finalize()

        assert corrupt.severity_score > fr.severity_score

    def test_decode_integrity_fail_outranks_framerate_warn(self):
        """Decode Integrity FAIL used to share a bucket with Frame Rate WARN."""
        decode = QAResult("d.mov")
        decode.add_pass("Container Format", "ok")
        decode.add_fail("Decode Integrity", "3 decode errors", confidence=90)
        decode.finalize()

        fr = QAResult("f.mov")
        fr.add_pass("Container Format", "ok")
        fr.add_warn("Frame Rate", "slightly high")
        fr.finalize()

        assert decode.severity_score > fr.severity_score

    def test_clean_file_scores_zero(self):
        r = QAResult("c.mov")
        r.add_pass("Container Format", "ok")
        r.finalize()
        assert r.severity_score == 0.0

    def test_weights_cover_all_essential_checks(self):
        from frameguard.core import ESSENTIAL_CHECKS
        for name in ESSENTIAL_CHECKS:
            assert name in CHECK_SEVERITY_WEIGHTS, f"{name} has no severity weight"

    def test_sort_key_orders_corrupted_first(self):
        from frameguard.gui import ResultsPanel
        rp = ResultsPanel()

        fr = _finalized("framerate_only.mov", "fail")  # Bitrate FAIL ~60
        corrupt = QAResult("corrupt.mov")
        corrupt.add_fail("FFprobe", "unreadable")
        corrupt.finalize()

        rp.set_results([fr, corrupt], append=True)
        assert rp._results[0].filename == "corrupt.mov"

    def test_attention_banner_reports_critical_split(self):
        from frameguard.gui import ResultsPanel
        rp = ResultsPanel()
        corrupt = QAResult("corrupt.mov")
        corrupt.add_fail("FFprobe", "unreadable")
        corrupt.finalize()
        mild = _finalized("mild.mov", "fail")
        rp.set_results([corrupt, mild], append=True)
        assert "critical" in rp._attention_banner.text().lower()

    def test_check_severity_warn_counts_half(self):
        assert check_severity("Frame Rate", "FAIL", 100) == \
            pytest.approx(2 * check_severity("Frame Rate", "WARN", 100))

    def test_result_severity_returns_top_check(self):
        r = QAResult("x.mov")
        r.add_warn("Frame Rate", "off", confidence=80)
        r.add_fail("Decode Integrity", "errors", confidence=90)
        score, top = result_severity(r)
        assert top == "Decode Integrity"
        assert score > 0


# ---------------------------------------------------------------------------
# Phase D: macOS / packaging
# ---------------------------------------------------------------------------

class TestMacHardening:
    def test_manifest_honours_target_arch_env(self, monkeypatch, tmp_path):
        """Cross-building arm64 on x86_64 must not poison the manifest with
        the host arch, or the updater applies Intel builds on Apple Silicon."""
        import importlib.util
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "fgmanifest_arch", _os.path.join(repo, "frameguard", "manifest.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # Small tree: hashing the whole checkout would walk multi-GB build
        # outputs on a network mount and stall the suite for minutes.
        (tmp_path / "FrameGuard.exe").write_bytes(b"fake")
        monkeypatch.setenv("FRAMEGUARD_TARGET_ARCH", "arm64")
        manifest = m.build_manifest(str(tmp_path))
        assert manifest["arch"] == "arm64"

    def test_mac_channel_is_built_with_os_path_join(self):
        """The old darwin channel concatenated backslashes — invalid on POSIX,
        so fresh Mac installs checked a path that could never exist. Channels
        must be built with os.path.join (backslashes are correct ON Windows,
        which is why this asserts on the source, not the separator)."""
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        src = open(_os.path.join(repo, "frameguard", "updater.py"),
                   encoding="utf-8").read()
        assert 'os.path.join(_RELEASE_ROOT, "RELEASES-macos")' in src
        assert '_RELEASE_ROOT + "\\\\RELEASES-macos"' not in src
        from frameguard import updater
        mac = updater.get_update_channel_for("darwin")
        assert mac != updater.DEFAULT_UPDATE_CHANNEL
        assert "macos" in mac.lower()
        if _os.name != "nt":
            assert "\\" not in mac

    def test_updater_restored_last_check_method(self):
        """check_channel_for_delta used to swallow a get_last_check_time def —
        dead code after `return True`."""
        from frameguard.updater import UpdateManager
        assert callable(getattr(UpdateManager, "get_last_check_time", None))

    def test_build_mac_signs_inside_out(self):
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        sh = open(_os.path.join(repo, "build_mac.sh"), encoding="utf-8").read()
        assert "codesign --force --deep --sign" not in sh, \
            "deprecated codesign --deep still present"
        assert "--options runtime" in sh
        assert "--timestamp" in sh
        assert "notarytool" in sh
        assert "FrameGuard-${TARGET_ARCH}.dmg" in sh

    def test_swap_script_signs_inside_out(self):
        import os as _os
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        src = open(_os.path.join(repo, "frameguard", "deltaupdate.py"),
                   encoding="utf-8").read()
        assert "--deep --timestamp=none" not in src
        assert "--sign -" in src  # ad-hoc re-sign retained

    def test_requirements_are_pinned(self):
        import os as _os
        import re
        repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        body = open(_os.path.join(repo, "requirements.txt"), encoding="utf-8").read()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert re.search(r"[<>=!~]", line), f"unpinned requirement: {line}"
