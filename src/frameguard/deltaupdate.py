"""Differential updates: transfer only what changed, then swap on exit.

WHY THIS EXISTS
    A full build is ~90 MB, of which Python, Qt and FFmpeg are ~89 MB and
    change between releases almost never. Shipping the installer for a one-line
    fix makes every user download 90 MB to receive 200 KB of change.

HOW IT WORKS
    1. Read the release manifest (SHA-256 of every file in the new build).
    2. Hash the current installation and diff the two.
    3. Copy only the differing files into a staging directory.
    4. Verify every staged file against the manifest. Nothing in the
       installation has been touched at this point, so a failure here is free.
    5. Write a swap helper script, launch it detached, and quit.
    6. The helper waits for this process to exit, backs up the files it is
       about to overwrite, applies the change, and relaunches. If any step
       fails it restores the backup.

WHY A HELPER SCRIPT
    A running executable cannot replace itself on Windows - the file is locked
    for the lifetime of the process. The swap therefore has to be performed by
    something that outlives the app.

LAYOUT OF A PUBLISHED RELEASE
    <release>/manifest.json
    <release>/files/...          full tree; clients pull only what they lack
"""

import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from frameguard import manifest as M
from frameguard.updatesource import UpdateSourceError, make_source

STAGING_DIRNAME = "frameguard_update_staging"


def find_install_root() -> Optional[str]:
    """Directory that a differential update would modify.

    Returns None when running from a source checkout: there is no packaged
    installation to update, and overwriting a developer's working tree with a
    release payload would be destructive.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if sys.platform == "darwin":
        marker = f"{os.sep}Contents{os.sep}MacOS"
        if exe_dir.endswith(marker):
            return exe_dir[: -len(marker)]   # the .app bundle itself
    return exe_dir


class StageWorker(QObject):
    """Fetches and verifies the changed files off the GUI thread."""

    progress = Signal(int, str)     # percent, current file
    finished = Signal(str)          # staging directory
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, source, plan: M.UpdatePlan, remote: dict):
        super().__init__()
        self._source = source
        self._plan = plan
        self._remote = remote
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _abort(self, staging: str):
        """Report cancellation and leave nothing behind in %TEMP%."""
        shutil.rmtree(staging, ignore_errors=True)
        self.cancelled.emit()

    def run(self):
        staging = os.path.join(tempfile.gettempdir(), STAGING_DIRNAME)
        try:
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging, exist_ok=True)

            transfer = self._plan.transfer
            total = len(transfer) or 1

            for index, rel in enumerate(transfer, start=1):
                if self._cancelled:
                    self._abort(staging)
                    return
                dst = os.path.join(staging, rel.replace("/", os.sep))
                try:
                    self._source.fetch(rel, dst)
                except UpdateSourceError as e:
                    shutil.rmtree(staging, ignore_errors=True)
                    self.failed.emit(str(e))
                    return
                self.progress.emit(int(index * 100 / total), os.path.basename(rel))

            # Cancel was only tested at the top of each iteration, so pressing
            # it during the final (usually largest) file was ignored entirely:
            # the loop simply ended, verification ran, and the app closed and
            # updated itself anyway.
            if self._cancelled:
                self._abort(staging)
                return

            # Verify BEFORE anything in the installation is touched, so a bad
            # download costs nothing.
            self.progress.emit(100, "Verifying...")
            bad = M.verify_tree(staging, self._remote, transfer)
            if bad:
                shutil.rmtree(staging, ignore_errors=True)
                self.failed.emit(
                    "Downloaded files failed their integrity check:\n"
                    + "\n".join(bad[:5])
                    + (f"\n...and {len(bad) - 5} more" if len(bad) > 5 else ""))
                return

            if self._cancelled:
                self._abort(staging)
                return

            self.finished.emit(staging)
        except Exception as e:
            # Deliberately broad. Only OSError was handled, but HttpSource
            # raises http.client.HTTPException/IncompleteRead, which are not
            # OSError subclasses. Those escaped run(), no signal was ever
            # emitted, and the modal progress dialog hung at whatever percent
            # it had reached with no way out but Task Manager.
            shutil.rmtree(staging, ignore_errors=True)
            self.failed.emit(f"Could not prepare the update: {type(e).__name__}: {e}")


def _windows_swap_script(install: str, staging: str, plan, pid: int,
                         backup: str, entry: str) -> str:
    deletes = "\n".join(
        f'if exist "{os.path.join(install, rel.replace("/", os.sep))}" '
        f'del /f /q "{os.path.join(install, rel.replace("/", os.sep))}"'
        for rel in plan.delete)

    return f"""@echo off
