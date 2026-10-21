#!/usr/bin/env python3
"""FrameGuard Desktop -- Tool Downloader (build-time only).

Downloads FFmpeg, FFprobe and MediaInfo and stages them next to the built
application. Called by build_installer.bat (Windows) and build_mac.sh (macOS).
End users never run this.

    python download_tools.py                        # auto-detect platform
    python download_tools.py --platform darwin      # cross-inspect
    python download_tools.py --dest ~/Library/Caches/FrameGuard/src/dist/FrameGuard.app/Contents/Resources/tools
    python download_tools.py --verify-only          # check the cache, download nothing

Binaries are cached per-platform under tools_cache/<platform>/ so switching
between a Windows and a macOS build on the same checkout doesn't clobber the
other platform's tools.
"""

import argparse
import os
import platform as _platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# The team checkout lives under a path containing ⭐ (U+2B50). Windows consoles
# default to cp1252, so printing any path inside this repo raises
# UnicodeEncodeError. Force UTF-8 for stdout/stderr so build-time scripts work
# from this checkout.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
TOOLS_CACHE_ROOT = SCRIPT_DIR / "tools_cache"

# ---------------------------------------------------------------------------
# Per-platform sources
#
# Every artifact is pinned: an immutable, versioned URL (optionally a byte-
# identical mirror of the same file) plus the SHA-256 of that exact file,
# committed here. fetch() refuses any download whose hash differs, and there
# are no moving "latest"/nightly fallbacks: a build either bundles exactly
# these bytes or fails. Bump a tool deliberately, like a dependency: change
# the version, download the new artifact, verify it against the publisher's
# own checksum where one exists, and update the hash in the same commit.
#
# macOS: ffmpeg and ffprobe ship as SEPARATE archives, unlike the Windows
# builds which ship both in one zip; each archive is its own entry.
#
# The previous macOS source, evermeet.cx, returns HTTP 410 Gone for every
# getrelease download (verified live 2026-10-14) and its builds were
# Intel-only. macOS tools come from Martin Riedl's build site, which
# publishes per-architecture zips (arm64 and amd64) in timestamped,
# never-reused build directories with a .sha256 sidecar per archive.
#
# Pins verified 2026-10-21:
#   - Windows FFmpeg 9.0.2 essentials (Gyan Doshi): the GitHub release asset
#     and the versioned gyan.dev package are byte-identical and match both
#     gyan.dev's published .sha256 and GitHub's asset digest.
#   - macOS FFmpeg/FFprobe 9.0.2 (Martin Riedl): each hash matches the
#     publisher's .sha256 sidecar; the binaries report "ffmpeg version 9.0.2"
#     and are thin Mach-O for the expected architecture.
#   - MediaInfo CLI 26.10 (MediaArea, current release): MediaArea publishes
#     no checksum, so each hash was confirmed by two independent downloads.
# ---------------------------------------------------------------------------
FFMPEG_VERSION = "9.0.2"
MEDIAINFO_VERSION = "26.10"

_WINDOWS_FFMPEG_ARCHIVE = f"ffmpeg-{FFMPEG_VERSION}-essentials_build.zip"
_WINDOWS_FFMPEG_URLS = [
    # Primary: Gyan Doshi's official GitHub release (immutable asset).
    f"https://github.com/GyanD/codexffmpeg/releases/download/"
    f"{FFMPEG_VERSION}/{_WINDOWS_FFMPEG_ARCHIVE}",
    # Mirror: the same file under gyan.dev's versioned package path.
    f"https://www.gyan.dev/ffmpeg/builds/packages/{_WINDOWS_FFMPEG_ARCHIVE}",
]
_WINDOWS_FFMPEG_SHA256 = (
    "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba")

