"""
FrameGuard Desktop -- Watch Folder Monitor (Polling-based)

Monitors a directory for new video files and emits signals when they appear.
Uses QTimer-based polling instead of QFileSystemWatcher because:
- QFileSystemWatcher relies on OS-level inotify/ReadDirectoryChangesW
- These notifications DON'T FIRE on network/shared/virtual filesystems
- Google Drive, NFS, SMB, USB drives all fail with QFileSystemWatcher
- Polling works universally on any filesystem

Features:
- Batch collection: accumulates files during a settling window before emitting
- Burst detection: speeds up polling when new files are arriving
- Manual sync: button to re-scan and pick up missed files
- Robust stability check: files must be stable for 2 consecutive polls
"""

import os
import sys
import time
from pathlib import Path
from typing import Set, List, Optional, Dict

from PySide6.QtCore import QObject, Signal, QTimer


# Video file extensions to watch for
VIDEO_EXTENSIONS = {
    '.mp4', '.mov', '.avi', '.mkv', '.mxf', '.wmv',
    '.webm', '.m4v', '.mpg', '.mpeg', '.ts', '.m2ts',
    '.3gp', '.flv', '.f4v', '.vob', '.ogv',
}

# Extensions the scan engine actually accepts. Anything outside this set is a
# video, but auto-scanning it guarantees a red Container Format FAIL that the
# operator then has to triage — so the watcher ignores them by default.
SCANNABLE_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.mxf'}

# Ignore obvious in-progress artefacts from copy tools and NLEs outright.
TRANSIENT_SUFFIXES = ('.part', '.crdownload', '.tmp', '.download', '.filepart', '!qb')
TRANSIENT_PREFIXES = ('.', '~$', '._')

# Minimum file size to consider (avoids partial downloads/copies)
MIN_FILE_SIZE_BYTES = 100 * 1024  # 100 KB

# Polling intervals (milliseconds)
DEFAULT_POLL_INTERVAL_MS = 3000   # Normal: 3 seconds
BURST_POLL_INTERVAL_MS = 1000    # During burst: 1 second

# Batch collection: how long to wait after last new file before emitting (ms)
BATCH_SETTLE_MS = 4000  # 4 seconds after last new file detected

# Ceilings so a steady trickle of arrivals cannot postpone the batch forever.
# Without these, a card offload writing one clip every 3s never settles and
# nothing is scanned for the whole ingest.
MAX_BATCH_AGE_MS = 60_000   # flush at least once a minute
MAX_BATCH_SIZE = 50         # or once this many files are waiting

# A file must hold the same size across this many consecutive polls AND for at
# least this long in wall-clock terms before it is considered finished. One
# unchanged poll (the old behaviour) declares a stalled network copy complete.
STABLE_POLLS_REQUIRED = 2
MIN_STABLE_SECONDS = 3.0

# Stop hammering a folder that keeps failing (unmounted share, revoked rights).
MAX_CONSECUTIVE_ERRORS = 5


def get_default_watch_path() -> str:
    """Return a sensible default watch folder path.

    macOS uses ~/Movies, not ~/Videos, so the Windows-only check silently fell
    through to the Desktop on every Mac.
    """
    candidates = [Path.home() / "Movies"] if sys.platform == "darwin" else []
    candidates.append(Path.home() / "Videos")
    for path in candidates:
        if path.is_dir():
            return str(path)
    return str(Path.home() / "Desktop")


def _is_video_file(filepath: str, scannable_only: bool = True) -> bool:
    """True if the path looks like a video this app should pick up.

    os.path.splitext is used rather than pathlib because this runs against
    every directory entry on every poll; constructing a PurePath per entry on
    a 10,000-file folder is measurable UI jank.
    """
    name = os.path.basename(filepath)
    lowered = name.lower()
    if lowered.startswith(TRANSIENT_PREFIXES) or lowered.endswith(TRANSIENT_SUFFIXES):
        return False
    ext = os.path.splitext(lowered)[1]
    return ext in (SCANNABLE_EXTENSIONS if scannable_only else VIDEO_EXTENSIONS)