rem Switch to UTF-8. Install paths can contain non-ASCII characters (an
rem accented username, a Drive folder with a symbol in it), and under the
rem default OEM codepage cmd would mangle them and the copy would fail.
chcp 65001 >nul
setlocal
title FrameGuard Update

rem Wait for FrameGuard (PID {pid}) to exit; a running exe is locked and
rem cannot be replaced.
set /a tries=0
:waitloop
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if errorlevel 1 goto proceed
set /a tries+=1
if %tries% GEQ 60 goto timeout
timeout /t 1 /nobreak >nul
goto waitloop

:timeout
echo FrameGuard did not exit; update aborted. Nothing was changed.
pause
exit /b 1

:proceed
rem Back up exactly the files about to be overwritten so a partial failure
rem can be undone.
if exist "{backup}" rmdir /s /q "{backup}"
mkdir "{backup}"
robocopy "{install}" "{backup}" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto backupfail

rem Apply the staged files.
robocopy "{staging}" "{install}" /E /IS /IT /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto rollback

{deletes}

rmdir /s /q "{backup}"
rmdir /s /q "{staging}"
start "" "{os.path.join(install, entry)}"
exit /b 0

:rollback
echo Update failed. Restoring the previous version...
robocopy "{backup}" "{install}" /E /IS /IT /NFL /NDL /NJH /NJS /NP >nul
start "" "{os.path.join(install, entry)}"
echo The previous version was restored.
pause
exit /b 1

:backupfail
echo Could not create a backup; update aborted. Nothing was changed.
pause
exit /b 1
"""


def _posix_swap_script(install: str, staging: str, plan, pid: int,
                       backup: str, entry: str) -> str:
    deletes = "\n".join(
        f'rm -f "{os.path.join(install, rel)}"' for rel in plan.delete)
    launch = (f'open "{install}"' if sys.platform == "darwin"
              else f'"{os.path.join(install, entry)}" &')

    return f"""#!/usr/bin/env bash
# FrameGuard differential update

for _ in $(seq 1 60); do
    kill -0 {pid} 2>/dev/null || break
    sleep 1
done
if kill -0 {pid} 2>/dev/null; then
    echo "FrameGuard did not exit; update aborted. Nothing was changed."
    exit 1
fi

rm -rf "{backup}"
mkdir -p "{backup}"
if ! cp -R "{install}/." "{backup}/"; then
    echo "Could not create a backup; update aborted."
    exit 1
fi

if cp -R "{staging}/." "{install}/"; then
{deletes}
    rm -rf "{backup}" "{staging}"
    # The swapped files break the bundle's code signature, so Gatekeeper
    # would refuse the next launch. Re-sign ad-hoc: enough for the app to
    # run, no cert needed. Inside-out (helpers first, bundle last): Apple
    # deprecated `codesign --deep`, which seals nested code unreliably.
    for _f in $(find "{install}/Contents/Resources/tools" -type f 2>/dev/null); do
        codesign --force --timestamp=none --sign - "$_f" 2>/dev/null || true
    done
    codesign --force --timestamp=none --sign - "{install}" 2>/dev/null || true
    {launch}
    exit 0
else
    echo "Update failed. Restoring the previous version..."
    rm -rf "{install}"
    mv "{backup}" "{install}"
    {launch}
    exit 1
