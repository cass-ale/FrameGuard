"""Windows shell integration: keep FrameGuard's own shortcuts in the theme's icon.

A running window's taskbar button shows the window icon (MainWindow sets it
per theme), but a *pinned* taskbar button, the Start menu tile and the
desktop shortcut show the icon their ``.lnk`` file names. The installer
points those at ``frameguard.ico`` in the install folder, so they stayed
Blue Hour whatever theme was chosen.

``retheme_shortcuts`` writes the theme's icon to a per-user cache
(``%LOCALAPPDATA%\\FrameGuard\\icons\\<theme>.ico``; one file per theme so the
shell's icon cache, which is keyed by path, never serves a stale image),
then points every shortcut whose target is this FrameGuard.exe at it and
tells Explorer the items changed. Chrome re-badges its pinned profile
shortcuts the same way. Only shortcuts that launch this very executable are
touched; anything else, and every failure, is left alone - a shortcut icon
is cosmetic and must never cost a crash or a modal.

Explorer may keep showing the previous image on an already-pinned button
until it next redraws the taskbar (sign-out, or the button being re-laid
out); the Start menu and desktop update at once.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
_lock = threading.Lock()


# Known-folder IDs (KnownFolders.h). Resolved through the shell rather than
# %APPDATA% / ~\Desktop, which miss folders redirected to OneDrive or a share.
_FOLDERID_ROAMING_APPDATA = "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}"
_FOLDERID_PROGRAMS = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def _known_folder(folder_id: str) -> Optional[str]:
    """SHGetKnownFolderPath, or None (off Windows, or the folder is absent)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                        ("d", ctypes.c_ubyte * 8)]
        guid = GUID()
        if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid)) != 0:
            return None
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                      ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except (OSError, AttributeError, ValueError):
        return None


def shortcut_dirs() -> List[str]:
    """Per-user folders that hold FrameGuard's shortcuts: the pinned taskbar
    and Start items, the user's Start menu (where the per-user installer puts
    its shortcuts; it never writes the all-users ProgramData menu, which a
    standard user cannot modify anyway) and the desktop."""
    appdata = _known_folder(_FOLDERID_ROAMING_APPDATA) or os.environ.get("APPDATA", "")
    pinned = os.path.join(appdata, r"Microsoft\Internet Explorer\Quick Launch\User Pinned") if appdata else ""
    dirs = [
        os.path.join(pinned, "TaskBar") if pinned else "",
        os.path.join(pinned, "StartMenu") if pinned else "",
        _known_folder(_FOLDERID_PROGRAMS) or (os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs") if appdata else ""),
        _known_folder(_FOLDERID_DESKTOP) or os.path.join(os.path.expanduser("~"), "Desktop"),
    ]
    seen, result = set(), []
    for d in dirs:
        key = os.path.normcase(d)
        if d and key not in seen and os.path.isdir(d):
            seen.add(key)
            result.append(d)
    return result


def icon_cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FrameGuard", "icons")


def icon_file_name(theme_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", theme_name.lower()).strip("-") or "theme"
    return f"frameguard-{slug}.ico"


def write_theme_icon(icon, theme_name: str, directory: Optional[str] = None) -> Optional[str]:
    """Write `icon` (a QIcon) as a multi-resolution .ico; returns its path.

    Qt's ICO writer stores one image per file, so the sizes are packed here:
    PNG-compressed entries (Vista+), which every supported Windows reads.
    """
    import struct
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize

    directory = directory or icon_cache_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, icon_file_name(theme_name))
    images = []
    for px in _ICON_SIZES:
        pixmap = icon.pixmap(QSize(px, px))
        if pixmap.isNull():
            continue
        if pixmap.width() != px:
            pixmap = pixmap.scaled(px, px)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        pixmap.toImage().save(buffer, "PNG")
        buffer.close()
        images.append((px, bytes(data)))
    if not images:
        return None
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for px, blob in images:
        side = 0 if px >= 256 else px  # 0 means 256 in an ICONDIRENTRY
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(header + entries + blobs)
    os.replace(tmp, path)
    return path


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def _iter_shortcuts(dirs: Iterable[str]) -> Iterable[str]:
    for root in dirs:
        # Shallow: the Start menu keeps our shortcuts one folder down
        # ("FrameGuard Desktop\\"); never walk a whole profile tree.
        for depth_root, subdirs, files in os.walk(root):
            if depth_root.count(os.sep) - root.count(os.sep) >= 2:
                subdirs[:] = []
            for name in files:
                if name.lower().endswith(".lnk"):
                    yield os.path.join(depth_root, name)


def _is_frameguard_icon(icon_location: str, target_exe: str) -> bool:
    """True when a shortcut still shows a FrameGuard icon we may replace: none
    set (the exe's own), the exe, the installer's frameguard.ico, or one of
    our cached theme icons. A user's own custom icon is never overwritten."""
    if not icon_location:
        return True
    location = os.path.normcase(os.path.abspath(os.path.expandvars(icon_location)))
    install_dir = os.path.normcase(os.path.dirname(os.path.abspath(target_exe)))
    cache_dir = os.path.normcase(os.path.abspath(icon_cache_dir()))
    if _same_file(location, target_exe):
        return True
    if os.path.dirname(location) == install_dir and os.path.basename(location) == "frameguard.ico":
        return True
    return os.path.dirname(location) == cache_dir and os.path.basename(location).startswith("frameguard-")


_pending_icon: Optional[str] = None
_worker: Optional[threading.Thread] = None


def request_retheme(icon_path: str) -> None:
    """Queue a re-theme for `icon_path`; the latest request wins.

    One worker thread drains the queue, so rapid theme switches can never
    finish out of order and leave the shortcuts on an earlier theme.
    """
    global _pending_icon, _worker
    with _lock:
        _pending_icon = icon_path
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_drain, name="fg-shortcut-icons", daemon=True)
        _worker.start()


