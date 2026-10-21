"""Container structure, timing, codec-configuration, XR and track-layout checks.

FrameGuard's metadata checks used to see a file only through ffprobe's
stream summary. That summary cannot show where the ``moov`` index sits,
whether a box runs past the end of the file, what an edit list does, whether
a timecode track jumps, which spherical metadata generation (V1 uuid XML or
V2 ``st3d``/``sv3d``) a 360 file carries, or the ``SA3D`` box that marks an
audio track as ambisonic (ffmpeg 9 does not expose it at all). This module
adds a small, strictly bounded ISO-BMFF / QuickTime reader and the checks
built on it.

Reader rules (ISO/IEC 14496-12 §4.2 box structure; QuickTime File Format):
  * only box headers are read at top level (seek + 16 bytes); the media data
    is never loaded, so a 100 GB master costs the same as a 10 s clip;
  * the ``moov`` box is read whole only below ``MAX_MOOV_BYTES``;
  * every child must fit inside its parent and every box inside the file;
    anything that does not is reported as damage, never raised;
  * nothing here imports Qt or touches the deep scan.

The check functions take the ``QAResult`` and the engine config; core.py
calls them from ``_run_metadata_checks``.
"""

import math
import os
import re
import struct
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_TOP_LEVEL_BOXES = 200_000
MAX_MOOV_BYTES = 256 * 1024 * 1024
MAX_BOX_DEPTH = 16
MAX_TIMECODE_SAMPLES = 100_000
MAX_GOP_HEADERS = 20_000
SNIFF_BYTES = 65_536

# Check names owned by this module (core.py classifies and weights them).
CONTAINER_STRUCTURE = "Container Structure"
EDIT_LIST = "Edit List"
TIMECODE = "Timecode"
AV_SYNC_OFFSET = "A/V Sync Offset"
CODEC_PROFILE = "Codec Profile"
SPATIAL_AUDIO = "Spatial Audio"
TRACK_LAYOUT = "Track Layout"

# Checks a spec may switch off with ``disabled_checks``.
SWITCHABLE_CHECKS = (CONTAINER_STRUCTURE, EDIT_LIST, TIMECODE, AV_SYNC_OFFSET,
                     CODEC_PROFILE, SPATIAL_AUDIO, TRACK_LAYOUT)

# ---------------------------------------------------------------------------
# Container family sniffing
# ---------------------------------------------------------------------------

# First boxes seen in real MP4/MOV files: ISO-BMFF puts ftyp first, older
# QuickTime files may start with any of the others.
_ISO_FIRST_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide",
                    b"pnot", b"uuid", b"styp", b"junk", b"sidx"}
_MXF_PARTITION_KEY = bytes.fromhex("060e2b34020501010d010201")

EXTENSION_FAMILY = {
    ".mp4": "ISO-BMFF", ".m4v": "ISO-BMFF", ".mov": "ISO-BMFF", ".qt": "ISO-BMFF",
    ".3gp": "ISO-BMFF", ".3g2": "ISO-BMFF",
    ".mkv": "Matroska", ".webm": "Matroska",
    ".mxf": "MXF", ".ts": "MPEG-TS", ".m2ts": "MPEG-TS", ".mts": "MPEG-TS",
    ".avi": "AVI",
}


def sniff_container(path: str) -> Tuple[str, str]:
    """(family, detail) from the file's own signature, independent of its name.

    Families: ISO-BMFF, Matroska, MPEG-TS, MXF, AVI or unknown. Raises OSError
    only when the file cannot be opened (the caller already checked it exists).
    """
    with open(path, "rb") as stream:
        head = stream.read(SNIFF_BYTES)
    if len(head) >= 8 and head[4:8] in _ISO_FIRST_BOXES:
        detail = ""
        if head[4:8] == b"ftyp" and len(head) >= 12:
            detail = f"brand '{head[8:12].decode('latin-1').strip()}'"
        return "ISO-BMFF", detail
    if head[:4] == b"\x1a\x45\xdf\xa3":
        doctype = "webm" if b"webm" in head[:64] else "matroska"
        return "Matroska", f"DocType {doctype}"
    if len(head) > 376 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return "MPEG-TS", "188-byte packets"
    if len(head) > 388 and head[4] == 0x47 and head[196] == 0x47 and head[388] == 0x47:
        return "MPEG-TS", "192-byte (M2TS) packets"
    if _MXF_PARTITION_KEY in head:
        return "MXF", ""
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "AVI", ""
    return "unknown", ""


def extension_mismatch(path: str, ext: str) -> Optional[str]:
    """A plain-language FAIL detail when the content is not what the name says.

    A Matroska or MPEG-TS file renamed ``.mp4`` used to pass Container Format
    because only the extension was read. Players and platforms that trust
    the extension (Quest, many ingest services) reject such files. An
    unrecognised signature is not judged here: ffprobe and the decode pass
    decide whether such a file is readable at all.
    """
    expected = EXTENSION_FAMILY.get(ext.lower())
    if expected is None:
        return None
    try:
        family, detail = sniff_container(path)
    except OSError as exc:
        return f"File could not be read to verify its container ({type(exc).__name__}: {exc})."
    if family in ("unknown", expected):
        return None
    shown = f"{family} ({detail})" if detail else family
    return (f"The file is {shown} but is named '{ext}', which promises {expected}. "
            f"Rename or remux it; players that trust the extension will refuse it.")


# ---------------------------------------------------------------------------
# Box reader
# ---------------------------------------------------------------------------

_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf",
                    b"udta", b"mvex", b"sv3d", b"proj", b"tref", b"gmhd"}
# Damage inside these is structural; a malformed child of a sample entry is
# tolerated (QuickTime allows a 4-byte zero terminator there).
_STRICT_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}
SPHERICAL_V1_UUID = bytes.fromhex("ffcc8263f8554a938814587a02521fdd")


@dataclass
class Box:
    type: bytes
    offset: int          # absolute file offset of the box header
    size: int            # declared size including the header
    header: int          # 8 or 16
    payload: bytes = b""  # only for boxes inside moov
    children: List["Box"] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.type.decode("latin-1")

    @property
    def end(self) -> int:
        return self.offset + self.size

    def child(self, *path: bytes) -> Optional["Box"]:
        node = self
        for part in path:
            node = next((c for c in node.children if c.type == part), None)
            if node is None:
                return None
        return node

    def all(self, kind: bytes) -> List["Box"]:
        return [c for c in self.children if c.type == kind]


def _plausible_type(kind: bytes) -> bool:
    return len(kind) == 4 and all(0x20 <= b <= 0x7E or b == 0xA9 for b in kind)


