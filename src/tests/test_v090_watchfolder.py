"""0.9.0 (D5): watch-folder subfolders and the finished-copying window."""

import os

import pytest

from frameguard import watchfolder as W


def _video(folder, name, size_kb=200):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(str(folder), name)
    with open(path, "wb") as handle:
        handle.write(b"\0" * (size_kb * 1024))
    return path


class TestWatchFolderSubfolders:
    def test_camera_card_layouts_are_found(self, qapp, tmp_path):
        gopro = _video(tmp_path / "DCIM" / "100GOPRO", "GX010001.mp4")
        sony = _video(tmp_path / "PRIVATE" / "M4ROOT" / "CLIP", "C0001.mp4")
        hidden = _video(tmp_path / ".Trashes" / "501", "old.mp4")
        system = _video(tmp_path / "$RECYCLE.BIN", "gone.mp4")
        monitor = W.WatchFolderMonitor()
        monitor._watch_path = str(tmp_path)
        found = monitor._scan_directory()
        assert {gopro, sony} <= found
        assert hidden not in found and system not in found

    def test_depth_limit_and_opt_out(self, qapp, tmp_path):
        deep = _video(tmp_path / "a" / "b" / "c" / "d" / "e", "deep.mp4")
        top = _video(tmp_path, "top.mp4")
        monitor = W.WatchFolderMonitor()
        monitor._watch_path = str(tmp_path)
        assert deep not in monitor._scan_directory()
        monitor._recursive = False
        assert monitor._scan_directory() == {top}

    def test_new_file_in_a_subfolder_is_detected(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "_can_open_exclusively", lambda _p: True)
        monitor = W.WatchFolderMonitor()
        monitor.set_watch_path(str(tmp_path))
        path = _video(tmp_path / "DCIM" / "101GOPRO", "GX020001.mp4")
        for _ in range(W.STABLE_POLLS_REQUIRED + 1):
            monitor._poll()
        assert path in monitor._batch_queue

    def test_enabling_recursion_does_not_queue_existing_files(self, qapp, tmp_path):
        old = _video(tmp_path / "sub", "old.mp4")
        monitor = W.WatchFolderMonitor()
        monitor.set_recursive(False)
        monitor.set_watch_path(str(tmp_path))
        monitor.set_recursive(True)
        assert old in monitor._known_files and old in monitor._emitted_files


class TestStableWindow:
    def test_shipped_window_is_longer_than_two_quick_polls(self):
        assert W.STABLE_POLLS_REQUIRED >= 3
        assert W.MIN_STABLE_SECONDS >= 10.0

    def test_a_file_needs_the_full_poll_count(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "_can_open_exclusively", lambda _p: True)
        monitor = W.WatchFolderMonitor()
        monitor.set_watch_path(str(tmp_path))
        path = _video(tmp_path, "clip.mp4")
        for _ in range(W.STABLE_POLLS_REQUIRED):
            monitor._poll()
        assert path not in monitor._batch_queue
        monitor._poll()
        assert path in monitor._batch_queue

    @pytest.mark.skipif(os.name != "nt", reason="exclusive-open probe is Windows-only")
    def test_windows_probe_refuses_a_file_open_elsewhere(self, tmp_path):
        path = _video(tmp_path, "busy.mp4")
        with open(path, "ab"):
            assert W._can_open_exclusively(path) is False
        assert W._can_open_exclusively(path) is True


class TestLockedFiles:
    """Review L6: a file held open elsewhere was polled every second, silently."""

    def test_lock_is_reported_once_and_polling_relaxes(self, qapp, tmp_path, monkeypatch):
        locked = {"value": True}
        monkeypatch.setattr(W, "MIN_STABLE_SECONDS", 0.0)
        monkeypatch.setattr(W, "_can_open_exclusively", lambda _p: not locked["value"])
        monitor = W.WatchFolderMonitor()
        monitor.set_watch_path(str(tmp_path))
        monitor.start()
        statuses, waiting = [], []
        monitor.status_changed.connect(statuses.append)
        monitor.file_waiting.connect(waiting.append)
        path = _video(tmp_path, "open_in_player.mp4")

        for _ in range(W.STABLE_POLLS_REQUIRED + 6):
            monitor._poll()

        assert waiting == [path]
        assert statuses.count("Waiting: open_in_player.mp4 is open in another app") == 1
        assert path not in monitor._batch_queue
        assert not monitor._in_burst_mode
        assert monitor._timer.interval() == W.DEFAULT_POLL_INTERVAL_MS

        locked["value"] = False
        monitor._poll()
        assert path in monitor._batch_queue
        assert any("released" in s for s in statuses)
        monitor.stop()
