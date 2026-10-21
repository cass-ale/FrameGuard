"""Watch folder tests.

An audit found 11 bugs in this feature and no test file covering it. These
exercise the failure modes that actually matter for unattended operation:
partial writes being scanned mid-copy, batches that never settle, folders that
vanish, and duplicate emissions.
"""

import os
import stat
from types import SimpleNamespace
from unittest.mock import mock_open

import pytest
from frameguard import watchfolder as W
from frameguard.watchfolder import WatchFolderMonitor


_REAL_CAN_OPEN_EXCLUSIVELY = W._can_open_exclusively


@pytest.fixture(autouse=True)
def _stable_test_environment(qapp, monkeypatch):
    """Keep unit tests independent of transient host file locks.

    Successful-flow tests exercise watch-folder state transitions, not Defender
    or indexer timing. Tests for lock handling override this deterministic
    default, while TestExclusiveOpenProbe calls the saved real implementation.
    """
    monkeypatch.setattr(W, "_can_open_exclusively", lambda _path: True)
    # State-transition tests were written against a two-poll window; the
    # shipped window (three polls, ten seconds) is pinned separately in
    # TestStableWindow.
    monkeypatch.setattr(W, "STABLE_POLLS_REQUIRED", 2)
    return qapp


def write_video(folder, name, size_kb=200):
    """Create a plausible video file of a given size."""
    path = os.path.join(str(folder), name)
    with open(path, "wb") as f:
        f.write(b"\x00" * (size_kb * 1024))
    return path


def mark_video_settled(path, age_seconds=10):
    """Give a fixture an explicit age instead of trusting host clock granularity."""
    settled_mtime = W.time.time() - age_seconds
    os.utime(path, (settled_mtime, settled_mtime))
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