@dataclass
class SampleEntry:
    fourcc: str
    children: List[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    channels: Optional[int] = None
    st3d: Optional[int] = None
    sv3d: Optional[dict] = None
    sa3d: Optional[dict] = None
    pasp: Optional[Tuple[int, int]] = None
    tmcd: Optional[dict] = None


@dataclass
class Edit:
    segment_duration: int   # movie timescale
    media_time: int         # media timescale; -1 = empty edit
    rate: float


@dataclass
class TrackInfo:
    track_id: int = 0
    handler: str = ""
    timescale: int = 0
    duration: int = 0
    edits: List[Edit] = field(default_factory=list)
    entries: List[SampleEntry] = field(default_factory=list)
    stts: List[Tuple[int, int]] = field(default_factory=list)
    ctts: List[Tuple[int, int]] = field(default_factory=list)
    sync_samples: Optional[List[int]] = None
    sample_count: int = 0
    max_sample_end: int = 0
    first_missing_sample: Optional[int] = None
    table_problem: str = ""
    v1_xml: str = ""
    _chunk_offsets: Tuple[int, ...] = ()
    _stsc: List[Tuple[int, int, int]] = field(default_factory=list)
    _sizes: Tuple[int, ...] = ()
    _constant_size: int = 0

    @property
    def is_av(self) -> bool:
        return self.handler in ("vide", "soun")

    def seconds(self, ticks: int) -> float:
        return ticks / self.timescale if self.timescale else 0.0

    def sample_time(self, index: int) -> float:
        """Decode time of zero-based sample ``index`` in seconds."""
        ticks, seen = 0, 0
        for count, delta in self.stts:
            if index < seen + count:
                return self.seconds(ticks + (index - seen) * delta)
            ticks += count * delta
            seen += count
        return self.seconds(ticks)

    def min_composition_time(self, first_samples: int = 256) -> int:
        """Earliest presentation time (media ticks) among the first samples.

        The single edit an encoder writes for B-frame reordering starts the
        presentation exactly here; anything an edit skips beyond it is
        stored media that is hidden (a lossless trim's lead-in).
        """
        if not self.ctts:
            return 0
        decode, best, index = 0, None, 0
        deltas = (d for count, d in self.stts for _ in range(count))
        offsets = (o for count, o in self.ctts for _ in range(count))
        for delta, offset in zip(deltas, offsets):
            composed = decode + offset
            best = composed if best is None else min(best, composed)
            decode += delta
            index += 1
            if index >= first_samples:
                break
        return best or 0

    def frame_duration(self) -> Optional[float]:
        if not self.stts or not self.timescale:
            return None
        delta = max(self.stts, key=lambda run: run[0])[1]
        return delta / self.timescale if delta else None


@dataclass
class Structure:
    """What the box reader found. ``family`` is set for every file."""
    family: str = "unknown"
    file_size: int = 0
    brand: str = ""
    top: List[Box] = field(default_factory=list)
    damage: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    moov_count: int = 0
    moov_offset: Optional[int] = None
    first_mdat_offset: Optional[int] = None
    moof_count: int = 0
    has_mvex: bool = False
    movie_timescale: int = 0
    tracks: List[TrackInfo] = field(default_factory=list)
    parsed: bool = False
    error: str = ""

    @property
    def fragmented(self) -> bool:
        return self.moof_count > 0 or self.has_mvex

    def tracks_of(self, handler: str) -> List[TrackInfo]:
        return [t for t in self.tracks if t.handler == handler]

    def track_by_id(self, track_id: int) -> Optional[TrackInfo]:
        return next((t for t in self.tracks if t.track_id == track_id), None)


def _walk_top_level(stream, structure: Structure) -> None:
    pos, size_total = 0, structure.file_size
    while pos < size_total:
        if len(structure.top) >= MAX_TOP_LEVEL_BOXES:
            structure.notes.append(f"more than {MAX_TOP_LEVEL_BOXES} top-level boxes; the rest were not inspected")
            return
        stream.seek(pos)
        head = stream.read(16)
        if len(head) < 8:
            structure.damage.append(f"{size_total - pos} stray byte(s) after the last box at offset {pos}")
            return
        size, kind = struct.unpack(">I4s", head[:8])
        header = 8
        if size == 1:
            if len(head) < 16:
                structure.damage.append(f"box header at offset {pos} is cut off by the end of the file")
                return
            size = struct.unpack(">Q", head[8:16])[0]
            header = 16
        elif size == 0:
            size = size_total - pos  # ISO/IEC 14496-12 §4.2: extends to end of file
        if kind == b"\0\0\0\0" and not any(head[:8]):
            structure.notes.append(f"{size_total - pos} byte(s) of zero padding after the last box")
            return
        if not _plausible_type(kind) or size < header:
            structure.damage.append(f"unreadable data at offset {pos} (not a valid box header): "
                                    f"the box structure is damaged from here on")
            return
        box = Box(kind, pos, size, header)
        structure.top.append(box)
        if box.end > size_total:
            missing = box.end - size_total
            structure.damage.append(f"the '{box.name}' box at offset {pos} declares {size:,} bytes but the "
                                    f"file ends {missing:,} bytes early (truncated)")
            return
        pos = box.end


def _parse_children(data: bytes, start: int, end: int, base: int, depth: int,
                    parent: bytes, handler: str, structure: Structure) -> List[Box]:
    """Parse boxes in data[start:end]; ``base`` maps data offsets to file offsets."""
    out: List[Box] = []
    pos = start
    strict = parent in _STRICT_BOXES
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1 and pos + 16 <= end:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end or not _plausible_type(kind):
            if strict:
                structure.damage.append(f"box inside '{parent.decode('latin-1')}' at offset {base + pos} "
                                        f"overruns its parent or is not a valid box")
            break
        box = Box(kind, base + pos, size, header, data[pos + header:pos + size])
        if depth < MAX_BOX_DEPTH:
            _expand(box, data, pos, depth, parent, handler, structure)
        out.append(box)
        pos += size
    return out


def _audio_entry_header(payload: bytes) -> int:
    """Bytes before an audio sample entry's child boxes (QuickTime v0/v1/v2)."""
    version = struct.unpack(">H", payload[8:10])[0] if len(payload) >= 10 else 0
    return {0: 28, 1: 44, 2: 64}.get(version, 28)


def _entry_header_size(handler: str, payload: bytes) -> Optional[int]:
    if handler == "vide":
        return 78
    if handler == "soun":
        return _audio_entry_header(payload)
    if handler == "tmcd":
        return 26
    return None


def _expand(box: Box, data: bytes, pos: int, depth: int, parent: bytes,
            handler: str, structure: Structure) -> None:
    start, end = pos + box.header, pos + box.size
    if box.type in _CONTAINER_BOXES:
        box.children = _parse_children(data, start, end, box.offset - pos, depth + 1,
                                       box.type, handler, structure)
    elif box.type == b"stsd":
        box.children = _parse_children(data, start + 8, end, box.offset - pos, depth + 1,
                                       b"stsd", handler, structure)
    elif parent == b"stsd":
        skip = _entry_header_size(handler, box.payload)
        if skip is not None and start + skip <= end:
            box.children = _parse_children(data, start + skip, end, box.offset - pos, depth + 1,
                                           box.type, handler, structure)


def _fullbox(payload: bytes) -> Tuple[int, bytes]:
    return (payload[0] if payload else 0), payload[4:]


def _u32_array(body: bytes, count: int, offset: int = 0, wide: bool = False) -> Optional[Tuple[int, ...]]:
    width = 8 if wide else 4
    need = offset + count * width
    if count < 0 or need > len(body):
        return None
    return struct.unpack(f">{count}{'Q' if wide else 'I'}", body[offset:need])


def _parse_sv3d(box: Box) -> dict:
    """Spherical Video V2 RFC: sv3d{svhd, proj{prhd, equi|cbmp|mshp}}."""
    info: dict = {"projection": "unknown", "bounds": (0, 0, 0, 0), "pose": (0.0, 0.0, 0.0)}
    proj = box.child(b"proj")
    for part in (proj.children if proj else []):
        _, body = _fullbox(part.payload)
        if part.type == b"prhd" and len(body) >= 12:
            info["pose"] = tuple(v / 65536 for v in struct.unpack(">iii", body[:12]))
        elif part.type == b"equi" and len(body) >= 16:
            info["projection"] = "equirectangular"
            info["bounds"] = struct.unpack(">IIII", body[:16])
        elif part.type == b"cbmp":
            info["projection"] = "cubemap"
        elif part.type == b"mshp":
            info["projection"] = "mesh"
    return info


def _parse_sa3d(payload: bytes) -> dict:
    """Spatial Audio RFC: SA3D (version, head_locked|type, order, ordering, normalization, n, map)."""
    if len(payload) < 12:
        return {"error": f"SA3D box is {len(payload)} bytes, shorter than its 12-byte header"}
    version, flags, order, ordering, norm, count = struct.unpack(">BBIBBI", payload[:12])
    available = (len(payload) - 12) // 4
    channel_map = list(struct.unpack(f">{min(count, available)}I", payload[12:12 + 4 * min(count, available)]))
    return {"version": version, "head_locked_stereo": bool(flags & 0x80), "ambisonic_type": flags & 0x7F,
            "order": order, "ordering": ordering, "normalization": norm, "num_channels": count,
            "channel_map": channel_map, "map_complete": available >= count}


def _sample_entry(box: Box, handler: str) -> SampleEntry:
    entry = SampleEntry(fourcc=box.name, children=[c.name for c in box.children])
    payload = box.payload
    if handler == "vide" and len(payload) >= 28:
        entry.width, entry.height = struct.unpack(">HH", payload[24:28])
    elif handler == "soun" and len(payload) >= 28:
        version = struct.unpack(">H", payload[8:10])[0]
        if version == 2 and len(payload) >= 44:
            entry.channels = struct.unpack(">I", payload[40:44])[0]
        else:
            entry.channels = struct.unpack(">H", payload[16:18])[0]
    elif handler == "tmcd" and len(payload) >= 25:
        flags, timescale, frame_duration = struct.unpack(">III", payload[12:24])
        entry.tmcd = {"drop_frame": bool(flags & 0x1), "counter": bool(flags & 0x8),
                      "timescale": timescale, "frame_duration": frame_duration,
                      "frames": payload[24]}
    for child in box.children:
        if child.type == b"st3d" and len(child.payload) >= 5:
            entry.st3d = child.payload[4]
        elif child.type == b"sv3d":
            entry.sv3d = _parse_sv3d(child)
        elif child.type == b"SA3D":
            entry.sa3d = _parse_sa3d(child.payload)
        elif child.type == b"pasp" and len(child.payload) >= 8:
            entry.pasp = struct.unpack(">II", child.payload[:8])
    return entry


def _read_track(trak: Box, structure: Structure) -> TrackInfo:
    track = TrackInfo()
    tkhd = trak.child(b"tkhd")
    if tkhd is not None:
        version, body = _fullbox(tkhd.payload)
        id_at = 16 if version == 1 else 8
        if len(body) >= id_at + 4:
            track.track_id = struct.unpack(">I", body[id_at:id_at + 4])[0]
    hdlr = trak.child(b"mdia", b"hdlr")
    if hdlr is not None and len(hdlr.payload) >= 12:
        track.handler = hdlr.payload[8:12].decode("latin-1")
    mdhd = trak.child(b"mdia", b"mdhd")
    if mdhd is not None:
        version, body = _fullbox(mdhd.payload)
        if version == 1 and len(body) >= 28:
            track.timescale, track.duration = struct.unpack(">IQ", body[16:28])
        elif len(body) >= 16:
            track.timescale, track.duration = struct.unpack(">II", body[8:16])
    elst = trak.child(b"edts", b"elst")
    if elst is not None:
        track.edits = _read_edits(elst, structure)
    for child in trak.all(b"uuid"):
        if child.payload[:16] == SPHERICAL_V1_UUID:
            track.v1_xml = child.payload[16:].decode("utf-8", "replace")
    stbl = trak.child(b"mdia", b"minf", b"stbl")
    if stbl is not None:
        _read_sample_tables(track, stbl)
        stsd = stbl.child(b"stsd")
        if stsd is not None:
            track.entries = [_sample_entry(e, track.handler) for e in stsd.children]
    return track


def _read_edits(elst: Box, structure: Structure) -> List[Edit]:
    version, body = _fullbox(elst.payload)
    if len(body) < 4:
        return []
    count = struct.unpack(">I", body[:4])[0]
    width = 20 if version == 1 else 12
    if 4 + count * width > len(body):
        structure.damage.append(f"edit list at offset {elst.offset} declares {count} entries but holds "
                                f"{(len(body) - 4) // width}")
        count = (len(body) - 4) // width
    edits = []
    for i in range(count):
        at = 4 + i * width
        if version == 1:
            duration, media_time, rate_int, rate_frac = struct.unpack(">QqhH", body[at:at + 20])
        else:
            duration, media_time, rate_int, rate_frac = struct.unpack(">IihH", body[at:at + 12])
        edits.append(Edit(duration, media_time, rate_int + rate_frac / 65536))
    return edits


def _read_sample_tables(track: TrackInfo, stbl: Box) -> None:
    stts = stbl.child(b"stts")
    if stts is not None:
        _, body = _fullbox(stts.payload)
        count = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0
        values = _u32_array(body, count * 2, 4)
        if values is None:
            track.table_problem = "time-to-sample table is shorter than its entry count"
        else:
            track.stts = list(zip(values[0::2], values[1::2]))
    ctts = stbl.child(b"ctts")
    if ctts is not None:
        version, body = _fullbox(ctts.payload)
        count = min(struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0, 4096)
        if 4 + 8 * count <= len(body):
            pairs = struct.unpack(f">{count * 2}{'i' if version == 1 else 'I'}", body[4:4 + 8 * count])
            # Only the first runs are kept: they cover the opening samples.
            track.ctts = list(zip(pairs[0::2], pairs[1::2]))
    stss = stbl.child(b"stss")
    if stss is not None:
        _, body = _fullbox(stss.payload)
        count = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0
        values = _u32_array(body, count, 4)
        track.sync_samples = list(values) if values is not None else None
    stsz = stbl.child(b"stsz")
    if stsz is not None:
        _, body = _fullbox(stsz.payload)
        if len(body) >= 8:
            constant, count = struct.unpack(">II", body[:8])
            track.sample_count = count
            if constant:
                track._constant_size = constant
            else:
                sizes = _u32_array(body, count, 8)
                if sizes is None:
                    track.table_problem = "sample-size table is shorter than its sample count"
                else:
                    track._sizes = sizes
    stsc = stbl.child(b"stsc")
    if stsc is not None:
        _, body = _fullbox(stsc.payload)
        count = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0
        values = _u32_array(body, count * 3, 4)
        if values is None:
            track.table_problem = "sample-to-chunk table is shorter than its entry count"
        else:
            track._stsc = list(zip(values[0::3], values[1::3], values[2::3]))
    for kind, wide in ((b"stco", False), (b"co64", True)):
        box = stbl.child(kind)
        if box is None:
            continue
        _, body = _fullbox(box.payload)
        count = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else 0
        offsets = _u32_array(body, count, 4, wide)
        if offsets is None:
            track.table_problem = "chunk-offset table is shorter than its entry count"
        else:
            track._chunk_offsets = offsets


def _chunk_layout(track: TrackInfo):
    """Yield (chunk_offset, first_sample_index, samples_in_chunk) per chunk."""
    runs = track._stsc
    offsets = track._chunk_offsets
    sample = 0
    for i, (first_chunk, per_chunk, _) in enumerate(runs):
        last_chunk = runs[i + 1][0] - 1 if i + 1 < len(runs) else len(offsets)
        for chunk in range(max(first_chunk, 1), min(last_chunk, len(offsets)) + 1):
            yield offsets[chunk - 1], sample, per_chunk
            sample += per_chunk


def _locate_samples(track: TrackInfo, file_size: int) -> None:
    """Furthest sample byte, and the first sample that lies beyond the file end.

    Iterates chunks, not samples: a PCM track stores one "sample" per audio
    frame (48 000 a second), so a per-sample loop over a long camera file
    would cost seconds.
    """
    if not track._chunk_offsets or not track._stsc or not track.sample_count:
        return
    prefix = None
    if not track._constant_size:
        if len(track._sizes) != track.sample_count:
            return
        prefix = (0,) + tuple(accumulate(track._sizes))
    mapped = 0
    for offset, first, count in _chunk_layout(track):
        count = min(count, track.sample_count - first)
        if count <= 0:
            break
        mapped = first + count
        if prefix is None:
            span = count * track._constant_size
        else:
            span = prefix[first + count] - prefix[first]
        end = offset + span
        track.max_sample_end = max(track.max_sample_end, end)
        if end > file_size and track.first_missing_sample is None:
            track.first_missing_sample = _first_sample_past(offset, first, count, file_size, track, prefix)
    if mapped < track.sample_count and not track.table_problem:
        track.table_problem = (f"the chunk tables place {mapped} of {track.sample_count} samples")


def _first_sample_past(offset: int, first: int, count: int, file_size: int,
                       track: TrackInfo, prefix) -> int:
    for k in range(count):
        size = track._constant_size if prefix is None else track._sizes[first + k]
        if offset + size > file_size:
            return first + k
        offset += size
    return first + count


def _read_moov(stream, box: Box, structure: Structure) -> None:
    payload_size = box.size - box.header
    if payload_size > MAX_MOOV_BYTES:
        structure.notes.append(f"the moov index is {payload_size / 1048576:.0f} MB; it was not inspected")
        return
    stream.seek(box.offset)
    data = stream.read(box.size)
    box.children = _parse_children(data, box.header, len(data), box.offset, 1, b"moov", "", structure)
    mvhd = box.child(b"mvhd")
    if mvhd is not None:
        version, body = _fullbox(mvhd.payload)
        at = 16 if version == 1 else 8
        if len(body) >= at + 4:
            structure.movie_timescale = struct.unpack(">I", body[at:at + 4])[0]
    structure.has_mvex = box.child(b"mvex") is not None
    for trak in box.all(b"trak"):
        handler_box = trak.child(b"mdia", b"hdlr")
        handler = handler_box.payload[8:12].decode("latin-1") if handler_box and len(handler_box.payload) >= 12 else ""
        if handler:
            _reparse_stsd_with_handler(trak, handler, structure)
        track = _read_track(trak, structure)
        _locate_samples(track, structure.file_size)
        structure.tracks.append(track)
    structure.parsed = True


def _reparse_stsd_with_handler(trak: Box, handler: str, structure: Structure) -> None:
    """Sample entries are laid out per handler type, unknown until hdlr is read."""
    stsd = trak.child(b"mdia", b"minf", b"stbl", b"stsd")
    if stsd is None:
        return
    payload = stsd.payload
    entries = []
    pos = 8  # FullBox header + entry_count
    while pos + 8 <= len(payload):
        size, kind = struct.unpack(">I4s", payload[pos:pos + 8])
        if size < 8 or pos + size > len(payload):
            structure.damage.append(f"sample description at offset {stsd.offset + stsd.header + pos} "
                                    f"overruns its stsd box")
            break
        entry = Box(kind, stsd.offset + stsd.header + pos, size, 8, payload[pos + 8:pos + size])
        skip = _entry_header_size(handler, entry.payload)
        if skip is not None and skip <= len(entry.payload):
            entry.children = _parse_children(entry.payload, skip, len(entry.payload),
                                             entry.offset + 8, 2, kind, handler, structure)
        entries.append(entry)
        pos += size
    stsd.children = entries


def read_structure(path: str) -> Structure:
    """Read the box structure of ``path``; never raises (errors land in ``error``)."""
    structure = Structure()
    try:
        structure.file_size = os.path.getsize(path)
        structure.family, detail = sniff_container(path)
        if structure.family != "ISO-BMFF":
            return structure
        with open(path, "rb") as stream:
            _walk_top_level(stream, structure)
            for box in structure.top:
                if box.type == b"ftyp":
                    stream.seek(box.offset + box.header)
                    structure.brand = stream.read(4).decode("latin-1").strip()
                elif box.type == b"moov":
                    structure.moov_count += 1
                    if structure.moov_offset is None:
                        structure.moov_offset = box.offset
                elif box.type == b"mdat" and structure.first_mdat_offset is None and box.size > box.header:
                    structure.first_mdat_offset = box.offset
                elif box.type == b"moof":
                    structure.moof_count += 1
            moov = next((b for b in structure.top if b.type == b"moov"), None)
            if moov is not None:
                _read_moov(stream, moov, structure)
    except (OSError, struct.error, ValueError) as exc:
        structure.error = f"{type(exc).__name__}: {exc}"
    return structure


def sample_offsets(track: TrackInfo, wanted: List[int]) -> Dict[int, Tuple[int, int]]:
    """{sample index: (file offset, size)} for the zero-based indices in ``wanted``."""
    targets = sorted(set(i for i in wanted if 0 <= i < track.sample_count))
    found: Dict[int, Tuple[int, int]] = {}
    if not targets or not track._chunk_offsets:
        return found
    sizes = track._sizes
    if not track._constant_size and len(sizes) != track.sample_count:
        return found
    pending = iter(targets)
    target = next(pending, None)
    for offset, first, count in _chunk_layout(track):
        while target is not None and target < first + count:
            if target < first:
                target = next(pending, None)
                continue
            if track._constant_size:
                at = offset + (target - first) * track._constant_size
                size = track._constant_size
            else:
                at = offset + sum(sizes[first:target])
                size = sizes[target]
            found[target] = (at, size)
            target = next(pending, None)
        if target is None:
            break
    return found


# ---------------------------------------------------------------------------
# Shared helpers for the checks
# ---------------------------------------------------------------------------

def _core():
    # Imported lazily: core.py imports this module at load time.
    from frameguard import core
    return core


def is_disabled(cfg: dict, name: str) -> bool:
    return name in (cfg.get("disabled_checks") or ())


def _sentence(parts: List[str]) -> str:
    text = "; ".join(p for p in parts if p)
    return (text[0].upper() + text[1:] + ".") if text else ""


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:+.0f} ms"