_RIEDL_BASE = "https://ffmpeg.martin-riedl.de"
_RIEDL_RELEASES = {  # pinned build directories, per architecture segment
    "arm64": "1789931890_9.0.2",
    "amd64": "1789931006_9.0.2",
}
_RIEDL_SHA256 = {  # (architecture segment, tool) -> archive SHA-256
    ("arm64", "ffmpeg"):
        "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924",
    ("arm64", "ffprobe"):
        "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6",
    ("amd64", "ffmpeg"):
        "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4",
    ("amd64", "ffprobe"):
        "2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd",
}

_MEDIAINFO_BASE = (
    f"https://mediaarea.net/download/binary/mediainfo/{MEDIAINFO_VERSION}")
_MEDIAINFO_WINDOWS_URL = (
    f"{_MEDIAINFO_BASE}/MediaInfo_CLI_{MEDIAINFO_VERSION}_Windows_x64.zip")
_MEDIAINFO_WINDOWS_SHA256 = (
    "4cc94bd272903914c04f4f9a9f53a6c94b5ea6c6454cf738d29fa182c41e8a29")
_MEDIAINFO_MAC_URL = f"{_MEDIAINFO_BASE}/MediaInfo_CLI_{MEDIAINFO_VERSION}_Mac.dmg"
_MEDIAINFO_MAC_SHA256 = (
    "2345a52f6e5281a15e9ef87acbe5756a9c4727d5629873c013f148a8f82525ab")


def _norm_arch(arch: str) -> str:
    """Normalize an architecture name to 'arm64' or 'x86_64'."""
    a = (arch or "").lower()
    if a in ("arm64", "aarch64"):
        return "arm64"
    if a in ("x86_64", "amd64", "intel"):
        return "x86_64"
    raise SystemExit(f"Unsupported architecture: {arch!r} (expected arm64 or x86_64)")


def _riedl_segment(arch: str) -> str:
    return "arm64" if _norm_arch(arch) == "arm64" else "amd64"


def _riedl_entry(tool: str, arch: str) -> dict:
    seg = _riedl_segment(arch)
    return {
        "required": True,
        "members": [tool],
        "urls": [f"{_RIEDL_BASE}/download/macos/{seg}/"
                 f"{_RIEDL_RELEASES[seg]}/{tool}.zip"],
        "sha256": _RIEDL_SHA256[(seg, tool)],
    }


def _darwin_sources(arch: str) -> dict:
    return {
        "suffix": "",
        "ffmpeg": _riedl_entry("ffmpeg", arch),
        "ffprobe": _riedl_entry("ffprobe", arch),
        "mediainfo": {
            "required": False,
            "members": ["mediainfo"],
            # MediaArea's Mac CLI ships as a DMG whose internal layout
            # changes between versions (loose binary, .app bundle, or an
            # installer .pkg — the 26.05 DMG contained no loose binary and
            # the tool was silently absent from every v0.5.5 Mac build).
            # The extractor therefore DISCOVERS the payload (loose files,
            # .app contents, expanded .pkg payloads) instead of assuming
            # one layout. MediaInfo is optional (the app degrades), so a
            # miss still does not fail the build. Pinned to the current
            # universal (x86_64+arm64) CLI release. .dmg extraction
            # requires a Mac host (hdiutil); on other hosts the archive
            # is cached and extraction is retried on the Mac.
            "urls": [_MEDIAINFO_MAC_URL],
            "sha256": _MEDIAINFO_MAC_SHA256,
        },
    }


SOURCES = {
    "windows": {
        "suffix": ".exe",
        "ffmpeg": {
            "required": True,
            "members": ["ffmpeg.exe", "ffprobe.exe"],
            "urls": list(_WINDOWS_FFMPEG_URLS),
            "sha256": _WINDOWS_FFMPEG_SHA256,
        },
        "mediainfo": {
            "required": False,
            "members": ["mediainfo.exe"],
            "urls": [_MEDIAINFO_WINDOWS_URL],
            "sha256": _MEDIAINFO_WINDOWS_SHA256,
        },
    },
    # "darwin" is intentionally absent here: its sources are
    # per-architecture and are built by _darwin_sources(arch) at runtime.
}


