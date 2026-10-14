"""Release manifests for differential updates.

A full FrameGuard build is ~90 MB, but almost all of that is Python, Qt and
FFmpeg, which change between releases roughly never. The application's own code
is a few hundred kilobytes. Shipping the whole installer for a one-line fix
means every user downloads 90 MB to receive 200 KB of change.

A manifest records the SHA-256 of every file in a build. The client compares
the manifest of the release against its own installation and transfers only
what actually differs.

This module is deliberately free of Qt and of any FrameGuard imports: it runs
both inside the app and from the build scripts.

Manifest format (JSON):

    {
      "manifest_version": 1,
      "app_version": "0.5.3",
      "created": "2026-08-18T12:00:00",
      "platform": "Windows",
      "entry_point": "FrameGuard.exe",
      "files": {
        "FrameGuard.exe":              {"sha256": "...", "size": 1234},
        "_internal/PySide6/QtCore.pyd": {"sha256": "...", "size": 5678}
      }
    }

Paths are relative and always use forward slashes so a manifest produced on
Windows is readable on macOS.
"""

import hashlib
import json
import os
import platform
from datetime import datetime
from typing import Dict, List, Optional

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
PAYLOAD_DIRNAME = "files"

# Written by the app at runtime, so they legitimately differ from the build
# output and must never be treated as drift that needs "repairing".
EXCLUDED_NAMES = {
    "manifest.json",
    "frameguard_crash.log",
    "frameguard_error.log",
    "frameguard_fault.log",
}
EXCLUDED_DIRS = {"__pycache__", "Logs", "CrashLogs"}
EXCLUDED_SUFFIXES = (".log", ".pyc", ".pyo", ".tmp")

# Files that live in the installation but are NOT produced by the build, so
# they never appear in a manifest. They must be protected from the delete pass:
# a diff between "what the build contains" and "what is installed" would
# otherwise mark all of them as removed.
#
# Deleting unins000.exe breaks Add/Remove Programs and leaves the app
# uninstallable, which is exactly the kind of damage an updater must not do.
PROTECTED_PREFIXES = ("unins",)          # Inno Setup uninstaller + its data
PROTECTED_NAMES = {
    "frameguard.ico",                    # placed at the app root by installer.iss
}
PROTECTED_SUFFIXES = (".log", ".ini", ".bak")


def is_protected(rel_path: str) -> bool:
    """True if the installer or the user owns this file, not the build."""
    name = rel_path.split("/")[-1].lower()
    return (name.startswith(PROTECTED_PREFIXES)
            or name in PROTECTED_NAMES
            or name.endswith(PROTECTED_SUFFIXES))

_CHUNK = 1024 * 1024  # 1 MB; hashing a 90 MB tree must not load it into memory


def file_sha256(path: str) -> str:
    """SHA-256 of a file, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _should_include(rel_path: str, name: str) -> bool:
    if name in EXCLUDED_NAMES or name.lower().endswith(EXCLUDED_SUFFIXES):
        return False
    parts = rel_path.split("/")
    return not any(part in EXCLUDED_DIRS for part in parts)


def build_manifest(root: str, app_version: str = "",
                   entry_point: str = "") -> Dict:
    """Hash every file under `root` and return a manifest dict."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise ValueError(f"Not a directory: {root}")

    files: Dict[str, Dict] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not _should_include(rel, name):
                continue
            try:
                files[rel] = {
                    "sha256": file_sha256(full),
                    "size": os.path.getsize(full),
                }
            except OSError as e:
                # Skipping was NOT safe. diff_manifests() reads "present
                # locally, absent from the release" as a removal, and
                # is_protected() does not cover python312.dll, the PySide6
                # .pyds or ffmpeg.exe. One transient sharing violation on the
                # build machine (AV scanner, a mapped .pyd) therefore shipped a
                # manifest that instructed every client to delete a file it
                # needs to start — after the swap script had already discarded
                # the backup. Fail the publish instead.
                raise OSError(
                    f"Cannot read {rel!r} while building the manifest: {e}. "
                    f"Publishing would tell clients to delete this file. "
                    f"Close anything holding it open and rebuild."
                ) from e

    return {
        "manifest_version": MANIFEST_VERSION,
        "app_version": app_version,
        "created": datetime.now().isoformat(timespec="seconds"),
        "platform": platform.system(),
        # Cross-build aware: FRAMEGUARD_TARGET_ARCH is set by build_mac.sh
        # when cross-targeting (e.g. building arm64 on an x86_64 host).
        # platform.machine() alone would poison the manifest with the host
        # arch and let the updater apply an Intel build on Apple Silicon.
        "arch": os.environ.get("FRAMEGUARD_TARGET_ARCH") or platform.machine(),
        "entry_point": entry_point or _default_entry_point(),
        "files": files,
    }


