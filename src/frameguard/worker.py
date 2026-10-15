"""
FrameGuard Desktop -- Worker Thread for non-blocking QA scans.
Runs scans in a background thread and emits Qt signals for progress/results.

v0.3: Added strict mode, subprocess cleanup on cancel, psutil child process termination.
"""

import os
import subprocess
import threading
from typing import List, Optional
from PySide6.QtCore import QObject, Signal

from frameguard.core import (
    run_qa_checks, DEFAULT_CONFIG, PRESETS
)


class ScanWorker(QObject):
    """Background worker that runs QA checks and emits progress signals."""

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
        # One ownership registry backs both psutil and direct-Popen cancel
        # paths. A PID alone is not ownership evidence because the OS may
        # recycle it after the child exits.
        self._own_pids: set[int] = set()
        self._own_pid_create_times: dict[int, float] = {}
        self._own_procs: list = []
        self._own_procs_lock = threading.Lock()

    def register_proc(self, proc) -> None:
        """Register a subprocess to this worker and no other worker."""
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        create_time = None
        if pid:
            try:
                import psutil
                create_time = psutil.Process(pid).create_time()
            except Exception:
                pass
        with self._own_procs_lock:
            cancelled = self._cancel_event.is_set()
            if not cancelled:
                if proc not in self._own_procs:
                    self._own_procs.append(proc)
                if pid:
                    self._own_pids.add(int(pid))
                    if create_time is not None:
                        self._own_pid_create_times[int(pid)] = create_time

        # Close the registration/cancel race: a process born after cancel's
        # snapshot must still be stopped immediately.
        if cancelled:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def unregister_proc(self, proc) -> None:
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        with self._own_procs_lock:
            try:
                self._own_procs.remove(proc)
            except ValueError:
                pass
            if pid:
                self._own_pids.discard(int(pid))
                self._own_pid_create_times.pop(int(pid), None)

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

        with self._own_procs_lock:
            owned = [
                (proc, self._own_pid_create_times.get(getattr(proc, "pid", None)))
                for proc in self._own_procs
            ]

        try:
            import psutil
        except ImportError:
            psutil = None
        if psutil is None:
            process_errors = (OSError, ValueError)
        else:
            process_errors = (
                psutil.NoSuchProcess, psutil.AccessDenied,
                psutil.TimeoutExpired, OSError, ValueError,
            )

        for popen_proc, expected_create_time in owned:
            try:
                # poll() reaps an exited child and prevents acting on a PID
                # that may already have been recycled.
                if popen_proc.poll() is not None:
                    continue
                if psutil is None:
                    popen_proc.terminate()
                    try:
                        popen_proc.wait(timeout=0.5)
                    except (subprocess.TimeoutExpired, AttributeError):
                        if popen_proc.poll() is None:
                            popen_proc.kill()
                    continue

                pid = int(popen_proc.pid)
                proc = psutil.Process(pid)
                if expected_create_time is not None:
                    try:
                        if proc.create_time() != expected_create_time:
                            continue
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except psutil.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.5)
            except process_errors:
                pass
            finally:
                self.unregister_proc(popen_proc)

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
                    cancel_event=self._cancel_event,
                    process_owner=self,
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



