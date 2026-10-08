#!/usr/bin/env python3
"""Write build metadata for the packaged About dialog.

The default remains frameguard/_build_stamp.json for the Windows builder. The
macOS builder passes --output so its generated state stays in its local cache.
Kept as a real script rather than an
inline `python -c` one-liner: the quoting required to embed this in a .bat file
is fragile and untestable.
"""

import argparse
import datetime
import json
import platform
import re
import sys
from pathlib import Path

# The team checkout lives under a path containing non-ASCII characters, and the
# stamp echoes the platform/machine. Force UTF-8 output so this never dies on
# a cp1252 Windows console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
INIT = HERE / "frameguard" / "__init__.py"
STAMP = HERE / "frameguard" / "_build_stamp.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=STAMP,
        help="Destination for the build stamp (default: frameguard/_build_stamp.json).",
    )
    args = parser.parse_args(argv)

    try:
        source = INIT.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  ERROR: cannot read {INIT}: {e}")
        return 1

    match = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    if not match:
        print(f"  ERROR: no __version__ found in {INIT}")
        return 1

    stamp = {
        "version": match.group(1),
        "built": datetime.datetime.now().isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.system(),
        "machine": platform.machine(),
    }
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"  ERROR: cannot write {args.output}: {e}")
        return 1

    print(f"       Build stamp: {stamp['version']} @ {stamp['built'][:19]} "
          f"({stamp['platform']}/{stamp['machine']}) -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
