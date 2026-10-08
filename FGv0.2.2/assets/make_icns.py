#!/usr/bin/env python3
"""Generate assets/frameguard.icns from assets/frameguard_logo.png.

Run as part of the macOS build. Writes a real .icns container directly rather
than shelling out to iconutil, so the icon can also be regenerated on Windows
or Linux when the artwork changes.

    python assets/make_icns.py [--source PNG] [--out ICNS]

.icns layout: the 8-byte magic/length header 'icns' + total length, followed by
one chunk per image, each being a 4-byte OSType, a 4-byte big-endian length
(inclusive of the 8-byte chunk header), then the payload. Modern macOS accepts
PNG payloads for all the types written here.
"""

import argparse
import struct
import sys
from pathlib import Path

# OSType -> pixel size. Retina (@2x) variants intentionally reuse the pixel
# size of the next tier up, which is exactly what macOS expects.
ICNS_TYPES = [
    (b"icp4", 16),    # 16x16
    (b"icp5", 32),    # 32x32
    (b"icp6", 64),    # 64x64
    (b"ic07", 128),   # 128x128
    (b"ic08", 256),   # 256x256
    (b"ic09", 512),   # 512x512
    (b"ic10", 1024),  # 512x512@2x
    (b"ic11", 32),    # 16x16@2x
    (b"ic12", 64),    # 32x32@2x
    (b"ic13", 256),   # 128x128@2x
    (b"ic14", 512),   # 256x256@2x
]


def build_icns(source: Path, out: Path) -> Path:
    try:
        from PIL import Image
    except ImportError:
        sys.exit(
            "Pillow is required to generate the .icns icon.\n"
            "    pip install Pillow"
        )

    if not source.is_file():
        sys.exit(f"Source artwork not found: {source}")

    master = Image.open(source).convert("RGBA")
    if master.width != master.height:
        print(f"  WARNING: {source.name} is {master.width}x{master.height}, "
              f"not square; macOS icons will be distorted.")

    # Cache one resize per pixel size, then reuse it across the types that
    # share that size.
    rendered = {}
    for _, size in ICNS_TYPES:
        if size not in rendered:
            img = master.resize((size, size), Image.LANCZOS)
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="PNG", optimize=True)
            rendered[size] = buf.getvalue()

    chunks = b""
    for ostype, size in ICNS_TYPES:
        payload = rendered[size]
        chunks += ostype + struct.pack(">I", len(payload) + 8) + payload

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)

    sizes = sorted({s for _, s in ICNS_TYPES})
    print(f"  Wrote {out} ({out.stat().st_size / 1024:.0f} KB, "
          f"{len(ICNS_TYPES)} entries, sizes {sizes})")
    return out


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build the macOS .icns icon.")
    parser.add_argument("--source", type=Path, default=here / "frameguard_logo.png")
    parser.add_argument("--out", type=Path, default=here / "frameguard.icns")
    args = parser.parse_args()
    build_icns(args.source, args.out)


if __name__ == "__main__":
    main()
