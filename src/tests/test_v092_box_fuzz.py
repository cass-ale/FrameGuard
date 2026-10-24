"""MP4/MOV box reader on hostile input (review B1-B3, B13; fix 8).

FrameGuard reads the boxes of untrusted deliverables, synchronously, in
watch folders. These tests feed it structurally valid-looking but hostile
files: 64-bit sizes, size 0, truncated and overlapping boxes, deep nesting,
huge declared table counts, non-monotonic sample-to-chunk tables. The reader
and every check built on it must return (never raise), in bounded time and
with memory bounded by the bytes actually present.

The Hypothesis test is bounded (fixed examples, a per-example deadline, no
example database) so it runs in the normal suite.
"""

import os
import struct
import tempfile
import time
import tracemalloc

import pytest

from frameguard import container_checks as cc
from frameguard.core import DEFAULT_CONFIG

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, body: bytes, version: int = 0) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + body)


FTYP = box(b"ftyp", b"isom\0\0\x02\0isomiso2")
MVHD = full(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 1000) + bytes(80))


def _hdlr(handler: bytes) -> bytes:
    return full(b"hdlr", b"\0\0\0\0" + handler + bytes(12) + b"\0")


def _mdhd(timescale=1000, duration=1000) -> bytes:
    return full(b"mdhd", struct.pack(">IIII", 0, 0, timescale, duration) + b"\0\0\0\0")


def _tkhd(track_id=1) -> bytes:
    return full(b"tkhd", struct.pack(">III", 0, 0, track_id) + bytes(68))


def _track(handler: bytes, stbl_children: bytes, track_id=1, entry: bytes = b"") -> bytes:
    stsd = full(b"stsd", struct.pack(">I", 1 if entry else 0) + entry)
    stbl = box(b"stbl", stsd + stbl_children)
    return box(b"trak", _tkhd(track_id) + box(b"mdia", _mdhd() + _hdlr(handler) + box(b"minf", stbl)))


def _write(data: bytes) -> str:
    handle = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    handle.write(data)
    handle.close()
    return handle.name


def _measured(fn):
    """(result, seconds, peak traced bytes): timed without tracemalloc (which
    slows allocation-heavy code several-fold), then run again traced."""
    start = time.perf_counter()
    value = fn()
    elapsed = time.perf_counter() - start
    tracemalloc.start()
    try:
        fn()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return value, elapsed, peak


# ---------------------------------------------------------------------------
# Deterministic repros from r091_container/REPORT.md section 3
# ---------------------------------------------------------------------------