def sources_for(target: str, arch: str = None) -> dict:
    """The full tool spec for a platform.

    darwin is per-architecture (different download URLs per arch), so it is
    built on demand; every other platform reads the static SOURCES table.
    This is the module's public contract: callers and tests resolve specs
    here instead of assuming every platform is a static table entry.
    """
    if target == "darwin":
        return _darwin_sources(_norm_arch(arch or _platform.machine()))
    return SOURCES[target]


def detect_platform() -> str:
    system = _platform.system()
    if system == "Darwin":
        return "darwin"
    if system == "Windows":
        return "windows"
    raise SystemExit(f"Unsupported build platform: {system}")


def cache_dir(target: str, arch: str = None) -> Path:
    # macOS tools are per-architecture: an arm64 ffmpeg and an x86_64 ffmpeg
    # must never share a cache directory, or whichever downloaded first
    # would be silently bundled into both builds.
    if target == "darwin":
        return TOOLS_CACHE_ROOT / f"darwin-{_norm_arch(arch or _platform.machine())}"
    return TOOLS_CACHE_ROOT / target


def _legacy_cache_files(target: str):
    """Pre-0.5.2 builds cached Windows binaries directly in tools_cache/."""
    if target != "windows":
        return []
    return [p for p in TOOLS_CACHE_ROOT.glob("*.exe") if p.is_file()]


def migrate_legacy_cache(target: str):
    dest = cache_dir(target)
    for old in _legacy_cache_files(target):
        dest.mkdir(parents=True, exist_ok=True)
        new = dest / old.name
        if not new.exists():
            shutil.move(str(old), str(new))
            print(f"  Migrated cached {old.name} -> tools_cache/{target}/")


def download_file(url: str, dest: Path, label: str = ""):
    """Download url to dest. Returns (ok, final_url_after_redirects)."""
    import urllib.request

    print(f"  Downloading {label or url.split('/')[-1]}...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FrameGuard-Builder/1.0"})
        with urllib.request.urlopen(req, timeout=180) as response:
            final_url = response.geturl()
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded / total * 100)
                        print(f"\r  Progress: {pct}% "
                              f"({downloaded // (1024*1024)}MB / {total // (1024*1024)}MB)",
                              end="", flush=True)
            if total:
                print()
        return True, final_url
    except Exception as e:
        print(f"\n  ERROR: Download failed: {e}")
        if dest.exists():
            dest.unlink()
        return False, None


def _sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_SHA256_HEX = frozenset("0123456789abcdef")


def is_valid_sha256(value) -> bool:
    """True for a lowercase 64-character hex SHA-256 digest."""
    return (isinstance(value, str) and len(value) == 64
            and set(value) <= _SHA256_HEX)


# Records, per cache directory, the pinned archive hash each cached tool was
# extracted from. A cache filled from an older (or formerly unpinned) source
# no longer matches the pin, so it is re-fetched instead of silently bundled.
_CACHE_STAMP = ".frameguard-tool-pins.json"