fi
"""


def write_swap_script(install_root: str, staging: str, plan: M.UpdatePlan,
                      pid: Optional[int] = None) -> str:
    """Write the helper that performs the swap after this process exits."""
    pid = pid if pid is not None else os.getpid()
    backup = os.path.join(tempfile.gettempdir(), "frameguard_update_backup")
    entry = plan.entry_point or ("FrameGuard.exe" if os.name == "nt" else "FrameGuard")

    if os.name == "nt":
        path = os.path.join(tempfile.gettempdir(), "frameguard_apply_update.bat")
        body = _windows_swap_script(install_root, staging, plan, pid, backup, entry)
    else:
        path = os.path.join(tempfile.gettempdir(), "frameguard_apply_update.sh")
        body = _posix_swap_script(install_root, staging, plan, pid, backup, entry)

    with open(path, "w", encoding="utf-8", newline="\r\n" if os.name == "nt" else "\n") as f:
        f.write(body)
    if os.name != "nt":
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def launch_swap_script(script_path: str):
    """Start the helper detached so it survives this process exiting.

    CREATE_NEW_CONSOLE, not DETACHED_PROCESS: the script waits for this
    process to exit using `timeout /t 1`, and `timeout.exe` needs a console
    input handle. With no console it failed instantly, so the intended
    60-second grace period collapsed into a couple of seconds, the script
    gave up while Qt was still shutting down, and the user saw FrameGuard
    close and never reopen with no update applied and no message — every
    `echo` and `pause` in the script went nowhere too.
    """
    if os.name == "nt":
        subprocess.Popen(
            ["cmd", "/c", script_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    else:
        subprocess.Popen(["/bin/bash", script_path], start_new_session=True,
                         close_fds=True)


class DeltaUpdater(QObject):
    """Applies a differential update from a published release directory."""

    plan_ready = Signal(object)     # M.UpdatePlan
    progress = Signal(int, str)
    ready_to_swap = Signal(str)     # staging dir; caller should quit the app
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[StageWorker] = None
        self._source = None
        self._remote: Optional[dict] = None
        self._plan: Optional[M.UpdatePlan] = None
        self._staging: Optional[str] = None

    # -- inspection ---------------------------------------------------------

    def inspect(self, channel: str, token: Optional[str] = None) -> Optional[M.UpdatePlan]:
        """Compare a release channel against this installation.

        `channel` is either a filesystem path (a Drive-synced folder, a UNC
        share) or an http(s) URL. No files are changed.
        """
        install = find_install_root()
        if install is None:
            self.failed.emit(
                "Differential updates only apply to an installed copy of "
                "FrameGuard, not to a source checkout.")
            return None

        try:
            source = make_source(channel, token=token)
            remote = source.read_manifest()
        except UpdateSourceError as e:
            self.failed.emit(str(e))
            return None

        current = platform.system()
        if remote.get("platform") and remote["platform"] != current:
            self.failed.emit(
                f"That release is for {remote['platform']}, but this is {current}.")
            return None

        # An Apple Silicon bundle will not run on an Intel Mac and vice
        # versa. The manifest records the build arch (see publish_release);
        # refuse a mismatched arch rather than swapping in binaries the
        # machine cannot execute.
        current_arch = platform.machine()
        remote_arch = (remote.get("arch") or "").strip()
        if remote_arch and remote_arch.lower() != current_arch.lower():
            # universal2 runs anywhere; only enforce when both sides are
            # concrete single-arch builds.
            if "universal" not in remote_arch.lower():
                self.failed.emit(
                    f"That release is for {remote_arch}, but this Mac is "
                    f"{current_arch}. Install the matching DMG instead.")
                return None

        try:
            plan = M.diff_against_install(install, remote)
        except OSError as e:
            self.failed.emit(f"Could not read the installation: {e}")
            return None

        self._source, self._remote, self._plan = source, remote, plan
        self.plan_ready.emit(plan)
        return plan

    # -- staging ------------------------------------------------------------

    def stage(self):
        """Fetch and verify the changed files. Installation untouched."""
        if not self._plan or not self._remote or not self._source:
            self.failed.emit("Nothing to stage - inspect a channel first.")
            return
        if self._plan.is_empty:
            self.failed.emit("Already up to date.")
            return

        self._thread = QThread()
        self._worker = StageWorker(self._source, self._plan, self._remote)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self._on_staged)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._thread.start()

    def _on_staged(self, staging: str):
        self._staging = staging
        self._stop_thread()
        self.ready_to_swap.emit(staging)

    def _on_failed(self, message: str):
        self._stop_thread()
        self.failed.emit(message)

    def _on_cancelled(self):
        """A deliberate cancel is not a failure.

        It used to travel on the `failed` signal, so pressing Cancel produced
        a red "Update Failed" critical dialog.
        """
        self._stop_thread()
        self.cancelled.emit()

    def _stop_thread(self):
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
            self._worker = None

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel()

    # -- swap ---------------------------------------------------------------

    def apply_and_restart(self) -> bool:
        """Launch the swap helper. The caller must quit immediately after."""
        install = find_install_root()
        if not install or not self._staging or not self._plan:
            self.failed.emit("Update has not been staged.")
            return False
        try:
            script = write_swap_script(install, self._staging, self._plan)
            launch_swap_script(script)
            return True
        except OSError as e:
            self.failed.emit(f"Could not start the updater: {e}")
            return False
