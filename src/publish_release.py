#!/usr/bin/env python3
"""Publish a FrameGuard release for differential updating.

Takes a completed PyInstaller build and produces a release directory that the
in-app updater can pull from:

    <output>/manifest.json      SHA-256 of every file
    <output>/files/...          the full tree

Clients compare the manifest against their own installation and download only
what differs, so a code-only change costs a few hundred kilobytes instead of
the ~90 MB installer.

    python publish_release.py                       # build dir -> Output/release-<platform>
    python publish_release.py --compare-to OLD_DIR  # preview the delta first

Releases are platform-separated (Output/release-windows vs
Output/release-macos): the payloads overlap by filename, so sharing one
folder lets one platform's publish silently overwrite the other's.

The full tree is published every time: it is cheap to host in the repo and
means a client on ANY previous version can update in one step.

Publishing target: the repo's `releases` branch. Publish the Windows build
into that branch's RELEASES/ folder and the macOS build into RELEASES-macos/,
then push the branch — the in-app updater reads both straight from
raw.githubusercontent.com. Use --publish-to with a checkout of that branch.
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# The team checkout lives under a path containing non-ASCII characters, and
# this script prints absolute paths. Force UTF-8 output so it never dies on a
# cp1252 Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frameguard import manifest as M  # noqa: E402

HERE = Path(__file__).resolve().parent


def output_dir_for(platform_name: str = "") -> Path:
    """Where a release for `platform_name` lives.

    Windows and macOS builds must NEVER share one release folder: their
    payloads overlap by filename (FrameGuard.exe vs the .app tree, .pyd vs
    .so, ffmpeg.exe vs ffmpeg) and a shared Output/release would let one
    platform's publish silently overwrite the other's.
    """
    name = (platform_name or "").lower()
    if name.startswith("darwin") or name == "mac":
        return HERE / "Output" / "release-macos"
    if name.startswith("win"):
        return HERE / "Output" / "release-windows"
    return HERE / "Output" / "release"


def default_build_dir() -> Path:
    mac = HERE / "dist" / "FrameGuard.app"
    return mac if mac.is_dir() else HERE / "dist" / "FrameGuard"


def read_version() -> str:
    src = (HERE / "frameguard" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    return match.group(1) if match else "0.0.0"


def human(num_bytes: int) -> str:
    mb = num_bytes / (1024 * 1024)
    return f"{mb:.1f} MB" if mb >= 0.1 else f"{num_bytes / 1024:.0f} KB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-dir", type=Path, default=None,
                        help="PyInstaller output (default: dist/FrameGuard[.app])")
    parser.add_argument("--output", type=Path, default=None,
                        help="Where to write the release "
                             "(default: Output/release-<platform>; "
                             "Output/release only with --output explicitly)")
    parser.add_argument("--platform", default="auto",
                        choices=["auto", "windows", "darwin"],
                        help="Platform this release is for "
                             "(default: auto-detect the host).")
    parser.add_argument("--publish-to", type=Path, default=None,
                        help="Copy the finished release straight to the update "
                             "channel: the RELEASES (windows) or RELEASES-macos "
                             "folder in a checkout of the repo's releases branch")
    parser.add_argument("--compare-to", type=Path, default=None,
                        help="A previous release directory; prints the delta")
    parser.add_argument("--manifest-only", action="store_true",
                        help="Write the manifest without copying the payload")
    args = parser.parse_args()

    build_dir = args.build_dir or default_build_dir()
    if not build_dir.is_dir():
        print(f"ERROR: build directory not found: {build_dir}")
        print("       Run build_installer.bat or ./build_mac.sh first.")
        return 1

    import platform as _plat
    host = "darwin" if _plat.system() == "Darwin" else (
        "windows" if os.name == "nt" else "linux")
    plat = host if args.platform == "auto" else args.platform

    version = read_version()
    # Entry point follows the TARGET platform, not the build host: a Mac
    # release published from any host must still launch FrameGuard, and a
    # Windows release must still launch FrameGuard.exe.
    entry = "FrameGuard" if plat == "darwin" else "FrameGuard.exe"

    print(f"Build    : {build_dir}")
    print(f"Platform : {plat} (host: {host})")
    print(f"Version  : {version}")
    print("Hashing build tree...")
    manifest = M.build_manifest(str(build_dir), app_version=version, entry_point=entry)
    total = sum(f["size"] for f in manifest["files"].values())
    print(f"           {len(manifest['files'])} files, {human(total)}")

    # Show what an existing user would actually download.
    if args.compare_to:
        old_path = args.compare_to / M.MANIFEST_FILENAME
        old = M.load_manifest(str(old_path))
        if old is None:
            print(f"\nWARNING: no manifest at {old_path}; skipping comparison.")
        else:
            plan = M.diff_manifests(old, manifest)
            print(f"\nDelta from {old.get('app_version', '?')} -> {version}")
            print(f"  {plan.summary()}")
            saving = 100 - (plan.total_bytes * 100 / total) if total else 0
            print(f"  {saving:.1f}% smaller than shipping the whole build")
            for rel in plan.transfer[:10]:
                print(f"    ~ {rel}")
            if len(plan.transfer) > 10:
                print(f"    ... and {len(plan.transfer) - 10} more")
            for rel in plan.delete[:5]:
                print(f"    - {rel}")

    output = args.output or output_dir_for(plat)
    output.mkdir(parents=True, exist_ok=True)
    M.write_manifest(manifest, str(output / M.MANIFEST_FILENAME))
    print(f"\nManifest : {output / M.MANIFEST_FILENAME}")

    if args.manifest_only:
        print("Payload  : skipped (--manifest-only)")
        return 0

    payload = output / M.PAYLOAD_DIRNAME
    print("Copying payload...")
    if payload.exists():
        shutil.rmtree(payload)
    shutil.copytree(build_dir, payload)
    print(f"Payload  : {payload}")

    if args.publish_to:
        target = args.publish_to
        print(f"\nPublishing to the update channel:\n  {target}")
        try:
            target.mkdir(parents=True, exist_ok=True)
            # Copy the payload first, then the manifest. A client that reads
            # the channel mid-publish must not see a manifest describing files
            # that have not arrived yet.
            target_payload = target / M.PAYLOAD_DIRNAME
            if target_payload.exists():
                shutil.rmtree(target_payload)
            shutil.copytree(payload, target_payload)
            shutil.copy2(output / M.MANIFEST_FILENAME, target / M.MANIFEST_FILENAME)
            print("  Done. Commit and push the releases branch; clients will")
            print("  pick this up within 24 hours.")
        except OSError as e:
            print(f"  ERROR: could not publish: {e}")
            print("  The local release in Output/ is still valid; copy it manually.")
            return 1

    print("\nRelease ready. Publish this folder to the repo's releases branch")
    print("(RELEASES/ for Windows, RELEASES-macos/ for macOS) and push it;")
    print("clients will download only the files that differ from their install.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