def _read_cache_stamp(cache: Path) -> dict:
    import json
    try:
        data = json.loads((cache / _CACHE_STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache_stamp(cache: Path, name: str, sha256: str) -> None:
    import json
    data = _read_cache_stamp(cache)
    data[name] = sha256
    (cache / _CACHE_STAMP).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def is_cached(name: str, entry: dict, cache: Path) -> bool:
    """True when every member is cached AND came from the pinned archive."""
    if any(not (cache / m).is_file() for m in entry["members"]):
        return False
    return _read_cache_stamp(cache).get(name) == entry.get("sha256")


def make_executable(path: Path):
    """Set the executable bit. zipfile does NOT restore Unix permissions, so a
    macOS binary extracted from a zip is non-executable and every scan would
    fail with PermissionError."""
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def extract_members(archive: Path, wanted: list, dest_dir: Path) -> set:
    """Pull the named basenames out of a .zip, .tar.* or .dmg archive."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    found = set()
    wanted_lower = {w.lower() for w in wanted}

    if archive.suffix.lower() == ".dmg":
        return _extract_dmg_members(archive, wanted, dest_dir)

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    base = os.path.basename(info.filename)
                    if base.lower() in wanted_lower:
                        out = dest_dir / base
                        with zf.open(info) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        make_executable(out)
                        found.add(base.lower())
        else:
            import tarfile
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    base = os.path.basename(member.name)
                    if member.isfile() and base.lower() in wanted_lower:
                        extracted = tf.extractfile(member)
                        if extracted is None:
                            continue
                        out = dest_dir / base
                        with extracted as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        make_executable(out)
                        found.add(base.lower())
    except Exception as e:
        print(f"  ERROR: Extraction failed: {e}")

    return found


def _extract_pkg_payload(pkg_path: Path, wanted_lower: set,
                         dest_dir: Path, found: set) -> None:
    """Fallback for a flat .pkg: pull the Payload out of the xar container
    and explode the cpio with stock tools (xar, zcat and cpio all ship with
    macOS), then collect the wanted basenames into dest_dir / found.

    Exists because pkgutil --expand-full failed silently in the v0.5.6
    release run (the 26.10 MediaInfo DMG's pkg holds exactly one file,
    usr/local/bin/mediainfo, verified by opening the pinned DMG). Every
    stage here logs on failure, so a miss names itself in the build log.
    """
    import tempfile as _tf
    work = Path(_tf.mkdtemp(prefix="fg_xar_"))
    try:
        r = subprocess.run(["xar", "-xf", str(pkg_path), "-C", str(work)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"  xar fallback failed for {pkg_path.name}: "
                  f"{(r.stderr or r.stdout)[:200]}")
            return
        payloads = [q for q in work.rglob("Payload") if q.is_file()]
        if not payloads:
            print(f"  payload fallback: no Payload inside {pkg_path.name}")
            return
        out = work / "payload_out"
        out.mkdir()
        for payload in payloads:
            z = subprocess.Popen(["zcat", str(payload)],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
            c = subprocess.run(["cpio", "-idm"], cwd=out, stdin=z.stdout,
                               capture_output=True, text=True, timeout=120)
            if z.stdout:
                z.stdout.close()
            z.wait()
            if c.returncode != 0:
                print(f"  cpio fallback failed for {pkg_path.name}: "
                      f"{(c.stderr or '')[:200]}")
        for broot, _bdirs, bfiles in os.walk(out):
            for bfn in bfiles:
                if bfn.lower() in wanted_lower:
                    shutil.copy2(Path(broot) / bfn, dest_dir / bfn)
                    make_executable(dest_dir / bfn)
                    found.add(bfn.lower())
    except Exception as e:
        print(f"  payload fallback failed for {pkg_path.name}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


_HDIUTIL_ATTEMPTS = 3
_HDIUTIL_BACKOFF_SECONDS = (1, 2)


def _diagnostic_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _run_hdiutil(args: list, operation: str, timeout: int,
                   attempts: int = _HDIUTIL_ATTEMPTS):
    """Run hdiutil with bounded retries and complete failure diagnostics."""
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                ["hdiutil"] + list(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            print(f"  ERROR: hdiutil {operation} timed out "
                  f"(attempt {attempt}/{attempts}, {timeout}s)")
            stdout = _diagnostic_text(exc.stdout)
            stderr = _diagnostic_text(exc.stderr)
        except OSError as exc:
            print(f"  ERROR: hdiutil {operation} could not start "
                  f"(attempt {attempt}/{attempts}): {exc}")
            stdout = ""
            stderr = ""
        else:
            if result.returncode == 0:
                return result
            print(f"  ERROR: hdiutil {operation} exited {result.returncode} "
                  f"(attempt {attempt}/{attempts})")
            stdout = _diagnostic_text(result.stdout)
            stderr = _diagnostic_text(result.stderr)

        if stdout:
            print(f"  hdiutil {operation} stdout:\n{stdout.rstrip()}")
        if stderr:
            print(f"  hdiutil {operation} stderr:\n{stderr.rstrip()}")
        if attempt < attempts:
            delay = _HDIUTIL_BACKOFF_SECONDS[
                min(attempt - 1, len(_HDIUTIL_BACKOFF_SECONDS) - 1)
            ]
            print(f"  Retrying hdiutil {operation} in {delay}s...")
            time.sleep(delay)
    return None


def _extract_dmg_members(archive: Path, wanted: list, dest_dir: Path) -> set:
    """Pull files out of a macOS .dmg (mediaarea ships MediaInfo this way).

    Requires a Mac host (hdiutil). On other hosts the caller reports clearly
    and the archive is retried on the Mac build host.
    Returns the set of basenames found (lowercased).
    """
    wanted_lower = {w.lower() for w in wanted}
    found = set()
    if _platform.system() != "Darwin":
        print("  .dmg extraction needs a Mac (hdiutil). "
              "The archive will be retried on the Mac build host.")
        return found
    import tempfile as _tf
    mount = Path(_tf.mkdtemp(prefix="fg_dmg_"))
    attached = False
    detached = True
    try:
        result = _run_hdiutil(
            ["attach", "-nobrowse", "-readonly", "-mountpoint",
             str(mount), str(archive)],
            "attach", timeout=120,
        )
        if result is None:
            print(f"  MediaInfo remains optional; retaining no files from "
                  f"{archive.name} after attach retries.")
            return found
        attached = True
        try:
            for root, dirs, files in os.walk(mount):
                for fn in files:
                    if fn.lower() in wanted_lower:
                        src = Path(root) / fn
                        # mediainfo in the dmg may be inside MediaInfo CLI.app;
                        # copy the binary itself, not the .app wrapper.
                        out = dest_dir / fn
                        shutil.copy2(src, out)
                        make_executable(out)
                        found.add(fn.lower())
                # Bundles are directories, so they never show up in the
                # file list above (an earlier version checked files for a
                # .app suffix, which could never match). Search inside any
                # .app bundle for the wanted binaries.
                for dn in dirs:
                    if dn.endswith(".app"):
                        for broot, _bdirs, bfiles in os.walk(Path(root) / dn):
                            for bfn in bfiles:
                                if bfn.lower() in wanted_lower:
                                    shutil.copy2(Path(broot) / bfn, dest_dir / bfn)
                                    make_executable(dest_dir / bfn)
                                    found.add(bfn.lower())
            if not found:
                # Recent MediaArea Mac DMGs ship an installer package
                # instead of a loose binary. Expand any .pkg payload and
                # search inside it; discovering the payload per pinned
                # version beats hard-coding a layout that changes.
                for root, _dirs, files in os.walk(mount):
                    for fn in files:
                        if not fn.lower().endswith(".pkg"):
                            continue
                        # pkgutil --expand-full creates the destination
                        # itself and refuses a directory that already
                        # exists, so pass a not-yet-existing child of a
                        # fresh temp dir. (Handing it the mkdtemp directory
                        # directly made every expansion fail, and the old
                        # code swallowed the error silently: the v0.5.6
                        # release run shipped without mediainfo and the log
                        # showed no reason.)
                        pkg_root = Path(_tf.mkdtemp(prefix="fg_pkg_"))
                        expanded = pkg_root / "expanded"
                        before = len(found)
                        try:
                            r2 = subprocess.run(
                                ["pkgutil", "--expand-full",
                                 str(Path(root) / fn), str(expanded)],
                                capture_output=True, text=True, timeout=120)
                            if r2.returncode != 0:
                                print(f"  pkgutil --expand-full failed for "
                                      f"{fn}: "
                                      f"{(r2.stderr or r2.stdout)[:200]}")
                            else:
                                for broot, _bdirs, bfiles in os.walk(expanded):
                                    for bfn in bfiles:
                                        if bfn.lower() in wanted_lower:
                                            shutil.copy2(Path(broot) / bfn,
                                                         dest_dir / bfn)
                                            make_executable(dest_dir / bfn)
                                            found.add(bfn.lower())
                        finally:
                            shutil.rmtree(pkg_root, ignore_errors=True)
                        if len(found) == before:
                            # pkgutil produced nothing usable (its failure modes
                            # are opaque and version-dependent). Fall back to
                            # exploding the package payload directly with stock
                            # tools, so a .pkg can never silently yield nothing.
                            _extract_pkg_payload(Path(root) / fn, wanted_lower,
                                                 dest_dir, found)
        finally:
            result = _run_hdiutil(
                ["detach", str(mount)], "detach", timeout=60)
            if result is None:
                print("  Normal hdiutil detach retries exhausted; "
                      "trying one forced detach.")
                result = _run_hdiutil(
                    ["detach", "-force", str(mount)],
                    "forced detach", timeout=60, attempts=1,
                )
            detached = result is not None
            if not detached:
                print(f"  ERROR: could not detach MediaInfo image; "
                      f"mount retained for manual cleanup: {mount}")
    except Exception as e:
        print(f"  ERROR: .dmg extraction failed: {e}")
    finally:
        if not attached or detached:
            shutil.rmtree(mount, ignore_errors=True)
    return found


def verify_binary(path: Path) -> bool:
    """Actually run the tool. Catches a missing +x bit or a wrong-arch binary
    at build time instead of at the user's first scan."""
    flag = "--Version" if path.stem.lower() == "mediainfo" else "-version"
    try:
        result = subprocess.run(
            [str(path), flag], capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        first = (result.stdout or result.stderr or "").strip().split("\n")[0]
        if result.returncode == 0 and first:
            print(f"    {path.name}: {first[:70]}")
            return True
        print(f"    {path.name}: exited {result.returncode}")
        return False
    except PermissionError:
        print(f"    {path.name}: NOT EXECUTABLE (missing +x bit)")
        return False
    except OSError as e:
        # Cross-platform inspection can't run the other OS's binaries; that is
        # expected and not a build failure.
        print(f"    {path.name}: cannot execute here ({e.__class__.__name__})")
        return None
    except subprocess.TimeoutExpired:
        print(f"    {path.name}: timed out")
        return False


def fetch(target: str, name: str, entry: dict, cache: Path, force: bool) -> bool:
    members = entry["members"]
    expected = entry.get("sha256")
    if not is_valid_sha256(expected):
        # Fail closed: an entry without a pinned hash is a configuration
        # error, never a reason to trust whatever the URL serves today.
        print(f"\n[{name}] ERROR: no pinned SHA-256 for this tool; "
              f"refusing to download it.")
        return False
    if not force and is_cached(name, entry, cache):
        print(f"[{name}] Using cached copy (pinned {expected[:12]}...).")
        return True

    print(f"\n[{name}] Fetching {', '.join(members)}...")
    for url in entry["urls"]:
        if url.endswith(".tar.bz2"):
            suffix = ".tar.bz2"
        elif url.endswith(".dmg"):
            suffix = ".dmg"
        else:
            suffix = ".zip"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            ok, _final_url = download_file(url, tmp_path, name)
            if not ok:
                continue
            actual = _sha256_of(tmp_path)
            if actual != expected:
                print(f"  ERROR: SHA-256 mismatch for {url}\n"
                      f"    expected {expected}\n"
                      f"    got      {actual}\n"
                      f"  Refusing this download.")
                continue
            print(f"  SHA-256 verified ({expected[:12]}...).")
            found = extract_members(tmp_path, members, cache)
            if found >= {m.lower() for m in members}:
                _write_cache_stamp(cache, name, expected)
                print(f"  {name} ready.")
                return True
            print(f"  Archive did not contain {members}; trying next source.")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    print(f"  ERROR: could not obtain {name}.")
    return False


def main():
    parser = argparse.ArgumentParser(description="Download FrameGuard's bundled tools.")
    parser.add_argument("--platform", default="auto",
                        choices=["auto", "windows", "darwin"],
                        help="Target platform (default: auto-detect the host).")
    parser.add_argument("--arch", default=None,
                        help="Tool architecture for macOS: arm64 or x86_64 "
                             "(default: this machine's architecture). "
                             "Ignored on Windows.")
    parser.add_argument("--dest", type=Path, default=None,
                        help="Directory to stage tools into "
                             "(default: the platform's usual build output).")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if cached.")
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify the cache without downloading or staging.")
    args = parser.parse_args()

    target = detect_platform() if args.platform == "auto" else args.platform
    try:
        host = detect_platform()
    except SystemExit:
        # Cross-inspection from a third platform (e.g. reviewing the macOS
        # tool set from Linux): the documented --platform flag must still
        # work; host-dependent verification is simply skipped (host=None
        # never equals target below).
        host = None
    arch = (_norm_arch(args.arch or _platform.machine())
            if target == "darwin" else None)
    cache = cache_dir(target, arch)
    cache.mkdir(parents=True, exist_ok=True)
    migrate_legacy_cache(target)

    print(f"Target platform : {target}")
    if arch:
        print(f"Target arch     : {arch}")
    print(f"Cache directory : {cache}")

    spec = sources_for(target, arch)
    entries = {k: v for k, v in spec.items() if isinstance(v, dict)}

    required_ok = True
    if not args.verify_only:
        for name, entry in entries.items():
            if not fetch(target, name, entry, cache, args.force) and entry["required"]:
                required_ok = False

    # --- Verify ---
    # Only tools extracted from their pinned archive count. Files left in
    # the cache by an older or unverified source (or by a failed fetch) are
    # never verified, staged or shipped.
    print("\n[Verify] Checking binaries...")
    pinned = {name: is_cached(name, entry, cache)
              for name, entry in entries.items()}
    for name, entry in entries.items():
        if not pinned[name]:
            if entry["required"]:
                print(f"    {name}: MISSING (no copy from the pinned archive)")
                required_ok = False
            else:
                print(f"    {name}: not available (optional)")
            continue
        for member in entry["members"]:
            path = cache / member
            if target == host:
                if verify_binary(path) is False and entry["required"]:
                    required_ok = False
            else:
                print(f"    {member}: present ({path.stat().st_size // 1024} KB, "
                      f"not run - cross-platform build)")

    if args.verify_only:
        return 0 if required_ok else 1

    # --- Stage ---
    dest = args.dest
    if dest is None:
        if target == "darwin":
            dest = (Path.home() / "Library" / "Caches" / "FrameGuard" /
                    SCRIPT_DIR.name / "dist" / "FrameGuard.app" / "Contents" /
                    "Resources" / "tools")
        else:
            dest = SCRIPT_DIR / "dist" / "FrameGuard" / "tools"
    dest = Path(dest)

    if dest.parent.exists() or dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
        print(f"\n[Stage] -> {dest}")
        for name, entry in entries.items():
            if not pinned[name]:
                continue
            for member in entry["members"]:
                src = cache / member
                if src.is_file():
                    shutil.copy2(src, dest / member)
                    make_executable(dest / member)
                    print(f"  {member}")
    else:
        print(f"\n[Stage] Skipped - {dest.parent} does not exist yet "
              f"(run this after PyInstaller).")

    if not required_ok:
        print("\nWARNING: required tools are missing. The app will build, but "
              "video scanning will not work until FFmpeg is available.")
        return 1

    print("\nAll tools ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
