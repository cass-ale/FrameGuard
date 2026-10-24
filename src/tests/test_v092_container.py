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


# ---------------------------------------------------------------------------
# 2. A/V Sync Offset: unsignalled AAC priming is judged over its whole range (B5)
# ---------------------------------------------------------------------------

def _moov_last(media):
    return media.get("moovlast.mp4", lambda p: make_clip(p, duration=2) or p)


def _video_delayed(media, milliseconds):
    """Sound ``milliseconds`` early: an empty edit delays the picture (the
    audio track keeps its encoder-priming edit)."""
    from container_fixtures import set_edit_list

    def build(path):
        with open(_moov_last(media), "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
        structure = cc.read_structure(path)
        edit = structure.tracks_of("vide")[0].edits[0]
        lead = round(milliseconds * structure.movie_timescale / 1000)
        set_edit_list(path, b"vide", [(lead, -1, 1), (edit.segment_duration, edit.media_time, 1)])
        return path
    return media.get(f"video_late_{milliseconds}.mp4", build)


def _sync_probe(audio_start, profile="LC", padding=0, video_start=0.0):
    return {"format": {"format_name": "mpegts"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": str(video_start)},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "profile": profile,
                 "sample_rate": "48000", "channels": 2, "initial_padding": padding,
                 "start_time": str(audio_start)}]}


class _Rows:
    def __init__(self):
        self.checks, self.metadata = [], {}

    def add_pass(self, name, detail="", confidence=100):
        self.checks.append((name, "PASS", detail, confidence))

    def add_warn(self, name, detail="", confidence=100):
        self.checks.append((name, "WARN", detail, confidence))

    def add_fail(self, name, detail="", confidence=100):
        self.checks.append((name, "FAIL", detail, confidence))

    def add_skip(self, name, detail="", confidence=100):
        self.checks.append((name, "SKIP", detail, confidence))

    def add_not_verified(self, name, reason, by_choice=False):
        self.checks.append((name, "WARN", "Not verified: " + reason, 100))

    def set_metadata(self, key, value):
        self.metadata[key] = value


def _sync_row(probe, structure=None, **cfg):
    rows = _Rows()
    cc.check_av_sync_offset(structure, probe, rows, _cfg(**cfg))
    return rows.checks[0], rows.metadata


class TestAacPrimingRange:
    def test_transport_stream_sound_early_beyond_its_priming_is_not_a_pass(self, media, tmp_path):
        """FFmpeg's TS audio starts one AAC frame (21 ms) early; 20 ms more
        lead used to be absorbed by the 44 ms 'most favourable' window."""
        early = tmp_path / "early.ts"
        ffmpeg("-i", _clean(media), "-itsoffset", "-0.020", "-i", _clean(media), "-map", "0:v",
               "-map", "1:a", "-c", "copy", "-f", "mpegts", str(early))
        check = _row(_scan(early, accepted_containers=[".ts"]), "A/V Sync Offset")
        assert check[1] == "WARN", check
        assert "priming" in check[2] and "not signalled" in check[2], check
        known = _row(_scan(early, accepted_containers=[".ts"], aac_priming=1024), "A/V Sync Offset")
        assert known[1] == "WARN" and "20 ms before" in known[2], known

    def test_known_encoder_priming_makes_the_offset_exact(self):
        assert _sync_row(_sync_probe(-1024 / 48000), aac_priming=1024)[0][1] == "PASS"
        assert _sync_row(_sync_probe(-1024 / 48000), aac_priming=2112)[0][1] == "WARN"
        he, _ = _sync_row(_sync_probe(-2 * 2112 / 48000, profile="HE-AAC"), aac_priming=2112)
        assert he[1] == "PASS", he

    def test_priming_setting_follows_the_preset_pattern(self):
        from frameguard import presets
        from frameguard.report import spec_rows
        assert DEFAULT_CONFIG["aac_priming"] == "unknown"
        assert presets.validate_preset_config({"aac_priming": "1024"}) == {"aac_priming": 1024}
        with pytest.raises(presets.PresetValidationError):
            presets.validate_preset_config({"aac_priming": 999})
        assert presets.normalize_preset_config({"av_offset_warn_ms": 5}) == {"av_offset_warn_ms": 5}
        rows = dict(spec_rows(dict(DEFAULT_CONFIG)))
        assert "0-2112 samples" in rows["A/V start offset"], rows

    def test_he_aac_window_no_longer_hides_88_ms(self):
        check, meta = _sync_row(_sync_probe(-0.088, profile="HE-AAC"))
        assert check[1] == "WARN" and "88" in check[2], check
        assert meta["av_start_offset_range_ms"] == [-88.0, 0.0], meta

    def test_whole_range_beyond_r37_fails_and_whole_range_late_fails(self):
        assert _sync_row(_sync_probe(-0.090))[0][1] == "FAIL"     # -90 .. -46 ms: all beyond 40 early
        assert _sync_row(_sync_probe(0.070))[0][1] == "FAIL"      # priming only makes late later
        assert _sync_row(_sync_probe(-0.060))[0][1] == "WARN"     # -60 .. -16 ms straddles the limit

    def test_priming_is_read_from_the_audio_edit_list(self, media, monkeypatch):
        """ffprobe 6/7 report initial_padding=0 for an edit-listed AAC MP4;
        the audio track's own edit (media_time = priming) signals it."""
        from frameguard import core
        real = core.run_ffprobe

        def no_padding(*args, **kwargs):
            data = real(*args, **kwargs)
            for stream in data["streams"]:
                stream.pop("initial_padding", None)
            return data
        monkeypatch.setattr(core, "run_ffprobe", no_padding)
        check = _row(_scan(_video_delayed(media, 35)), "A/V Sync Offset")
        assert check[1] == "WARN" and "35 ms before" in check[2], check
        assert _row(_scan(_clean(media)), "A/V Sync Offset")[1] == "PASS"

    def test_a_trims_audio_edit_also_signals_the_priming(self, media, monkeypatch):
        """FFmpeg 8 reports initial_padding=0 for a stream-copy trim; the audio
        edit that hides the trimmed lead-in hides the priming with it."""
        from frameguard import core
        from media_fixtures import stream_copy_trim
        real = core.run_ffprobe

        def no_padding(*args, **kwargs):
            data = real(*args, **kwargs)
            for stream in data["streams"]:
                stream.pop("initial_padding", None)
            return data
        source = media.get("trim_src.mp4", lambda p: make_clip(p, duration=6) or p)
        trimmed = media.get("trimmed.mp4", lambda p: stream_copy_trim(source, p, 1.37, 3))
        monkeypatch.setattr(core, "run_ffprobe", no_padding)
        check = _row(_scan(trimmed), "A/V Sync Offset")
        assert "not signalled" not in check[2], check

    def test_signalled_priming_is_exact(self):
        check, _ = _sync_row(_sync_probe(0.0, padding=1024))
        assert check[1] == "PASS", check