class TestHostileTables:
    def test_non_monotonic_stsc_is_rejected_in_linear_time(self):
        """B1: alternating first_chunk (1, N, 1, N ...) made the chunk walk
        O(runs x chunks): 60 KB took 43 s. ISO/IEC 14496-12 8.7.4 requires
        first_chunk to increase strictly from 1."""
        runs, chunks = 1500, 1500
        stsc = full(b"stsc", struct.pack(">I", runs) + b"".join(
            struct.pack(">III", 1 if i % 2 == 0 else chunks, 1, 1) for i in range(runs)))
        stsz = full(b"stsz", struct.pack(">II", 1, 0xFFFFFFFF))
        stco = full(b"stco", struct.pack(">I", chunks) + b"".join(struct.pack(">I", 64) for _ in range(chunks)))
        stts = full(b"stts", struct.pack(">III", 1, 0xFFFFFFFF, 1))
        path = _write(FTYP + box(b"moov", MVHD + _track(b"vide", stts + stsc + stsz + stco)))
        try:
            structure, elapsed, _ = _measured(lambda: cc.read_structure(path))
        finally:
            os.unlink(path)
        assert elapsed < 1.0, elapsed
        problem = structure.tracks[0].table_problem
        assert "sample-to-chunk" in problem and "increase" in problem, problem

    def test_timecode_offsets_use_running_sums(self):
        """B2: sum(sizes[first:target]) per wanted sample was O(targets x chunk)."""
        samples = 40_000
        tmcd = box(b"tmcd", bytes(6) + struct.pack(">H", 1) + bytes(4) + struct.pack(">III", 0, 30000, 1001)
                   + bytes([30, 0]))
        stts = full(b"stts", struct.pack(">III", 1, samples, 1001))
        stsc = full(b"stsc", struct.pack(">IIII", 1, 1, samples, 1))
        stsz = full(b"stsz", struct.pack(">II", 0, samples) + b"".join(
            struct.pack(">I", 4 + (i % 3)) for i in range(samples)))
        mdat_payload = bytes(5 * samples)
        moov_body = MVHD + _track(b"tmcd", stts + stsc + stsz + full(b"stco", struct.pack(">II", 1, 0)), entry=tmcd)
        head = FTYP + box(b"moov", moov_body)
        # Point the single chunk at the mdat payload.
        offset = len(head) + 8
        moov_body = MVHD + _track(b"tmcd", stts + stsc + stsz + full(b"stco", struct.pack(">II", 1, offset)),
                                  entry=tmcd)
        path = _write(FTYP + box(b"moov", moov_body) + box(b"mdat", mdat_payload))
        try:
            structure = cc.read_structure(path)
            track = structure.tracks_of("tmcd")[0]
            report, elapsed, _ = _measured(lambda: cc._tmcd_report(path, track))
        finally:
            os.unlink(path)
        assert elapsed < 1.5, elapsed
        assert report[0] is not None

    def test_many_tiny_boxes_are_capped(self):
        """B3: 4 MiB of 8-byte boxes became 524k Box objects (131 MiB, 7.5 s)."""
        filler = box(b"free") * (2 * 1024 * 1024 // 8)
        path = _write(FTYP + box(b"moov", MVHD + box(b"udta", filler)))
        try:
            structure, elapsed, peak = _measured(lambda: cc.read_structure(path))
        finally:
            os.unlink(path)
        assert elapsed < 3.0 and peak < 48 * 1024 * 1024, (elapsed, peak)
        assert structure.moov_unread and "boxes" in structure.moov_unread, structure.moov_unread

    def test_nested_payloads_are_not_copied_per_level(self):
        """B3: each nesting level copied its payload (16 MiB nested 15 deep peaked at 272 MiB)."""
        inner = box(b"free", bytes(4 * 1024 * 1024))
        for kind in [b"udta", b"trak", b"mdia", b"minf", b"stbl"] * 3:
            inner = box(kind, inner)
        path = _write(FTYP + box(b"moov", MVHD + inner))
        try:
            _, _, peak = _measured(lambda: cc.read_structure(path))
        finally:
            os.unlink(path)
        assert peak < 3 * 4 * 1024 * 1024, peak

    def test_large_sample_size_table_stays_compact(self):
        """B3: a 2M-entry stsz cost 209 MiB as Python tuples plus a prefix tuple."""
        count = 1_000_000
        stsz = full(b"stsz", struct.pack(">II", 0, count) + struct.pack(f">{count}I", *([7] * count)))
        stsc = full(b"stsc", struct.pack(">IIII", 1, 1, count, 1))
        stco = full(b"stco", struct.pack(">II", 1, 64))
        stts = full(b"stts", struct.pack(">III", 1, count, 1))
        path = _write(FTYP + box(b"moov", MVHD + _track(b"soun", stts + stsc + stsz + stco)))
        try:
            structure, _, peak = _measured(lambda: cc.read_structure(path))
        finally:
            os.unlink(path)
        assert peak < 3 * 4 * count, peak
        # 1M x 7 bytes from offset 64 runs past the ~4 MB file: located, not raised.
        assert 0 < structure.tracks[0].first_missing_sample < count

    def test_size_zero_below_top_level_is_damage(self):
        """B13: ISO/IEC 14496-12 4.2 allows size 0 only for the last top-level box."""
        trak = struct.pack(">I4s", 0, b"trak") + _tkhd()
        path = _write(FTYP + box(b"moov", MVHD + trak))
        try:
            structure = cc.read_structure(path)
        finally:
            os.unlink(path)
        assert any("size 0" in d for d in structure.damage), structure.damage


# ---------------------------------------------------------------------------
# Property-based fuzzing
# ---------------------------------------------------------------------------

_CONTAINERS = [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"sv3d", b"proj", b"dinf",
               b"tref", b"gmhd", b"mvex"]
_TABLES = [b"stts", b"stsc", b"stsz", b"stco", b"co64", b"ctts", b"stss", b"elst"]
_LEAVES = [b"stsd", b"mdhd", b"hdlr", b"tkhd", b"mvhd", b"uuid", b"equi", b"prhd", b"st3d", b"SA3D", b"free",
           b"mdat", b"avc1", b"mp4a", b"tmcd", b"pasp", b"moof", b"wave", b"ftyp"]
_COUNTS = st.one_of(st.integers(0, 40), st.sampled_from([0x7FFFFFFF, 0xFFFFFFFF, 1 << 24]),
                    st.integers(0, 0xFFFFFFFF))


@st.composite
def _payload(draw, kind: bytes):
    if kind in _TABLES:
        version = draw(st.sampled_from([0, 1]))
        head = bytes([version, 0, 0, 0])
        if kind == b"stsz":
            head += struct.pack(">I", draw(st.sampled_from([0, 0, 1, 0xFFFFFFFF])))
        return head + struct.pack(">I", draw(_COUNTS)) + draw(st.binary(max_size=240))
    if kind == b"hdlr":
        return bytes(8) + draw(st.sampled_from([b"vide", b"soun", b"tmcd", b"text"])) + draw(st.binary(max_size=16))
    if kind == b"stsd":
        entries = draw(st.lists(_box(depth=5), max_size=2))
        return bytes(4) + struct.pack(">I", draw(_COUNTS)) + b"".join(entries)
    return draw(st.binary(max_size=96))


@st.composite
def _box(draw, depth: int = 0):
    kind = draw(st.sampled_from(_CONTAINERS + _TABLES + _LEAVES))
    if kind in _CONTAINERS and depth < 7 and draw(st.booleans()):
        payload = b"".join(draw(st.lists(_box(depth=depth + 1), max_size=4)))
    else:
        payload = draw(_payload(kind))
    mode = draw(st.sampled_from(["exact"] * 8 + ["zero", "large", "large_huge", "large_small",
                                                  "short", "long", "random"]))
    size = 8 + len(payload)
    if mode == "zero":
        return struct.pack(">I4s", 0, kind) + payload
    if mode == "large":
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    if mode == "large_huge":
        return struct.pack(">I4sQ", 1, kind, draw(st.integers(1 << 32, (1 << 64) - 1))) + payload
    if mode == "large_small":
        return struct.pack(">I4sQ", 1, kind, draw(st.integers(0, 15))) + payload
    if mode == "short":
        return struct.pack(">I4s", draw(st.integers(0, 7)), kind) + payload
    if mode == "long":
        return struct.pack(">I4s", size + draw(st.integers(1, 1 << 20)), kind) + payload
    if mode == "random":
        return struct.pack(">I4s", draw(st.integers(0, 0xFFFFFFFF)), kind) + payload
    return struct.pack(">I4s", size, kind) + payload


@st.composite
def _sized(draw, kind: bytes, payload: bytes):
    """A box whose size field is usually right and sometimes hostile."""
    mode = draw(st.sampled_from(["exact"] * 40 + ["zero", "large", "large_huge", "short", "long"]))
    if mode == "zero":
        return struct.pack(">I4s", 0, kind) + payload
    if mode == "large":
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    if mode == "large_huge":
        return struct.pack(">I4sQ", 1, kind, draw(st.integers(1 << 32, (1 << 64) - 1))) + payload
    if mode == "short":
        return struct.pack(">I4s", draw(st.integers(0, 7)), kind) + payload
    if mode == "long":
        return struct.pack(">I4s", 8 + len(payload) + draw(st.integers(1, 1 << 20)), kind) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _u32s(values):
    return b"".join(struct.pack(">I", v & 0xFFFFFFFF) for v in values)


_SMALL = st.integers(0, 64)
_ANY32 = st.one_of(_SMALL, st.integers(0, 0xFFFFFFFF), st.sampled_from([0xFFFFFFFF, 0x7FFFFFFF]))


@st.composite
def _table(draw, kind: bytes):
    """A sample table whose declared count may disagree with its entries."""
    width = {b"stts": 2, b"ctts": 2, b"stsc": 3, b"stsz": 1, b"stco": 1, b"co64": 2, b"stss": 1, b"elst": 3}[kind]
    entries = draw(st.lists(_ANY32, max_size=width * draw(st.integers(0, 60))))
    count = draw(st.one_of(st.just(len(entries) // width), _ANY32))
    version = draw(st.sampled_from([0, 0, 1]))
    head = bytes([version, 0, 0, 0])
    if kind == b"stsz":
        head += struct.pack(">I", draw(st.sampled_from([0, 0, 1, 4, 0xFFFFFFFF])))
    return draw(_sized(kind, head + struct.pack(">I", count & 0xFFFFFFFF) + _u32s(entries)))


@st.composite
def _sample_entry(draw, handler: bytes):
    kind = {b"vide": b"avc1", b"soun": b"mp4a", b"tmcd": b"tmcd"}.get(handler, b"text")
    fixed = {b"avc1": 78, b"mp4a": draw(st.sampled_from([28, 44, 64])), b"tmcd": 26}.get(kind, 8)
    head = bytearray(draw(st.binary(min_size=fixed, max_size=fixed)))
    if kind == b"mp4a" and len(head) >= 10:
        head[8:10] = struct.pack(">H", {28: 0, 44: 1, 64: 2}[fixed])
    children = []
    for child in draw(st.lists(st.sampled_from([b"st3d", b"sv3d", b"SA3D", b"pasp", b"esds", b"wave"]),
                               max_size=3)):
        if child == b"sv3d":
            body = draw(_sized(b"proj", draw(_sized(b"prhd", bytes(4) + draw(st.binary(max_size=12))))
                               + draw(_sized(b"equi", bytes(4) + draw(st.binary(max_size=16))))))
        else:
            body = draw(st.binary(max_size=40))
        children.append(draw(_sized(child, body)))
    return draw(_sized(kind, bytes(head) + b"".join(children)))


@st.composite
def _trak(draw):
    handler = draw(st.sampled_from([b"vide", b"soun", b"tmcd"]))
    tables = b"".join(draw(_table(k)) for k in draw(st.lists(st.sampled_from(_TABLES[:-1]), max_size=7)))
    stsd = draw(_sized(b"stsd", bytes(4) + struct.pack(">I", draw(st.integers(0, 3)))
                       + b"".join(draw(st.lists(_sample_entry(handler), max_size=2)))))
    stbl = draw(_sized(b"stbl", stsd + tables))
    mdhd = draw(_sized(b"mdhd", bytes([draw(st.sampled_from([0, 1])), 0, 0, 0]) + draw(st.binary(max_size=32))))
    mdia = draw(_sized(b"mdia", mdhd + _hdlr(handler) + draw(_sized(b"minf", stbl))))
    parts = [_tkhd(draw(st.integers(0, 4))), mdia]
    if draw(st.booleans()):
        parts.insert(1, draw(_sized(b"edts", draw(_table(b"elst")))))
    if draw(st.booleans()):
        parts.append(draw(_sized(b"uuid", cc.SPHERICAL_V1_UUID + draw(st.binary(max_size=64)))))
    return draw(_sized(b"trak", b"".join(parts)))


@st.composite
def _structured_file(draw):
    moov = draw(_sized(b"moov", MVHD + b"".join(draw(st.lists(_trak(), min_size=1, max_size=3)))))
    parts = [FTYP, moov, box(b"mdat", draw(st.binary(max_size=256)))]
    if draw(st.booleans()):
        parts[1], parts[2] = parts[2], parts[1]
    data = b"".join(parts)
    cut = draw(st.one_of(st.none(), st.none(), st.integers(0, len(data))))
    return data if cut is None else data[:cut]


@st.composite
def _file(draw):
    parts = [FTYP] if draw(st.booleans()) else []
    parts += draw(st.lists(_box(), min_size=1, max_size=4))
    if draw(st.booleans()):
        parts.insert(draw(st.integers(0, len(parts))), box(b"moov", b"".join(draw(st.lists(_box(depth=1),
                                                                                             max_size=4)))))
    data = b"".join(parts)
    cut = draw(st.one_of(st.none(), st.integers(0, len(data))))
    return data if cut is None else data[:cut]


class _Rows:
    def __init__(self):
        self.checks, self.metadata = [], {}

    def _add(self, status):
        return lambda name, detail="", confidence=100: self.checks.append((name, status, detail))

    def __getattr__(self, attr):
        status = {"add_pass": "PASS", "add_warn": "WARN", "add_fail": "FAIL", "add_skip": "SKIP"}.get(attr)
        if status:
            return self._add(status)
        raise AttributeError(attr)

    def add_not_verified(self, name, reason, by_choice=False):
        self.checks.append((name, "WARN", "Not verified: " + reason))

    def set_metadata(self, key, value):
        self.metadata[key] = value


def _probe_for(structure):
    streams = []
    for index, track in enumerate(structure.tracks):
        kind = {"vide": "video", "soun": "audio"}.get(track.handler, "data")
        stream = {"index": index, "codec_type": kind, "id": hex(track.track_id), "start_time": "0.0",
                  "codec_name": {"video": "mpeg2video", "audio": "aac"}.get(kind, "none"),
                  "codec_tag_string": track.handler, "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
                  "width": 320, "height": 160, "channels": 4, "sample_rate": "48000"}
        streams.append(stream)
    return {"format": {"format_name": "mov,mp4"}, "streams": streams}


def _exercise(path: str) -> None:
    structure = cc.read_structure(path)
    cfg = dict(DEFAULT_CONFIG, spatial_audio="required", required_projection="equirectangular",
               disabled_checks=[])
    probe = _probe_for(structure)
    rows = _Rows()
    cc.check_container_structure(structure, rows, cfg)
    cc.check_edit_lists(structure, rows, cfg)
    cc.check_av_sync_offset(structure, probe, rows, cfg)
    cc.check_timecode(path, structure, probe, rows, cfg)
    cc.check_spatial_audio(structure, probe, rows, cfg, "equirectangular")
    video = next((s for s in probe["streams"] if s["codec_type"] == "video"), {"width": 320, "height": 160})
    cc.xr_metadata_findings(video, "equirectangular", None, structure, cfg)
    for track in structure.tracks:
        cc.sample_offsets(track, [0, 1, 2, track.sample_count - 1, track.sample_count + 5])
        track.sample_time(10 ** 9)
        track.min_composition_time()
        track.frame_duration()


@settings(max_examples=200, deadline=2000, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(st.one_of(_structured_file(), _structured_file(), _file()))
def test_box_reader_never_raises_hangs_or_balloons(data):
    path = _write(data)
    try:
        _, elapsed, peak = _measured(lambda: _exercise(path))
    finally:
        os.unlink(path)
    # Work and memory scale with the bytes present, never with declared counts.
    assert elapsed < 1.5, elapsed
    assert peak < 16 * 1024 * 1024 + 64 * len(data), (peak, len(data))
