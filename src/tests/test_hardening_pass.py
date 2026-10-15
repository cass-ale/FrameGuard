"""Hardening-pass regressions: scan-banner permanence, strict mode, VLC, splash.

TDD red step — each test pins one reported behaviour:
  1. Clearing Results must dismiss the Scan page verdict (FAIL banners stayed
     "permanent" because history_cleared never reached the ScanPanel).
  2. Strict mode must escalate warnings to MISMATCH (overall_status ignored
     the strict flag, so the setting was a silent no-op).
  3. VLC: a "not found" result must not be pinned for the session, the
     default-player fallback must be able to report failure, the frozen
     bundle dir must be scrubbed from the player's PATH, and an
     immediately-dying VLC process must fall back instead of reading "vlc".
  4. Splash: set_status must drive the bar to its target synchronously —
     the eased value only moved on the 16ms timer, which never fires while
     MainWindow() blocks the event loop, so the bar looked frozen.
  5. Startup must not block the event loop on the FFmpeg availability probe.
"""

import os
from unittest import mock

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture
def no_modals(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    for name in ("critical", "information", "warning", "question"):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *a, **k: QMessageBox.Ok))


@pytest.fixture
def window(qapp):
    from frameguard.gui import MainWindow

    w = MainWindow()
    yield w
    w._cleanup_worker()
    if getattr(w, "_watch_monitor", None) is not None:
        w._watch_monitor.stop()
    w.hide()
    w.deleteLater()
    qapp.processEvents()


@pytest.fixture
def fresh_vlc_cache():
    from frameguard import gui as G

    G._vlc_path_cache.clear()
    yield
    G._vlc_path_cache.clear()


# ---------------------------------------------------------------------------
# 1. Scan-page verdict permanence
# ---------------------------------------------------------------------------

class TestScanVerdictClearsWithResults:
    def test_history_clear_dismisses_scan_verdict(self, window, qapp):
        """A FAIL verdict survived Clear Results — the scan page kept
        announcing an outcome whose files were gone."""
        sp = window.scan_panel
        sp.show_completion(2, 1, 0)
        qapp.processEvents()
        assert sp.completion_banner.isVisibleTo(sp)

        window.results_panel.clear_results()
        qapp.processEvents()

        assert sp._banner_state == "idle"
        assert not sp.completion_banner.isVisibleTo(sp)

    def test_idle_status_does_not_linger_forever(self, window, qapp):
        """Informational notes (auto-export paths, watch notices) wrote
        directly into the verdict banner with no state and no timer, so
        they stayed until the next scan."""
        sp = window.scan_panel
        window._report_idle_status("Report saved: FrameGuard_Report_x.pdf")
        qapp.processEvents()
        assert sp.completion_banner.isVisibleTo(sp)
        # Transient info must carry a state the lifecycle understands…
        assert sp._banner_state in ("info", "idle")
        # …and must go away on navigation instead of persisting.
        window._switch_panel(1)
        qapp.processEvents()
        assert not sp.completion_banner.isVisibleTo(sp)


# ---------------------------------------------------------------------------
# 2. Strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_finalize_escalates_warnings_when_strict(self):
        from frameguard.core import QAResult

        r = QAResult("x.mov")
        r.add_pass("Container Format", "ok")
        r.add_warn("Frame Rate", "31 fps — above target")
        r.finalize(strict=True)
        assert r.status == "MISMATCH"

    def test_non_strict_warnings_stay_warnings(self):
        from frameguard.core import QAResult

        r = QAResult("x.mov")
        r.add_pass("Container Format", "ok")
        r.add_warn("Frame Rate", "31 fps — above target")
        r.finalize(strict=False)
        assert r.status == "PASS_WITH_WARNINGS"

    def test_engine_strict_flag_reaches_the_verdict(self):
        """run_qa_checks(strict=True) must leave the verdict strict even
        though finalize() is called later, without arguments, by the
        worker."""
        from frameguard.core import QAResult

        r = QAResult("x.mov")
        r.add_pass("Container Format", "ok")
        r.add_warn("Frame Rate", "31 fps — above target")
        r.strict = True  # what run_qa_checks(strict=True) records
        r.finalize()
        assert r.status == "MISMATCH"


# ---------------------------------------------------------------------------
# 3. VLC handoff
# ---------------------------------------------------------------------------

