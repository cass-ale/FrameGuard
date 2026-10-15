#!/usr/bin/env python3
"""Fail a build when PyInstaller reports a required runtime import missing."""

import argparse
from pathlib import Path
import re
import sys


COMMON_REQUIRED = {
    "frameguard",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "shiboken6",
    "fpdf",
    "psutil",
}
PLATFORM_REQUIRED = {
    "darwin": {"psutil._psosx"},
    "win32": {"psutil._pswindows"},
}


def missing_required(text, platform):
    missing = set()
    patterns = (
        r"missing module named ['\"]?([A-Za-z0-9_.]+)",
        r"Hidden import ['\"]([A-Za-z0-9_.]+)['\"].*not found",
    )
    reported = set()
    for pattern in patterns:
        reported.update(re.findall(pattern, text, flags=re.IGNORECASE))
    required = COMMON_REQUIRED | PLATFORM_REQUIRED.get(platform, set())
    for module in reported:
        if module in required or module.startswith("frameguard."):
            missing.add(module)
    return sorted(missing)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("warning_file")
    parser.add_argument("--platform", choices=("darwin", "win32"), default=sys.platform)
    args = parser.parse_args(argv)
    path = Path(args.warning_file)
    if not path.is_file():
        print(f"No PyInstaller warning file found at {path}; nothing to inspect.")
        return 0
    failures = missing_required(
        path.read_text(encoding="utf-8", errors="replace"), args.platform)
    if failures:
        print("PyInstaller reported missing required runtime modules:", file=sys.stderr)
        for module in failures:
            print(f"  - {module}", file=sys.stderr)
        return 1
    print(f"Required PyInstaller imports are present ({path}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
