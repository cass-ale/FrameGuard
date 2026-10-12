"""
FrameGuard Desktop -- Professional Auto-Update System

Provides seamless "Update & Restart" functionality:
1. Background check on startup (once per 24 hours, silent)
2. Non-intrusive notification when update is available
3. Download with progress → close app → silent install → relaunch
4. Configurable update source with "Browse" + "Test" in Settings
5. Remembers last successful path and last check time

Update source: the project's GitHub repository (see
DEFAULT_UPDATE_CHANNEL). The delta channel reads manifest.json + files/
from the repo's `releases` branch; a per-machine override in Settings
can point at a local folder (Downloads, a network share, a test mirror)
or another URL instead.
"""

import os
import sys
import json
import subprocess
import tempfile
import threading
import time
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Signal, QTimer, QSettings


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_SETTINGS_ORG = "Meta Platforms"
_SETTINGS_APP = "FrameGuard Desktop"
_CHECK_INTERVAL_HOURS = 24  # How often to auto-check (hours)
_INSTALLER_FILENAME = "FrameGuardInstaller.exe"
_VERSION_FILENAME = "version.json"

# The repository releases live in — the same repo users download the app
# from, and the URL installer.iss publishes. One home for this identity:
# the update channel below and the GitHub Releases check both derive
# from it.
GITHUB_REPO = "cass-ale/FrameGuard"  # owner/repo
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# The update channel every machine checks: this repository itself.
# Releases are published to the repo's `releases` branch, which mirrors
# the layout the channel has always had:
#
#     RELEASES/manifest.json + RELEASES/files/...            (Windows)
#     RELEASES-macos/manifest.json + RELEASES-macos/files/... (macOS)
#
# raw.githubusercontent.com serves that tree over HTTPS, path-addressed,
# which is exactly the shape HttpSource consumes — a fresh install is
# pointed at the channel with no server to run, nothing to install, and
# nothing to sign into. Baked in so a fresh install just works, and
# overridable per machine via QSettings for testing a pre-release
# (a filesystem path or another URL).
#
# The two platforms must never share one folder: their payloads overlap
# by filename (FrameGuard.exe vs the .app tree, .pyd vs .so, ffmpeg.exe
# vs ffmpeg) and one publish would silently overwrite the other.
#
# Channels are URLs, built with "/" — never os.path.join, which would
# inject backslashes into the URL on Windows.
_RELEASE_BRANCH = "releases"
_RELEASE_BASE_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{_RELEASE_BRANCH}"
DEFAULT_UPDATE_CHANNEL = f"{_RELEASE_BASE_URL}/RELEASES"


def get_update_channel_for(platform_name: str = "") -> str:
    """The channel for a given platform ("windows" or "darwin").

    Mac clients must check RELEASES-macos, not the Windows RELEASES
    folder — same filenames, different payloads (see above).
    """
    name = (platform_name or "").lower()
    if name.startswith("darwin") or name == "mac":
        return f"{_RELEASE_BASE_URL}/RELEASES-macos"
    return DEFAULT_UPDATE_CHANNEL
_CHANNEL_KEY = "update/channel"

# The retired channel: the team's Google Drive folder, the shipped
# default until the channel moved to the repository. Machines that
# explicitly saved it as an override (Settings) would otherwise stay
# pinned to a location that no longer receives releases, silently —
# background checks swallow unreachable-channel errors by design.
# Stored values matching a legacy channel are discarded in favour of
# the shipped default (see get_update_channel / set_update_channel).
_LEGACY_RELEASE_ROOT = (
    "G:\\Shared drives\\TPG XR\\\u2b50gDrive Reorganization\u2b50\\"
    "Team - Innovation\\Q1\\T260340134 - NY QA Improvement\\"
    "03 Desktop App\\MetaMate Follower"
)


def _is_legacy_channel(value: str) -> bool:
    """True if `value` is one of the retired Drive update channels."""
    normalised = (value or "").replace("\\", "/").rstrip("/")
    root = _LEGACY_RELEASE_ROOT.replace("\\", "/")
    return normalised in (root, f"{root}/RELEASES", f"{root}/RELEASES-macos")


