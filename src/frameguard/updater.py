"""
FrameGuard Desktop -- Professional Auto-Update System

Provides seamless "Update & Restart" functionality:
1. Background check on startup (once per 24 hours, silent)
2. Non-intrusive notification when update is available
3. Download with progress → close app → silent install → relaunch
4. Remembers the last check time

Update source: the platform installer from the latest GitHub Release,
downloaded over HTTPS and verified against its published SHA-256 before it is
launched. A per-machine channel override in Settings can optionally point at
a local folder, network share, or static HTTPS host that provides
manifest.json + files/ for differential updates. No differential channel is
enabled by default.

There is deliberately no local-folder installer fallback: it trusted a hash
from the version.json sitting next to the installer, so the folder vouched for
itself.
"""

import hashlib
import hmac
import os
import re
import shutil
import ssl
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
_SETTINGS_ORG = "Capri la Ria"
_SETTINGS_APP = "FrameGuard Desktop"
_CHECK_INTERVAL_HOURS = 24  # How often to auto-check (hours)
_INSTALLER_FILENAME = "FrameGuardInstaller.exe"

# Full installers include Qt and media tooling. Two GiB leaves headroom for the
# current artifacts while putting a hard ceiling on unknown-length responses.
MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024
_MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
_MAX_CHECKSUM_BYTES = 1024 * 1024
_COPY_CHUNK_SIZE = 256 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30
_MAX_DOWNLOAD_SECONDS = 30 * 60
_MIN_FREE_SPACE_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Inno Setup arguments for the in-app Windows update. FrameGuard has already
# quit when Setup runs, so nothing may wait for a click: /SILENT shows only
# progress, /SUPPRESSMSGBOXES answers any SuppressibleMsgBox with its default
# (installer.iss also skips its prompts when WizardSilent), /NORESTART stops
# a suppressed "restart now?" from rebooting the machine, and
# /RESTARTAPPLICATIONS tells installer.iss to relaunch FrameGuard afterwards.
WINDOWS_INSTALLER_ARGS = (
    "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RESTARTAPPLICATIONS")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


# ---------------------------------------------------------------------------
# HTTPS trust
# ---------------------------------------------------------------------------
# Certificate verification is delegated to the operating system through
# `truststore`: Security.framework on macOS (which honours keychain trust
# settings, including roots an administrator trusted for a corporate proxy
# and certificates explicitly marked never-trust) and CryptoAPI on Windows.
#
# The frozen macOS app bundles python.org's OpenSSL, whose CA file does not
# exist on a normal Mac, so plain OpenSSL verification fails there. Earlier
# releases worked around that by loading every certificate in the System
# keychain as a trusted root, which also trusted certificates that were only
# imported, or explicitly distrusted. That workaround is gone and keychain
# certificates are never loaded as roots. Without truststore the context
# falls back to OpenSSL's default store, plus certifi's bundle if installed;
# either way it requires a valid chain and a matching hostname.


def _fallback_https_context() -> ssl.SSLContext:
    """OpenSSL default verification, extended with certifi when present."""
    try:
        context = ssl.create_default_context()
    except Exception:
        # Loading the platform store can fail on unusual systems; keep full
        # verification (CERT_REQUIRED + hostname checks) with OpenSSL defaults.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            context.set_default_verify_paths()
        except Exception:
            pass
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass  # certifi is optional; the default store still applies
    return context


def https_context() -> ssl.SSLContext:
    """A verifying TLS client context that trusts what the OS trusts."""
    try:
        import truststore
    except Exception:
        # Not installed, or its OS backend failed to load at import time
        # (truststore loads Security.framework / crypt32 when imported).
        truststore = None
    if truststore is not None:
        try:
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            pass  # e.g. the OS trust API is unavailable; verify with OpenSSL
    return _fallback_https_context()


def _urlopen_verified(request, timeout: float):
    """urlopen with OS-native certificate verification."""
    import urllib.request

    return urllib.request.urlopen(
        request, timeout=timeout, context=https_context())