class TestVlcHandoff:
    def test_negative_result_is_not_pinned(self, fresh_vlc_cache, monkeypatch):
        """Once find_vlc() missed, the empty string was cached and every
        later Open in VLC in the session kept reporting 'not found' even
        after VLC was installed."""
        import shutil
        from frameguard import gui as G

        monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
        assert G.find_vlc() is None

        monkeypatch.setattr(shutil, "which",
                            lambda *a, **k: "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe")
        assert G.find_vlc() == "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"

    def test_open_path_reports_failure(self, tmp_path):
        """_open_path swallowed every OSError, so open_in_player could never
        return 'failed' — a dead file association read as success."""
        from frameguard import gui as G

        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        if G.sys.platform == "win32":
            opener = mock.patch.object(
                G.os, "startfile", create=True,
                side_effect=OSError("no association"))
        else:
            opener = mock.patch.object(
                G.subprocess, "Popen", side_effect=OSError("no association"))
        with opener:
            assert G._open_path(str(clip)) is False

    def test_open_path_reports_success(self, tmp_path):
        from frameguard import gui as G

        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        if G.sys.platform == "win32":
            opener = mock.patch.object(
                G.os, "startfile", create=True, return_value=None)
        else:
            opener = mock.patch.object(G.subprocess, "Popen", return_value=None)
        with opener:
            assert G._open_path(str(clip)) is True

    def test_fallback_failure_is_reported(self, tmp_path, monkeypatch,
                                          fresh_vlc_cache):
        from frameguard import gui as G

        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        monkeypatch.setattr(G, "find_vlc", lambda: None)
        monkeypatch.setattr(G, "_open_path",
                            lambda p: (_ for _ in ()).throw(OSError("boom")))
        assert G.open_in_player(str(clip)) == "failed"

    def test_immediately_dying_vlc_falls_back(self, tmp_path, monkeypatch,
                                              fresh_vlc_cache):
        """Popen succeeding was treated as 'VLC opened' even when the
        process exited instantly with an error."""
        from frameguard import gui as G

        clip = tmp_path / "a.mov"
        clip.write_bytes(b"0")
        monkeypatch.setattr(G, "find_vlc", lambda: "C:\\VLC\\vlc.exe")

        class _DeadProc:
            def poll(self):
                return 1

        monkeypatch.setattr(G.subprocess, "Popen", lambda *a, **k: _DeadProc())
        monkeypatch.setattr(G, "_open_path", lambda p: True)
        assert G.open_in_player(str(clip)) == "default"

    def test_player_env_scrubs_frozen_bundle_from_path(self, monkeypatch):
        """The frozen build's bundled-Qt directory leaked into VLC's PATH,
        so VLC could start against the wrong DLLs and appear to do
        nothing."""
        import ntpath
        from types import SimpleNamespace
        from frameguard import gui as G

        windows_sys = SimpleNamespace(
            frozen=True,
            _MEIPASS=r"C:\bundle",
            executable=r"C:\bundle\FrameGuard.exe",
            platform="win32",
        )
        windows_os = SimpleNamespace(
            environ=os.environ, name="nt", path=ntpath, pathsep=";", sep="\\")
        monkeypatch.setattr(G, "sys", windows_sys)
        monkeypatch.setattr(G, "os", windows_os)
        monkeypatch.setenv(
            "PATH", windows_os.pathsep.join([r"C:\bundle", r"C:\Windows"]))

        env = G._player_env()
        entries = env["PATH"].split(windows_os.pathsep)
        assert r"C:\bundle" not in entries
        assert r"C:\Windows" in entries


# ---------------------------------------------------------------------------
# 4. Splash progress
# ---------------------------------------------------------------------------

class TestSplashProgress:
    def test_bar_reaches_target_without_the_event_loop(self, qapp):
        """With the 16ms timer stopped (as it effectively is while
        MainWindow() blocks startup), set_status must still move the bar —
        otherwise the splash sits at 0% saying 'Building interface…'."""
        from frameguard.gui import SplashScreen

        splash = SplashScreen()
        try:
            splash._timer.stop()
            splash.set_status("Building interface...", 70)
            assert splash._progress >= 69.0, splash._progress
        finally:
            splash.close()


# ---------------------------------------------------------------------------
# 5. Startup responsiveness
# ---------------------------------------------------------------------------

class TestStartupResponsiveness:
    def test_ffmpeg_probe_does_not_block_construction(
            self, qapp, monkeypatch, no_modals):
        """MainWindow() ran the ffmpeg -version probe synchronously, so a
        slow or hung tool install froze the whole app before first paint."""
        import time as _time
        from frameguard import gui as G

        calls = []

        def _slow():
            calls.append(1)
            _time.sleep(1.5)
            return True, "ok"

        monkeypatch.setattr(G, "check_ffmpeg_available", _slow)

        # A full-suite run reaches this test with dozens of earlier windows
        # merely close()d, never destroyed, and construction cost scales
        # with the live widget count (the theme pass restyles every widget
        # in the process). That accumulation measured 16s here while the
        # probe was never called. Destroy leftovers so this times a clean
        # process, i.e. a real app start. processEvents() alone does not
        # deliver DeferredDelete events; sendPostedEvents does.
        from PySide6.QtCore import QCoreApplication, QEvent
        for leftover in list(qapp.topLevelWidgets()):
            leftover.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

        started = _time.time()
        w = G.MainWindow()
        try:
            # The invariant under test is DEFERRAL: the probe must not run
            # during construction at all. Assert that directly (a call
            # count) instead of only a wall-clock bound: a tight bound
            # passes or fails with the machine's speed and load, not with
            # the code — it failed on a loaded VM where construction alone
            # took over a second with the probe never called. A generous
            # bound stays as a coarse backstop.
            assert calls == [], "ffmpeg probe ran during window construction"
            assert _time.time() - started < 5.0
        finally:
            w._cleanup_worker()
            if getattr(w, "_watch_monitor", None) is not None:
                w._watch_monitor.stop()
            w.hide()
            w.deleteLater()
        qapp.processEvents()
