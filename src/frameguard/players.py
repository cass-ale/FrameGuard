"""Find the video players installed on this machine and open files in them.

"Open in VLC" only knew about VLC: on a Mac without VLC every click opened
QuickTime behind a "VLC Not Found" dialog, and nothing else could be chosen.
This module lists every application the operating system says can open a
given video file and launches files in the one the operator picks.

Design rules:

* Discovery never blocks the UI for long. macOS asks LaunchServices (a few
  milliseconds); Windows reads the registry. Results are cached per file
  extension and can be warmed on a background thread at startup.
* Launching never blocks the UI. Commands are spawned detached and reaped on
  a daemon thread; a launch that fails (the app was removed, LaunchServices
  refused, the process died at once) is reported through a callback instead
  of a frozen window or a silent no-op.
* Nothing here imports Qt, so it is safe to call from any thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence

# Keys for the two entries that are not a specific application.
DEFAULT_KEY = "default"
CHOOSE_KEY = "choose"

# A batch larger than this is almost certainly a mis-click on "Select All";
# players open one window or playlist entry per file.
MAX_FILES_PER_LAUNCH = 50

# How long a launched process must survive before the launch counts as good.
_LAUNCH_GRACE_SECONDS = 3.0


@dataclass(frozen=True)
class Player:
    """One way to open a video."""

    name: str              # what the operator reads ("QuickTime Player")
    key: str               # stable identity: app bundle / exe path, or DEFAULT_KEY
    path: str = ""         # .app bundle (macOS) or executable (Windows/Linux)
    is_default: bool = False


# ---------------------------------------------------------------------------
# macOS: LaunchServices
# ---------------------------------------------------------------------------

# Apps that register for movie types but are not somewhere an operator would
# review a deliverable (Books claims .mp4 for audiobooks).
_MAC_EXCLUDED_APPS = {"Books", "Music", "Photos", "iMovie Theater", "FrameGuard"}


def _mac_launchservices(path: str):
    """Return ``(default_app, [apps])`` LaunchServices offers for ``path``."""
    import ctypes
    import ctypes.util
    from ctypes import c_bool, c_char_p, c_long, c_uint32, c_void_p

    cf = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("CoreFoundation")
        or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cs = ctypes.cdll.LoadLibrary(
        ctypes.util.find_library("CoreServices")
        or "/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFURLCreateFromFileSystemRepresentation.restype = c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
        c_void_p, c_char_p, c_long, c_bool]
    cs.LSCopyApplicationURLsForURL.restype = c_void_p
    cs.LSCopyApplicationURLsForURL.argtypes = [c_void_p, c_uint32]
    cs.LSCopyDefaultApplicationURLForURL.restype = c_void_p
    cs.LSCopyDefaultApplicationURLForURL.argtypes = [c_void_p, c_uint32, c_void_p]
    cf.CFArrayGetCount.restype = c_long
    cf.CFArrayGetCount.argtypes = [c_void_p]
    cf.CFArrayGetValueAtIndex.restype = c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
    cf.CFURLGetFileSystemRepresentation.restype = c_bool
    cf.CFURLGetFileSystemRepresentation.argtypes = [c_void_p, c_bool, c_char_p, c_long]
    cf.CFRelease.argtypes = [c_void_p]

    def _path_of(url) -> Optional[str]:
        buffer = ctypes.create_string_buffer(4096)
        if url and cf.CFURLGetFileSystemRepresentation(url, True, buffer, 4096):
            return os.fsdecode(buffer.value)
        return None

    raw = os.fsencode(path)
    url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
    if not url:
        return None, []
    all_roles = 0xFFFFFFFF
    try:
        array = cs.LSCopyApplicationURLsForURL(url, all_roles)
        apps = []
        if array:
            try:
                for index in range(cf.CFArrayGetCount(array)):
                    app = _path_of(cf.CFArrayGetValueAtIndex(array, index))
                    if app:
                        apps.append(app)
            finally:
                cf.CFRelease(array)
        default_url = cs.LSCopyDefaultApplicationURLForURL(url, all_roles, None)
        default = None
        if default_url:
            try:
                default = _path_of(default_url)
            finally:
                cf.CFRelease(default_url)
        return default, apps
    finally:
        cf.CFRelease(url)


def _mac_app_rank(path: str) -> int:
    """Prefer the copy in /Applications when the same app exists twice."""
    for rank, prefix in enumerate(("/Applications/", "/System/Applications/",
                                   os.path.expanduser("~/Applications/"))):
        if path.startswith(prefix):
            return rank
    return 9


def _discover_macos(sample: str) -> List[Player]:
    default, apps = _mac_launchservices(sample)
    by_name: Dict[str, str] = {}
    for app in ([default] if default else []) + apps:
        if not app or not app.endswith(".app"):
            continue
        # Skip copies inside a mounted disk image, the Trash, or another
        # app's bundle - they vanish or are not meant to be launched.
        if app.startswith("/Volumes/") or "/.Trash/" in app or ".app/" in app:
            continue
        name = os.path.splitext(os.path.basename(app))[0]
        if name in _MAC_EXCLUDED_APPS:
            continue
        current = by_name.get(name)
        if current is None or _mac_app_rank(app) < _mac_app_rank(current):
            by_name[name] = app
    players = [Player(name=name, key=path, path=path,
                      is_default=bool(default) and path == default)
               for name, path in by_name.items()]
    return players


# ---------------------------------------------------------------------------
# Windows: registry
# ---------------------------------------------------------------------------

# Players worth finding even when they did not register for this extension.
# (display name, executable names in preference order, common install dirs)
_WINDOWS_KNOWN = (
    ("VLC media player", ("vlc.exe",), (r"VideoLAN\VLC",)),
    ("MPC-HC", ("mpc-hc64.exe", "mpc-hc.exe"), (r"MPC-HC", r"K-Lite Codec Pack\MPC-HC64")),
    ("MPC-BE", ("mpc-be64.exe", "mpc-be.exe"), (r"MPC-BE x64", r"MPC-BE")),
    ("PotPlayer", ("PotPlayerMini64.exe", "PotPlayerMini.exe"), (r"DAUM\PotPlayer", r"PotPlayer")),
    ("mpv", ("mpv.exe",), (r"mpv",)),
    ("SMPlayer", ("smplayer.exe",), (r"SMPlayer",)),
    ("KMPlayer", ("KMPlayer64X.exe", "KMPlayer.exe"), (r"KMPlayer 64X", r"KMPlayer")),
    ("Windows Media Player Legacy", ("wmplayer.exe",), (r"Windows Media Player",)),
)


def _win_reg_value(hive, subkey: str, name: str = "") -> Optional[str]:
    try:
        import winreg
        with winreg.OpenKey(hive, subkey) as key:
            value = winreg.QueryValueEx(key, name)[0]
            return str(value) if value else None
    except (OSError, ImportError):
        return None


def _win_reg_subkeys_and_values(hive, subkey: str) -> List[str]:
    names: List[str] = []
    try:
        import winreg
        with winreg.OpenKey(hive, subkey) as key:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumValue(key, index)[0])
                except OSError:
                    break
                index += 1
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
    except (OSError, ImportError):
        pass
    return names


def _win_reg_value_data(hive, subkey: str) -> List[str]:
    """String data of every value under ``subkey`` (except MRUList)."""
    data: List[str] = []
    try:
        import winreg
        with winreg.OpenKey(hive, subkey) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if name.lower() != "mrulist" and isinstance(value, str):
                    data.append(value)
    except Exception:
        pass
    return data


def _exe_from_command(command: str) -> Optional[str]:
    """First token of a registry ``shell\\open\\command`` value, expanded."""
    if not command:
        return None
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        end = command.find('"', 1)
        exe = command[1:end] if end > 0 else command[1:]
    else:
        lowered = command.lower()
        cut = lowered.find(".exe")
        exe = command[:cut + 4] if cut >= 0 else command.split(" ")[0]
    return exe if exe.lower().endswith(".exe") else None


def _win_app_path(exe_name: str) -> Optional[str]:
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        value = _win_reg_value(
            hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}")
        if value:
            value = value.strip().strip('"')
            if os.path.isfile(value):
                return value
    command = _win_reg_value(
        winreg.HKEY_CLASSES_ROOT, rf"Applications\{exe_name}\shell\open\command")
    exe = _exe_from_command(command or "")
    return exe if exe and os.path.isfile(exe) else None


def _win_friendly_name(exe: str) -> str:
    import winreg
    base = os.path.basename(exe)
    name = _win_reg_value(winreg.HKEY_CLASSES_ROOT, rf"Applications\{base}", "FriendlyAppName")
    if name and not name.startswith("@"):
        return name
    return os.path.splitext(base)[0]


def _discover_windows(sample: str) -> List[Player]:
    import winreg

    extension = os.path.splitext(sample)[1].lower() or ".mp4"
    found: Dict[str, Player] = {}

    def _add(name: str, exe: Optional[str]):
        if not exe or not os.path.isfile(exe):
            return
        key = os.path.normcase(os.path.abspath(exe))
        if key in found or os.path.basename(key).lower() == "frameguard.exe":
            return
        found[key] = Player(name=name, key=key, path=exe)

    # 1. Known players: App Paths, then the usual install folders, then PATH.
    roots = [os.environ.get(var, "") for var in
             ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramW6432")]
    for name, exe_names, folders in _WINDOWS_KNOWN:
        exe = None
        for exe_name in exe_names:
            exe = _win_app_path(exe_name)
            if exe:
                break
            for root in filter(None, roots):
                for folder in folders:
                    candidate = os.path.join(root, folder, exe_name)
                    if os.path.isfile(candidate):
                        exe = candidate
                        break
                if exe:
                    break
            if exe:
                break
            exe = shutil.which(exe_name)
            if exe:
                break
        _add(name, exe)

    # 2. Whatever registered itself for this extension (Open With lists).
    progids = set()
    for hive, sub in ((winreg.HKEY_CURRENT_USER, rf"Software\Classes\{extension}\OpenWithProgids"),
                      (winreg.HKEY_CLASSES_ROOT, rf"{extension}\OpenWithProgids")):
        progids.update(_win_reg_subkeys_and_values(hive, sub))
    for progid in progids:
        if not progid or progid.lower().startswith("appx"):
            continue  # packaged apps: reached through "Choose another app"
        command = _win_reg_value(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command")
        exe = _exe_from_command(command or "")
        if exe:
            _add(_win_friendly_name(exe), exe)
    exe_names = set(_win_reg_subkeys_and_values(
        winreg.HKEY_CLASSES_ROOT, rf"{extension}\OpenWithList"))
    # The per-user list stores the executable names in the value *data*
    # (a="vlc.exe", b="mpc-hc64.exe", MRUList="ba").
    exe_names.update(_win_reg_value_data(
        winreg.HKEY_CURRENT_USER,
        rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{extension}\OpenWithList"))
    for exe_name in exe_names:
        if exe_name.lower().endswith(".exe"):
            exe = _win_app_path(exe_name)
            if exe:
                _add(_win_friendly_name(exe), exe)
    return list(found.values())


# ---------------------------------------------------------------------------
# Linux (best effort; not a release platform)
# ---------------------------------------------------------------------------

def _discover_linux(sample: str) -> List[Player]:
    players = []
    for name, command in (("VLC media player", "vlc"), ("mpv", "mpv"),
                          ("Celluloid", "celluloid"), ("Videos", "totem")):
        exe = shutil.which(command)
        if exe:
            players.append(Player(name=name, key=exe, path=exe))
    return players


# ---------------------------------------------------------------------------
# Discovery API
# ---------------------------------------------------------------------------

_cache: Dict[str, List[Player]] = {}
_cache_lock = threading.Lock()


def _probe_file(extension: str) -> str:
    """An empty file with ``extension``: LaunchServices needs a real file."""
    folder = os.path.join(tempfile.gettempdir(), "frameguard-player-probe")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"probe{extension}")
    if not os.path.exists(path):
        with open(path, "wb"):
            pass
    return path


def _sorted(players: Iterable[Player]) -> List[Player]:
    return sorted(players, key=lambda p: (not p.is_default, p.name.casefold()))


def discover_players(sample_path: Optional[str] = None, *, refresh: bool = False) -> List[Player]:
    """Applications that can open ``sample_path``'s file type, default first.

    The system default is marked ``is_default``. Results are cached per
    extension; ``refresh`` re-asks the operating system (an app may have been
    installed or removed since).
    """
    extension = (os.path.splitext(sample_path or "")[1] or ".mp4").lower()
    if not refresh:
        with _cache_lock:
            cached = _cache.get(extension)
        if cached is not None:
            return list(cached)
    try:
        sample = sample_path if sample_path and os.path.isfile(sample_path) else _probe_file(extension)
        if sys.platform == "darwin":
            players = _discover_macos(sample)
        elif sys.platform == "win32":
            players = _discover_windows(sample)
        else:
            players = _discover_linux(sample)
    except Exception:
        # Discovery is a convenience; the default handler always remains.
        players = []
    players = _sorted(players)
    with _cache_lock:
        _cache[extension] = players
    return list(players)


def warm_cache(extensions: Sequence[str] = (".mp4", ".mov")) -> threading.Thread:
    """Discover players for common types on a daemon thread."""
    def _run():
        for extension in extensions:
            discover_players(f"probe{extension}", refresh=True)
    thread = threading.Thread(target=_run, name="frameguard-player-discovery", daemon=True)
    thread.start()
    return thread


def default_player_name(players: Sequence[Player]) -> Optional[str]:
    return next((p.name for p in players if p.is_default), None)


def find_player(players: Sequence[Player], key: str) -> Optional[Player]:
    return next((p for p in players if p.key == key), None)


def player_from_app_path(path: str) -> Optional[Player]:
    """A Player for an application the operator picked by hand."""
    if not path:
        return None
    path = os.path.abspath(path)
    if sys.platform == "darwin":
        if not (path.endswith(".app") and os.path.isdir(path)):
            return None
    elif not os.path.isfile(path):
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    key = os.path.normcase(path) if sys.platform == "win32" else path
    return Player(name=name, key=key, path=path)


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

class LaunchError(RuntimeError):
    """A launch could not even be started."""


def _launch_env() -> dict:
    """Environment without PyInstaller's bundled-library overrides."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
            original = env.pop(f"{var}_ORIG", None)
            if original is not None:
                env[var] = original
            else:
                env.pop(var, None)
        bundle_dirs = set()
        meipass = getattr(sys, "_MEIPASS", None)
        for folder in filter(None, (meipass, meipass and os.path.join(meipass, "_internal"),
                                    os.path.dirname(sys.executable))):
            bundle_dirs.add(os.path.normcase(os.path.abspath(folder)))
        if env.get("PATH"):
            env["PATH"] = os.pathsep.join(
                part for part in env["PATH"].split(os.pathsep)
                if os.path.normcase(os.path.abspath(part)) not in bundle_dirs)
    return env