def _default_channel_for_this_machine() -> str:
    """The shipped channel default for the machine actually running this code.

    Mac clients must check RELEASES-macos, not the Windows RELEASES folder —
    the two releases share filenames (FrameGuard.exe vs the .app tree, .pyd
    vs .so) and one shared manifest cannot describe both.
    """
    if sys.platform == "darwin":
        return get_update_channel_for("darwin")
    return DEFAULT_UPDATE_CHANNEL


def get_update_channel() -> str:
    """The release location this machine checks.

    Self-healing: a stored override that no longer exists is discarded in
    favour of the shipped default. Without that, a machine which once saved an
    old path stays pinned to it forever and silently receives no updates -
    background checks swallow unreachable-channel errors by design, so there
    is nothing to alert anyone that it is stuck.
    """
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    stored = (settings.value(_CHANNEL_KEY, "") or "").strip()
    if not stored:
        return _default_channel_for_this_machine()

    # A machine still pinned to the retired Drive channel migrates to
    # the shipped default: that folder no longer receives releases.
    if _is_legacy_channel(stored):
        settings.remove(_CHANNEL_KEY)
        return _default_channel_for_this_machine()

    # A URL cannot be probed cheaply here; trust it.
    if stored.lower().startswith(("http://", "https://")):
        return stored

    if os.path.isdir(stored):
        return stored

    # Stored path is gone. Prefer the shipped default when it is usable.
    default = _default_channel_for_this_machine()
    if os.path.isdir(default):
        settings.remove(_CHANNEL_KEY)
        return default

    # Neither is reachable (offline laptop). Keep the user's choice.
    return stored


def set_update_channel(channel: str):
    """Persist a per-machine override.

    Storing a value identical to the default is pointless and actively
    harmful: it freezes this machine on today's path, so a future release that
    moves the channel would never reach it.
    """
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    channel = (channel or "").strip()
    if not channel or channel in (DEFAULT_UPDATE_CHANNEL,
                                  get_update_channel_for("darwin")) \
            or _is_legacy_channel(channel):
        settings.remove(_CHANNEL_KEY)
    else:
        settings.setValue(_CHANNEL_KEY, channel)


@dataclass
class UpdateInfo:
    """Information about an available update."""
    version: str
    installer_path: str
    changelog: str = ""
    file_size_mb: float = 0.0
    is_newer: bool = False
    source_path: str = ""


# ---------------------------------------------------------------------------
# Version utilities
# ---------------------------------------------------------------------------

def get_current_version() -> str:
    """Get the currently installed version from the package."""
    try:
        from frameguard import __version__
        return __version__
    except ImportError:
        return "0.0.0"


def _parse_version(version_str: str) -> tuple:
    """Parse version string to comparable tuple."""
    clean = version_str.strip().lstrip("v")
    parts = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_newer(remote: str, local: str) -> bool:
    """Check if remote version is newer than local."""
    return _parse_version(remote) > _parse_version(local)


# ---------------------------------------------------------------------------
# Update source discovery
# ---------------------------------------------------------------------------

# GITHUB_REPO / GITHUB_RELEASES_URL are defined with the channel
# configuration at the top of this module — one home for the repo
# identity, which the update channel also derives from.


def check_github_for_update() -> Optional["UpdateInfo"]:
    """Check GitHub Releases API for a newer version.
    
    Looks for the latest release and compares version tags.
    The release should have an asset named FrameGuardInstaller.exe.
    """
    try:
        import urllib.request
        import json
        
        req = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "FrameGuard-Updater"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        
        tag = data.get("tag_name", "").lstrip("v")
        body = data.get("body", "")
        
        # Find the installer asset
        installer_url = ""
        file_size = 0
        for asset in data.get("assets", []):
            if "installer" in asset.get("name", "").lower() or asset.get("name", "").endswith(".exe"):
                installer_url = asset.get("browser_download_url", "")
                file_size = asset.get("size", 0) / (1024 * 1024)
                break
        
        if not tag or not installer_url:
            return None
        
        local_ver = get_current_version()
        return UpdateInfo(
            version=tag,
            installer_path=installer_url,
            changelog=body[:500],
            file_size_mb=round(file_size, 1),
            is_newer=_is_newer(tag, local_ver),
            source_path="GitHub Releases",
        )
    except Exception:
        return None


def installer_filename() -> str:
    """Name of the installer artifact this platform can actually apply."""
    return "FrameGuard.dmg" if sys.platform == "darwin" else _INSTALLER_FILENAME


