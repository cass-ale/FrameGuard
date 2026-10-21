#!/usr/bin/env python3
"""Regenerate every static FrameGuard icon from the vector mark.

    python tools_dev/make_brand_assets.py [--check]

Writes:
  branding/icon_mac_<N>.png  Apple-grid app icon, brand colours (packed into
                             the .icns by assets/make_icns.py at build time)
  branding/*.svg             vector masters: two-tone glyph, single-colour
                             glyphs (regular and small optical size), app icon
  assets/frameguard.ico      Windows icon (exe, installer, shortcuts, .fgpreset)

All of it comes from frameguard/brand.py, the same code that draws the mark in
the running app, so the bundle icon and the in-app mark cannot drift apart.
With --check, nothing is written: it exits 1 if any committed file differs
from what the current geometry produces, allowing for anti-aliasing noise
between platforms (the test suite runs this).
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from frameguard import brand  # noqa: E402

BRANDING = SRC_ROOT / "branding"
ICO_PATH = SRC_ROOT / "assets" / "frameguard.ico"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
_APP = None


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    if not image.save(buf, "PNG"):
        raise RuntimeError("PNG encoding failed")
    buf.close()
    return bytes(data)


def ico_bytes(images) -> bytes:
    """ICO container with PNG payloads (the same layout the old icon used,
    which PyInstaller and Inno Setup 6 already accept)."""
    entries = [(img.width(), png_bytes(img)) for img in images]
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory, payload = b"", b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        payload += data
        offset += len(data)
    return header + directory + payload


def svg_glyph_two_tone(small: bool) -> str:
    stem, tick = brand.glyph_paths(small)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<title>FrameGuard</title>'
            f'<path fill="{brand.BRAND.tick}" d="{brand.svg_path_data(tick)}"/>'
            f'<path fill="{brand.BRAND.stem}" d="{brand.svg_path_data(stem)}"/></svg>\n')


def svg_glyph_mono(small: bool) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<title>FrameGuard</title>'
            f'<path fill="currentColor" d="{brand.svg_path_data(brand.silhouette(small))}"/></svg>\n')


def svg_app_icon() -> str:
    from PySide6.QtCore import QRectF
    p = brand.BRAND
    g = brand.MAC_GRID
    body = brand.MAC_BODY
    off = (g - body) / 2
    plate = brand.continuous_rounded_rect(QRectF(off, off, body, body), brand.MAC_RADIUS)
    stem, tick = brand.glyph_paths(False)
    box = g * 600 / 1024
    ink = brand.silhouette(False).boundingRect()
    gx = (g - box) / 2 + (50 - ink.center().x()) * box / 100
    gy = (g - box) / 2 + (50 - ink.center().y()) * box / 100
    k = box / 100
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            '<title>FrameGuard</title><defs>'
            f'<linearGradient id="plate" x1="{off}" y1="{off}" x2="{off + body}" y2="{off + body}" '
            f'gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{p.plate_top}"/>'
            f'<stop offset="1" stop-color="{p.plate_bottom}"/></linearGradient></defs>'
            f'<path fill="url(#plate)" d="{brand.svg_path_data(plate)}"/>'
            f'<g transform="translate({gx:.2f} {gy:.2f}) scale({k:.4f})">'
            f'<path fill="{p.tick}" d="{brand.svg_path_data(tick)}"/>'
            f'<path fill="{p.stem}" d="{brand.svg_path_data(stem)}"/></g></svg>\n')


def build() -> dict:
    global _APP
    _APP = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # held for the run
    files = {}
    for px in brand.MAC_ICON_SIZES:
        files[BRANDING / f"icon_mac_{px}.png"] = png_bytes(brand.app_icon_image(px, brand.BRAND, "mac"))
    files[BRANDING / "frameguard-glyph.svg"] = svg_glyph_two_tone(False).encode()
    files[BRANDING / "frameguard-glyph-small.svg"] = svg_glyph_two_tone(True).encode()
    files[BRANDING / "frameguard-glyph-mono.svg"] = svg_glyph_mono(False).encode()
    files[BRANDING / "frameguard-glyph-small-mono.svg"] = svg_glyph_mono(True).encode()
    files[BRANDING / "frameguard-app-icon.svg"] = svg_app_icon().encode()
    files[ICO_PATH] = ico_bytes([brand.app_icon_image(px, brand.BRAND, "full") for px in ICO_SIZES])
    return files


def _images_in(path: Path, data: bytes):
    """Decoded bitmaps in a PNG or ICO payload, keyed by width."""
    if path.suffix == ".ico":
        count = struct.unpack("<H", data[4:6])[0]
        out = {}
        for i in range(count):
            _, _, _, _, _, _, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
            img = QImage.fromData(data[offset:offset + size])
            out[img.width()] = img
        return out
    img = QImage.fromData(data)
    return {img.width(): img}


def _same_image(a: QImage, b: QImage) -> bool:
    """Equal within anti-aliasing noise: rasterisers differ slightly between
    platforms and Qt builds, so exact bytes would make the check flaky."""
    if a.size() != b.size():
        return False
    a = a.convertToFormat(QImage.Format_ARGB32)
    b = b.convertToFormat(QImage.Format_ARGB32)
    off = 0
    for y in range(a.height()):
        for x in range(a.width()):
            pa, pb = a.pixel(x, y), b.pixel(x, y)
            if pa != pb and max(abs(((pa >> s) & 255) - ((pb >> s) & 255)) for s in (0, 8, 16, 24)) > 24:
                off += 1
    return off <= max(2, a.width() * a.height() // 400)


def _same_svg(a: bytes, b: bytes) -> bool:
    import re
    # A Windows checkout may convert line endings (core.autocrlf).
    a, b = a.replace(b"\r\n", b"\n"), b.replace(b"\r\n", b"\n")
    num = re.compile(rb"-?\d+(?:\.\d+)?")
    if num.sub(b"#", a) != num.sub(b"#", b):
        return False
    return all(abs(float(x) - float(y)) <= 0.05 for x, y in zip(num.findall(a), num.findall(b)))


def is_current(path: Path, data: bytes) -> bool:
    if not path.is_file():
        return False
    have = path.read_bytes()
    if have == data:
        return True
    if path.suffix == ".svg":
        return _same_svg(have, data)
    want, got = _images_in(path, data), _images_in(path, have)
    return want.keys() == got.keys() and all(_same_image(want[k], got[k]) for k in want)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify committed files match the geometry; write nothing")
    args = parser.parse_args(argv)
    files = build()
    stale = [path for path, data in files.items() if not is_current(path, data)]
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(SRC_ROOT)}")
        if stale:
            print("Run: python tools_dev/make_brand_assets.py")
        return 1 if stale else 0
    BRANDING.mkdir(exist_ok=True)
    for path in stale:
        path.write_bytes(files[path])
    print(f"wrote {len(files)} files ({len(stale)} changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