def _drain() -> None:
    global _pending_icon
    while True:
        with _lock:
            icon_path, _pending_icon = _pending_icon, None
        if icon_path is None:
            return
        retheme_shortcuts(icon_path)


def retheme_shortcuts(icon_path: str, target_exe: Optional[str] = None,
                      dirs: Optional[Iterable[str]] = None) -> List[str]:
    """Point every shortcut that launches `target_exe` at `icon_path`.

    Returns the shortcuts changed. No-op (empty list) off Windows, when not
    running as the installed executable, or when COM is unavailable.
    """
    if sys.platform != "win32":
        return []
    target_exe = target_exe or (sys.executable if getattr(sys, "frozen", False) else None)
    if not target_exe or not icon_path or not os.path.isfile(icon_path):
        return []
    try:
        shortcuts = list(_iter_shortcuts(dirs if dirs is not None else shortcut_dirs()))
        return _retarget_icons(shortcuts, target_exe, icon_path)
    except Exception as exc:  # noqa: BLE001 - cosmetic; never break the app
        log.warning("Could not re-theme shortcuts: %s", exc)
        return []


def _retarget_icons(shortcuts: List[str], target_exe: str, icon_path: str) -> List[str]:
    """IShellLinkW / IPersistFile through ctypes (no pywin32 dependency)."""
    changed = []
    link = _ShellLink()
    try:
        for path in shortcuts:
            # One unreadable, foreign or corrupt shortcut must not stop the
            # rest (a read-only all-users shortcut used to end the walk
            # before the desktop was reached).
            try:
                if not link.load(path):
                    continue
                target = link.target()
                if not target or not _same_file(target, target_exe):
                    continue
                current, index = link.icon_location()
                if _same_file(current, icon_path) and index == 0:
                    continue
                if not _is_frameguard_icon(current, target_exe):
                    continue
                if link.set_icon_location(icon_path, 0) and link.save(path):
                    changed.append(path)
                    _notify_updated(path)
            except OSError as exc:
                log.debug("Skipped shortcut %s: %s", path, exc)
    finally:
        link.release()
    if changed:
        _notify_assoc_changed()
    return changed