def build_command(player: Player, paths: Sequence[str]) -> Optional[List[str]]:
    """The argv that opens ``paths`` in ``player``; None means os.startfile."""
    # Absolute paths only, so no file name can be read as an option.
    paths = [os.path.abspath(p) for p in paths]
    if sys.platform == "darwin":
        if player.key == DEFAULT_KEY:
            return ["/usr/bin/open", *paths]
        return ["/usr/bin/open", "-a", player.path, *paths]
    if sys.platform == "win32":
        if player.key == DEFAULT_KEY:
            return None
        return [player.path, *paths]
    if player.key == DEFAULT_KEY:
        return ["xdg-open", paths[0]]
    return [player.path, *paths]


def _spawn(command: List[str], capture_errors: bool = False):
    # Only the short-lived macOS `open` helper gets a stderr pipe. A player
    # that stays running would eventually fill an undrained pipe and stall.
    kwargs = {"env": _launch_env(), "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL,
              "stderr": subprocess.PIPE if capture_errors else subprocess.DEVNULL,
              "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _watch(proc, player: Player, on_error: Optional[Callable[[str], None]]):
    """Reap the launcher; report a non-zero exit inside the grace window."""
    def _run():
        try:
            code = proc.wait(timeout=_LAUNCH_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # Still running: a player that stays open. Reap it later so it
            # never lingers as a zombie, without holding a thread meanwhile.
            threading.Thread(target=proc.wait, daemon=True).start()
            return
        except Exception:
            return
        message = ""
        try:
            stderr = proc.stderr.read() if proc.stderr else b""
            message = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            message = message[0]
        except Exception:
            pass
        finally:
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
        if code and on_error is not None:
            detail = f": {message}" if message else f" (exit code {code})"
            on_error(f"{player.name} could not open the file{detail}")
    threading.Thread(target=_run, name="frameguard-player-launch", daemon=True).start()


def open_files(player: Player, paths: Sequence[str],
               on_error: Optional[Callable[[str], None]] = None) -> List[str]:
    """Open ``paths`` in ``player`` without blocking; return the paths used.

    Raises LaunchError when nothing could be started (no files, or the
    process could not be spawned). Later failures go to ``on_error``, which
    may be called from a worker thread.
    """
    existing = [p for p in paths if p and os.path.isfile(p)]
    if not existing:
        raise LaunchError("None of the selected files exist any more.")
    existing = existing[:MAX_FILES_PER_LAUNCH]
    if player.key != DEFAULT_KEY and player.path and not os.path.exists(player.path):
        raise LaunchError(f"{player.name} is no longer installed at {player.path}.")
    command = build_command(player, existing)
    try:
        if command is None:
            # Windows default handler: one ShellExecute per file.
            for path in existing:
                os.startfile(path)  # noqa: S606 - documented Windows API
            return existing
        if sys.platform != "darwin" and player.key == DEFAULT_KEY:
            for path in existing:
                _watch(_spawn(["xdg-open", path]), player, on_error)
            return existing
        _watch(_spawn(command, capture_errors=sys.platform == "darwin"), player, on_error)
    except OSError as error:
        raise LaunchError(f"Could not start {player.name}: {error}") from error
    return existing


def open_with_chooser(path: str) -> None:
    """Show the operating system's own "Open with" chooser (Windows only)."""
    if sys.platform != "win32":
        raise LaunchError("The system app chooser is only available on Windows.")
    try:
        _spawn(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", os.path.normpath(path)])
    except OSError as error:
        raise LaunchError(f"Could not open the app chooser: {error}") from error
