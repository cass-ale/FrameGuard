#!/usr/bin/env python3
"""FrameGuard -- GitHub release notes builder (release pipeline only).

Renders the notes for the version in version.json: the download and
verification instructions, then ONLY that version's changelog entry under
"## Changes". version.json keeps the whole history (every entry back to
0.5.2), and publishing it made every release body repeat that history while
the in-app update prompt showed none of what actually changed.

    python release_notes.py --output release-notes.md

Called by .github/workflows/release.yml; tests render the same template.
The in-app updater reads the text after "## Changes" (see
frameguard.updater.release_notes_summary), so keep that heading.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# One changelog entry starts at a line "v<major>.<minor>.<patch> - <title>".
_ENTRY_HEADER_RE = re.compile(r"^v(\d+\.\d+\.\d+)\b.*$", re.MULTILINE)

_TEMPLATE = """**Published by Capri la Ria · Developed by Cassian Aléria**

## Download

| Platform | File | Requirements |
|----------|------|----------------|
| Windows | `FrameGuardInstaller.exe` | Windows 10/11, 64-bit |
| macOS (Apple Silicon) | `FrameGuard-arm64.dmg` | macOS 12 or later |
| macOS (Intel) | `FrameGuard-intel.dmg` | macOS 12 or later |

**Windows:** run the installer. FrameGuard is not yet code-signed, so
SmartScreen may ask once — choose "More info", then "Run anyway".

**macOS:** open the DMG for your Mac's chip and drag FrameGuard into
Applications. The build is ad-hoc signed but not notarized, so the
first launch is blocked once. On macOS 15 or later: choose **Done** in
the dialog, then System Settings → Privacy & Security →
**Open Anyway**, authenticate, and launch again. On macOS 14 or
earlier: right-click FrameGuard → **Open**.

**Already installed?** FrameGuard checks GitHub Releases automatically
and offers the correct installer for this platform and architecture.
Reinstalling from this page is always a supported recovery path.

## Verify your download

- Checksums: `SHA256SUMS.txt` (Windows `Get-FileHash`, macOS
  `shasum -a 256 -c SHA256SUMS.txt`)
- Provenance: `gh attestation verify <file> --owner cass-ale`
- Components: `sbom-windows.cdx.json` / `sbom-macos.cdx.json`

## Changes

```
{changes}
```

© 2026 Cassian Aléria. MIT License.
"""


class ReleaseNotesError(ValueError):
    """version.json cannot produce release notes for its own version."""


def changelog_entry(changelog: str, version: str) -> str:
    """Return exactly the changelog entry for ``version``.

    The entry runs from its "v<version> - ..." header line to the line
    before the next entry header. Raises ReleaseNotesError when the version
    has no entry: publishing the previous release's notes would be wrong.
    """
    if not isinstance(changelog, str):
        raise ReleaseNotesError("version.json has no changelog text.")
    text = changelog.replace("\r\n", "\n")
    headers = list(_ENTRY_HEADER_RE.finditer(text))
    for index, header in enumerate(headers):
        if header.group(1) != version:
            continue
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        entry = text[header.start():end].strip()
        if "```" in entry:
            raise ReleaseNotesError(
                "The changelog entry contains ``` and would break the notes.")
        return entry
    raise ReleaseNotesError(
        f"version.json's changelog has no entry for v{version}; add one that "
        f"starts with the line 'v{version} - <title>'.")


def render(data: dict) -> str:
    """The complete release notes for a parsed version.json."""
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ReleaseNotesError("version.json has no version.")
    entry = changelog_entry(data.get("changelog", ""), version.strip())
    return _TEMPLATE.replace("{changes}", entry)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version-file", type=Path,
                        default=SCRIPT_DIR / "version.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.version_file.read_text(encoding="utf-8"))
        notes = render(data)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    args.output.write_text(notes, encoding="utf-8")
    print(f"{args.output} written for v{data['version']} "
          f"({len(notes)} characters)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