def _notify_updated(path: str) -> None:
    import ctypes
    SHCNE_UPDATEITEM, SHCNF_PATHW = 0x00002000, 0x0005
    ctypes.windll.shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, ctypes.c_wchar_p(path), None)


def _notify_assoc_changed() -> None:
    import ctypes
    SHCNE_ASSOCCHANGED, SHCNF_IDLIST = 0x08000000, 0x0000
    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


class _ShellLink:
    """Minimal ctypes binding of CLSID_ShellLink (IShellLinkW + IPersistFile)."""

    _CLSID_ShellLink = "{00021401-0000-0000-C000-000000000046}"
    _IID_IShellLinkW = "{000214F9-0000-0000-C000-000000000046}"
    _IID_IPersistFile = "{0000010B-0000-0000-C000-000000000046}"
    # IShellLinkW vtable slots (after IUnknown's 3).
    _GET_PATH, _GET_ICON_LOCATION, _SET_ICON_LOCATION = 3, 16, 17
    # IPersistFile slots: GetClassID 3, IsDirty 4, Load 5, Save 6.
    _LOAD, _SAVE = 5, 6

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes, self._wintypes = ctypes, wintypes
        ole32 = ctypes.windll.ole32
        self._uninit = ole32.CoInitializeEx(None, 0x2) in (0, 1)  # APARTMENTTHREADED
        clsid, iid = self._guid(self._CLSID_ShellLink), self._guid(self._IID_IShellLinkW)
        self._link = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(self._link))
        if hr != 0:
            raise OSError(f"CoCreateInstance(ShellLink) failed: 0x{hr & 0xFFFFFFFF:08X}")
        self._file = ctypes.c_void_p()
        iid_pf = self._guid(self._IID_IPersistFile)
        hr = self._call(self._link, 0, ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)(
            self._link, ctypes.byref(iid_pf), ctypes.byref(self._file))
        if hr != 0:
            raise OSError(f"QueryInterface(IPersistFile) failed: 0x{hr & 0xFFFFFFFF:08X}")

    def _guid(self, text):
        ctypes = self._ctypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                        ("d", ctypes.c_ubyte * 8)]
        guid = GUID()
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
        return guid

    def _call(self, obj, slot, restype, *argtypes):
        ctypes = self._ctypes
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])

    def load(self, path: str) -> bool:
        ctypes = self._ctypes
        return self._call(self._file, self._LOAD, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_uint)(
            self._file, path, 0) == 0  # STGM_READ: Save(path) writes regardless

    def save(self, path: str) -> bool:
        ctypes = self._ctypes
        return self._call(self._file, self._SAVE, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_int)(
            self._file, path, 1) == 0

    def target(self) -> str:
        ctypes = self._ctypes
        buf = ctypes.create_unicode_buffer(1024)
        hr = self._call(self._link, self._GET_PATH, ctypes.HRESULT, ctypes.c_wchar_p, ctypes.c_int,
                        ctypes.c_void_p, ctypes.c_uint)(self._link, buf, 1024, None, 0)
        return buf.value if hr == 0 else ""

    def icon_location(self):
        ctypes = self._ctypes
        buf, index = ctypes.create_unicode_buffer(1024), ctypes.c_int(0)
        hr = self._call(self._link, self._GET_ICON_LOCATION, ctypes.HRESULT, ctypes.c_wchar_p,
                        ctypes.c_int, ctypes.c_void_p)(self._link, buf, 1024, ctypes.byref(index))
        return (buf.value, index.value) if hr == 0 else ("", 0)

    def set_icon_location(self, path: str, index: int) -> bool:
        ctypes = self._ctypes
        return self._call(self._link, self._SET_ICON_LOCATION, ctypes.HRESULT, ctypes.c_wchar_p,
                          ctypes.c_int)(self._link, path, index) == 0

    def release(self):
        ctypes = self._ctypes
        for obj in (getattr(self, "_file", None), getattr(self, "_link", None)):
            if obj:
                self._call(obj, 2, ctypes.c_ulong)(obj)
        if getattr(self, "_uninit", False):
            ctypes.windll.ole32.CoUninitialize()