def _clock(seconds: float) -> str:
    return _core()._timecode(max(0.0, seconds))


def _xr_context(cfg: dict, projection: Optional[str]) -> bool:
    """True for a file that declares spherical video or a spec that requires it."""
    want = str(cfg.get("required_projection", "any") or "any").lower()
    return bool(projection) or want not in ("any", "none")


def _xr_enforced(cfg: dict) -> bool:
    return str(cfg.get("required_projection", "any") or "any").lower() not in ("any", "none")


# ---------------------------------------------------------------------------
# Container Structure (ADVISORY)
# ---------------------------------------------------------------------------

def _missing_media_text(structure: Structure) -> List[str]:
    out = []
    for track in structure.tracks:
        if not track.is_av or track.first_missing_sample is None:
            continue
        kind = "video" if track.handler == "vide" else "audio"
        at = track.sample_time(track.first_missing_sample)
        total = track.seconds(track.duration)
        out.append(f"{kind} samples from {_clock(at)} on (sample {track.first_missing_sample + 1} of "
                   f"{track.sample_count}, {total - at:.2f} s) lie beyond the end of the file")
    return out


def check_container_structure(structure: Structure, result, cfg: dict) -> None:
    """Box-level health of an MP4/MOV: truncation, index position, duplicates.

    Advisory by design. Decode-level truncation is File Integrity's verdict
    (essential); this row says *where* the file is cut (which box, which
    sample and timecode) and is the only evidence in a quick scan. Sources:
    ISO/IEC 14496-12 §4.2/§8; Telestream Qualify VM-127 (one moov) and
    VM-128 (one sample description); YouTube recommended upload settings
    ("moov atom at the front of the file (Fast Start)").
    """
    name = CONTAINER_STRUCTURE
    if structure.error:
        result.add_not_verified(name, f"the file's box structure could not be read ({structure.error}).")
        return
    if structure.family != "ISO-BMFF":
        result.add_skip(name, f"{structure.family} file: box-level checks apply to MP4/MOV."
                        if structure.family != "unknown" else "Not an MP4/MOV file: box-level checks do not apply.")
        return
    problems, notes = list(structure.damage), []
    problems += _missing_media_text(structure)
    if structure.moov_count == 0 and not structure.fragmented and not structure.damage:
        problems.append("no moov index box: players cannot open the file")
    if structure.moov_count > 1:
        problems.append(f"{structure.moov_count} moov index boxes (one allowed): players may read either")
    for track in structure.tracks:
        if track.is_av and len(track.entries) > 1:
            problems.append(f"{track.handler} track {track.track_id} has {len(track.entries)} sample descriptions "
                            f"(a splice of differently encoded media)")
        if track.is_av and track.table_problem:
            problems.append(f"{track.handler} track {track.track_id}: {track.table_problem}")
    if structure.moov_offset is not None and structure.first_mdat_offset is not None:
        if structure.moov_offset > structure.first_mdat_offset:
            text = "index (moov) after the media data, not fast-start"
            if cfg.get("require_faststart", False):
                problems.append(text + ": this spec requires fast-start so streaming players can begin "
                                       "before the whole file has downloaded")
            else:
                notes.append(text + " (fine for file delivery)")
        else:
            notes.append("fast-start (index before media)")
    if structure.fragmented:
        notes.append(f"fragmented MP4 ({structure.moof_count} fragment(s))")
    if structure.brand:
        notes.append(f"brand '{structure.brand}'")
    notes += structure.notes
    result.set_metadata("container_brand", structure.brand or None)
    result.set_metadata("faststart", structure.moov_offset is not None and structure.first_mdat_offset is not None
                        and structure.moov_offset < structure.first_mdat_offset)
    if problems:
        result.add_warn(name, _sentence(problems + notes))
    else:
        tracks = sum(1 for t in structure.tracks if t.is_av)
        result.add_pass(name, _sentence([f"ISO-BMFF, {tracks} audio/video track(s), every box and sample "
                                         f"inside the file"] + notes))


