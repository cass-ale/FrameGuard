"""Box-level edits for container/XR regression tests (Spherical V2, SA3D, edit lists).

FFmpeg reads these boxes but its CLI cannot write them from scratch, so the
tests patch a real FFmpeg-made MP4 whose moov box is last (no +faststart):
growing a box inside moov then needs no chunk-offset fix-up.
"""

import struct

from media_fixtures import _boxes


def full_box(kind: bytes, version: int, flags: int, body: bytes) -> bytes:
    payload = bytes([version]) + flags.to_bytes(3, "big") + body
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def st3d(stereo_mode: int) -> bytes:
    """Spherical Video V2 RFC: 0 mono, 1 top-bottom, 2 left-right."""
    return full_box(b"st3d", 0, 0, bytes([stereo_mode]))


def sv3d(bounds=(0, 0, 0, 0), pose=(0, 0, 0)) -> bytes:
    svhd = full_box(b"svhd", 0, 0, b"FrameGuard tests\0")
    prhd = full_box(b"prhd", 0, 0, struct.pack(">iii", *(int(a * 65536) for a in pose)))
    equi = full_box(b"equi", 0, 0, struct.pack(">IIII", *bounds))
    return box(b"sv3d", svhd + box(b"proj", prhd + equi))


def sa3d(num_channels: int, order: int = 1, head_locked: bool = False, ordering: int = 0,
         normalization: int = 0, channel_map=None) -> bytes:
    """Spatial Audio RFC SA3D (version 0)."""
    channel_map = list(range(num_channels)) if channel_map is None else channel_map
    body = struct.pack(">BBIBBI", 0, 0x80 if head_locked else 0, order, ordering, normalization, num_channels)
    body += b"".join(struct.pack(">I", c) for c in channel_map)
    return box(b"SA3D", body)


def _moov(data):
    moov = next(b for b in _boxes(data, 0, len(data)) if b[2] == b"moov")
    if moov[0] + moov[1] != len(data):
        raise ValueError("moov must be the last box (do not use +faststart)")
    return moov


def _child(data, parent, kind, skip=0):
    pos, size, _, header = parent
    for child in _boxes(data, pos + header + skip, pos + size):
        if child[2] == kind:
            return child
    raise ValueError(f"no {kind!r} box")


def _track(data, handler: bytes):
    moov = _moov(data)
    for trak in _boxes(data, moov[0] + moov[3], moov[0] + moov[1]):
        if trak[2] != b"trak":
            continue
        hdlr = _child(data, _child(data, trak, b"mdia"), b"hdlr")
        if data[hdlr[0] + hdlr[3] + 8:hdlr[0] + hdlr[3] + 12] == handler:
            return moov, trak
    raise ValueError(f"no {handler!r} track")


def _grow(data, ancestors, insert_at: int, blob: bytes):
    data[insert_at:insert_at] = blob
    for pos, _, _, _ in ancestors:
        struct.pack_into(">I", data, pos, struct.unpack(">I", data[pos:pos + 4])[0] + len(blob))


def add_to_sample_entry(path, handler: bytes, *blobs: bytes) -> str:
    """Append boxes inside the first sample entry of the ``handler`` track."""
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    moov, trak = _track(data, handler)
    mdia = _child(data, trak, b"mdia")
    minf = _child(data, mdia, b"minf")
    stbl = _child(data, minf, b"stbl")
    stsd = _child(data, stbl, b"stsd")
    entry = next(iter(_boxes(data, stsd[0] + stsd[3] + 8, stsd[0] + stsd[1])))
    blob = b"".join(blobs)
    _grow(data, [moov, trak, mdia, minf, stbl, stsd, entry], entry[0] + entry[1], blob)
    with open(path, "wb") as handle:
        handle.write(bytes(data))
    return str(path)


def set_edit_list(path, handler: bytes, entries) -> str:
    """Replace a track's edit list: entries are (segment_duration, media_time, rate)."""
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    moov, trak = _track(data, handler)
    body = struct.pack(">I", len(entries)) + b"".join(
        struct.pack(">IihH", duration, media_time, rate, 0) for duration, media_time, rate in entries)
    new = box(b"edts", full_box(b"elst", 0, 0, body))
    try:
        old = _child(data, trak, b"edts")
    except ValueError:
        tkhd = _child(data, trak, b"tkhd")
        _grow(data, [moov, trak], tkhd[0] + tkhd[1], new)
    else:
        del data[old[0]:old[0] + old[1]]
        delta = len(new) - old[1]
        data[old[0]:old[0]] = new
        for pos, _, _, _ in (moov, trak):
            struct.pack_into(">I", data, pos, struct.unpack(">I", data[pos:pos + 4])[0] + delta)
    with open(path, "wb") as handle:
        handle.write(bytes(data))
    return str(path)


def movie_timescale(path) -> int:
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    moov = _moov(data)
    mvhd = _child(data, moov, b"mvhd")
    payload = data[mvhd[0] + mvhd[3]:mvhd[0] + mvhd[1]]
    at = 20 if payload[0] == 1 else 12
    return struct.unpack(">I", payload[at:at + 4])[0]
