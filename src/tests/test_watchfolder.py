"""Watch folder tests.

An audit found 11 bugs in this feature and no test file covering it. These
exercise the failure modes that actually matter for unattended operation:
partial writes being scanned mid-copy, batches that never settle, folders that
vanish, and duplicate emissions.
"""

import os

import pytest

from frameguard import watchfolder as W
from frameguard.watchfolder import WatchFolderMonitor


@pytest.fixture(autouse=True)
def _qt(qapp):
    """QTimer needs a QApplication to report isActive() correctly."""
    return qapp


def write_video(folder, name, size_kb=200):
    """Create a plausible video file of a given size."""
    path = os.path.join(str(folder), name)
    with open(path, "wb") as f:
        f.write(b"\x00" * (size_kb * 1024))
    return path


def poll_until_stable(monitor, times=6):
    """Drive enough polls for the stability window to elapse."""
    for _ in range(times):
        monitor._poll()


class TestFileFiltering:
    def test_only_engine_supported_containers_are_picked_up(self):
        """The watcher used to accept 17 extensions while the engine accepts 4,
        so a stray .avi was auto-scanned and auto-FAILED on Container Format."""
        assert W._is_video_file("/x/a.mp4")
        assert W._is_video_file("/x/a.mov")
        assert W._is_video_file("/x/a.mkv")
        assert W._is_video_file("/x/a.mxf")
        assert not W._is_video_file("/x/a.avi")
        assert not W._is_video_file("/x/a.flv")
        assert not W._is_video_file("/x/a.txt")

    def test_in_progress_download_artifacts_are_ignored(self):
        for name in ("clip.mp4.part", "clip.mp4.crdownload", "clip.mp4.tmp",
                     ".hidden.mp4", "._resourcefork.mov", "~$lock.mp4"):
            assert not W._is_video_file(f"/x/{name}"), name

    def test_extension_matching_is_case_insensitive(self):
        assert W._is_video_file("/x/CLIP.MOV")
        assert W._is_video_file("/x/Clip.Mp4")

    def test_unreadable_file_size_is_distinguishable_from_empty(self):
        """Returning 0 for both made an unreadable file look stable."""
        assert W._get_file_size_safe("/nonexistent/nope.mp4") == -1


class TestStability:
    def test_a_growing_file_is_not_scanned(self, tmp_path):
        """The core promise: a file still being written must never be scanned."""
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(emitted.append)

        path = write_video(tmp_path, "growing.mp4", 200)
        for _ in range(4):
            with open(path, "ab") as f:
                f.write(b"\x00" * (100 * 1024))   # still growing
            m._poll()

        assert emitted == [], "a file that is still growing was emitted"

    def test_one_unchanged_poll_is_not_enough(self, tmp_path):
        """A network copy that stalls for a second used to be declared done."""
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(emitted.append)

        write_video(tmp_path, "stalled.mp4")
        m._poll()   # first sighting
        m._poll()   # size unchanged once
        assert emitted == [], "emitted after a single unchanged poll"

    def test_a_finished_file_is_eventually_emitted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        path = write_video(tmp_path, "done.mp4")
        poll_until_stable(m)
        m._emit_batch()
        assert path in emitted

    def test_tiny_files_do_not_pin_burst_mode_forever(self, tmp_path, monkeypatch):
        """A sub-100KB file was re-detected every poll, permanently holding the
        app at 1Hz full-directory scans and making set_poll_interval a no-op."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        write_video(tmp_path, "tiny.mp4", size_kb=1)
        for _ in range(8):
            m._poll()

        # Retired, not endlessly rediscovered.
        assert m._pending_files == {}
        assert not m._in_burst_mode, "burst mode never exits, pinning polling at 1Hz"

    def test_a_retired_tiny_file_is_never_scanned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        write_video(tmp_path, "tiny.mp4", size_kb=1)
        for _ in range(8):
            m._poll()
        m._emit_batch()
        assert emitted == []


class TestBatching:
    def test_a_steady_trickle_cannot_starve_the_batch(self, tmp_path, monkeypatch):
        """BATCH_SETTLE_MS restarts on every arrival; without a ceiling an
        ingest that drops a file every few seconds never scans anything."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "MAX_BATCH_AGE_MS", 0)   # force the overdue path
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        write_video(tmp_path, "a.mp4")
        poll_until_stable(m)
        assert emitted, "batch never flushed despite exceeding max age"

    def test_batch_flushes_once_it_gets_large(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "MAX_BATCH_SIZE", 3)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        for i in range(5):
            write_video(tmp_path, f"clip{i}.mp4")
        poll_until_stable(m)
        assert len(emitted) >= 3, f"only {len(emitted)} emitted; ceiling did not fire"

    def test_batch_age_starts_with_the_first_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        write_video(tmp_path, "first.mp4")
        poll_until_stable(m, 4)
        if m._batch_queue:
            assert m._batch_started_at > 0


