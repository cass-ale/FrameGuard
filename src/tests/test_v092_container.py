"""0.9.2 container / XR / preset / verdict fixes from the independent 0.9.1 review
(r091_container/REPORT.md B1-B19, REVIEW.md section 3), run against the REAL FFmpeg.

Each class is one defect; every test failed on release/0.9.2 before its fix.
"""

import pytest

from frameguard import container_checks as cc
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import MediaLibrary, ffmpeg, make_clip, needs_ffmpeg

pytestmark = needs_ffmpeg


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=240, target_fps=30.0, min_bitrate_mbps=0,
               loudness_mode="off", accepted_containers=[".mp4", ".mov", ".mkv", ".ts"],
               accepted_codecs=["h264", "hevc", "mpeg2video", "prores"])
    cfg.update(overrides)
    return cfg


def _scan(path, quick=True, strict=False, **overrides):
    result = run_qa_checks(str(path), cfg=_cfg(**overrides), quick=quick, strict=strict)
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _rows(result, name):
    return [check for check in result.checks if check[0] == name]


def _not_verified(check):
    return check[1] == "WARN" and str(check[2]).startswith("Not verified")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg092c")))


def _clean(media):
    return media.get("clean.mp4", lambda p: make_clip(p, duration=2, extra=("-movflags", "+faststart"))
                     or p)


# ---------------------------------------------------------------------------
# 1. Container Format: an unrecognised or disguised container never passes (B4)
# ---------------------------------------------------------------------------

def _prefixed(src, dst, junk: bytes):
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(junk + a.read())
    return str(dst)


def _ts_packets(packet: int, junk: int, count: int = 12) -> bytes:
    """Synthetic transport stream: ``junk`` stray bytes, then ``count`` packets
    whose sync byte sits where ISO/IEC 13818-1 (188), BDAV M2TS (4-byte
    timestamp + 188) or 204-byte (188 + 16 Reed-Solomon) framing puts it."""
    body = bytearray()
    for n in range(count):
        pkt = bytearray((n * 7 + k * 13) & 0xFF for k in range(packet))
        sync_at = 4 if packet == 192 else 0
        pkt[sync_at] = 0x47
        # Keep 0x47 out of the payload so only the real sync positions match.
        for k in range(packet):
            if k != sync_at and pkt[k] == 0x47:
                pkt[k] = 0x46
        body += pkt
    return bytes(range(1, junk + 1)) + bytes(body)


class TestContainerSniff:
    def test_junk_prefixed_ts_named_mp4_fails_in_default_and_strict(self, media, tmp_path):
        ts = tmp_path / "src.ts"
        ffmpeg("-i", _clean(media), "-c", "copy", "-f", "mpegts", str(ts))
        disguised = _prefixed(ts, tmp_path / "junk_ts.mp4", b"\x00\x01\x02\x03")
        for strict in (False, True):
            result = _scan(disguised, strict=strict)
            check = _row(result, "Container Format")
            assert check[1] == "FAIL" and "MPEG-TS" in check[2], check
            assert result.status == "MISMATCH", [c[:3] for c in result.checks]

    @pytest.mark.parametrize("packet,junk", [(188, 0), (188, 5), (188, 187), (192, 0), (192, 9),
                                             (204, 0), (204, 33)])
    def test_transport_stream_resync(self, tmp_path, packet, junk):
        path = tmp_path / f"ts_{packet}_{junk}.bin"
        path.write_bytes(_ts_packets(packet, junk))
        family, detail = cc.sniff_container(str(path))
        assert family == "MPEG-TS" and str(packet) in detail, (family, detail)
        assert ("junk" in detail) == (junk > 0), detail

    def test_flv_and_program_stream_named_mp4_fail(self, media, tmp_path):
        flv = tmp_path / "flv.mp4"
        ffmpeg("-i", _clean(media), "-c:v", "copy", "-c:a", "aac", "-f", "flv", str(flv))
        check = _row(_scan(flv), "Container Format")
        assert check[1] == "FAIL" and "FLV" in check[2], check
        ps = tmp_path / "ps.mp4"
        ffmpeg("-i", _clean(media), "-c:v", "mpeg2video", "-c:a", "mp2", "-f", "vob", str(ps))
        check = _row(_scan(ps, accepted_codecs=["mpeg2video"]), "Container Format")
        assert check[1] == "FAIL" and "MPEG-PS" in check[2], check

    def test_unrecognised_signature_is_judged_by_ffprobe_format(self, media, tmp_path):
        """A raw H.264 elementary stream has no container signature; ffprobe
        names it, and that must contradict the .mp4 name."""
        raw = tmp_path / "raw.mp4"
        ffmpeg("-i", _clean(media), "-an", "-c:v", "copy", "-f", "h264", str(raw))
        assert cc.sniff_container(str(raw))[0] == "unknown"
        check = _row(_scan(raw), "Container Format")
        assert check[1] == "FAIL" and "h264" in check[2].lower(), check

    def test_unreadable_unknown_file_is_never_a_container_pass(self, tmp_path):
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(bytes(range(256)) * 64)
        result = _scan(junk)
        rows = _rows(result, "Container Format")
        assert rows and all(r[1] != "PASS" for r in rows), rows
        assert result.status == "MISMATCH"

    def test_real_containers_still_pass(self, media, tmp_path):
        mkv = tmp_path / "real.mkv"
        ts = tmp_path / "real.ts"
        ffmpeg("-i", _clean(media), "-c", "copy", str(mkv))
        ffmpeg("-i", _clean(media), "-c", "copy", str(ts))
        for path in (_clean(media), mkv, ts):
            check = _row(_scan(path), "Container Format")
            assert check[1] == "PASS", (path, check)