class UpdateCheckError(RuntimeError):
    """GitHub Releases could not be reached or answered with garbage."""


# Reason the most recent check_github_for_update() call could not complete,
# or "" when it reached GitHub. Read by UpdateManager._do_check, which runs one
# check at a time, so the failure is reported instead of "Up to date".
_last_github_error = ""


def last_github_error() -> str:
    return _last_github_error

# The repository releases live in. GitHub Releases is the zero-configuration,
# architecture-aware update source and carries the complete installers.
GITHUB_REPO = "cass-ale/FrameGuard"  # owner/repo
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Differential payload trees do not fit safely in this Git repository: the
# macOS tree is over 600 MB and the Windows FFmpeg binaries individually exceed
# GitHub's hard 100 MB Git-object limit. Keep this empty so packaged clients go
# directly to GitHub Releases. Teams with a real static artifact host can still
# configure a local/HTTP differential channel in Settings.
DEFAULT_UPDATE_CHANNEL = ""


def get_update_channel_for(platform_name: str = "") -> str:
    """Return the shipped differential channel (disabled on every platform)."""
    del platform_name
    return DEFAULT_UPDATE_CHANNEL


_CHANNEL_KEY = "update/channel"

# Retired Drive and Git-backed channels. Machines that explicitly saved one
# as an override would otherwise stay pinned to a location that no longer
# receives releases. Stored legacy values are discarded so the normal GitHub
# Releases check runs (see get_update_channel / set_update_channel).
_LEGACY_RELEASE_ROOT = (
    "G:\\Shared drives\\TPG XR\\\u2b50gDrive Reorganization\u2b50\\"
    "Team - Innovation\\Q1\\T260340134 - NY QA Improvement\\"
    "03 Desktop App\\MetaMate Follower"
)


def _is_legacy_channel(value: str) -> bool:
    """True if `value` is a retired Drive or Git-backed update channel."""
    normalised = (value or "").replace("\\", "/").rstrip("/")
    root = _LEGACY_RELEASE_ROOT.replace("\\", "/")
    retired_repo = (
        f"https://raw.githubusercontent.com/{GITHUB_REPO}/releases/RELEASES",
        f"https://raw.githubusercontent.com/{GITHUB_REPO}/releases/RELEASES-macos",
    )
    return normalised in (root, f"{root}/RELEASES", f"{root}/RELEASES-macos") \
        or normalised.lower() in {url.lower() for url in retired_repo}


def _default_channel_for_this_machine() -> str:
    """The optional shipped differential channel; empty means GitHub Releases."""
    return get_update_channel_for(sys.platform)