class TestDuplicates:
    def test_sync_does_not_double_emit_a_queued_file(self, tmp_path, monkeypatch):
        """sync() checked _emitted_files but not _batch_queue, so syncing
        during the settle window scanned the same video twice."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        write_video(tmp_path, "clip.mp4")
        poll_until_stable(m)          # lands in _batch_queue
        m.sync()                      # user presses Sync mid-settle
        m._emit_batch()               # settle timer fires

        assert len(emitted) == len(set(emitted)), f"duplicate emission: {emitted}"

    def test_existing_files_are_not_scanned_on_start(self, tmp_path):
        write_video(tmp_path, "already_here.mp4")
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))
        poll_until_stable(m)
        assert emitted == [], "pre-existing files should not auto-scan"

    def test_scan_backlog_opts_into_existing_files(self, tmp_path, monkeypatch):
        """Users who point the watcher at a folder with content expect a way
        to process it."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        # The exclusivity probe is environmental on Windows: it renames the
        # file onto itself, which any transient holder (Defender scanning a
        # just-written file, the indexer) refuses without delete-sharing.
        # That made this test fail intermittently on Windows CI with one of
        # two identical files deferred. The probe has its own contract test
        # below; here it is pinned open so the backlog behaviour itself is
        # what is under test.
        monkeypatch.setattr(W, "_can_open_exclusively", lambda f: True)
        write_video(tmp_path, "old1.mp4")
        write_video(tmp_path, "old2.mp4")
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))

        count = m.scan_backlog()
        assert count == 2
        assert len(emitted) == 2

    def test_scan_backlog_defers_a_locked_file(self, tmp_path, monkeypatch):
        """A file the exclusivity probe refuses is deferred to the poller,
        not emitted and not lost — the behaviour that fired on Windows CI
        when a freshly written file was transiently held."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(
            W, "_can_open_exclusively",
            lambda f: not str(f).endswith("locked.mp4"))
        write_video(tmp_path, "free.mp4")
        locked = write_video(tmp_path, "locked.mp4")
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))

        assert m.scan_backlog() == 1
        assert len(emitted) == 1
        assert locked in m._pending_files

    def test_backlog_is_not_re_emitted_by_a_later_poll(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        write_video(tmp_path, "old.mp4")
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))
        m.scan_backlog()
        poll_until_stable(m)
        m._emit_batch()
        assert len(emitted) == 1


class TestFolderLifecycle:
    def test_a_deleted_folder_reports_and_stops(self, tmp_path):
        """This used to return silently while the UI claimed 'Active'."""
        folder = tmp_path / "watched"
        folder.mkdir()
        m = WatchFolderMonitor()
        m.set_watch_path(str(folder))
        m.start()

        errors, lost = [], []
        m.error.connect(errors.append)
        m.folder_lost.connect(lost.append)

        folder.rmdir()
        for _ in range(W.MAX_CONSECUTIVE_ERRORS + 1):
            m._poll()

        assert errors, "vanishing folder produced no error"
        assert lost, "watching was not stopped after repeated failures"
        assert not m.is_active

    def test_the_error_is_reported_once_not_every_poll(self, tmp_path):
        """It used to emit on every poll - several times a second."""
        folder = tmp_path / "gone"
        folder.mkdir()
        m = WatchFolderMonitor()
        m.set_watch_path(str(folder))
        m.start()
        errors = []
        m.error.connect(errors.append)

        folder.rmdir()
        for _ in range(4):
            m._poll()

        assert len(errors) == 1, f"error repeated {len(errors)} times"

    def test_scan_directory_returns_none_rather_than_partial_results(self, tmp_path):
        m = WatchFolderMonitor()
        m._watch_path = str(tmp_path / "does_not_exist")
        assert m._scan_directory() is None

    def test_reset_rebaselines_without_scanning(self, tmp_path):
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        write_video(tmp_path, "new.mp4")
        m.reset()
        poll_until_stable(m)
        m._emit_batch()
        assert emitted == []


class TestMidCopyProtection:
    """Sync and backlog used to gate on file size alone.

    Both paths also mark what they emit as emitted, so a clip scanned while
    still copying produced a bogus FAIL that could never be re-scanned: the
    poller skipped it (known), and Sync skipped it (emitted).
    """

    def test_sync_does_not_emit_a_file_that_is_still_being_written(self, tmp_path):
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.reset()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        write_video(tmp_path, "copying.mp4")   # just written == still copying
        m.sync()

        assert emitted == [], "sync scanned a file that was still being written"
        assert not m._emitted_files, "an in-progress file was marked done forever"

    def test_sync_hands_an_in_progress_file_to_the_poller(self, tmp_path):
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.reset()
        path = write_video(tmp_path, "copying.mp4")
        m.sync()
        assert path in m._pending_files, "file was neither scanned nor tracked"

    def test_backlog_does_not_emit_a_file_that_is_still_being_written(self, tmp_path):
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        write_video(tmp_path, "copying.mp4")
        assert m.scan_backlog() == 0
        assert emitted == []

    def test_a_settled_file_is_still_synced(self, tmp_path, monkeypatch):
        """The guard must not block the normal case."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.reset()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        path = write_video(tmp_path, "done.mp4")
        m.sync()
        assert emitted == [path]

    def test_unreadable_folder_is_not_reported_as_empty(self, tmp_path, monkeypatch):
        """_scan_directory returns None for 'cannot read' and set() for
        'empty'; scan_backlog treated both as empty, so a dropped share was
        reported to the operator as a folder with no files in it."""
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        monkeypatch.setattr(m, "_scan_directory", lambda: None)

        messages = []
        m.status_changed.connect(messages.append)
        assert m.scan_backlog() == 0
        assert any("could not read" in msg.lower() for msg in messages), messages