def get_default_search_paths() -> List[str]:
    """Get the default list of paths to search for updates.

    Filesystem fallback for the installer-based flow only. The primary
    channel is the repository (see DEFAULT_UPDATE_CHANNEL); these paths
    cover a user who dropped an installer into Downloads or next to the
    app by hand.
    """
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, "Downloads", "FGv0.2.2"),
        os.path.join(home, "Downloads", "FrameGuard"),
        os.path.join(home, "OneDrive", "FrameGuard"),
    ]

    # Add app's own parent directory (for portable/dev installs)
    if getattr(sys, "frozen", False):
        paths.append(os.path.dirname(sys.executable))
    else:
        paths.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    return paths


def _check_path_for_update(update_dir: str) -> Optional[UpdateInfo]:
    """Check a single directory for version.json and installer.
    
    Returns UpdateInfo if a valid, newer version is found.
    """
    if not update_dir or not os.path.isdir(update_dir):
        return None
    
    version_file = os.path.join(update_dir, _VERSION_FILENAME)
    if not os.path.isfile(version_file):
        return None
    
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        remote_ver = data.get("version", "0.0.0")
        local_ver = get_current_version()

        # Prefer a platform-specific artifact name when the manifest offers one
        # ("installer_darwin"/"installer_windows"), so one shared version.json
        # can serve both platforms.
        key = "installer_darwin" if sys.platform == "darwin" else "installer_windows"
        installer_name = data.get(key) or data.get("installer") or installer_filename()
        installer_path = os.path.join(update_dir, installer_name)

        if not os.path.isfile(installer_path):
            # Announcing an update we cannot apply just produces a confusing
            # "Installer not found" a few clicks later.
            return None

        return UpdateInfo(
            version=remote_ver,
            installer_path=installer_path,
            changelog=data.get("changelog", ""),
            file_size_mb=data.get("file_size_mb", 0),
            is_newer=_is_newer(remote_ver, local_ver),
            source_path=update_dir,
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return None


# ---------------------------------------------------------------------------
# UpdateManager — the main controller
# ---------------------------------------------------------------------------

class UpdateManager(QObject):
    """Professional auto-update manager with background check and UI signals.
    
    Signals:
        update_available(UpdateInfo): A newer version was found.
        update_not_found(): Current version is up to date.
        check_failed(str): Error message from failed check.
        download_progress(int): 0-100 progress during installer copy.
        download_complete(str): Path to downloaded installer.
        status_changed(str): Human-readable status message.
    """
    
    update_available = Signal(object)
    delta_update_available = Signal(str, str)   # channel, version   # UpdateInfo
    update_not_found = Signal()
    check_failed = Signal(str)
    download_progress = Signal(int)
    download_complete = Signal(str)
    status_changed = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._checking = False
        self._check_lock = threading.Lock()
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        self._last_found_update: Optional[UpdateInfo] = None
        
        # Auto-check timer (fires once after startup delay)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._auto_check)
    
    # --- Public API ---
    
    def start_auto_check(self, delay_seconds: int = 5):
        """Schedule an automatic background check after a short delay.
        
        Only checks if enough time has passed since the last check.
        Called once at app startup.
        """
        last_check_str = self._settings.value("update/last_check", "")
        if last_check_str:
            try:
                last_check = datetime.fromisoformat(last_check_str)
                if datetime.now() - last_check < timedelta(hours=_CHECK_INTERVAL_HOURS):
                    # Checked recently — skip
                    return
            except (ValueError, TypeError):
                pass
        
        self._startup_timer.start(delay_seconds * 1000)
    
    def check_now(self):
        """Manually trigger an update check (from Settings button).
        Returns immediately — results come via signals.
        """
        with self._check_lock:
            if self._checking:
                return
            self._checking = True
        self.status_changed.emit("Checking for updates...")
        thread = threading.Thread(target=self._do_check, daemon=True)
        thread.start()

    # Older call sites used `check()`. A shipped build crashed with
    # "'UpdateManager' object has no attribute 'check'" after the rename.
    check = check_now
    
    def get_update_source(self) -> str:
        """Get the user-configured update source path."""
        return self._settings.value("update/source_path", "")
    
    def set_update_source(self, path: str):
        """Set a custom update source path."""
        self._settings.setValue("update/source_path", path)
    
    def check_channel_for_delta(self) -> bool:
        """Look for a newer build on the configured update channel.

        This is the single-source-of-truth path: one location, every machine
        checks it on the same 24-hour cadence, and only the changed files are
        transferred.
        """
        from frameguard.deltaupdate import find_install_root
        from frameguard.updatesource import UpdateSourceError, make_source

        channel = get_update_channel()
        if not channel or find_install_root() is None:
            return False

        try:
            remote = make_source(channel).read_manifest()
        except UpdateSourceError:
            # A missing share or an offline laptop must not surface an error
            # dialog on a background check.
            return False

        remote_version = remote.get("version") or remote.get("app_version", "")
        if not remote_version or not _is_newer(remote_version, get_current_version()):
            return False

        self._settings.setValue("update/last_check", datetime.now().isoformat())
        self._settings.setValue("update/last_source", channel)
        self.delta_update_available.emit(channel, remote_version)
        return True

    def get_last_check_time(self) -> str:
        """Get human-readable last check time."""
        last = self._settings.value("update/last_check", "")
        if not last:
            return "Never"
        try:
            dt = datetime.fromisoformat(last)
            return dt.strftime("%b %d, %Y at %I:%M %p")
        except (ValueError, TypeError):
            return "Unknown"
    
    def get_last_update_info(self) -> Optional[UpdateInfo]:
        """Return the last found update (if any)."""
        return self._last_found_update
    
    def apply_update(self, info: UpdateInfo = None):
        """Download/copy the installer and launch it, then close the app.
        
        This is the "Update & Restart" action:
        1. Copy installer to temp (with progress)
        2. Launch installer with /SILENT flag
        3. Close this application
        """
        if info is None:
            info = self._last_found_update
        if info is None:
            self.check_failed.emit("No update information available.")
            return
        
        # Run in background thread
        thread = threading.Thread(target=self._do_apply, args=(info,), daemon=True)
        thread.start()
    
    def test_path(self, path: str) -> str:
        """Test if a path contains valid update files. Returns status string."""
        if not path:
            return "No path specified"
        if not os.path.isdir(path):
            return f"Directory not found: {path}"
        
        vj = os.path.join(path, _VERSION_FILENAME)
        if not os.path.isfile(vj):
            return f"No {_VERSION_FILENAME} found in: {path}"
        
        try:
            with open(vj, "r") as f:
                data = json.load(f)
            ver = data.get("version", "?")
            installer = data.get("installer", _INSTALLER_FILENAME)
            has_installer = os.path.isfile(os.path.join(path, installer))
            
            if has_installer:
                return f"Valid! Version {ver} with installer ready."
            else:
                return f"version.json found (v{ver}) but {installer} is missing."
        except (json.JSONDecodeError, OSError) as e:
            return f"Error reading version.json: {e}"
    
    # --- Private methods ---
    
    def _auto_check(self):
        """Background auto-check triggered by startup timer."""
        self.check_now()
    
    def _do_check(self):
        """Perform the actual update check (runs in background thread)."""
        try:
            # The configured channel is the single source of truth and the
            # cheapest update (only changed files transfer), so try it first.
            if self.check_channel_for_delta():
                return

            # Try GitHub Releases first (primary update source)
            from frameguard.updater import check_github_for_update
            github_info = check_github_for_update()
            if github_info and github_info.is_newer:
                self._last_found_update = github_info
                self._settings.setValue("update/last_check", datetime.now().isoformat())
                self._settings.setValue("update/last_source", "GitHub Releases")
                self.status_changed.emit(f"Update found: v{github_info.version}")
                self.update_available.emit(github_info)
                self._checking = False
                return
            
            # Fallback: check local/network paths
            paths = []
            custom_path = self.get_update_source()
            if custom_path:
                paths.append(custom_path)
            paths.extend(get_default_search_paths())
            
            # Check each path
            checked_paths = []
            for path in paths:
                if not path or path in checked_paths:
                    continue
                checked_paths.append(path)
                
                info = _check_path_for_update(path)
                if info and info.is_newer:
                    # Found an update!
                    self._last_found_update = info
                    self._settings.setValue("update/last_check", datetime.now().isoformat())
                    self._settings.setValue("update/last_source", path)
                    self.status_changed.emit(f"Update found: v{info.version}")
                    self.update_available.emit(info)
                    self._checking = False
                    return
            
            # No update found
            self._settings.setValue("update/last_check", datetime.now().isoformat())
            self.status_changed.emit("Up to date")
            self.update_not_found.emit()
            
        except Exception as e:
            self.check_failed.emit(f"Check failed: {str(e)[:100]}")
        finally:
            self._checking = False
    
    def _download_installer(self, url: str) -> str:
        """Download an installer URL to a temp file, with progress.

        Returns the local path, or "" on failure. The GitHub Releases
        check reports the asset's download URL as installer_path; the
        rest of the apply flow works on local files, so the URL has to
        become one first — without this, that check could find an
        update it could never apply.
        """
        import urllib.parse
        import urllib.request

        name = os.path.basename(urllib.parse.urlparse(url).path) \
            or installer_filename()
        dest = os.path.join(
            tempfile.mkdtemp(prefix="frameguard_update_"), name)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "FrameGuard-Updater"})
            with urllib.request.urlopen(req, timeout=120) as resp, \
                    open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                copied = 0
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    copied += len(chunk)
                    if total > 0:
                        self.download_progress.emit(int(copied * 100 / total))
            self.download_progress.emit(100)
            return dest
        except Exception:
            return ""

    def _do_apply(self, info: UpdateInfo):
        """Download/copy installer and launch it (runs in background thread)."""
        try:
            self.status_changed.emit("Preparing update...")

            # Determine installer source
            source = info.installer_path
            if source and source.lower().startswith(("http://", "https://")):
                self.status_changed.emit("Downloading installer...")
                source = self._download_installer(source)
                if not source:
                    self.check_failed.emit("Could not download the installer.")
                    return
            if not source or not os.path.isfile(source):
                self.check_failed.emit(f"Installer not found at: {source}")
                return

            # Copy to temp directory (avoids file locking issues)
            temp_dir = tempfile.mkdtemp(prefix="frameguard_update_")
            dest = os.path.join(temp_dir, os.path.basename(source))

            # Copy with progress
            self.status_changed.emit("Copying installer...")
            total_size = os.path.getsize(source)
            copied = 0
            chunk_size = 256 * 1024  # 256KB chunks

            with open(source, "rb") as src, open(dest, "wb") as dst:
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    pct = int(copied * 100 / total_size) if total_size > 0 else 100
                    self.download_progress.emit(pct)

            self.download_progress.emit(100)
            self.download_complete.emit(dest)

            if sys.platform == "darwin":
                # A running .app cannot replace itself, and there is no silent
                # installer. Reveal the disk image and let the user drag the new
                # build into /Applications, which is the standard Mac flow.
                self.status_changed.emit("Opening the update...")
                subprocess.Popen(["/usr/bin/open", dest])
                # Deliberately do NOT quit: the user needs FrameGuard closed
                # only at the moment they replace it, and the Finder copy
                # prompt makes that obvious.
                return

            self.status_changed.emit("Launching installer...")
            # Inno Setup silent mode: /SILENT shows progress but no prompts.
            subprocess.Popen(
                [dest, "/SILENT", "/RESTARTAPPLICATIONS"],
                creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
            )

            # Give installer a moment to start, then close the app
            time.sleep(1)
            self.status_changed.emit("Restarting...")

            # Qt objects must only be touched from the GUI thread; _do_apply
            # runs on a worker thread, so marshal the quit through the event
            # loop instead of calling it directly (which hangs or crashes).
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)

        except Exception as e:
            self.check_failed.emit(f"Update failed: {str(e)[:200]}")


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def create_version_json(version: str, installer_filename: str = _INSTALLER_FILENAME,
                        changelog: str = "", file_size_mb: float = 0,
                        installer_darwin: str = "FrameGuard.dmg") -> str:
    """Generate a version.json file content for publishing an update."""
    data = {
        "version": version,
        "installer": installer_filename,
        "installer_windows": installer_filename,
        "installer_darwin": installer_darwin,
        "changelog": changelog,
        "file_size_mb": file_size_mb,
        "min_version": "0.4.0",
        "published": datetime.now().isoformat(),
    }
    return json.dumps(data, indent=2)