def get_update_channel() -> str:
    """The optional differential release location this machine checks.

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

    # An HTTPS URL cannot be probed cheaply here; trust it. Any other URL
    # (plain http:// included) is refused by updatesource.make_source, so
    # keeping it would only pin this machine to a channel that never works.
    if stored.lower().startswith("https://"):
        return stored
    if "://" in stored:
        settings.remove(_CHANNEL_KEY)
        return _default_channel_for_this_machine()

    if os.path.isdir(stored):
        return stored

    # A dead filesystem override must not pin the machine. Clearing it makes
    # the normal GitHub Releases check run immediately after the optional delta
    # lookup returns False.
    settings.remove(_CHANNEL_KEY)
    return _default_channel_for_this_machine()


def set_update_channel(channel: str):
    """Persist a per-machine override.

    Storing a value identical to the default is pointless and actively
    harmful: it freezes this machine on today's path, so a future release that
    moves the channel would never reach it.
    """
    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    channel = (channel or "").strip()
    if not channel or _is_legacy_channel(channel):
        settings.remove(_CHANNEL_KEY)
    else:
        settings.setValue(_CHANNEL_KEY, channel)


@dataclass
class UpdateInfo:
    """Information about an available update."""
    version: str
    installer_path: str
    # Display-ready and bounded (see release_notes_summary).
    changelog: str = ""
    file_size_mb: float = 0.0
    is_newer: bool = False
    source_path: str = ""
    sha256: str = ""
    size_bytes: int = 0
    asset_name: str = ""


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


def _parse_version(version_str: str) -> Optional[tuple]:
    """Parse a complete stable three-part version, or return None."""
    if not isinstance(version_str, str):
        return None
    match = _VERSION_RE.fullmatch(version_str.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _is_newer(remote: str, local: str) -> bool:
    """Check if two valid stable versions show a newer remote release."""
    remote_parts = _parse_version(remote)
    local_parts = _parse_version(local)
    return bool(remote_parts and local_parts and remote_parts > local_parts)


# ---------------------------------------------------------------------------
# Update source discovery
# ---------------------------------------------------------------------------

# GITHUB_REPO / GITHUB_RELEASES_URL are defined with update configuration at
# the top of this module.


def _pick_release_asset(assets, platform: Optional[str] = None,
                        machine: Optional[str] = None):
    """Return one exact installer asset for this platform and architecture."""
    if not isinstance(assets, list):
        return None
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        if machine is None:
            import platform as _platform
            machine = _platform.machine()
        machine = str(machine).lower()
        if machine in ("arm64", "aarch64"):
            expected = "FrameGuard-arm64.dmg"
        elif machine in ("x86_64", "amd64", "intel"):
            expected = "FrameGuard-intel.dmg"
        else:
            return None
    else:
        expected = _INSTALLER_FILENAME

    matches = [
        asset for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name", "")).casefold() == expected.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _read_bounded(response, maximum: int) -> bytes:
    """Read a small HTTP response without trusting its Content-Length."""
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, maximum + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise RuntimeError("Update metadata response is too large.")


def _http_status_ok(response) -> bool:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return 200 <= int(status) < 300


def _normalise_sha256(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.lower().startswith("sha256:"):
        value = value.split(":", 1)[1].strip()
    return value.lower() if _SHA256_RE.fullmatch(value) else ""


def _checksum_from_release(assets, filename: str) -> str:
    """Resolve filename from the release's single SHA256SUMS.txt asset."""
    checksum_assets = [
        asset for asset in assets
        if isinstance(asset, dict)
        and str(asset.get("name", "")).casefold() == "sha256sums.txt"
    ]
    if len(checksum_assets) != 1:
        return ""
    url = str(checksum_assets[0].get("browser_download_url", ""))
    if not url.lower().startswith("https://"):
        return ""

    import urllib.request

    request = urllib.request.Request(
        url, headers={"User-Agent": "FrameGuard-Updater"})
    with _urlopen_verified(request, timeout=10) as response:
        if not _http_status_ok(response):
            return ""
        text = _read_bounded(response, _MAX_CHECKSUM_BYTES).decode(
            "utf-8", errors="strict")

    matches = []
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest = _normalise_sha256(parts[0])
        listed_name = parts[1].lstrip("* ")
        if digest and listed_name == filename:
            matches.append(digest)
    return matches[0] if len(matches) == 1 else ""


# The update prompt shows what changed, not the release page's download
# instructions. Release bodies come from release_notes.py: boilerplate, then
# "## Changes" with this version's changelog entry in a fenced block.
MAX_CHANGELOG_CHARS = 600
_CHANGES_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*changes\s*#*\s*$",
                                 re.IGNORECASE | re.MULTILINE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def _changes_section(text: str) -> Optional[str]:
    """Text under the "Changes" heading, up to the next heading."""
    match = _CHANGES_HEADING_RE.search(text)
    if match is None:
        return None
    lines = []
    in_fence = False
    fenced: List[str] = []
    saw_fence = False
    for line in text[match.end():].split("\n"):
        if _FENCE_RE.match(line):
            if in_fence:
                in_fence = False
                continue
            in_fence = saw_fence = True
            continue
        if in_fence:
            fenced.append(line)
            continue
        if _HEADING_RE.match(line):
            break
        lines.append(line)
    # The release template fences the changelog and puts a footer after it;
    # when a fence is present its contents ARE the changes.
    return "\n".join(fenced if saw_fence else lines)