def _get_file_size_safe(filepath: str) -> int:
    """Get file size, returning -1 when it cannot be read.

    -1 rather than 0 so a permissions error is distinguishable from a genuinely
    empty file; treating both as 0 made unreadable files look 'stable'.
    """
    try:
        return os.path.getsize(filepath)
    except OSError:
        return -1


def _can_open_exclusively(filepath: str) -> bool:
    """Best-effort check that nothing else is still writing the file.

    On Windows the rename-onto-itself is the meaningful part: it is refused
    while another process holds the file without FILE_SHARE_DELETE. The plain
    open is NOT exclusive (CPython opens with full sharing), and on POSIX
    there is no equivalent guarantee at all, so this only ever supplements the
    size/age checks — it never replaces them.
    """
    try:
        if os.name == "nt":
            handle = os.open(filepath, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            os.close(handle)
            os.rename(filepath, filepath)
            return True
        with open(filepath, "rb"):
            return True
    except OSError:
        return False


def _looks_complete(filepath: str) -> bool:
    """True if a file is large enough and has been untouched long enough.

    Used by the manual Sync and backlog paths, which have no poll history to
    draw on. They previously gated on file size alone, so pressing Sync during
    a card offload scanned half-written clips — and because those paths also
    mark files as emitted, the resulting bogus FAIL was permanent: neither the
    poller nor a later Sync would ever look at the file again.
    """
    if _get_file_size_safe(filepath) < MIN_FILE_SIZE_BYTES:
        return False
    try:
        age = time.time() - os.path.getmtime(filepath)
    except OSError:
        return False
    return age >= MIN_STABLE_SECONDS and _can_open_exclusively(filepath)


class WatchFolderMonitor(QObject):
    """Polls a directory for new video files and emits signals.
    
    Uses QTimer instead of QFileSystemWatcher for universal filesystem support.
    Implements batch collection to properly handle multiple files dropped at once.
    
    Signals:
        files_detected(list): Emitted with list of new stable video file paths.
        status_changed(str): Emitted when monitor status changes.
        error(str): Emitted on errors.
    """
    
    files_detected = Signal(list)
    status_changed = Signal(str)
    error = Signal(str)
    folder_lost = Signal(str)   # watched folder became unreachable; watching stopped

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watch_path: Optional[str] = None
        self._known_files: Set[str] = set()       # All files we've seen (prevents re-detection)
        self._emitted_files: Set[str] = set()     # Files we've actually emitted for scanning
        # filepath -> {"size", "stable_polls", "first_seen", "last_change"}
        self._pending_files: Dict[str, dict] = {}
        self._batch_queue: List[str] = []          # Accumulated stable files waiting to emit
        self._batch_started_at: float = 0.0        # for MAX_BATCH_AGE_MS
        self._consecutive_errors = 0               # drives backoff / auto-stop
        self._enabled = False
        self._poll_interval = DEFAULT_POLL_INTERVAL_MS
        self._in_burst_mode = False
        
        # Main polling timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        
        # Batch settle timer: fires once after settling period to emit batch
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._emit_batch)
    
    @property
    def is_active(self) -> bool:
        """Whether monitoring is switched on (True even while paused).

        Deliberately NOT ``self._timer.isActive()``: pause() stops the timer,
        so a timer-based definition makes ``if is_active: resume()`` unreachable
        and auto-scan dies permanently after the first batch.
        """
        return self._enabled

    @property
    def is_paused(self) -> bool:
        """Enabled, but the poll timer is currently stopped."""
        return self._enabled and not self._timer.isActive()

    @property
    def is_polling(self) -> bool:
        """Whether the poll timer is actually running right now."""
        return self._timer.isActive()

    @property
    def watch_path(self) -> Optional[str]:
        """The currently monitored directory."""
        return self._watch_path

    def set_watch_path(self, path: str):
        """Set the directory to monitor."""
        if path and os.path.isdir(path):
            self._watch_path = path
            # Initialize known files (don't trigger on existing files)
            found = self._scan_directory()
            self._known_files = found if found is not None else set()
            self._emitted_files = set(self._known_files)  # Mark existing as "already handled"
            self._pending_files.clear()
            self._batch_queue.clear()
            self._batch_started_at = 0.0
            self._consecutive_errors = 0
            self.status_changed.emit(f"Watching: {path}")
        else:
            # Leaving the timer running against a None path would keep the UI
            # claiming "Watching:" while nothing is monitored.
            self._watch_path = None
            self._enabled = False
            self._timer.stop()
            self._settle_timer.stop()
            self.status_changed.emit("Invalid path")
    
    def start(self, path: str = None):
        """Start monitoring. Optionally set path first."""
        if path:
            self.set_watch_path(path)
        
        if not self._watch_path:
            self.error.emit("No watch folder configured")
            return
        if not os.path.isdir(self._watch_path):
            self.error.emit(f"Folder not found: {self._watch_path}")
            return
        
        self._enabled = True
        found = self._scan_directory()
        self._known_files = found if found is not None else set()
        self._emitted_files = set(self._known_files)
        self._pending_files.clear()
        self._batch_queue.clear()
        self._in_burst_mode = False
        # Without this, restarting after a share dropped out began already at
        # the error ceiling, so the first hiccup stopped watching again
        # immediately and the operator saw the toggle switch itself back off.
        self._consecutive_errors = 0
        self._timer.start(self._poll_interval)
        self.status_changed.emit(f"Active \u2014 monitoring {self._watch_path}")

    def stop(self):
        """Stop monitoring."""
        self._enabled = False
        self._timer.stop()
        self._settle_timer.stop()
        self._pending_files.clear()
        # Files sitting in the batch queue were already added to _known_files,
        # so simply dropping the queue meant the poller would never see them
        # again and they were silently never scanned. Un-know them so a later
        # sync or backlog scan can still find them.
        self._known_files.difference_update(self._batch_queue)
        self._batch_queue.clear()
        self._batch_started_at = 0.0
        self._in_burst_mode = False
        self.status_changed.emit("Stopped")
    
    def pause(self):
        """Temporarily pause monitoring (during active scan)."""
        self._timer.stop()
        self._settle_timer.stop()
        self.status_changed.emit("Paused (scan in progress)")
    
    def resume(self):
        """Resume monitoring after pause."""
        if self._enabled and self._watch_path:
            self._timer.start(self._poll_interval)
            self.status_changed.emit(f"Active \u2014 monitoring {self._watch_path}")
            # If there were pending batch items, restart settle timer
            if self._batch_queue:
                self._settle_timer.start(BATCH_SETTLE_MS)
    
    def set_poll_interval(self, ms: int):
        """Set polling interval in milliseconds (minimum 1000ms)."""
        self._poll_interval = max(1000, ms)
        if self._timer.isActive() and not self._in_burst_mode:
            self._timer.setInterval(self._poll_interval)
    
    def sync(self):
        """Manual sync: scan folder and emit any un-emitted video files.

        This picks up files that were missed during automatic detection
        (e.g., copied while monitor was paused, or arrived during a scan).

        Files still being written are NOT emitted. They are handed to the
        poller's pending set instead, so they are scanned once they settle
        rather than being scanned truncated and marked done forever.
        """
        if not self._watch_path or not os.path.isdir(self._watch_path):
            self.error.emit("No valid watch folder to sync")
            return

        current_files = self._scan_directory()
        if current_files is None:
            return

        # Find files that exist but haven't been emitted for scanning.
        # The batch queue must be consulted too: those files are already in
        # _known_files but not yet in _emitted_files, so syncing mid-settle
        # used to emit them here AND again when the settle timer fired,
        # scanning the same video twice.
        queued = set(self._batch_queue)
        candidates = [
            f for f in sorted(current_files)
            if f not in self._emitted_files and f not in queued
        ]
        missed_files = [f for f in candidates if _looks_complete(f)]
        still_writing = [f for f in candidates if f not in set(missed_files)]

        now = time.time()
        for f in still_writing:
            self._pending_files.setdefault(
                f, {"size": -1, "stable_polls": 0, "first_seen": now})

        if missed_files:
            # Mark them as known and emitted
            self._known_files.update(missed_files)
            self._emitted_files.update(missed_files)
            # Clear from pending if they were there
            for f in missed_files:
                self._pending_files.pop(f, None)
            # Emit immediately
            self.files_detected.emit(missed_files)
            note = (f"Sync: found {len(missed_files)} "
                    f"file{'s' if len(missed_files) != 1 else ''}")
            if still_writing:
                note += f" ({len(still_writing)} still copying)"
            self.status_changed.emit(note)
        elif still_writing:
            self.status_changed.emit(
                f"Sync: {len(still_writing)} file(s) still copying \u2014 "
                f"they will be scanned automatically once complete"
            )
        else:
            self.status_changed.emit("Sync: no new files found")
    
    def _scan_directory(self) -> Optional[Set[str]]:
        """Video files in the watch folder, or None if the folder can't be read.

        Returning None rather than a partial set matters: a scandir that throws
        part-way used to hand back whatever it had collected, which then pruned
        live stability state for every file it hadn't reached yet.
        """
        if not self._watch_path or not os.path.isdir(self._watch_path):
            return None

        files = set()
        try:
            with os.scandir(self._watch_path) as it:
                for entry in it:
                    try:
                        if entry.is_file() and _is_video_file(entry.path):
                            files.add(entry.path)
                    except OSError:
                        continue  # entry vanished mid-iteration
        except OSError as e:
            self._consecutive_errors += 1
            # Report once, not on every poll: this used to spam the status
            # label several times a second for as long as the fault persisted.
            if self._consecutive_errors == 1:
                self.error.emit(f"Cannot read the watch folder: {e}")
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.stop()
                self.folder_lost.emit(self._watch_path or "")
                self.status_changed.emit("Stopped \u2014 folder could not be read")
            return None

        return files
        return files
    
    def _enter_burst_mode(self):
        """Switch to faster polling to catch straggling files."""
        if not self._in_burst_mode:
            self._in_burst_mode = True
            if self._timer.isActive():
                self._timer.setInterval(BURST_POLL_INTERVAL_MS)
    
    def _exit_burst_mode(self):
        """Return to normal polling speed."""
        if self._in_burst_mode:
            self._in_burst_mode = False
            if self._timer.isActive():
                self._timer.setInterval(self._poll_interval)
    
    def _poll(self):
        """Poll the directory for changes. Called by QTimer."""
        if not self._watch_path:
            return

        # A folder that has been deleted, renamed or unmounted used to return
        # here silently, leaving the UI claiming "Active - monitoring ..."
        # forever while nothing was watched.
        if not os.path.isdir(self._watch_path):
            self._consecutive_errors += 1
            if self._consecutive_errors == 1:
                self.error.emit(
                    f"Watch folder is no longer reachable:\n{self._watch_path}")
                self.status_changed.emit("Folder unavailable \u2014 retrying")
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                self.stop()
                self.folder_lost.emit(self._watch_path)
                self.status_changed.emit("Stopped \u2014 folder unavailable")
            return

        current_files = self._scan_directory()
        if current_files is None:            # scandir failed; already reported
            return
        self._consecutive_errors = 0

        new_files = current_files - self._known_files

        # Drop stability state for files that vanished mid-write.
        self._pending_files = {
            f: state for f, state in self._pending_files.items()
            if f in current_files
        }

        if not new_files and not self._pending_files:
            self._exit_burst_mode()
            self._flush_if_batch_is_overdue()
            return

        if new_files:
            self._enter_burst_mode()

        # Every unseen file joins the pending set; stability is decided in one
        # place below rather than in two near-duplicate loops.
        now = time.time()
        for filepath in new_files:
            self._pending_files.setdefault(
                filepath, {"size": -1, "stable_polls": 0, "first_seen": now})

        newly_stable = []
        for filepath, state in list(self._pending_files.items()):
            size = _get_file_size_safe(filepath)
            if size < 0:
                continue  # unreadable right now; try again next poll

            if size != state["size"]:
                state["size"] = size
                state["stable_polls"] = 0
                state["last_change"] = now
                continue

            state["stable_polls"] += 1
            held_for = now - state.get("last_change", state["first_seen"])
            settled = (state["stable_polls"] >= STABLE_POLLS_REQUIRED
                       and held_for >= MIN_STABLE_SECONDS)

            if not settled:
                continue

            if size < MIN_FILE_SIZE_BYTES:
                # A finished file that is simply too small to be a deliverable.
                # It used to stay in _pending_files forever, which kept it in
                # `new_files` on every poll, re-entered burst mode every time,
                # and made set_poll_interval() a no-op for the rest of the
                # session. Retire it: known, so it stops being new; not
                # emitted, so it is never scanned.
                del self._pending_files[filepath]
                self._known_files.add(filepath)
                self._emitted_files.add(filepath)
                continue

            if _can_open_exclusively(filepath):
                newly_stable.append(filepath)
                del self._pending_files[filepath]

        if newly_stable:
            self._known_files.update(newly_stable)
            if not self._batch_queue:
                self._batch_started_at = now
            self._batch_queue.extend(newly_stable)
            self._settle_timer.stop()
            self._settle_timer.start(BATCH_SETTLE_MS)

        # Everything has settled: emit promptly rather than waiting out the
        # full window.
        if not self._pending_files and self._batch_queue:
            if self._settle_timer.isActive() and self._settle_timer.remainingTime() > 1500:
                self._settle_timer.stop()
                self._settle_timer.start(1000)

        self._flush_if_batch_is_overdue()

    def _flush_if_batch_is_overdue(self):
        """Emit the batch if it has grown too large or waited too long.

        BATCH_SETTLE_MS restarts on every arrival, so without a ceiling a
        steady trickle (a card offload, a render farm) postpones the batch
        indefinitely and nothing is scanned for the entire ingest.
        """
        if not self._batch_queue:
            return
        age_ms = (time.time() - self._batch_started_at) * 1000
        if len(self._batch_queue) >= MAX_BATCH_SIZE or age_ms >= MAX_BATCH_AGE_MS:
            self._settle_timer.stop()
            self._emit_batch()

    def _emit_batch(self):
        """Emit the collected batch of files."""
        self._exit_burst_mode()
        if not self._batch_queue:
            return
        files_to_emit = list(self._batch_queue)
        self._batch_queue.clear()
        self._batch_started_at = 0.0
        self._emitted_files.update(files_to_emit)
        self.files_detected.emit(files_to_emit)

    def reset(self):
        """Forget everything seen so far and re-baseline on the current folder.

        Next poll treats all current files as known, so nothing is re-scanned
        until genuinely new files arrive.
        """
        found = self._scan_directory()
        self._known_files = found if found is not None else set()
        self._emitted_files = set(self._known_files)
        self._pending_files.clear()
        self._batch_queue.clear()
        self._batch_started_at = 0.0
        self._consecutive_errors = 0
        self._settle_timer.stop()
        self._exit_burst_mode()
        self.status_changed.emit("Reset \u2014 all current files marked as known")

    def scan_backlog(self):
        """Queue every file already sitting in the folder.

        Watching deliberately ignores pre-existing files, which surprises
        people who point it at a folder that already has content. This is the
        explicit opt-in, so it queues what is there now regardless of the
        start-up baseline - that baseline is exactly what it exists to override.

        Files still being written are left for the poller, for the same reason
        as in sync(): emitting them here marks them done permanently.
        """
        found = self._scan_directory()
        if found is None:
            # None means the folder could not be read, which is not at all the
            # same as it being empty. Reporting "no files" for an unreachable
            # share told operators everything was fine.
            self.status_changed.emit("Could not read the watch folder")
            return 0
        if not found:
            self.status_changed.emit("No files found in the watch folder")
            return 0

        queued = set(self._batch_queue)
        candidates = sorted(f for f in found if f not in queued)
        backlog = [f for f in candidates if _looks_complete(f)]
        still_writing = [f for f in candidates if f not in set(backlog)]

        now = time.time()
        for f in still_writing:
            self._pending_files.setdefault(
                f, {"size": -1, "stable_polls": 0, "first_seen": now})

        if not backlog:
            if still_writing:
                self.status_changed.emit(
                    f"{len(still_writing)} file(s) still copying \u2014 they will be "
                    f"scanned automatically once complete"
                )
            else:
                self.status_changed.emit("No scannable files in the watch folder")
            return 0

        self._known_files.update(backlog)
        self._emitted_files.update(backlog)
        for f in backlog:
            self._pending_files.pop(f, None)
        self.status_changed.emit(f"Queued {len(backlog)} existing file(s)")
        self.files_detected.emit(backlog)
        return len(backlog)