# ---------------------------------------------------------------------------
# Edit List (ADVISORY)
# ---------------------------------------------------------------------------

# Audio an encoder primes (AAC 1024/2112, Opus 312-3840 samples) is hidden by
# the first edit and is never presented; hiding more than this is a trim.
AUDIO_PRIMING_MAX_S = 0.1


def _edit_findings(track: TrackInfo, movie_ts: int, allow_trims: bool = True) -> Tuple[List[str], str]:
    """(anomalies, description of a normal edit list) for one track.

    A single edit that hides stored lead-in (a lossless trim) is normal
    unless ``allow_trims`` is off: every player that honours edit lists
    presents it correctly, and 0.9.0 deliberately treats trims as clean.
    """
    kind = "video" if track.handler == "vide" else "audio"
    if not track.edits or not movie_ts or not track.timescale:
        return [], ""
    frame = track.frame_duration() if kind == "video" else None
    out, lead, seen_media, at = [], 0, False, 0.0
    trim_note = ""
    media = [e for e in track.edits if e.media_time >= 0]
    for edit in track.edits:
        span = edit.segment_duration / movie_ts
        if edit.media_time < 0:
            if seen_media:
                out.append(f"{kind}: blank gap of {span:.3f} s inside the edit list at {_clock(at)}")
            else:
                lead += edit.segment_duration
        else:
            seen_media = True
            if abs(edit.rate - 1.0) > 1e-6:
                out.append(f"{kind}: an edit plays at rate {edit.rate:g} (dwell or speed edit)")
        at += span
    lead_s = lead / movie_ts
    if lead_s >= (frame / 2 if frame else 0.005):
        out.append(f"{kind} starts with {lead_s * 1000:.0f} ms of blank (an empty edit)")
    if len(media) > 1:
        cuts, t = [], lead_s
        for edit in media[:-1]:
            t += edit.segment_duration / movie_ts
            cuts.append(_clock(t))
        out.append(f"{kind}: {len(media)} media segments spliced by the edit list (playback jumps at "
                   f"{', '.join(cuts[:4])}) - a cut that exists only in the index")
    if media:
        first = media[0]
        if kind == "video":
            hidden = first.media_time - track.min_composition_time()
            if frame and hidden > track.timescale * frame / 2:
                frames = round(hidden / (track.timescale * frame))
                trim_note = (f"video: the edit list hides the first {frames} stored frame(s) "
                             f"({hidden / track.timescale:.3f} s), as a lossless trim leaves them")
        elif first.media_time / track.timescale > AUDIO_PRIMING_MAX_S:
            trim_note = (f"audio: the edit list hides the first {first.media_time / track.timescale:.3f} s "
                         f"of stored audio, as a lossless trim leaves it")
        if trim_note and not allow_trims:
            out.append(trim_note)
        # The presentation timeline of reordered video ends one composition
        # delay after the decode timeline that mdhd's duration measures.
        media_end = track.duration + (track.min_composition_time() if kind == "video" else 0)
        # A stream-copy cut drops tail B-frames whose references fell outside
        # it (up to the reorder depth), so its edit may run a few frames long.
        slack = max(0.5, 8 * frame) if frame else 0.5
        for edit in media:
            span = edit.segment_duration * track.timescale / movie_ts
            overrun = (edit.media_time + span - media_end) / track.timescale
            if overrun > slack:
                out.append(f"{kind}: an edit points {overrun:.3f} s past the end of the stored media")
                break
    normal = ""
    if not out and trim_note:
        normal = trim_note + " (allowed)"
    elif not out and len(track.edits) == 1 and media:
        offset = media[0].media_time
        normal = (f"{kind}: one edit starting at media tick {offset}"
                  + (" (B-frame reorder delay)" if kind == "video" and offset else
                     " (encoder priming)" if offset else ""))
    return out, normal


def check_edit_lists(structure: Structure, result, cfg: dict) -> None:
    """Edit lists that change what plays: splices, gaps, hidden lead-in, speed edits.

    Every ffmpeg/x264 file carries one benign edit (the B-frame delay, the
    AAC priming); those pass, and so does a lossless trim's single edit
    unless ``allow_trim_edit_lists`` is off. Splices, gaps, blank lead-in,
    speed edits and edits past the media are reported because players
    disagree on them and YouTube's upload guidance is "No Edit Lists (or
    the video might not get processed correctly)". Telestream DC-107
    likewise requires exactly one elst entry for iTunes delivery.
    """
    name = EDIT_LIST
    if structure.error:
        result.add_not_verified(name, f"the file's box structure could not be read ({structure.error}).")
        return
    if structure.family != "ISO-BMFF":
        result.add_skip(name, "Edit lists exist only in MP4/MOV files.")
        return
    if not structure.parsed:
        result.add_not_verified(name, "the moov index could not be inspected.")
        return
    findings, normal = [], []
    for track in structure.tracks:
        if not track.is_av:
            continue
        found, plain = _edit_findings(track, structure.movie_timescale,
                                      bool(cfg.get("allow_trim_edit_lists", True)))
        findings += found
        if plain:
            normal.append(plain)
    if findings:
        result.add_warn(name, _sentence(findings) + " Players that ignore edit lists will present this "
                        "differently.", confidence=80)
    elif normal:
        result.add_pass(name, _sentence(normal) + " Normal.")
    else:
        result.add_pass(name, "No edit lists.")


