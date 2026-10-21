#!/usr/bin/env python3
"""Pack assets/frameguard.icns from the pre-rendered brand icons.

Run as part of the macOS build (build_mac.sh step 5). The bitmaps come from
branding/icon_mac_<N>.png, which tools_dev/make_brand_assets.py renders from
the vector mark, with a heavier optical master at the small sizes. Packing
them, rather than downscaling one large PNG, keeps 16 and 32 px crisp. No
third-party imaging library is needed.

    python assets/make_icns.py [--branding DIR] [--out ICNS]

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
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> int:
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if width != height:
        raise ValueError(f"not square ({width}x{height})")
    return width


def build_icns(branding: Path, out: Path) -> Path:
    payloads = {}
    for _, size in ICNS_TYPES:
        if size in payloads:
            continue
        src = branding / f"icon_mac_{size}.png"
        if not src.is_file():
            sys.exit(f"Missing {src}. Regenerate with: python tools_dev/make_brand_assets.py")
        data = src.read_bytes()
        try:
            actual = _png_size(data)
        except ValueError as exc:
            sys.exit(f"{src.name}: {exc}")
        if actual != size:
            sys.exit(f"{src.name} is {actual}px, expected {size}px")
        payloads[size] = data

    chunks = b"".join(ostype + struct.pack(">I", len(payloads[size]) + 8) + payloads[size]
                      for ostype, size in ICNS_TYPES)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)
    tmp.replace(out)
    print(f"  Wrote {out} ({out.stat().st_size / 1024:.0f} KB, "
          f"{len(ICNS_TYPES)} entries, sizes {sorted(payloads)})")
    return out


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build the macOS .icns icon.")
    parser.add_argument("--branding", type=Path, default=here.parent / "branding")
    parser.add_argument("--out", type=Path, default=here / "frameguard.icns")
    args = parser.parse_args()
    build_icns(args.branding, args.out)


if __name__ == "__main__":
    main()