class TestExclusiveOpenProbe:
    def test_reports_available_when_host_operations_succeed(self, monkeypatch):
        if os.name == "nt":
            calls = []
            monkeypatch.setattr(W.os, "open", lambda _path, _flags: 41)
            monkeypatch.setattr(
                W.os, "close", lambda handle: calls.append(("close", handle)))
            monkeypatch.setattr(
                W.os, "rename",
                lambda src, dst: calls.append(("rename", src, dst)))

            assert _REAL_CAN_OPEN_EXCLUSIVELY("clip.mp4")
            assert calls == [
                ("close", 41),
                ("rename", "clip.mp4", "clip.mp4"),
            ]
        else:
            opened = mock_open()
            monkeypatch.setattr("builtins.open", opened)

            assert _REAL_CAN_OPEN_EXCLUSIVELY("clip.mp4")
            opened.assert_called_once_with("clip.mp4", "rb")

    def test_reports_locked_when_host_refuses_access(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise PermissionError("simulated sharing violation")

        if os.name == "nt":
            monkeypatch.setattr(W.os, "open", lambda _path, _flags: 41)
            monkeypatch.setattr(W.os, "close", lambda _handle: None)
            monkeypatch.setattr(W.os, "rename", refuse)
        else:
            monkeypatch.setattr("builtins.open", refuse)

        assert not _REAL_CAN_OPEN_EXCLUSIVELY("clip.mp4")

    @pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
    def test_windows_open_denial_reports_locked(self, monkeypatch):
        def refuse_open(*_args, **_kwargs):
            raise PermissionError("simulated open denial")

        monkeypatch.setattr(W.os, "open", refuse_open)
        assert not _REAL_CAN_OPEN_EXCLUSIVELY("clip.mp4")


class TestManualCompleteness:
    def test_requires_a_file_older_than_the_stability_window(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 3600.0)
        path = write_video(tmp_path, "clip.mp4")

        assert not W._looks_complete(path)
        mark_video_settled(path, age_seconds=7200)
        assert W._looks_complete(path)

    def test_requires_the_exclusive_open_probe(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        path = mark_video_settled(write_video(tmp_path, "clip.mp4"))
        monkeypatch.setattr(W, "_can_open_exclusively", lambda _path: False)

        assert not W._looks_complete(path)


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

    def test_a_finished_file_is_eventually_emitted(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        path = write_video(tmp_path, "done.mp4")
        poll_until_stable(m)
        m._emit_batch()
        assert emitted == [path]

    def test_same_size_rewrite_resets_stability(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        path = write_video(tmp_path, "rewrite.mp4")
        m._poll()
        m._poll()
        assert m._pending_files[path]["stable_polls"] == 1

        before = os.stat(path)
        with open(path, "r+b") as stream:
            stream.write(b"changed without changing the file length")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        m._poll()

        assert m._pending_files[path]["stable_polls"] == 0
        m._poll()
        assert path not in m._batch_queue
        m._poll()
        assert path in m._batch_queue

    def test_atomic_same_size_replacement_resets_stability(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        path = write_video(tmp_path, "replace.mp4")
        m._poll()
        m._poll()

        replacement = write_video(tmp_path, "replacement.mp4")
        os.replace(replacement, path)
        m._poll()

        assert m._pending_files[path]["stable_polls"] == 0
        m._poll()
        assert path not in m._batch_queue
        m._poll()
        assert path in m._batch_queue

    def test_coarse_timestamp_fallback_remains_part_of_identity(self, monkeypatch):
        values = [
            SimpleNamespace(st_mode=stat.S_IFREG, st_size=204800,
                            st_mtime=10.0, st_ctime=20.0, st_dev=1, st_ino=2),
            SimpleNamespace(st_mode=stat.S_IFREG, st_size=204800,
                            st_mtime=11.0, st_ctime=20.0, st_dev=1, st_ino=2),
        ]
        monkeypatch.setattr(W.os, "stat", lambda *_args, **_kwargs: values.pop(0))

        first = W._get_file_identity_safe("clip.mp4")
        second = W._get_file_identity_safe("clip.mp4")

        assert first != second
        assert first[1] == 10_000_000_000
        assert second[1] == 11_000_000_000

    def test_locked_same_size_rewrite_must_settle_after_unlock(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        locked = {"value": True}
        monkeypatch.setattr(W, "_can_open_exclusively", lambda _path: not locked["value"])
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        path = write_video(tmp_path, "locked.mp4")
        poll_until_stable(m)
        assert path in m._pending_files

        before = os.stat(path)
        with open(path, "r+b") as stream:
            stream.write(b"same-size locked rewrite")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        locked["value"] = False
        m._poll()
        assert m._pending_files[path]["stable_polls"] == 0
        m._poll()
        assert path not in m._batch_queue
        m._poll()
        assert path in m._batch_queue

    def test_replacement_of_emitted_path_is_new_work(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        path = write_video(tmp_path, "reused.mp4")
        poll_until_stable(m, 3)
        m._emit_batch()
        assert emitted == [path]

        replacement = write_video(tmp_path, "new.mp4")
        os.replace(replacement, path)
        poll_until_stable(m, 3)
        m._emit_batch()

        assert emitted == [path, path]

    def test_batch_revalidates_identity_before_emission(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        path = write_video(tmp_path, "queued.mp4")
        poll_until_stable(m, 3)
        assert path in m._batch_queue

        replacement = write_video(tmp_path, "replacement.mp4")
        os.replace(replacement, path)
        m._emit_batch()

        assert emitted == []
        assert path in m._pending_files

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


class TestSymlinkPolicy:
    def test_file_symlinks_are_ignored_inside_watch_root(self, tmp_path):
        watched = tmp_path / "watched"
        watched.mkdir()
        external = write_video(tmp_path, "external.mp4")
        internal = write_video(watched, "internal.mp4")
        external_link = watched / "external-link.mp4"
        internal_link = watched / "internal-link.mp4"
        broken_link = watched / "broken.mp4"
        try:
            external_link.symlink_to(external)
            internal_link.symlink_to(internal)
            broken_link.symlink_to(watched / "missing.mp4")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are unavailable on this host")

        m = WatchFolderMonitor()
        m.set_watch_path(str(watched))
        found = m._scan_directory()

        assert internal in found
        assert str(external_link) not in found
        assert str(internal_link) not in found
        assert str(broken_link) not in found

    def test_directory_symlinks_and_symlink_roots_are_ignored(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        write_video(target, "inside.mp4")
        linked_dir = tmp_path / "linked.mkv"
        root_link = tmp_path / "watch-root"
        try:
            linked_dir.symlink_to(target, target_is_directory=True)
            root_link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("directory symlinks are unavailable on this host")

        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        assert str(linked_dir) not in m._scan_directory()

        invalid = WatchFolderMonitor()
        invalid.set_watch_path(str(root_link))
        assert invalid.watch_path is None


class TestBatching:
    def test_a_steady_trickle_cannot_starve_the_batch(
            self, tmp_path, monkeypatch):
        """BATCH_SETTLE_MS restarts on every arrival; without a ceiling an
        ingest that drops a file every few seconds never scans anything."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "MAX_BATCH_AGE_MS", 0)   # force the overdue path
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        path = write_video(tmp_path, "a.mp4")
        poll_until_stable(m)
        assert emitted == [path], "batch never flushed despite exceeding max age"

    def test_batch_flushes_once_it_gets_large(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "MAX_BATCH_SIZE", 3)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        paths = [write_video(tmp_path, f"clip{i}.mp4") for i in range(5)]
        poll_until_stable(m)
        assert len(emitted) == len(paths), \
            f"only {len(emitted)} emitted; ceiling did not fire"
        assert set(emitted) == set(paths)

    def test_batch_age_starts_with_the_first_file(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        path = write_video(tmp_path, "first.mp4")
        poll_until_stable(m, 4)
        assert m._batch_queue == [path]
        assert m._batch_started_at > 0


class TestDuplicates:
    def test_sync_does_not_double_emit_a_queued_file(
            self, tmp_path, monkeypatch):
        """sync() checked _emitted_files but not _batch_queue, so syncing
        during the settle window scanned the same video twice."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        path = write_video(tmp_path, "clip.mp4")
        poll_until_stable(m)          # lands in _batch_queue
        m.sync()                      # user presses Sync mid-settle
        m._emit_batch()               # settle timer fires

        assert emitted == [path], f"duplicate or missing emission: {emitted}"

    def test_existing_files_are_not_scanned_on_start(self, tmp_path):
        write_video(tmp_path, "already_here.mp4")
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))
        poll_until_stable(m)
        assert emitted == [], "pre-existing files should not auto-scan"

    def test_scan_backlog_opts_into_existing_files(
            self, tmp_path, monkeypatch):
        """Users who point the watcher at a folder with content expect a way
        to process it."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        paths = [
            mark_video_settled(write_video(tmp_path, "old1.mp4")),
            mark_video_settled(write_video(tmp_path, "old2.mp4")),
        ]
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))

        count = m.scan_backlog()
        assert count == len(paths)
        assert emitted == sorted(paths)

    def test_scan_backlog_defers_then_emits_a_locked_file(
            self, tmp_path, monkeypatch):
        """A refused file waits in the poller, then emits after its lock clears."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "BATCH_SETTLE_MS", 10)
        lock_state = {"held": True}
        monkeypatch.setattr(
            W, "_can_open_exclusively",
            lambda f: not (lock_state["held"]
                           and str(f).endswith("locked.mp4")))
        free = mark_video_settled(write_video(tmp_path, "free.mp4"))
        locked = mark_video_settled(write_video(tmp_path, "locked.mp4"))
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))

        assert m.scan_backlog() == 1
        assert emitted == [free]
        assert locked in m._pending_files

        # Repeated polling must not advance the file while the probe refuses it.
        poll_until_stable(m)
        assert emitted == [free]
        assert locked in m._pending_files
        assert locked not in m._batch_queue

        lock_state["held"] = False
        poll_until_stable(m)
        assert locked not in m._pending_files
        assert locked in m._batch_queue
        assert m._settle_timer.isActive()

        # Timer delivery is already covered separately. Drive the proven queued
        # batch directly so saturated CI event loops cannot turn this state
        # transition test into a scheduler race.
        m._settle_timer.stop()
        m._emit_batch()
        assert emitted == [free, locked]
        assert not m._settle_timer.isActive()

    def test_backlog_is_not_re_emitted_by_a_later_poll(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        path = mark_video_settled(write_video(tmp_path, "old.mp4"))
        m = WatchFolderMonitor()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))
        m.set_watch_path(str(tmp_path))
        m.scan_backlog()
        poll_until_stable(m)
        m._emit_batch()
        assert emitted == [path]


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

    def test_settled_batch_survives_folder_loss_and_remount(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        folder = tmp_path / "watched"
        offline = tmp_path / "offline"
        folder.mkdir()
        m = WatchFolderMonitor()
        m.set_watch_path(str(folder))
        m.start()
        emitted = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        path = write_video(folder, "queued.mp4")
        poll_until_stable(m, 3)
        assert m._batch_queue == [path]

        folder.rename(offline)
        for _ in range(W.MAX_CONSECUTIVE_ERRORS):
            m._poll()
        assert not m.is_active
        assert m._batch_queue == [path]

        offline.rename(folder)
        m.start()
        m._emit_batch()

        assert emitted == [path]

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

    def test_a_settled_file_is_still_synced(
            self, tmp_path, monkeypatch):
        """The guard must not block the normal case."""
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.reset()
        emitted = []
        m.files_detected.connect(lambda f: emitted.extend(f))

        path = mark_video_settled(write_video(tmp_path, "done.mp4"))
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

    def test_stop_retains_the_pending_batch(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.start()
        path = write_video(tmp_path, "x.mp4")
        poll_until_stable(m, 3)
        assert m._batch_queue == [path]
        m.stop()
        assert m._batch_queue == [path]

    def test_stop_start_does_not_silently_lose_a_queued_file(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.start()
        emitted = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        path = write_video(tmp_path, "x.mp4")
        poll_until_stable(m, 3)

        m.stop()
        m.start()
        m._emit_batch()

        assert emitted == [path]
        assert path in m._emitted_files

    def test_path_change_flushes_settled_work(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        m = WatchFolderMonitor()
        m.set_watch_path(str(first))
        emitted = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        path = write_video(first, "x.mp4")
        poll_until_stable(m, 3)

        m.set_watch_path(str(second))

        assert emitted == [path]
        assert m.watch_path == str(second)

    def test_reset_flushes_settled_work_before_rebaseline(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        emitted = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        path = write_video(tmp_path, "x.mp4")
        poll_until_stable(m, 3)

        m.reset()

        assert emitted == [path]
        assert path in m._emitted_files

    def test_pause_resume_keeps_settled_batch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path))
        m.start()
        path = write_video(tmp_path, "x.mp4")
        poll_until_stable(m, 3)

        m.pause()
        assert m._batch_queue == [path]
        m.resume()
        assert m._batch_queue == [path]
        assert m._settle_timer.isActive()

    def test_large_backlog_reports_and_emits_every_file(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        paths = [
            mark_video_settled(write_video(tmp_path, f"clip-{index:03d}.mp4"))
            for index in range(55)
        ]
        m = WatchFolderMonitor()
        emitted = []
        statuses = []
        m.files_detected.connect(lambda files: emitted.extend(files))
        m.status_changed.connect(statuses.append)
        m.set_watch_path(str(tmp_path))

        assert m.scan_backlog() == len(paths)
        assert emitted == sorted(paths)
        assert statuses[-1] == f"Queued {len(paths)} existing file(s)"

    def test_default_watch_path_is_sensible(self):
        path = W.get_default_watch_path()
        assert path and os.path.isabs(path)