# ---------------------------------------------------------------------------
# A/V Sync Offset (ESSENTIAL)
# ---------------------------------------------------------------------------

def _effective_audio_start(track: dict, video_start: float) -> Tuple[float, str]:
    """(first presented audio time, note) with encoder priming removed.

    MPEG-TS stamps the first AAC packet one frame (1024 samples) before the
    decoded audio begins; the decoder discards that priming, so it is not
    an offset.
    """
    core = _core()
    start = core._safe_float(track.get("start_time"), 0.0) or 0.0
    rate = core._safe_int(track.get("sample_rate"), 0) or 0
    padding = core._safe_int(track.get("initial_padding"), 0) or 0
    codec = str(track.get("codec_name") or "").lower()
    priming = padding / rate if rate and padding else (1024 / rate if rate and codec == "aac" else 0.0)
    if priming and start < video_start and abs((video_start - start) - priming) <= 0.0015:
        return video_start, f" ({priming * 1000:.0f} ms encoder priming, discarded on decode)"
    return start, ""


def stream_track_id(stream: dict) -> Optional[int]:
    """The MP4 track_ID ffprobe reports as the stream ``id`` ("0x1")."""
    text = str(stream.get("id") or "").strip().lower()
    try:
        return int(text, 16) if text.startswith("0x") else (int(text) if text else None)
    except ValueError:
        return None


def _empty_edit_note(structure: Optional[Structure], stream: dict, kind: str) -> str:
    if structure is None or not structure.parsed or not structure.movie_timescale:
        return ""
    track_id = stream_track_id(stream)
    track = structure.track_by_id(track_id) if track_id else None
    if track is None:
        return ""
    lead = 0
    for edit in track.edits:
        if edit.media_time >= 0:
            break
        lead += edit.segment_duration
    if not lead:
        return ""
    return f"; the {kind} edit list begins with {lead / structure.movie_timescale * 1000:.0f} ms of blank"


def check_av_sync_offset(structure: Optional[Structure], probe_data: dict, result, cfg: dict) -> None:
    """Audio and video must start together: a container start offset is a sync error.

    The offset is the first presented audio time minus the first presented
    video time, from ffprobe start times (which apply MP4 edit lists).
    Positive = sound after picture. Limits: UK DPP v5 §2.8 allows sound to
    lead or lag vision by no more than 5 ms in a delivered file (WARN beyond
    ``av_offset_warn_ms``, confidence 60: a start offset is strong evidence
    of a sync error but can also be a capture-start gap). EBU R37-2007
    Table 1 allows sound 40 ms before / 60 ms after vision for the whole
    broadcast chain end to end; a file that alone exceeds that budget
    cannot meet R37 (FAIL beyond ``av_offset_max_early_ms`` /
    ``av_offset_max_late_ms``). R37 is a chain budget, not a file
    tolerance, which is why it bounds the FAIL and not the WARN. Sync inside
    the essence (a lip-sync error baked into picture or sound) is not
    measurable from metadata.
    """
    core = _core()
    name = AV_SYNC_OFFSET
    video = core._primary_video_stream(probe_data)
    tracks = core._audio_streams(probe_data)
    if video is None or not tracks:
        result.add_skip(name, "Need both video and audio streams to compare.")
        return
    warn_ms = float(cfg.get("av_offset_warn_ms", 5.0))
    max_early = max(warn_ms, float(cfg.get("av_offset_max_early_ms", 40.0)))
    max_late = max(warn_ms, float(cfg.get("av_offset_max_late_ms", 60.0)))
    v_start = core._safe_float(video.get("start_time"), 0.0) or 0.0
    entries, worst = [], 0.0
    for position, track in enumerate(tracks):
        label = core._track_label(position, track, len(tracks))
        a_start, priming_note = _effective_audio_start(track, v_start)
        offset = (a_start - v_start) * 1000
        worst = offset if abs(offset) > abs(worst) else worst
        direction = "after" if offset > 0 else "before"
        cause = _empty_edit_note(structure, video, "video") + _empty_edit_note(structure, track, "audio")
        if abs(offset) <= warn_ms:
            entries.append(("PASS", core._with_track(label, f"Audio and video start together ({offset:+.1f} ms"
                                                            f"{priming_note}; container timing only)")))
            continue
        limit = max_late if offset > 0 else max_early
        text = (f"Audio starts {abs(offset):.0f} ms {direction} the picture{cause} "
                f"(file limit ±{warn_ms:g} ms, DPP v5 §2.8")
        if abs(offset) <= limit:
            entries.append(("WARN", core._with_track(label, text + f"; within the {limit:g} ms EBU R37 "
                                                                   f"chain budget)")))
        else:
            entries.append(("FAIL", core._with_track(label, text + f"; beyond the {limit:g} ms EBU R37 allows "
                                                                   f"a whole broadcast chain)")))
    result.set_metadata("av_start_offset_ms", round(worst, 1))
    statuses = {s for s, _ in entries}
    confidence = 85 if "FAIL" in statuses else 60 if "WARN" in statuses else 100
    core._add_aggregate(result, name, entries, confidence)


# ---------------------------------------------------------------------------
# Timecode (ADVISORY)
# ---------------------------------------------------------------------------