def _strip_release_boilerplate(text: str) -> str:
    """Fallback for bodies without a Changes heading: drop markdown tables,
    headings, fences and bold-only lines, keeping the prose."""
    kept = []
    for line in text.split("\n"):
        stripped = line.strip()
        if (stripped.startswith("|") or _HEADING_RE.match(line)
                or _FENCE_RE.match(line)
                or re.fullmatch(r"\*\*[^*]+\*\*", stripped)):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept)


def release_notes_summary(body, limit: int = MAX_CHANGELOG_CHARS) -> str:
    """Display-ready "what changed" text from a GitHub release body.

    Prefers the section under "## Changes"; otherwise strips tables and
    boilerplate. Blank-line runs collapse, and the result is cut at a line
    (or word) boundary within ``limit`` characters with a trailing ellipsis.
    """
    if not isinstance(body, str) or limit <= 0:
        return ""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    section = _changes_section(text)
    if section is None or not section.strip():
        section = _strip_release_boilerplate(text)
    lines: List[str] = []
    for line in section.strip().split("\n"):
        line = line.rstrip()
        if not line and (not lines or not lines[-1]):
            continue
        lines.append(line)
    summary = "\n".join(lines).strip()
    if len(summary) <= limit:
        return summary
    cut = summary.rfind("\n", 0, limit)
    if cut <= 0:
        cut = summary.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit - 1
    return summary[:cut].rstrip() + "\u2026"


def check_github_for_update() -> Optional["UpdateInfo"]:
    """Check GitHub Releases for one integrity-verifiable stable installer.

    Returns None when there is nothing installable. When GitHub could not be
    reached at all, the reason is also left in ``last_github_error()`` so the
    caller can say "check failed" rather than "up to date".
    """
    global _last_github_error
    _last_github_error = ""
    try:
        import urllib.request

        req = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github.v3+json",
                     "User-Agent": "FrameGuard-Updater"},
        )
        try:
            with _urlopen_verified(req, timeout=10) as response:
                if not _http_status_ok(response):
                    status = getattr(response, "status", "")
                    raise UpdateCheckError(f"GitHub answered HTTP {status}".strip())
                data = json.loads(
                    _read_bounded(response, _MAX_RELEASE_METADATA_BYTES).decode(
                        "utf-8", errors="strict"))
        except UpdateCheckError as error:
            _last_github_error = str(error)
            return None
        except Exception as error:  # network, TLS, proxy, malformed JSON
            reason = getattr(error, "reason", None) or error
            _last_github_error = str(reason)[:160] or type(error).__name__
            return None

        if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
            return None
        raw_tag = data.get("tag_name", "")
        parsed_tag = _parse_version(raw_tag)
        if not parsed_tag:
            return None
        tag = ".".join(str(part) for part in parsed_tag)
        assets = data.get("assets", [])
        asset = _pick_release_asset(assets)
        if asset is None:
            return None

        name = str(asset.get("name", ""))
        installer_url = str(asset.get("browser_download_url", ""))
        if not installer_url.lower().startswith("https://"):
            return None
        size = asset.get("size")
        if not isinstance(size, int) or isinstance(size, bool) \
                or size <= 0 or size > MAX_INSTALLER_BYTES:
            return None

        digest = _normalise_sha256(asset.get("digest"))
        if not digest:
            digest = _checksum_from_release(assets, name)
        if not digest:
            return None

        return UpdateInfo(
            version=tag,
            installer_path=installer_url,
            changelog=release_notes_summary(data.get("body", "")),
            file_size_mb=round(size / (1024 * 1024), 1),
            is_newer=_is_newer(tag, get_current_version()),
            source_path="GitHub Releases",
            sha256=digest,
            size_bytes=size,
            asset_name=name,
        )
    except Exception:
        return None


