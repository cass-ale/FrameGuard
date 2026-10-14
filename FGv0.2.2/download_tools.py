#!/usr/bin/env python3
"""FrameGuard Desktop -- Tool Downloader (build-time only).

Downloads FFmpeg, FFprobe and MediaInfo and stages them next to the built
application. Called by build_installer.bat (Windows) and build_mac.sh (macOS).
End users never run this.

    python download_tools.py                        # auto-detect platform
    python download_tools.py --platform darwin      # cross-inspect
    python download_tools.py --dest ~/Library/Caches/FrameGuard/FGv0.2.2/dist/FrameGuard.app/Contents/Resources/tools
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
# macOS: ffmpeg and ffprobe ship as SEPARATE archives, unlike the Windows
# builds which ship both in one zip. Each entry is therefore a list of
# (url, archive_member_basename) pairs.
#
# The previous macOS source, evermeet.cx, returns HTTP 410 Gone for every
# getrelease download (verified live 2026-10-14) and its builds were
# Intel-only. macOS tools now come from Martin Riedl's build site, which
# publishes per-architecture zips (arm64 and amd64) with a .sha256 sidecar
# next to every archive. The pinned URLs below are the reproducible source;
# the redirect/latest URL is the fallback, and its checksum is taken from
# the sidecar of the version it actually resolved to, so the fallback is
# verified too. Bump _RIEDL_RELEASES deliberately, like a dependency.
# ---------------------------------------------------------------------------
_RIEDL_BASE = "https://ffmpeg.martin-riedl.de"
_RIEDL_RELEASES = {  # pinned build directories, per architecture segment
    "arm64": "1789931890_9.0.2",
    "amd64": "1789931006_9.0.2",
}


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


def _riedl_urls(tool: str, arch: str):
    seg = _riedl_segment(arch)
    pinned = (f"{_RIEDL_BASE}/download/macos/{seg}/"
              f"{_RIEDL_RELEASES[seg]}/{tool}.zip")
    latest = f"{_RIEDL_BASE}/redirect/latest/macos/{seg}/release/{tool}.zip"
    return [pinned, latest]


def _darwin_sources(arch: str) -> dict:
    return {
        "suffix": "",
        "ffmpeg": {
            "required": True,
            "members": ["ffmpeg"],
            "urls": _riedl_urls("ffmpeg", arch),
            "sha256_from_url": True,
        },
        "ffprobe": {
            "required": True,
            "members": ["ffprobe"],
            "urls": _riedl_urls("ffprobe", arch),
            "sha256_from_url": True,
        },
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
            "urls": [
                "https://mediaarea.net/download/binary/mediainfo/26.10/MediaInfo_CLI_26.10_Mac.dmg",
            ],
        },
    }


SOURCES = {
    "windows": {
        "suffix": ".exe",
        "ffmpeg": {
            "required": True,
            "members": ["ffmpeg.exe", "ffprobe.exe"],
            "urls": [
                "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
            ],
        },
        "mediainfo": {
            "required": False,
            "members": ["mediainfo.exe"],
            "urls": [
                "https://mediaarea.net/download/binary/mediainfo/26.10/MediaInfo_CLI_26.10_Windows_x64.zip",
            ],
        },
    },
    # "darwin" is intentionally absent here: its sources are
    # per-architecture and are built by _darwin_sources(arch) at runtime.
}


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


def _expected_sha256(final_url: str):
    """Read the '<archive>.sha256' sidecar published next to an archive and
    return the hex digest, or None when it cannot be read or parsed. The
    caller treats None as a failed source: an unverifiable required tool is
    a failed tool, never a silently trusted one."""
    import urllib.request
    try:
        req = urllib.request.Request(final_url + ".sha256",
                                     headers={"User-Agent": "FrameGuard-Builder/1.0"})
        with urllib.request.urlopen(req, timeout=60) as response:
            text = response.read().decode("utf-8", "replace").strip()
        parts = text.split()
        token = parts[0].lower() if parts else ""
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            return token
    except Exception as e:
        print(f"  ERROR: Could not read checksum sidecar: {e}")
    return None


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
    try:
        r = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint",
             str(mount), str(archive)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"  ERROR: hdiutil attach failed: {(r.stderr or r.stdout)[:200]}")
            return found
        try:
            for root, _dirs, files in os.walk(mount):
                for fn in files:
                    if fn.lower() in wanted_lower:
                        src = Path(root) / fn
                        # mediainfo in the dmg may be inside MediaInfo CLI.app;
                        # copy the binary itself, not the .app wrapper.
                        out = dest_dir / fn
                        shutil.copy2(src, out)
                        make_executable(out)
                        found.add(fn.lower())
                    # Also look inside a .app bundle for the CLI binary
                    if fn.endswith(".app"):
                        for broot, _bdirs, bfiles in os.walk(Path(root) / fn):
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
                        expanded = Path(_tf.mkdtemp(prefix="fg_pkg_"))
                        try:
                            r2 = subprocess.run(
                                ["pkgutil", "--expand-full",
                                 str(Path(root) / fn), str(expanded)],
                                capture_output=True, text=True, timeout=120)
                            if r2.returncode != 0:
                                continue
                            for broot, _bdirs, bfiles in os.walk(expanded):
                                for bfn in bfiles:
                                    if bfn.lower() in wanted_lower:
                                        shutil.copy2(Path(broot) / bfn,
                                                     dest_dir / bfn)
                                        make_executable(dest_dir / bfn)
                                        found.add(bfn.lower())
                        finally:
                            shutil.rmtree(expanded, ignore_errors=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount), "-quiet"],
                           capture_output=True, timeout=60)
    except Exception as e:
        print(f"  ERROR: .dmg extraction failed: {e}")
    finally:
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
    missing = [m for m in members if not (cache / m).is_file()]
    if not missing and not force:
        print(f"[{name}] Using cached copy.")
        return True

    print(f"\n[{name}] Fetching {', '.join(missing or members)}...")
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
            ok, final_url = download_file(url, tmp_path, name)
            if not ok:
                continue
            if entry.get("sha256_from_url"):
                expected = _expected_sha256(final_url or url)
                if expected is None:
                    print("  No checksum sidecar available; refusing this source.")
                    continue
                actual = _sha256_of(tmp_path)
                if actual != expected:
                    print(f"  ERROR: SHA-256 mismatch (expected "
                          f"{expected[:16]}..., got {actual[:16]}...); "
                          f"trying next source.")
                    continue
                print("  SHA-256 verified.")
            found = extract_members(tmp_path, members, cache)
            if found >= {m.lower() for m in members}:
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

    spec = _darwin_sources(arch) if target == "darwin" else SOURCES[target]
    entries = {k: v for k, v in spec.items() if isinstance(v, dict)}

    required_ok = True
    if not args.verify_only:
        for name, entry in entries.items():
            if not fetch(target, name, entry, cache, args.force) and entry["required"]:
                required_ok = False

    # --- Verify ---
    print("\n[Verify] Checking binaries...")
    for name, entry in entries.items():
        for member in entry["members"]:
            path = cache / member
            if not path.is_file():
                if entry["required"]:
                    print(f"    {member}: MISSING")
                    required_ok = False
                continue
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
        for entry in entries.values():
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