def _default_entry_point() -> str:
    return "FrameGuard.exe" if os.name == "nt" else "FrameGuard"


def write_manifest(manifest: Dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return path


def _is_safe_rel_path(rel) -> bool:
    """True if a manifest path stays inside the tree it describes.

    Manifests arrive over the update channel — a folder share or an HTTP
    endpoint — and their keys are joined onto the staging directory, the
    payload directory, and (by the swap scripts, including a delete pass)
    the installation directory. An entry like ``../..`` or an absolute path
    would escape all three. Legitimate manifests come from
    build_manifest(), whose keys are os.path.relpath() output: relative,
    forward-slashed, with no dot components — so anything else is refused.
    """
    if not isinstance(rel, str) or not rel:
        return False
    if "\\" in rel or rel.startswith("/") or rel.startswith("~"):
        return False
    # Windows drive-absolute ("C:/..."): os.path.join would discard the
    # staging/install prefix entirely.
    if len(rel) >= 2 and rel[1] == ":" and rel[0].isalpha():
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def manifest_paths_are_safe(data) -> bool:
    """True if every file path in a parsed manifest is a safe relative path."""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return False
    return all(_is_safe_rel_path(rel) for rel in files)


def load_manifest(path: str) -> Optional[Dict]:
    """Read a manifest, returning None if it is missing or unusable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "files" not in data:
        return None
    if data.get("manifest_version", 0) > MANIFEST_VERSION:
        # A newer publisher format we do not understand. Refuse rather than
        # guess: a half-understood manifest could delete the wrong files.
        return None
    if not manifest_paths_are_safe(data):
        # A path that escapes the tree (see _is_safe_rel_path). Refuse the
        # whole manifest rather than guess which entries are honest.
        return None
    return data


class UpdatePlan:
    """What has to change to turn the local install into the remote release."""

    def __init__(self, add: List[str], replace: List[str], delete: List[str],
                 total_bytes: int, app_version: str = "", entry_point: str = ""):
        self.add = add
        self.replace = replace
        self.delete = delete
        self.total_bytes = total_bytes
        self.app_version = app_version
        self.entry_point = entry_point

    @property
    def transfer(self) -> List[str]:
        """Files that must actually be fetched."""
        return self.add + self.replace

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.replace or self.delete)

    @property
    def changed_count(self) -> int:
        return len(self.add) + len(self.replace) + len(self.delete)

    def summary(self) -> str:
        if self.is_empty:
            return "Already up to date."
        mb = self.total_bytes / (1024 * 1024)
        bits = []
        if self.add:
            bits.append(f"{len(self.add)} new")
        if self.replace:
            bits.append(f"{len(self.replace)} updated")
        if self.delete:
            bits.append(f"{len(self.delete)} removed")
        return f"{', '.join(bits)} \u2014 {mb:.1f} MB to download"

    def __repr__(self):
        return (f"UpdatePlan(add={len(self.add)}, replace={len(self.replace)}, "
                f"delete={len(self.delete)}, bytes={self.total_bytes})")


def diff_manifests(local: Optional[Dict], remote: Dict) -> UpdatePlan:
    """Work out which files differ between an installation and a release."""
    remote_files = remote.get("files", {})
    local_files = (local or {}).get("files", {})

    add, replace = [], []
    total = 0
    for rel, meta in sorted(remote_files.items()):
        current = local_files.get(rel)
        if current is None:
            add.append(rel)
            total += meta.get("size", 0)
        elif current.get("sha256") != meta.get("sha256"):
            replace.append(rel)
            total += meta.get("size", 0)

    # Files present locally but absent from the release are removals - except
    # for anything the installer or the user owns. Deleting unins000.exe here
    # would leave the app permanently uninstallable.
    delete = sorted(
        rel for rel in set(local_files) - set(remote_files)
        if not is_protected(rel)
    )

    return UpdatePlan(add, replace, delete, total,
                      app_version=remote.get("app_version", ""),
                      entry_point=remote.get("entry_point", _default_entry_point()))


def diff_against_install(install_root: str, remote: Dict) -> UpdatePlan:
    """Diff a release against a real directory, hashing it as it goes.

    Used instead of a stored local manifest so the plan reflects what is
    actually on disk - a file corrupted or hand-edited since install is then
    repaired rather than skipped.
    """
    local = build_manifest(install_root)
    return diff_manifests(local, remote)


def verify_tree(root: str, manifest: Dict, paths: Optional[List[str]] = None) -> List[str]:
    """Return the relative paths whose contents do not match the manifest."""
    files = manifest.get("files", {})
    targets = paths if paths is not None else list(files)
    bad = []
    for rel in targets:
        expected = files.get(rel, {}).get("sha256")
        if not expected:
            continue
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            if file_sha256(full) != expected:
                bad.append(rel)
        except OSError:
            bad.append(rel)
    return bad