def timecode_text(frames: int, fps: int, drop: bool) -> str:
    """HH:MM:SS:FF (';' before frames for drop-frame, SMPTE ST 12-1)."""
    if fps <= 0:
        return str(frames)
    if drop and fps in (30, 60):
        dropped = 2 if fps == 30 else 4
        per_ten = fps * 600 - dropped * 9
        per_minute = fps * 60 - dropped
        tens, rest = divmod(frames, per_ten)
        if rest > dropped:
            frames += dropped * 9 * tens + dropped * ((rest - dropped) // per_minute)
        else:
            frames += dropped * 9 * tens
    ff = frames % fps
    total_s = frames // fps
    sep = ";" if drop else ":"
    return f"{total_s // 3600 % 24:02d}:{total_s // 60 % 60:02d}:{total_s % 60:02d}{sep}{ff:02d}"


def _timecode_frames(h: int, m: int, s: int, f: int, fps: int, drop: bool) -> int:
    frames = ((h * 60 + m) * 60 + s) * fps + f
    if drop and fps in (30, 60):
        dropped = 2 if fps == 30 else 4
        minutes = h * 60 + m
        frames -= dropped * (minutes - minutes // 10)
    return frames


def _strided(count: int, limit: int) -> List[int]:
    if count <= limit:
        return list(range(count))
    step = count / limit
    picks = sorted({int(i * step) for i in range(limit)} | {count - 1})
    return picks


def _decode_times(track: TrackInfo, indices: List[int]) -> Dict[int, float]:
    out, wanted = {}, iter(sorted(indices))
    target = next(wanted, None)
    ticks, seen = 0, 0
    for count, delta in track.stts:
        while target is not None and target < seen + count:
            out[target] = track.seconds(ticks + (target - seen) * delta)
            target = next(wanted, None)
        ticks += count * delta
        seen += count
    while target is not None:
        out[target] = track.seconds(ticks)
        target = next(wanted, None)
    return out


def _tmcd_report(path: str, track: TrackInfo) -> Tuple[Optional[str], Optional[str], int]:
    """(start text, discontinuity text, samples checked) for a QuickTime tmcd track.

    Each tmcd sample is a frame number (big-endian uint32) valid from its
    decode time; the frame number counts frames, so drop-frame affects
    only how it is displayed (QuickTime File Format, Timecode Media).
    """
    entry = next((e.tmcd for e in track.entries if e.tmcd), None)
    if entry is None or not track.sample_count or not entry["frame_duration"] or not entry["timescale"]:
        return None, None, 0
    fps = entry["frames"] or round(entry["timescale"] / entry["frame_duration"])
    drop = entry["drop_frame"]
    frame_s = entry["frame_duration"] / entry["timescale"]
    indices = _strided(track.sample_count, MAX_TIMECODE_SAMPLES)
    where = sample_offsets(track, indices)
    times = _decode_times(track, indices)
    values: Dict[int, int] = {}
    with open(path, "rb") as stream:
        for index in indices:
            if index not in where or where[index][1] < 4:
                continue
            stream.seek(where[index][0])
            raw = stream.read(4)
            if len(raw) == 4:
                values[index] = struct.unpack(">i", raw)[0]
    if 0 not in values:
        return None, None, 0
    first = values[0]
    start = f"{timecode_text(first, fps, drop)} ({fps} fps {'DF' if drop else 'NDF'})"
    for index in sorted(values):
        expected = first + round(times.get(index, 0.0) / frame_s)
        if values[index] != expected:
            jump = (f"timecode track jumps to {timecode_text(values[index], fps, drop)} at "
                    f"{_clock(times.get(index, 0.0))} (continuous timecode would read "
                    f"{timecode_text(expected, fps, drop)})")
            return start, jump, len(values)
    return start, None, len(values)


def _gop_report(path: str, track: TrackInfo, fps: int) -> Tuple[Optional[str], Optional[str], int]:
    """MPEG-2 GOP-header timecode (ISO/IEC 13818-2 §6.2.2.6) at every sync sample."""
    sync = track.sync_samples or list(range(1, track.sample_count + 1))
    indices = [n - 1 for n in sync[:MAX_GOP_HEADERS]]
    where = sample_offsets(track, indices)
    found: List[Tuple[int, int, bool]] = []
    with open(path, "rb") as stream:
        for index in indices:
            if index not in where:
                continue
            stream.seek(where[index][0])
            head = stream.read(min(where[index][1], 4096))
            at = head.find(b"\x00\x00\x01\xb8")
            if at < 0 or at + 8 > len(head):
                continue
            bits = int.from_bytes(head[at + 4:at + 8], "big")
            drop = bool(bits >> 31 & 1)
            h, m, s, f = bits >> 26 & 0x1F, bits >> 20 & 0x3F, bits >> 13 & 0x3F, bits >> 7 & 0x3F
            found.append((index, _timecode_frames(h, m, s, f, fps, drop), drop))
    if not found:
        return None, None, 0
    start = f"{timecode_text(found[0][1], fps, found[0][2])} ({fps} fps)"
    for (i0, f0, _), (i1, f1, d1) in zip(found, found[1:]):
        if f1 - f0 != i1 - i0:
            when = track.sample_time(i1)
            return start, (f"picture (GOP header) timecode jumps to {timecode_text(f1, fps, d1)} at "
                           f"{_clock(when)} (continuous timecode would read "
                           f"{timecode_text(f0 + i1 - i0, fps, d1)})"), len(found)
    return start, None, len(found)


def check_timecode(path: str, structure: Structure, probe_data: dict, result, cfg: dict) -> None:
    """Timecode continuity in a QuickTime tmcd track and in MPEG-2 GOP headers.

    Emits a row only when the file carries timecode. Telestream documents
    start/end timecode checks (Qualify FC-105/106/115/116); a jump *within*
    the file (an edit or a recorder restart) breaks conform and EDL
    relinking, so it is reported as advisory.
    """
    if structure.family != "ISO-BMFF" or not structure.parsed:
        return
    core = _core()
    video = core._primary_video_stream(probe_data) or {}
    problems, notes = [], []
    try:
        for track in structure.tracks_of("tmcd"):
            start, jump, checked = _tmcd_report(path, track)
            if start:
                notes.append(f"timecode track starts {start}, {checked} sample(s) checked")
            if jump:
                problems.append(jump)
        if str(video.get("codec_name") or "").lower() == "mpeg2video":
            vtrack = structure.track_by_id(stream_track_id(video) or -1)
            fps = round(core.parse_frame_rate(video.get("r_frame_rate", "0/0")))
            if vtrack is not None and fps > 0:
                start, jump, checked = _gop_report(path, vtrack, fps)
                if start:
                    notes.append(f"picture GOP timecode starts {start}, {checked} GOP header(s) checked")
                if jump:
                    problems.append(jump)
    except OSError as exc:
        result.add_not_verified(TIMECODE, f"timecode samples could not be read ({type(exc).__name__}: {exc}).")
        return
    if problems:
        result.add_warn(TIMECODE, _sentence(problems + notes), confidence=90)
    elif notes:
        result.add_pass(TIMECODE, _sentence(notes) + " Continuous.")


# ---------------------------------------------------------------------------
# Codec Profile (ESSENTIAL when the spec sets an allowlist or ceiling)
# ---------------------------------------------------------------------------

# ITU-T H.264 Table A-1: level_idc -> (MaxMBPS, MaxFS in macroblocks).
H264_LEVELS = [
    (10, 1485, 99), (9, 1485, 99), (11, 3000, 396), (12, 6000, 396), (13, 11880, 396),
    (20, 11880, 396), (21, 19800, 792), (22, 20250, 1620), (30, 40500, 1620),
    (31, 108000, 3600), (32, 216000, 5120), (40, 245760, 8192), (41, 245760, 8192),
    (42, 522240, 8704), (50, 589824, 22080), (51, 983040, 36864), (52, 2073600, 36864),
    (60, 4177920, 139264), (61, 8355840, 139264), (62, 16711680, 139264),
]
# ITU-T H.265 Table A.8 (general_level_idc = 30 x level): (MaxLumaPs, MaxLumaSr).
HEVC_LEVELS = [
    (30, 36864, 552960), (60, 122880, 3686400), (63, 245760, 7372800),
    (90, 552960, 16588800), (93, 983040, 33177600), (120, 2228224, 66846720),
    (123, 2228224, 133693440), (150, 8912896, 267386880), (153, 8912896, 534773760),
    (156, 8912896, 1069547520), (180, 35651584, 1069547520), (183, 35651584, 2139095040),
    (186, 35651584, 4278190080),
]


def level_label(codec: str, idc: int) -> str:
    if codec == "h264":
        return "1b" if idc == 9 else f"{idc / 10:g}"
    return f"{idc / 30:g}"


def _level_rank(codec: str, idc: int) -> float:
    """Order levels; H.264 level_idc 9 (1b) sits between 1 and 1.1."""
    return 10.5 if codec == "h264" and idc == 9 else float(idc)


def required_level(codec: str, width: int, height: int, fps: float) -> Optional[int]:
    """Lowest level whose picture-size and sample-rate limits admit w x h at fps.

    H.264: MaxFS and MaxMBPS in 16x16 macroblocks, and neither dimension
    may exceed sqrt(8 x MaxFS) macroblocks (Table A-1, A.3.1). HEVC: MaxLumaPs
    and MaxLumaSr, and neither dimension may exceed sqrt(8 x MaxLumaPs)
    (Table A.8, A.4.1). 5760x2880 HEVC therefore needs Level 6, which is
    legitimate for 5.7K/8K XR masters.
    """
    if width <= 0 or height <= 0 or fps <= 0:
        return None
    rate_slack = 1.0005  # 30000/1001 vs 30 must not change the answer
    if codec == "h264":
        mbw, mbh = math.ceil(width / 16), math.ceil(height / 16)
        frame = mbw * mbh
        for idc, max_mbps, max_fs in H264_LEVELS:
            if idc == 9:
                continue
            side = math.sqrt(8 * max_fs)
            if frame <= max_fs and mbw <= side and mbh <= side and frame * fps <= max_mbps * rate_slack:
                return idc
        return None
    if codec == "hevc":
        luma = width * height
        for idc, max_ps, max_sr in HEVC_LEVELS:
            side = math.sqrt(8 * max_ps)
            if luma <= max_ps and width <= side and height <= side and luma * fps <= max_sr * rate_slack:
                return idc
        return None
    return None


def _allowed(values, actual: str) -> bool:
    items = [str(v).strip().lower() for v in (values or ["any"])]
    return "any" in items or actual.strip().lower() in items


def check_codec_profile(probe_data: dict, result, cfg: dict) -> None:
    """Profile, level, pixel format and decoder ceiling of the picture stream.

    FAILs only on what the spec sets: ``allowed_video_profiles``,
    ``allowed_pix_fmts`` (Meta Media Studio: "Yuv 420p" for Quest), and
    the decoder ceiling ``max_frame_pixels`` / ``max_pixel_rate`` (Meta
    Horizon OS: 180/360 "Max 8192x4096 / 60fps (hardware decode limit)").
    A level declared below what the frame size and rate need is a warning:
    strict hardware decoders refuse such streams (ITU-T H.264 Table A-1,
    H.265 Table A.8). A flat level ceiling is never applied because 5.7K
    and 8K HEVC legitimately need Level 6.x.
    """
    core = _core()
    name = CODEC_PROFILE
    video = core._primary_video_stream(probe_data)
    if video is None:
        result.add_skip(name, "No video stream found.")
        return
    codec = str(video.get("codec_name") or "unknown").lower()
    profile = str(video.get("profile") or "unknown")
    pix_fmt = str(video.get("pix_fmt") or "unknown")
    width = core._safe_int(video.get("width"), 0) or 0
    height = core._safe_int(video.get("height"), 0) or 0
    fps = (core.parse_frame_rate(video.get("avg_frame_rate", "0/0"))
           or core.parse_frame_rate(video.get("r_frame_rate", "0/0")))
    level = core._safe_int(video.get("level"), -99)
    fails, warns = [], []
    summary = f"{ {'h264': 'H.264', 'hevc': 'HEVC'}.get(codec, codec)} {profile}"
    if codec in ("h264", "hevc") and level and level > 0:
        summary += f" @ Level {level_label(codec, level)}"
    summary += f", {pix_fmt}"
    result.set_metadata("video_profile", profile)
    result.set_metadata("pix_fmt", pix_fmt)
    if not _allowed(cfg.get("allowed_video_profiles"), profile):
        fails.append(f"profile '{profile}' is not allowed by this spec "
                     f"(allowed: {', '.join(cfg.get('allowed_video_profiles') or [])})")
    if not _allowed(cfg.get("allowed_pix_fmts"), pix_fmt):
        fails.append(f"pixel format '{pix_fmt}' is not allowed by this spec "
                     f"(allowed: {', '.join(cfg.get('allowed_pix_fmts') or [])})")
    max_pixels = int(cfg.get("max_frame_pixels", 0) or 0)
    max_rate = int(cfg.get("max_pixel_rate", 0) or 0)
    if max_pixels and width * height > max_pixels:
        fails.append(f"{width}x{height} = {width * height:,} pixels exceeds the target decoder's "
                     f"{max_pixels:,}-pixel frame limit")
    if max_rate and fps > 0 and width * height * fps > max_rate * 1.0005:
        fails.append(f"{width}x{height} at {fps:.3f} fps = {width * height * fps / 1e6:,.0f} Mpixel/s exceeds "
                     f"the target decoder's {max_rate / 1e6:,.0f} Mpixel/s limit")
    needed = required_level(codec, width, height, fps)
    if needed is not None:
        result.set_metadata("required_level", level_label(codec, needed))
        if level and level > 0 and _level_rank(codec, level) < _level_rank(codec, needed):
            table = "ITU-T H.264 Table A-1" if codec == "h264" else "ITU-T H.265 Table A.8"
            warns.append(f"declares Level {level_label(codec, level)} but {width}x{height} at {fps:.3f} fps "
                         f"needs Level {level_label(codec, needed)} ({table}); strict hardware decoders "
                         f"may refuse it")
        if codec == "h264" and max(needed, level or 0) > 52:
            warns.append(f"H.264 Level {level_label(codec, max(needed, level or 0))} is beyond what most "
                         f"hardware decoders support (5.2); HEVC is the usual choice at this size")
    elif codec in ("h264", "hevc") and width and height and fps > 0:
        warns.append(f"{width}x{height} at {fps:.3f} fps exceeds every {codec.upper()} level")
    if fails:
        result.add_fail(name, _sentence([summary] + fails + warns))
    elif warns:
        result.add_warn(name, _sentence([summary] + warns), confidence=80)
    else:
        result.add_pass(name, summary + (f" (Level {level_label(codec, needed)} required)" if needed else ""))


# ---------------------------------------------------------------------------
# Spatial Audio (ESSENTIAL when the spec requires spatial audio)
# ---------------------------------------------------------------------------

# Ambisonic channel counts: (order+1)^2, optionally + 2 head-locked stereo.
_AMBISONIC_COUNTS = {4, 9, 16, 6, 11, 18}
_TBE_COUNTS = {8, 10}


def _sa3d_for(structure: Optional[Structure], stream: dict) -> Optional[dict]:
    if structure is None or not structure.parsed:
        return None
    track = structure.track_by_id(stream_track_id(stream) or -1)
    if track is None:
        return None
    return next((e.sa3d for e in track.entries if e.sa3d is not None), None)


def sa3d_problems(sa3d: dict, channels: int) -> List[str]:
    """Inconsistencies in an SA3D box (Google Spatial Audio RFC, version 0)."""
    if "error" in sa3d:
        return [sa3d["error"]]
    out = []
    count = sa3d["num_channels"]
    if sa3d["version"] != 0:
        out.append(f"SA3D version {sa3d['version']} (only version 0 is defined)")
    if sa3d["ambisonic_type"] != 0:
        out.append(f"ambisonic type {sa3d['ambisonic_type']} (only periphonic, 0, is defined)")
    if sa3d["ordering"] != 0:
        out.append(f"channel ordering {sa3d['ordering']} (YouTube and Quest expect ACN, 0)")
    if sa3d["normalization"] != 0:
        out.append(f"normalization {sa3d['normalization']} (YouTube and Quest expect SN3D, 0)")
    needed = (sa3d["order"] + 1) ** 2 + (2 if sa3d["head_locked_stereo"] else 0)
    if count != needed:
        out.append(f"SA3D declares {count} channels but order {sa3d['order']}"
                   f"{' + head-locked stereo' if sa3d['head_locked_stereo'] else ''} needs {needed}")
    if channels and count != channels:
        out.append(f"SA3D declares {count} channels but the audio track has {channels}")
    if not sa3d["map_complete"] or sorted(sa3d["channel_map"]) != list(range(count)):
        out.append(f"the SA3D channel map is not a permutation of 0..{count - 1}")
    return out


def _spatial_entry(sa3d: Optional[dict], channels: int, layout: str, xr: bool,
                   strict: bool) -> Tuple[str, str]:
    if sa3d is not None:
        problems = sa3d_problems(sa3d, channels)
        if problems:
            return ("FAIL" if strict else "WARN"), _sentence(problems)
        extra = " + head-locked stereo" if sa3d["head_locked_stereo"] else ""
        return "PASS", f"Ambisonics order {sa3d['order']} (ACN/SN3D){extra}, {channels} channels, SA3D consistent"
    if xr and channels in _AMBISONIC_COUNTS:
        return "WARN", (f"{channels}-channel track ({layout}) in a 360° file without an SA3D box: players treat it "
                        f"as ordinary multichannel audio, not ambisonics (Google Spatial Audio RFC)")
    if xr and channels in _TBE_COUNTS:
        return "PASS", (f"{channels} channels without SA3D: if this is FB360 TBE (8 or 8+2), its layout cannot be "
                        f"verified from metadata")
    return "PASS", f"{channels}-channel {layout}: not spatial audio"


def check_spatial_audio(structure: Optional[Structure], probe_data: dict, result, cfg: dict,
                        projection: Optional[str]) -> None:
    """SA3D ambisonic metadata present and consistent with each track's channels.

    Reported for 360° files, for files that carry SA3D, and whenever the spec
    sets ``spatial_audio = required`` (Meta Media Studio: FOA 4-channel
    ACN/SN3D AAC, or FB360 8/10-channel). ffmpeg 9 does not expose SA3D, so
    the box is read directly.
    """
    core = _core()
    name = SPATIAL_AUDIO
    tracks = core._audio_streams(probe_data)
    required = str(cfg.get("spatial_audio", "any")).lower() == "required"
    xr = _xr_context(cfg, projection)
    boxes = [_sa3d_for(structure, t) for t in tracks]
    if not (xr or required or any(b is not None for b in boxes)):
        return
    if not tracks:
        if required:
            result.add_fail(name, "This spec requires spatial (ambisonic) audio; the file has no audio.")
        else:
            result.add_skip(name, "No audio stream.")
        return
    strict = required or _xr_enforced(cfg)
    entries = []
    for position, (track, sa3d) in enumerate(zip(tracks, boxes)):
        label = core._track_label(position, track, len(tracks))
        channels = core._safe_int(track.get("channels"), 0) or 0
        layout = str(track.get("channel_layout") or "unknown layout")
        status, text = _spatial_entry(sa3d, channels, layout, xr, strict)
        entries.append((status, core._with_track(label, text)))
    valid = any(b is not None and not sa3d_problems(b, core._safe_int(t.get("channels"), 0) or 0)
                for t, b in zip(tracks, boxes))
    tbe = any((core._safe_int(t.get("channels"), 0) or 0) in _TBE_COUNTS for t in tracks)
    if required and not valid and not tbe:
        entries.append(("FAIL", "This spec requires spatial audio: no track carries a valid SA3D ambisonic "
                                "description (or an FB360 8/10-channel layout)"))
    core._add_aggregate(result, name, entries)


# ---------------------------------------------------------------------------
# Track Layout (ADVISORY)
# ---------------------------------------------------------------------------

def check_track_layout(probe_data: dict, result, cfg: dict) -> None:
    """Stream inventory and multi-track consistency (Telestream FC-121 counts).

    Per-track audio judgments (codec, rate, silence, loudness) already run
    on every track; this row flags what only the set of tracks shows: an
    extra picture stream nobody checks, more audio tracks than the spec
    allows, no default track, or tracks at different sample rates.
    """
    core = _core()
    name = TRACK_LAYOUT
    streams = (probe_data or {}).get("streams", []) or []
    video = [s for s in streams if s.get("codec_type") == "video"
             and not (s.get("disposition") or {}).get("attached_pic")]
    covers = [s for s in streams if s.get("codec_type") == "video"
              and (s.get("disposition") or {}).get("attached_pic")]
    audio = core._audio_streams(probe_data)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    data = [s for s in streams if s.get("codec_type") in ("data", "attachment")]
    parts = [f"{len(video)} video", f"{len(audio)} audio"]
    if subs:
        parts.append(f"{len(subs)} subtitle")
    if data:
        tags = sorted({str(s.get("codec_tag_string") or s.get("codec_name") or "data") for s in data})
        parts.append(f"{len(data)} data ({', '.join(tags)})")
    if covers:
        parts.append(f"{len(covers)} cover image")
    inventory = ", ".join(parts)
    problems = []
    if len(video) > 1:
        problems.append(f"{len(video)} picture streams: QC and most players use only the first")
    limit = int(cfg.get("max_audio_tracks", 0) or 0)
    if limit and len(audio) > limit:
        problems.append(f"{len(audio)} audio tracks; this spec allows {limit}")
    if len(audio) > 1:
        defaults = [s for s in audio if (s.get("disposition") or {}).get("default")]
        if not defaults:
            problems.append("no audio track is marked default, so players choose one arbitrarily")
        rates = sorted({core._safe_int(s.get("sample_rate"), 0) for s in audio} - {0})
        if len(rates) > 1:
            problems.append(f"audio tracks use different sample rates ({', '.join(map(str, rates))} Hz)")
    result.set_metadata("stream_inventory", inventory)
    if problems:
        result.add_warn(name, _sentence([inventory] + problems), confidence=80)
    else:
        result.add_pass(name, inventory)


# ---------------------------------------------------------------------------
# XR Metadata extension (called from core.check_xr_metadata)
# ---------------------------------------------------------------------------

_V1_STEREO = {"mono": "mono", "top-bottom": "top_bottom", "left-right": "side_by_side"}
_V2_STEREO = {0: "mono", 1: "top_bottom", 2: "side_by_side", 3: "stereo_custom", 4: "side_by_side"}


def display_transform(video: dict) -> Tuple[int, bool]:
    """(rotation degrees, mirrored) from the stream's display matrix.

    A negative determinant of the 2x2 part means the picture is flipped,
    which a rotation angle alone cannot express.
    """
    rotation = _core()._stream_rotation_degrees(video)
    mirrored = False
    for side in video.get("side_data_list") or []:
        text = side.get("displaymatrix") if isinstance(side, dict) else None
        if not text:
            continue
        rows = [[int(v) for v in re.findall(r"-?\d+", line.split(":", 1)[1])]
                for line in str(text).strip().splitlines() if ":" in line]
        if len(rows) >= 2 and len(rows[0]) >= 2 and len(rows[1]) >= 2:
            mirrored = rows[0][0] * rows[1][1] - rows[0][1] * rows[1][0] < 0
    return rotation, mirrored


def _generations(structure: Optional[Structure], video: dict) -> Tuple[Optional[dict], Optional[dict]]:
    """(V1 uuid metadata, V2 st3d/sv3d metadata) of the picture track, if present."""
    if structure is None or not structure.parsed:
        return None, None
    track = structure.track_by_id(stream_track_id(video) or -1)
    if track is None:
        track = next(iter(structure.tracks_of("vide")), None)
    if track is None:
        return None, None
    v1 = None
    if track.v1_xml and re.search(r"<GSpherical:Spherical>\s*true", track.v1_xml, re.I):
        proj = re.search(r"<GSpherical:ProjectionType>\s*([^<\s]+)", track.v1_xml)
        mode = re.search(r"<GSpherical:StereoMode>\s*([^<\s]+)", track.v1_xml)
        v1 = {"projection": (proj.group(1).lower() if proj else "equirectangular"),
              "stereo": _V1_STEREO.get(mode.group(1).lower(), mode.group(1).lower()) if mode else "mono"}
    v2 = None
    entry = next((e for e in track.entries if e.st3d is not None or e.sv3d is not None), None)
    if entry is not None:
        v2 = {"projection": (entry.sv3d or {}).get("projection"),
              "stereo": _V2_STEREO.get(entry.st3d, "mono") if entry.st3d is not None else "mono",
              "bounds": (entry.sv3d or {}).get("bounds", (0, 0, 0, 0)),
              "pose": (entry.sv3d or {}).get("pose", (0.0, 0.0, 0.0))}
    return v1, v2


def _bounds(video: dict, v2: Optional[dict]) -> Tuple[float, float, float, float]:
    """Equirect crop as fractions (top, bottom, left, right): 0.32 fixed point."""
    if v2 and v2.get("bounds"):
        return tuple(b / 2 ** 32 for b in v2["bounds"])
    for side in video.get("side_data_list") or []:
        if isinstance(side, dict) and str(side.get("side_data_type", "")).lower() == "spherical mapping":
            vals = [_core()._safe_int(side.get(k), 0) or 0 for k in ("bound_top", "bound_bottom", "bound_left", "bound_right")]
            return tuple(v / 2 ** 32 for v in vals)
    return 0.0, 0.0, 0.0, 0.0


def xr_metadata_findings(video: dict, projection: Optional[str], stereo: Optional[str],
                         structure: Optional[Structure], cfg: dict) -> Tuple[List[str], List[str], List[str]]:
    """(failures, warnings, notes) beyond what the spec's projection/stereo fields enforce.

    Failures arise only when the spec requires a projection (an XR delivery
    spec); the same findings are warnings on files that merely declare
    spherical metadata. Sources: Google Spherical Video V1/V2 RFCs (V2
    supersedes V1; V2 wins on conflict; the view orientation belongs in
    the projection header, not in a track matrix); Meta Media Studio
    360/180 requirements (2:1 per eye for 360, top-bottom only for 360
    stereo, square top-bottom frames).
    """
    hard: List[str] = []
    soft: List[str] = []
    notes: List[str] = []
    enforced = _xr_enforced(cfg)
    flag = hard.append if enforced else soft.append
    v1, v2 = _generations(structure, video)
    if v1 and not v2:
        soft.append("only Spherical V1 metadata (uuid XML), superseded by V2 (st3d/sv3d) which current "
                    "players read first")
    if v1 and v2 and (v1["stereo"] != v2["stereo"]
                      or (v2["projection"] and v1["projection"] != v2["projection"])):
        flag(f"Spherical V1 says {v1['projection']} {v1['stereo']} but V2 says "
             f"{v2['projection'] or 'no projection'} {v2['stereo']}: players disagree on the layout")
    rotation, mirrored = display_transform(video)
    if (rotation or mirrored) and (projection or enforced) and not cfg.get("allow_display_rotation", False):
        what = " and ".join(filter(None, [f"rotated {abs(rotation)}°" if rotation else "",
                                          "mirrored" if mirrored else ""]))
        flag(f"the display matrix says {what}: headsets apply it on top of the projection; the view "
             f"orientation belongs in the projection pose (prhd)")
    if v2 and any(abs(a) > 1e-6 for a in v2.get("pose", ())):
        yaw, pitch, roll = v2["pose"]
        notes.append(f"initial view yaw {yaw:g}°, pitch {pitch:g}°, roll {roll:g}°")
    if projection == "equirectangular":
        _equirect_geometry(video, stereo, v2, cfg, flag, hard, notes)
    return hard, soft, notes


def _equirect_geometry(video: dict, stereo: Optional[str], v2: Optional[dict], cfg: dict,
                       flag, hard: List[str], notes: List[str]) -> None:
    core = _core()
    width = core._safe_int(video.get("width"), 0) or 0
    height = core._safe_int(video.get("height"), 0) or 0
    if not width or not height:
        return
    top, bottom, left, right = _bounds(video, v2)
    span_h, span_v = 360.0 * (1 - left - right), 180.0 * (1 - top - bottom)
    if span_h <= 0 or span_v <= 0:
        flag("the equirectangular bounds leave no picture")
        return
    expected = span_h / span_v
    layout = stereo or "mono"
    eye_w, eye_h = width, height
    if layout == "top_bottom":
        eye_h = height / 2
    elif layout == "side_by_side":
        eye_w = width / 2
    ratio = eye_w / eye_h
    cover = "360°" if span_h >= 359.9 else f"{span_h:.0f}°"
    want_stereo = str(cfg.get("required_stereo_mode", "any") or "any")
    if abs(ratio / expected - 1) > 0.01:
        if layout == "top_bottom" and abs(ratio / (2 * expected) - 1) <= 0.01:
            text = (f"half-height top-bottom: each eye is {eye_w:.0f}x{eye_h:.0f} ({ratio:.0f}:1, "
                    f"squeezed; a full {cover} eye is {expected:g}:1)")
            if want_stereo == "top_bottom":
                hard.append(text + "; Meta Quest top-bottom 360° must be a square frame "
                                   "(e.g. 3840x3840, 5760x5760)")
            else:
                notes.append(text)
        elif layout == "side_by_side" and abs(ratio * 2 / expected - 1) <= 0.01:
            notes.append(f"half-width side-by-side (each eye squeezed to {eye_w:.0f}x{eye_h:.0f})")
        else:
            flag(f"{width}x{height} does not fit an equirectangular {layout.replace('_', '-')} picture: "
                 f"each eye is {ratio:.2f}:1, a {cover} equirect is {expected:.2f}:1")
    if layout == "side_by_side" and span_h >= 359.9:
        flag("360° stereo is tagged side-by-side; Meta Quest and YouTube take 360° stereo only as top-bottom")


# ---------------------------------------------------------------------------
# Orchestration (called once from core._run_metadata_checks)
# ---------------------------------------------------------------------------

def run_structure_checks(filepath: str, structure: Structure, probe_data: dict, result, cfg: dict,
                         projection: Optional[str]) -> None:
    """Run every check of this module in report order, honouring ``disabled_checks``."""
    steps = (
        (CODEC_PROFILE, lambda: check_codec_profile(probe_data, result, cfg)),
        (CONTAINER_STRUCTURE, lambda: check_container_structure(structure, result, cfg)),
        (EDIT_LIST, lambda: check_edit_lists(structure, result, cfg)),
        (AV_SYNC_OFFSET, lambda: check_av_sync_offset(structure, probe_data, result, cfg)),
        (TIMECODE, lambda: check_timecode(filepath, structure, probe_data, result, cfg)),
        (SPATIAL_AUDIO, lambda: check_spatial_audio(structure, probe_data, result, cfg, projection)),
        (TRACK_LAYOUT, lambda: check_track_layout(probe_data, result, cfg)),
    )
    result.set_metadata("container_family", structure.family)
    for name, step in steps:
        if is_disabled(cfg, name):
            result.add_skip(name, "Switched off by this spec.")
            continue
        step()
