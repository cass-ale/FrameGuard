"""
FrameGuard Desktop -- Worker Thread for non-blocking QA scans.
Runs scans in a background thread and emits Qt signals for progress/results.

v0.3: Added strict mode, subprocess cleanup on cancel, psutil child process termination.
"""

import os
import threading
from typing import List, Optional
from PySide6.QtCore import QObject, Signal

from frameguard.core import (
    run_qa_checks, DEFAULT_CONFIG, PRESETS
)


class ScanWorker(QObject):
    """Background worker that runs QA checks and emits progress signals."""

    # PIDs of spawned subprocesses (ffmpeg/ffprobe) to kill on cancel.
    # Instance-scoped (not class-global): a class-global set leaks PIDs across
    # scans and risks terminate() hitting a recycled PID owned by another
    # process. core.py registers via the callbacks below.
    # Kept as a class attribute name for back-compat with tests that poke it.
    _active_pids: set[int] = set()
    _active_pids_lock = threading.Lock()

    # Signals
    scan_started = Signal(int)                    # total file count
    file_started = Signal(int, str)               # file_index, filename
    file_progress = Signal(int, str, float, float, str)  # file_index, phase, current, total, label
    file_finished = Signal(int, object)           # file_index, QAResult
    scan_finished = Signal(list)                  # list of QAResult
    scan_error = Signal(str)                      # error message
    scan_cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel_event = threading.Event()
        self._files: List[str] = []
        self._cfg: dict = DEFAULT_CONFIG.copy()
        self._preset: Optional[str] = None
        self._quick: bool = False
        self._naming: bool = False
        self._strict: bool = False

        # H5: configure() race protection
        self._state_lock = threading.Lock()
        self._is_running: bool = False
        # Per-scan PID set: instance-scoped so a stale PID from a previous
        # scan (possibly recycled by the OS) can never kill an unrelated
        # process. The class-level _active_pids is retained for back-compat
        # with the core.py registration callbacks, which cannot know which
        # worker instance owns a given ffmpeg invocation.
        self._own_pids: set[int] = set()
        self._own_pids_lock = threading.Lock()
        # Tracks live Popen handles when psutil is unavailable.
        self._own_procs: list = []
        self._own_procs_lock = threading.Lock()

    # H6: Allow external modules (e.g., core.py) to register PIDs to be terminated on cancel().
    @classmethod
    def register_pid(cls, pid: int) -> None:
        if not pid:
            return
        with cls._active_pids_lock:
            cls._active_pids.add(int(pid))

    @classmethod
    def unregister_pid(cls, pid: int) -> None:
        if not pid:
            return
        with cls._active_pids_lock:
            cls._active_pids.discard(int(pid))

    def register_proc(self, proc) -> None:
        """Track a live Popen handle for the psutil-missing fallback path."""
        if proc is None:
            return
        with self._own_procs_lock:
            self._own_procs.append(proc)

    def unregister_proc(self, proc) -> None:
        if proc is None:
            return
        with self._own_procs_lock:
            try:
                self._own_procs.remove(proc)
            except ValueError:
                pass

    def configure(self, files: List[str], preset: Optional[str] = None,
                  quick: bool = False, naming: bool = False,
                  strict: bool = False,
                  cfg_overrides: dict = None):
        """Configure the scan before starting."""
        # H5: Prevent configure while scan is running
        with self._state_lock:
            if self._is_running:
                raise RuntimeError("Cannot configure ScanWorker while a scan is running.")

        self._files = list(files)  # Defensive copy
        self._preset = preset
        self._quick = quick
        self._naming = naming
        self._strict = strict
        self._cfg = DEFAULT_CONFIG.copy()
        if cfg_overrides:
            unknown = set(cfg_overrides) - set(DEFAULT_CONFIG)
            if unknown:
                # A key the engine never reads is always a caller bug: the value
                # would be silently discarded and the scan would run on defaults.
                raise ValueError(
                    "Unknown scan config key(s): " + ", ".join(sorted(unknown))
                )
            self._cfg.update(cfg_overrides)
        if preset:
            if preset not in PRESETS:
                raise ValueError(f"Unknown scan preset: {preset!r}")
            p = PRESETS[preset]
            self._cfg["target_width"] = p["width"]
            self._cfg["target_height"] = p["height"]
            self._cfg["target_fps"] = p["fps"]
            self._cfg["fps_tolerance"] = p["fps_tolerance"]
            self._cfg["min_bitrate_mbps"] = p["min_bitrate_mbps"]
        self._cancel_event.clear()

    def cancel(self):
        """Request cancellation of the current scan and kill subprocesses."""
        self._cancel_event.set()

        # H6: Only terminate specific registered PIDs; do not kill all app children.
        # M4: After terminate(), wait with timeout, then escalate to kill() if still alive.
        try:
            import psutil

            with self.__class__._active_pids_lock:
                pids = list(self.__class__._active_pids)

            for pid in pids:
                try:
                    proc = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    with self.__class__._active_pids_lock:
                        self.__class__._active_pids.discard(pid)
                    continue

                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)  # M4: wait after terminate
                    except psutil.TimeoutExpired:
                        try:
                            proc.kill()
                            proc.wait(timeout=1.5)
                        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                            pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                finally:
                    with self.__class__._active_pids_lock:
                        self.__class__._active_pids.discard(pid)

        except ImportError:
            # psutil not available: kill tracked Popen handles directly so
            # cancel still stops a running ffmpeg instead of waiting up to
            # two hours for the segment to finish on its own. Consult both
            # the worker's own list and core's live-handle registry (segment
            # scans register there via _track_proc).
            with self._own_procs_lock:
                procs = list(self._own_procs)
            try:
                from frameguard.core import get_live_procs
                procs += [p for p in get_live_procs() if p not in procs]
            except Exception:
                pass
            for proc in procs:
                try:
                    proc.terminate()
                except Exception:
                    pass
            # Give them a beat, then escalate.
            import time as _time
            _time.sleep(0.5)
            with self._own_procs_lock:
                procs = list(self._own_procs)
            try:
                from frameguard.core import get_live_procs as _live
                procs += [p for p in _live() if p not in procs]
            except Exception:
                pass
            for proc in procs:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass

    def run(self):
        """Execute the scan. Called from QThread."""
        # H5: mark running state
        with self._state_lock:
            self._is_running = True

        try:
            total = len(self._files)
            if total == 0:
                self.scan_error.emit("No video files found.")
                return

            # Fail fast with a clear message instead of letting Popen(None)
            # raise TypeError mid-scan, which used to surface as a cryptic
            # per-file "Internal Error".
            try:
                from frameguard.tools import get_ffmpeg_path, get_ffprobe_path
                missing = [n for n, p in (("FFmpeg", get_ffmpeg_path()),
                                          ("FFprobe", get_ffprobe_path())) if not p]
            except Exception:
                missing = []
            if missing:
                self.scan_error.emit(
                    " and ".join(missing) + " not found. Reinstall or relink "
                    "the analysis tools, then start the scan again.")
                return

            self.scan_started.emit(total)
            results = []
            cancelled = False

            for idx, filepath in enumerate(self._files):
                if self._cancel_event.is_set():
                    # Completed files were already streamed via file_finished,
                    # so nothing is lost by emitting the single terminal
                    # scan_cancelled here instead of a partial scan_finished.
                    cancelled = True
                    break

                filename = os.path.basename(filepath)
                self.file_started.emit(idx, filename)

                def progress_cb(phase, current, total_dur, label,
                                _idx=idx):
                    self.file_progress.emit(_idx, phase, current, total_dur, label)

                result = run_qa_checks(
                    filepath,
                    cfg=self._cfg,
                    strict=self._strict,
                    quick=self._quick,
                    naming_check=self._naming,
                    progress_callback=progress_cb,
                    cancel_event=self._cancel_event
                )

                result.finalize()  # Cache overall_status as plain attribute

                if self._cancel_event.is_set():
                    cancelled = True
                    break

                results.append(result)
                self.file_finished.emit(idx, result)
                # Bound per-scan memory: the GUI caps rendering at
                # MAX_RETAINED_RESULTS, but the worker list itself used to
                # grow without bound on very large batches.
                if len(results) >= 2000:
                    result.set_metadata("batch_truncated", True)
                    break

            if cancelled:
                # Exactly ONE terminal signal. Emitting both scan_finished and
                # scan_cancelled used to double-run GUI cleanup, auto-export
                # and the watch-queue drain.
                self.scan_cancelled.emit()
            else:
                self.scan_finished.emit(results)

        except Exception as e:
            self.scan_error.emit(str(e))
        finally:
            # H5: clear running state
            with self._state_lock:
                self._is_running = False

# Register PID callbacks with core.py to avoid circular imports
try:
    from frameguard.core import set_pid_callbacks
    set_pid_callbacks(ScanWorker.register_pid, ScanWorker.unregister_pid)
except ImportError:
    pass