def installer_filename() -> str:
    """Exact release asset name this platform and architecture can apply."""
    if sys.platform != "darwin":
        return _INSTALLER_FILENAME
    import platform as _platform
    machine = _platform.machine().lower()
    return ("FrameGuard-arm64.dmg"
            if machine in ("arm64", "aarch64")
            else "FrameGuard-intel.dmg")


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
    # Install failures only. check_failed is shared with update checks, so a
    # failed hourly check used to close the install progress dialog mid-download.
    apply_failed = Signal(str)
    download_progress = Signal(int)
    download_complete = Signal(str)
    status_changed = Signal(str)
    # Emitted on the GUI thread's queue once the installer has been handed
    # off (macOS: the DMG is open; Windows: Setup is running). The window must
    # then quit so the installed app can be replaced. The old code called
    # a zero-delay QTimer quit from this worker thread, which has no Qt
    # event loop, so the quit never ran: Setup sat behind a "FrameGuard is
    # running" prompt and macOS could not replace the running bundle.
    install_handoff = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._checking = False
        self._check_lock = threading.Lock()
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        self._last_found_update: Optional[UpdateInfo] = None
        self._cancel_event = threading.Event()
        self._apply_lock = threading.Lock()
        self._applying = False
        # Successful launch hands staging ownership to the external installer.
        self._retained_staging_dirs = set()
        
        # Auto-check timer (fires once after startup delay)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._auto_check)
        # FrameGuard often runs for days in the tray watching a folder; the
        # startup check alone meant such a session never saw a new release.
        self._periodic_timer = QTimer(self)
        self._periodic_timer.setInterval(60 * 60 * 1000)
        self._periodic_timer.timeout.connect(
            lambda: self.start_auto_check(delay_seconds=0))
        self._stale_staging_cleaned = False

    @staticmethod
    def _emit(signal, *args) -> bool:
        """Emit unless the QObject was deleted during background shutdown."""
        try:
            signal.emit(*args)
            return True
        except RuntimeError:
            return False
    
    # --- Public API ---
    
    def start_auto_check(self, delay_seconds: int = 5):
        """Schedule an automatic background check after a short delay.
        
        Only checks if enough time has passed since the last check.
        Called at app startup and then hourly.
        """
        if not self._periodic_timer.isActive():
            self._periodic_timer.start()
        if not self._stale_staging_cleaned:
            self._stale_staging_cleaned = True
            remove_stale_staging_dirs()
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
        if self._applying:
            # Never race a check against an install in progress, but always
            # answer so a manual "Check for Updates" button is released.
            self._emit(self.check_failed, "An update is already being installed.")
            return
        with self._check_lock:
            if self._checking:
                return
            self._checking = True
        self._emit(self.status_changed, "Checking for updates...")
        thread = threading.Thread(target=self._do_check, daemon=True)
        thread.start()

    # Older call sites used `check()`. A shipped build crashed with
    # "'UpdateManager' object has no attribute 'check'" after the rename.
    check = check_now
    
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

        # A separate QSettings per thread: one instance shared between this
        # worker thread and the GUI thread is not thread-safe.
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue("update/last_check", datetime.now().isoformat())
        settings.setValue("update/last_source", channel)
        self._emit(self.delta_update_available, channel, remote_version)
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
        """Verify, stage, and launch one full installer in the background."""
        if info is None:
            info = self._last_found_update
        if info is None:
            self._emit(self.check_failed, "No update information available.")
            self._emit(self.apply_failed, "No update information available.")
            return
        with self._apply_lock:
            if self._applying:
                return
            self._applying = True
            self._cancel_event.clear()
        thread = threading.Thread(target=self._do_apply, args=(info,), daemon=True)
        thread.start()

    def cancel_update(self):
        """Request cancellation at the next bounded copy/download checkpoint."""
        self._cancel_event.set()
    
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

            settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

            # GitHub Releases is the only full-installer source.
            from frameguard import updater as _updater
            _updater._last_github_error = ""
            github_info = _updater.check_github_for_update()
            github_error = _updater.last_github_error()
            if github_info and github_info.is_newer:
                self._last_found_update = github_info
                settings.setValue("update/last_check", datetime.now().isoformat())
                settings.setValue("update/last_source", "GitHub Releases")
                self._emit(self.status_changed, f"Update found: v{github_info.version}")
                self._emit(self.update_available, github_info)
                self._checking = False
                return
            
            if github_error:
                # Unreachable is not "up to date". Leave last_check alone so
                # the next launch (or the hourly timer) tries again.
                self._emit(self.status_changed, "Update check failed")
                self._emit(self.check_failed,
                           f"Could not reach GitHub Releases: {github_error}")
                return

            # No update found
            settings.setValue("update/last_check", datetime.now().isoformat())
            self._emit(self.status_changed, "Up to date")
            self._emit(self.update_not_found)
            
        except Exception as e:
            self._emit(self.check_failed, f"Check failed: {str(e)[:100]}")
        finally:
            self._checking = False
    
    def _check_cancelled(self):
        if self._cancel_event.is_set():
            raise RuntimeError("Update cancelled.")

    @staticmethod
    def _ensure_staging_capacity(staging_dir: str, required: int):
        if required <= 0 or required > MAX_INSTALLER_BYTES:
            raise RuntimeError("Installer size is invalid or exceeds the 2 GiB limit.")
        free = shutil.disk_usage(staging_dir).free
        if free < required + _MIN_FREE_SPACE_BYTES:
            raise RuntimeError("Not enough free disk space to stage the update.")

    def _download_installer(self, url: str, *, staging_dir: str = None,
                            expected_size: int = 0,
                            expected_sha256: str = "",
                            filename: str = "") -> str:
        """Stream one bounded installer into an explicitly owned staging tree."""
        import urllib.parse
        import urllib.request

        owns_staging = staging_dir is None
        if staging_dir is None:
            staging_dir = tempfile.mkdtemp(prefix="frameguard_update_")
        os.makedirs(staging_dir, exist_ok=True)
        name = filename or os.path.basename(
            urllib.parse.unquote(urllib.parse.urlparse(url).path))
        name = os.path.basename(name) or installer_filename()
        dest = os.path.join(staging_dir, name)
        digest = _normalise_sha256(expected_sha256)

        try:
            self._check_cancelled()
            req = urllib.request.Request(
                url, headers={"User-Agent": "FrameGuard-Updater"})
            with _urlopen_verified(
                    req, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
                if not _http_status_ok(response):
                    raise RuntimeError("Installer download returned a non-success status.")
                raw_length = response.headers.get("Content-Length")
                declared = 0
                if raw_length not in (None, ""):
                    try:
                        declared = int(raw_length)
                    except (TypeError, ValueError) as error:
                        raise RuntimeError("Installer Content-Length is invalid.") from error
                    if declared <= 0 or declared > MAX_INSTALLER_BYTES:
                        raise RuntimeError(
                            "Installer size is invalid or exceeds the 2 GiB limit.")
                if expected_size and declared and declared != expected_size:
                    raise RuntimeError("Installer size does not match release metadata.")
                required = expected_size or declared
                self._ensure_staging_capacity(staging_dir, required)

                copied = 0
                hasher = hashlib.sha256()
                started = time.monotonic()
                with open(dest, "xb") as output:
                    while True:
                        self._check_cancelled()
                        if time.monotonic() - started > _MAX_DOWNLOAD_SECONDS:
                            raise RuntimeError("Installer download exceeded 30 minutes.")
                        chunk = response.read(_COPY_CHUNK_SIZE)
                        self._check_cancelled()
                        if time.monotonic() - started > _MAX_DOWNLOAD_SECONDS:
                            raise RuntimeError("Installer download exceeded 30 minutes.")
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_INSTALLER_BYTES:
                            raise RuntimeError("Installer exceeds the 2 GiB limit.")
                        if declared and copied > declared:
                            raise RuntimeError("Installer exceeds its Content-Length.")
                        if expected_size and copied > expected_size:
                            raise RuntimeError("Installer exceeds release metadata size.")
                        output.write(chunk)
                        hasher.update(chunk)
                        total = expected_size or declared
                        if total:
                            self._emit(
                                self.download_progress,
                                min(99, int(copied * 100 / total)),
                            )

                if declared and copied != declared:
                    raise RuntimeError("Installer download was truncated.")
                if expected_size and copied != expected_size:
                    raise RuntimeError("Installer size does not match release metadata.")
                if digest and not hmac.compare_digest(hasher.hexdigest(), digest):
                    raise RuntimeError("Installer integrity verification failed.")
                self._emit(self.download_progress, 100)
                return dest
        except Exception as error:
            try:
                os.remove(dest)
            except OSError:
                pass
            if owns_staging:
                shutil.rmtree(staging_dir, ignore_errors=True)
                return ""
            if isinstance(error, RuntimeError):
                raise
            raise RuntimeError(f"Installer download failed: {error}") from error

    def _do_apply(self, info: UpdateInfo):
        """Verify, stage once, and launch the installer from a worker thread."""
        staging_dir = None
        retained = False
        try:
            self._emit(self.status_changed, "Preparing update...")
            digest = _normalise_sha256(info.sha256)
            if not digest:
                raise RuntimeError("Installer has no trusted SHA-256 digest.")
            if (not isinstance(info.size_bytes, int)
                    or isinstance(info.size_bytes, bool)
                    or info.size_bytes <= 0):
                raise RuntimeError("Installer has no trusted size metadata.")

            source = info.installer_path or ""
            if not source.lower().startswith("https://"):
                # Only GitHub Releases installers are applied; there is no
                # local-file or plain-HTTP installer path.
                raise RuntimeError("Installer location is not an HTTPS URL.")

            staging_dir = tempfile.mkdtemp(prefix="frameguard_update_")
            os.makedirs(staging_dir, exist_ok=True)
            self._emit(self.status_changed, "Downloading installer...")
            dest = self._download_installer(
                source,
                staging_dir=staging_dir,
                expected_size=info.size_bytes,
                expected_sha256=digest,
                filename=info.asset_name,
            )

            self._check_cancelled()
            self._emit(self.download_complete, dest)
            if sys.platform == "darwin":
                self._emit(self.status_changed, "Opening the update...")
                subprocess.Popen(["/usr/bin/open", dest])
                retained = True
                self._retained_staging_dirs.add(staging_dir)
                self._emit(self.install_handoff, dest)
                return

            self._emit(self.status_changed, "Launching installer...")
            subprocess.Popen(
                [dest, *WINDOWS_INSTALLER_ARGS],
                creationflags=(subprocess.DETACHED_PROCESS
                               if sys.platform == "win32" else 0),
            )
            retained = True
            self._retained_staging_dirs.add(staging_dir)
            self._emit(self.status_changed, "Restarting...")
            # Setup waits for this process's mutex to clear before installing.
            self._emit(self.install_handoff, dest)
        except Exception as error:
            if self._cancel_event.is_set():
                self._emit(self.status_changed, "Update cancelled.")
            else:
                self._emit(self.check_failed, f"Update failed: {str(error)[:200]}")
                self._emit(self.apply_failed, f"Update failed: {str(error)[:200]}")
        finally:
            if staging_dir and not retained:
                shutil.rmtree(staging_dir, ignore_errors=True)
            with self._apply_lock:
                self._applying = False


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

_STALE_STAGING_SECONDS = 24 * 60 * 60


def remove_stale_staging_dirs(now: Optional[float] = None) -> int:
    """Delete update staging folders left by earlier, completed updates.

    A successful launch hands the staged installer to the OS, so the folder
    cannot be removed by the process that created it; each update used to
    leave a 100-200 MB copy in the temp directory forever.
    """
    root = tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("frameguard_update_"):
            continue
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            if now - os.path.getmtime(path) < _STALE_STAGING_SECONDS:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed
