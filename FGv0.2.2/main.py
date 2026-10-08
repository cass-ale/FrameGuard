#!/usr/bin/env python3
"""
FrameGuard Desktop -- Entry Point
Video QA for XR Deliverables

Launch the desktop GUI application.
"""

import sys
import os
import tempfile
import threading
import multiprocessing

# --- PyInstaller freeze support (MUST be called before any other imports) ---
multiprocessing.freeze_support()

# Ensure the package directory is on the path
if getattr(sys, 'frozen', False):
    # Running as PyInstaller bundle
    _app_dir = os.path.dirname(sys.executable)
    _internal = os.path.join(_app_dir, '_internal')
    if os.path.isdir(_internal):
        sys.path.insert(0, _internal)
    sys.path.insert(0, _app_dir)

    # Windows: Reset DLL search directory so external tools (FFmpeg etc.)
    # can find their own DLLs instead of PyInstaller's bundled ones.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _log_dir() -> str:
    """Writable directory for crash and error logs.

    Never write inside a macOS .app bundle: /Applications is not user-writable,
    and modifying bundle contents invalidates the code signature so Gatekeeper
    refuses subsequent launches.
    """
    if sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Logs", "FrameGuard")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "FrameGuard", "Logs")
    else:
        path = os.path.join(os.path.expanduser("~"), ".local", "share", "FrameGuard", "logs")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return tempfile.gettempdir()
    return path


def _show_error(msg: str):
    """Show an error message safely in both console and windowed mode."""
    # Try GUI messagebox first (works in --windowed mode)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "FrameGuard — Startup Error", msg)
        return
    except Exception:
        pass

    # Fallback: write to stderr or log file
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg + "\n")
        else:
            # --noconsole mode with no stderr — write to a log file
            with open(os.path.join(_log_dir(), "frameguard_error.log"), "a",
                      encoding="utf-8") as f:
                f.write(msg + "\n")
    except Exception:
        pass


try:
    from frameguard.gui import main
except ImportError as e:
    _show_error(
        f"[FrameGuard] Missing dependency: {e}\n\n"
        "Please install required packages:\n"
        "  pip install PySide6 fpdf2 psutil\n\n"
        "Or run the installer which bundles all dependencies."
    )
    sys.exit(1)

if __name__ == "__main__":
    # Create named mutex so the installer can detect a running instance
    # (matches AppMutex=FrameGuardDesktopApp in installer.iss)
    if sys.platform == "win32":
        try:
            import ctypes
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "FrameGuardDesktopApp")
        except Exception:
            pass

    # Global exception hook — prevents silent crashes in windowed mode
    def _global_excepthook(exc_type, exc_value, exc_tb):
        """Show error dialog, log locally, and auto-upload to shared CrashLogs folder."""
        import traceback
        from datetime import datetime as _dt
        
        timestamp = _dt.now()
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        
        # Build crash report content
        try:
            from frameguard import __version__
            ver = __version__
        except Exception:
            ver = "unknown"
        
        crash_report = (
            f"FrameGuard Desktop Crash Report\n"
            f"{'='*60}\n"
            f"Version:   {ver}\n"
            f"Timestamp: {timestamp.isoformat()}\n"
            f"Platform:  {sys.platform}\n"
            f"Python:    {sys.version.split()[0]}\n"
            f"Frozen:    {getattr(sys, 'frozen', False)}\n"
            f"{'='*60}\n\n"
            f"Exception: {exc_type.__name__}: {exc_value}\n\n"
            f"Traceback:\n{tb_text}\n"
        )
        
        # 1. Write to local log file
        try:
            with open(os.path.join(_log_dir(), "frameguard_crash.log"), "a",
                      encoding="utf-8") as f:
                f.write(crash_report)
        except Exception:
            pass

        # 2. Also drop a copy in the shared CrashLogs folder if one is synced
        #    locally, so the team sees failures without the user reporting them.
        try:
            crash_filename = f"crash_{timestamp.strftime('%Y%m%d_%H%M%S')}_{ver}.log"
            home = os.path.expanduser("~")

            # "MetaMate Follower" is the project's Drive folder name, not a
            # stray reference: <root>/CrashLogs is the shared collection point.
            shared_tail = os.path.join("MetaMate Follower", "CrashLogs")
            team_root = (
                "G:\\Shared drives\\TPG XR\\\u2b50gDrive Reorganization\u2b50\\"
                "Team - Innovation\\Q1\\T260340134 - NY QA Improvement\\"
                "03 Desktop App\\MetaMate Follower\\CrashLogs"
            )

            drive_paths = [
                os.path.join(home, "Downloads", "FGv0.2.2", "CrashLogs"),
                os.path.join(home, "Google Drive", "My Drive", shared_tail),
            ]
            if sys.platform == "win32":
                drive_paths.insert(0, team_root)
                drive_paths.append(os.path.join("G:\\My Drive", shared_tail))
            elif sys.platform == "darwin":
                import glob
                drive_paths.extend(glob.glob(os.path.join(
                    home, "Library", "CloudStorage", "GoogleDrive-*",
                    "My Drive", shared_tail)))

            for dp in drive_paths:
                if os.path.isdir(dp):
                    with open(os.path.join(dp, crash_filename), "w", encoding="utf-8") as f:
                        f.write(crash_report)
                    break
            else:
                # Fall back to the same private log dir. Deliberately NOT
                # ~/Documents: on macOS that triggers a TCC consent dialog,
                # and prompting from inside a crash handler is the worst
                # possible moment for it.
                with open(os.path.join(_log_dir(), crash_filename), "w",
                          encoding="utf-8") as f:
                    f.write(crash_report)
        except Exception:
            pass  # Don't let crash reporting crash the crash handler
        
        # 3. Show error dialog to user
        _show_error(
            f"[FrameGuard] Unexpected Error\n\n"
            f"{exc_type.__name__}: {exc_value}\n\n"
            f"A crash log has been saved locally and, where available, to "
            f"the team's shared CrashLogs folder for review."
        )

    sys.excepthook = _global_excepthook

    # sys.excepthook only fires for the MAIN thread. Scans run on a QThread and
    # segment work on a QThreadPool, so an exception there used to vanish with
    # no log and no dialog - exactly the crashes worth hearing about.
    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        _global_excepthook(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _thread_excepthook

    # Exceptions Python could not propagate (e.g. inside __del__ during
    # interpreter shutdown, or a failing C-level callback).
    def _unraisable_hook(unraisable):
        _global_excepthook(unraisable.exc_type, unraisable.exc_value,
                           unraisable.exc_traceback)

    sys.unraisablehook = _unraisable_hook

    # Native crashes (segfault in a Qt or FFmpeg binding) bypass Python
    # entirely. faulthandler at least leaves a C-level traceback behind.
    try:
        import faulthandler
        _fault_log = open(os.path.join(_log_dir(), "frameguard_fault.log"),
                          "a", encoding="utf-8")
        faulthandler.enable(file=_fault_log)
    except Exception:
        pass

    # --- Headless build-verification flags (used by the build scripts) ---
    if "--version" in sys.argv:
        from frameguard import __version__
        print(f"FrameGuard Desktop {__version__}")
        sys.exit(0)

    if "--selftest" in sys.argv:
        # Proves the frozen bundle can import every module, construct the UI,
        # and locate FFmpeg — then exits. Far more meaningful than launching
        # the GUI and checking the process is still alive, which passes even
        # when the app is stuck behind a modal error dialog.
        import os as _os
        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        failures = []
        try:
            from PySide6.QtWidgets import QApplication
            _app = QApplication.instance() or QApplication([])

            from frameguard import __version__
            from frameguard.core import DEFAULT_CONFIG, check_ffmpeg_available
            from frameguard.presets import list_available_presets
            from frameguard.gui import MainWindow
            from frameguard.tools import get_ffmpeg_path, get_ffprobe_path

            print(f"  version        : {__version__}")
            print(f"  config keys    : {len(DEFAULT_CONFIG)}")

            presets = list_available_presets()
            print(f"  bundled presets: {len(presets)}")
            if not presets:
                failures.append("no presets were bundled")

            ffmpeg, ffprobe = get_ffmpeg_path(), get_ffprobe_path()
            print(f"  ffmpeg         : {ffmpeg or 'NOT FOUND'}")
            print(f"  ffprobe        : {ffprobe or 'NOT FOUND'}")
            ok, msg = check_ffmpeg_available()
            if not ok:
                failures.append(f"ffmpeg unavailable: {msg}")

            MainWindow()
            print("  main window    : constructed")
        except Exception as e:
            import traceback
            traceback.print_exc()
            failures.append(f"{type(e).__name__}: {e}")

        if failures:
            print("\nSELFTEST FAILED:")
            for f in failures:
                print(f"  - {f}")
            sys.exit(1)
        print("\nSELFTEST PASSED")
        sys.exit(0)

    # Check for --dev flag (hot-reload mode for UI development)
    dev_mode = "--dev" in sys.argv
    if dev_mode:
        print("[DEV MODE] Hot-reload enabled - theme.py changes will apply live")
    main(dev_mode=dev_mode)

