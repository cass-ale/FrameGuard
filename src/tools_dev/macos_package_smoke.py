#!/usr/bin/env python3
"""Verify a macOS DMG and run mounted/copied real-startup preflights."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


def _run(command, *, capture=True):
    return subprocess.run(
        command,
        check=True,
        capture_output=capture,
        text=False,
        timeout=120,
    )


def _detach(device):
    """Try normal then forced detach, returning whether either succeeded."""
    if not device:
        return True
    for command in (
        ["hdiutil", "detach", device],
        ["hdiutil", "detach", "-force", device],
    ):
        try:
            _run(command)
            return True
        except Exception:
            # Cleanup must remain fail-closed even for an unexpected launcher error.
            continue
    return False


def _require_layout(app, expected_arch):
    info_path = app / "Contents" / "Info.plist"
    executable = app / "Contents" / "MacOS" / "FrameGuard"
    tools = app / "Contents" / "Resources" / "tools"
    if not info_path.is_file() or not executable.is_file():
        raise RuntimeError(f"incomplete application layout: {app}")
    info = plistlib.loads(info_path.read_bytes())
    if info.get("CFBundleExecutable") != "FrameGuard":
        raise RuntimeError("Info.plist has the wrong CFBundleExecutable")
    if info.get("CFBundleIdentifier") != "com.caprilaria.frameguard":
        raise RuntimeError("Info.plist has the wrong CFBundleIdentifier")
    for name in ("ffmpeg", "ffprobe"):
        path = tools / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"required executable tool is missing: {path}")
    archs = _run(["lipo", "-archs", str(executable)]).stdout.decode().split()
    if expected_arch not in archs:
        raise RuntimeError(
            f"{executable} has architectures {archs}, expected {expected_arch}")
    _run(["codesign", "--verify", "--deep", "--strict", str(app)])
    return executable


def _startup(python, executable, artifacts, label, qpa):
    command = [
        python,
        str(Path(__file__).with_name("startup_smoke.py")),
        "--executable", str(executable),
        "--artifacts-dir", str(artifacts),
        "--label", label,
        "--expect-frozen",
        "--timeout", "30",
    ]
    if qpa:
        command.extend(["--qpa", qpa])
    subprocess.run(command, check=True, timeout=45)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmg", required=True)
    parser.add_argument("--expected-arch", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--qpa")
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        raise SystemExit("macos_package_smoke.py must run on macOS")
    dmg = Path(args.dmg).resolve()
    if not dmg.is_file() or dmg.stat().st_size == 0:
        raise SystemExit(f"DMG is missing or empty: {dmg}")
    artifacts = Path(args.artifacts_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="frameguard-dmg-smoke-"))
    mountpoint = root / "mount"
    copied = root / "copied" / "FrameGuard.app"
    mountpoint.mkdir()
    device = None
    summary = {
        "dmg": str(dmg),
        "expected_arch": args.expected_arch,
        "mounted_preflight": False,
        "copied_preflight": False,
        "detach_attempted": False,
        "detach_completed": None,
        "temp_root_removed": False,
        "status": "running",
    }
    errors = []
    try:
        _run(["hdiutil", "verify", str(dmg)])
        attached = _run([
            "hdiutil", "attach", "-readonly", "-nobrowse", "-plist",
            "-mountpoint", str(mountpoint), str(dmg),
        ])
        plist = plistlib.loads(attached.stdout)
        for entity in plist.get("system-entities", []):
            if entity.get("mount-point"):
                mountpoint = Path(entity["mount-point"])
                device = entity.get("dev-entry") or device
            elif entity.get("dev-entry"):
                device = entity["dev-entry"]
        if not device:
            raise RuntimeError("hdiutil attach did not report a device entry")
        mounted_app = mountpoint / "FrameGuard.app"
        mounted_executable = _require_layout(mounted_app, args.expected_arch)
        _startup(
            sys.executable, mounted_executable, artifacts,
            "mounted-startup", args.qpa,
        )
        summary["mounted_preflight"] = True

        copied.parent.mkdir(parents=True)
        subprocess.run(
            ["ditto", str(mounted_app), str(copied)],
            check=True,
            timeout=120,
        )
        copied_executable = _require_layout(copied, args.expected_arch)
        _startup(
            sys.executable, copied_executable, artifacts,
            "copied-startup", args.qpa,
        )
        summary["copied_preflight"] = True
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if device:
            summary["detach_attempted"] = True
            summary["detach_completed"] = _detach(device)
            if not summary["detach_completed"]:
                errors.append(
                    f"Could not detach {device} after normal and forced attempts")
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"Could not remove temporary root {root}: {exc}")
        summary["temp_root_removed"] = not root.exists()
        if not summary["temp_root_removed"]:
            errors.append(f"Temporary root still exists: {root}")

        summary["status"] = "failed" if errors else "passed"
        if errors:
            summary["error"] = "; ".join(errors)
        summary_path = artifacts / "macos-package-smoke.json"
        temporary = summary_path.with_name(f".{summary_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        os.replace(temporary, summary_path)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