class TestErrorRecovery:
    def test_start_resets_the_error_counter(self, tmp_path):
        """Restarting after a dropped share began at the error ceiling, so the
        first hiccup stopped watching again immediately."""
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m._consecutive_errors = W.MAX_CONSECUTIVE_ERRORS
        m.start()
        assert m._consecutive_errors == 0


class TestMonitorState:
    def test_pause_and_resume(self, tmp_path):
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.start()
        assert m.is_active and not m.is_paused
        m.pause()
        assert m.is_paused and not m.is_polling
        m.resume()
        assert not m.is_paused and m.is_polling

    def test_stop_clears_the_pending_batch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.start()
        write_video(tmp_path, "x.mp4")
        poll_until_stable(m, 3)
        m.stop()
        assert m._batch_queue == []

    def test_stop_does_not_silently_lose_a_queued_file(self, tmp_path, monkeypatch):
        """Queued files were already in _known_files, so clearing the batch on
        stop left them marked as seen while never having been scanned.

        Note this only repairs the in-session bookkeeping. start() still
        re-baselines the folder by design (see
        test_existing_files_are_not_scanned_on_start), so recovering a file
        after a full restart is what "Scan files already in this folder" is
        for.
        """
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.start()
        path = write_video(tmp_path, "x.mp4")
        poll_until_stable(m, 3)
        assert path in m._batch_queue

        m.stop()
        assert path not in m._known_files, \
            "file was dropped from the batch but left marked as already seen"
        assert path not in m._emitted_files

        # The poller can therefore rediscover it without a restart.
        m._enabled = True
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        poll_until_stable(m, 3)
        m._emit_batch()
        assert path in emitted

    def test_default_watch_path_is_sensible(self):
        path = W.get_default_watch_path()
        assert path and os.path.isabs(path)
