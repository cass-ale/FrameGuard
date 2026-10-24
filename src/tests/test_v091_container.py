"""0.9.1 container / timing / codec-configuration checks, run against the REAL FFmpeg.

Every detector has a positive clip, a clean negative and, where it has a
threshold, clips either side of it. Clips are 320x240 and 2-3 s so the whole
file runs well under a minute.
"""

import struct
from pathlib import Path

import pytest

from frameguard import container_checks as cc
from frameguard.core import DEFAULT_CONFIG, ESSENTIAL_CHECKS, run_ffprobe, run_qa_checks
from media_fixtures import MediaLibrary, ffmpeg, make_clip, needs_ffmpeg, stream_copy_trim, truncate
from container_fixtures import movie_timescale, set_edit_list

pytestmark = needs_ffmpeg


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=240, target_fps=30.0, min_bitrate_mbps=0,
               loudness_mode="off", accepted_containers=[".mp4", ".mov", ".mkv"],
               accepted_codecs=["h264", "hevc", "mpeg2video", "prores"])
    cfg.update(overrides)
    return cfg


def _scan(path, quick=True, **overrides):
    result = run_qa_checks(str(path), cfg=_cfg(**overrides), quick=quick)
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _has_row(result, name):
    return any(check[0] == name for check in result.checks)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg091c")))


def _clean(media):
    return media.get("clean.mp4", lambda p: make_clip(p, duration=3, extra=("-movflags", "+faststart"))
                     or p)


def _moov_last(media):
    return media.get("moovlast.mp4", lambda p: make_clip(p, duration=3) or p)


def _offset_clip(media, milliseconds, handler=b"soun"):
    """Delay one track by an empty edit of ``milliseconds`` (precise, unlike -itsoffset,
    which interacts with the AAC priming edit)."""
    def build(path):
        with open(_moov_last(media), "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
        structure = cc.read_structure(path)
        track = structure.tracks_of("vide" if handler == b"vide" else "soun")[0]
        edit = track.edits[0]
        lead = round(milliseconds * structure.movie_timescale / 1000)
        set_edit_list(path, handler, [(lead, -1, 1), (edit.segment_duration, edit.media_time, 1)])
        return path
    kind = handler.decode()
    return media.get(f"offset_{kind}_{milliseconds}.mp4", build)


# ---------------------------------------------------------------------------
# Container Format: real signature vs extension
# ---------------------------------------------------------------------------

class TestContainerSignature:
    def test_matroska_named_mp4_fails(self, media, tmp_path):
        target = tmp_path / "renamed.mp4"
        ffmpeg("-i", _clean(media), "-c", "copy", "-f", "matroska", str(target))
        check = _row(_scan(target), "Container Format")
        assert check[1] == "FAIL" and "Matroska" in check[2], check

    def test_mpegts_named_mp4_fails(self, media, tmp_path):
        target = tmp_path / "renamed_ts.mp4"
        ffmpeg("-i", _clean(media), "-c", "copy", "-f", "mpegts", str(target))
        check = _row(_scan(target), "Container Format")
        assert check[1] == "FAIL" and "MPEG-TS" in check[2], check

    def test_real_mp4_and_mkv_pass(self, media, tmp_path):
        mkv = tmp_path / "real.mkv"
        ffmpeg("-i", _clean(media), "-c", "copy", str(mkv))
        assert _row(_scan(_clean(media)), "Container Format")[1] == "PASS"
        assert _row(_scan(mkv), "Container Format")[1] == "PASS"
        assert _row(_scan(mkv), "Container Structure")[1] == "SKIP"

    def test_unrecognised_signature_is_left_to_ffprobe(self, tmp_path):
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(bytes(range(256)) * 64)
        assert cc.extension_mismatch(str(junk), ".mp4") is None


# ---------------------------------------------------------------------------
# Container Structure: truncation, fast-start, damage (advisory)
# ---------------------------------------------------------------------------

class TestContainerStructure:
    def test_clean_faststart_passes(self, media):
        check = _row(_scan(_clean(media)), "Container Structure")
        assert check[1] == "PASS" and "fast-start" in check[2], check

    def test_truncated_file_says_where_it_ends(self, media, tmp_path):
        cut = truncate(_clean(media), tmp_path / "cut.mp4", 0.6)
        result = _scan(cut)
        check = _row(result, "Container Structure")
        assert check[1] == "WARN", check
        assert "truncated" in check[2] and "beyond the end of the file" in check[2], check
        assert "Container Structure" not in ESSENTIAL_CHECKS

    def test_truncated_file_still_fails_file_integrity(self, media, tmp_path):
        cut = truncate(_clean(media), tmp_path / "cut_full.mp4", 0.6)
        result = _scan(cut, quick=False)
        assert _row(result, "File Integrity")[1] == "FAIL"
        assert result.status == "MISMATCH"

    def test_moov_at_end_is_a_note_unless_streaming_spec(self, media):
        relaxed = _row(_scan(_moov_last(media)), "Container Structure")
        assert relaxed[1] == "PASS" and "not fast-start" in relaxed[2], relaxed
        strict = _row(_scan(_moov_last(media), require_faststart=True), "Container Structure")
        assert strict[1] == "WARN" and "fast-start" in strict[2], strict

    def test_garbage_after_a_box_is_reported_not_raised(self, media, tmp_path):
        with open(_clean(media), "rb") as handle:
            data = handle.read()
        damaged = tmp_path / "garbage.mp4"
        damaged.write_bytes(data + b"\xff\xfe\x00\x01" * 8)
        structure = cc.read_structure(str(damaged))
        assert structure.damage and "not a valid box header" in structure.damage[0]

    def test_box_larger_than_the_file_is_bounded(self, tmp_path):
        path = tmp_path / "huge.mp4"
        path.write_bytes(struct.pack(">I4s4sI", 24, b"ftyp", b"isom", 0) + b"\0" * 8
                         + struct.pack(">I4sQ", 1, b"mdat", 1 << 40))
        structure = cc.read_structure(str(path))
        assert not structure.error and "truncated" in structure.damage[0]

    def test_fragmented_mp4_is_noted(self, media, tmp_path):
        frag = tmp_path / "frag.mp4"
        ffmpeg("-i", _clean(media), "-c", "copy", "-movflags", "frag_keyframe+empty_moov", str(frag))
        check = _row(_scan(frag), "Container Structure")
        assert "fragmented" in check[2], check


# ---------------------------------------------------------------------------
# Edit List (advisory) and A/V Sync Offset (essential)
# ---------------------------------------------------------------------------

class TestEditList:
    def test_encoder_edits_are_normal(self, media):
        check = _row(_scan(_clean(media)), "Edit List")
        assert check[1] == "PASS" and "Normal" in check[2], check

    def test_lossless_trim_is_allowed_by_default(self, media, tmp_path):
        source = media.get("long.mp4", lambda p: make_clip(p, duration=6,
                           extra=("-x264-params", "keyint=60:min-keyint=60")) or p)
        trimmed = stream_copy_trim(source, tmp_path / "trim.mp4", 1.5, 3)
        check = _row(_scan(trimmed), "Edit List")
        assert check[1] == "PASS" and "lossless trim" in check[2], check
        strict = _row(_scan(trimmed, allow_trim_edit_lists=False), "Edit List")
        assert strict[1] == "WARN" and "hides the first" in strict[2], strict

    def test_splice_is_reported(self, media, tmp_path):
        path = tmp_path / "splice.mp4"
        path.write_bytes(open(_moov_last(media), "rb").read())
        ts = movie_timescale(path)
        set_edit_list(path, b"vide", [(ts, 0, 1), (ts, 15360 * 2, 1)])
        check = _row(_scan(path), "Edit List")
        assert check[1] == "WARN" and "2 media segments" in check[2], check

    def test_empty_audio_edit_is_reported(self, media):
        check = _row(_scan(_offset_clip(media, 200)), "Edit List")
        assert check[1] == "WARN" and "blank" in check[2], check


class TestAVSyncOffset:
    def test_clean_starts_together(self, media):
        check = _row(_scan(_clean(media)), "A/V Sync Offset")
        assert check[1] == "PASS", check

    def test_offsets_either_side_of_the_limits(self, media):
        late = {3: "PASS", 8: "WARN", 55: "WARN", 70: "FAIL", 200: "FAIL"}
        for ms, expected in late.items():
            check = _row(_scan(_offset_clip(media, ms)), "A/V Sync Offset")
            assert check[1] == expected, (ms, check)
        early = {3: "PASS", 35: "WARN", 45: "FAIL"}  # EBU R37: sound may lead by 40 ms
        for ms, expected in early.items():
            check = _row(_scan(_offset_clip(media, ms, b"vide")), "A/V Sync Offset")
            assert check[1] == expected, (ms, check)
        late_text = _row(_scan(_offset_clip(media, 200)), "A/V Sync Offset")[2]
        assert "after the picture" in late_text and "edit list" in late_text, late_text

    def test_warn_tier_is_low_confidence_and_fail_is_essential(self, media):
        assert _row(_scan(_offset_clip(media, 8)), "A/V Sync Offset")[3] < 70
        assert _scan(_offset_clip(media, 200)).status == "MISMATCH"

    def test_limits_are_configurable(self, media):
        check = _row(_scan(_offset_clip(media, 200), av_offset_max_late_ms=500), "A/V Sync Offset")
        assert check[1] == "WARN", check

    def test_mpegts_aac_priming_is_not_an_offset(self, media, tmp_path):
        """MPEG-TS cannot signal priming: with the encoder known (FFmpeg,
        1024 samples) the start is exact; unknown, the whole 0-2112 sample
        range is judged and the ±5 ms tolerance cannot be confirmed (0.9.2, B5)."""
        ts = tmp_path / "prime.ts"
        ffmpeg("-i", _clean(media), "-c:v", "copy", "-c:a", "aac", str(ts))
        check = _row(_scan(ts, accepted_containers=[".ts"], aac_priming=1024), "A/V Sync Offset")
        assert check[1] == "PASS", check
        # Encoder unknown: FFmpeg's TS lead of exactly 1024 samples is read as its
        # priming (ISO/IEC 13818-1 PTS semantics; 0.9.2 round 2, bug 6).
        unknown = _row(_scan(ts, accepted_containers=[".ts"]), "A/V Sync Offset")
        assert unknown[1] == "PASS" and "inferred" in unknown[2], unknown

    def _bframe_encode(self, path, *extra, audio=("-c:a", "aac")):
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=3", "-f", "lavfi",
               "-i", "sine=f=440:sample_rate=48000:d=3", "-c:v", "libx264", "-preset", "medium",
               "-bf", "3", "-pix_fmt", "yuv420p", *audio, *extra, str(path))
        return str(path)

    def test_bframes_without_edit_list_are_judged_with_priming(self, tmp_path):
        """No edit list: the picture really starts 2 frames (80 ms) after the track.
        PCM has no priming, so that is the programme offset; unsignalled AAC
        priming (1024-2112 samples) plays first, so AAC is judged at -36 ms."""
        pcm = self._bframe_encode(tmp_path / "noedit.mov", "-use_editlist", "0", audio=("-c:a", "pcm_s16le"))
        check = _row(_scan(pcm, target_fps=25.0), "A/V Sync Offset")
        assert check[1] == "FAIL" and "80 ms before" in check[2] and "no edit list" in check[2], check
        aac = self._bframe_encode(tmp_path / "noedit.mp4", "-use_editlist", "0")
        check = _row(_scan(aac, target_fps=25.0), "A/V Sync Offset")
        # Unknown priming (0-2624 samples): -80 .. -25 ms, no compliant value: WARN.
        assert check[1] == "WARN" and "80 and 25 ms before" in check[2] and "priming" in check[2], check

    def test_ffmpeg_transport_stream_with_bframes_passes(self, tmp_path):
        ts = self._bframe_encode(tmp_path / "bf.ts")
        check = _row(_scan(ts, target_fps=25.0, accepted_containers=[".ts"], aac_priming=1024),
                     "A/V Sync Offset")
        assert check[1] == "PASS", check

    def test_with_edit_list_is_clean(self, tmp_path):
        mp4 = self._bframe_encode(tmp_path / "edit.mp4")
        assert _row(_scan(mp4, target_fps=25.0), "A/V Sync Offset")[1] == "PASS"


class TestTransportStreamEnds:
    """MPEG-TS has no stream durations; ffprobe's tail estimate read a clean
    remux as up to 0.3 s of audio short (release review 0.9.1, finding 1)."""

    @pytest.mark.parametrize("seconds", [6, 10])
    def test_clean_remux_passes_even_in_strict_mode(self, tmp_path, seconds):
        mp4 = tmp_path / f"c{seconds}.mp4"
        make_clip(mp4, duration=seconds, extra=("-movflags", "+faststart"))
        ts = tmp_path / f"c{seconds}.ts"
        ffmpeg("-i", str(mp4), "-c", "copy", str(ts))
        # FFmpeg-encoded AAC: TS cannot signal its 1024-sample priming (0.9.2, B5).
        result = run_qa_checks(str(ts), cfg=_cfg(accepted_containers=[".ts"], aac_priming=1024), strict=True)
        result.finalize()
        assert _row(result, "A/V Duration")[1] == "PASS", _row(result, "A/V Duration")
        assert _row(result, "Frame Count")[1] == "SKIP"
        assert result.status == "PASS", [c[:3] for c in result.checks if c[1] in ("WARN", "FAIL")]

    @pytest.mark.parametrize("short,expected", [(0.3, "WARN"), (1.0, "FAIL"), (8.0, "FAIL")])
    def test_genuinely_short_audio_still_flags(self, tmp_path, short, expected):
        ts = tmp_path / f"short{short}.ts"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=12", "-f", "lavfi",
               "-i", f"sine=f=440:sample_rate=48000:d={12 - short}", "-c:v", "libx264", "-preset", "ultrafast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", str(ts))
        check = _row(_scan(ts, accepted_containers=[".ts"]), "A/V Duration")
        assert check[1] == expected, check

    def test_ends_are_only_measured_for_program_streams(self, media):
        probe = run_ffprobe(_clean(media))
        assert cc.measured_stream_ends(_clean(media), probe) == {}


# ---------------------------------------------------------------------------
# Timecode (advisory)
# ---------------------------------------------------------------------------

def _mpeg2(path, timecode, duration=2):
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=320x240:r=30:d={duration}", "-c:v", "mpeg2video",
           "-g", "15", "-timecode", timecode, "-f", "mpeg2video", str(path))
    return str(path)


class TestTimecode:
    def test_clean_timecode_track_is_continuous(self, media, tmp_path):
        mov = tmp_path / "tc.mov"
        ffmpeg("-i", _clean(media), "-c", "copy", "-timecode", "01:00:00:00", str(mov))
        check = _row(_scan(mov), "Timecode")
        assert check[1] == "PASS" and "01:00:00:00" in check[2], check

    def test_no_timecode_no_row(self, media):
        assert not _has_row(_scan(_clean(media)), "Timecode")

    def test_gop_timecode_jump_is_found(self, tmp_path):
        first = _mpeg2(tmp_path / "a.m2v", "01:00:00:00")
        second = _mpeg2(tmp_path / "b.m2v", "05:00:00:00")
        listing = tmp_path / "list.txt"
        listing.write_text("".join(f"file '{Path(p).as_posix()}'\n" for p in (first, second)))
        mov = tmp_path / "jump.mov"
        ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(mov))
        check = _row(_scan(mov, audio_required=False), "Timecode")
        assert check[1] == "WARN" and "05:00:00:00" in check[2], check

    def test_continuous_gop_timecode_passes(self, tmp_path):
        mov = tmp_path / "gop_ok.mov"
        ffmpeg("-i", _mpeg2(tmp_path / "ok.m2v", "10:00:00:00", 3), "-c", "copy", str(mov))
        check = _row(_scan(mov, audio_required=False), "Timecode")
        assert check[1] == "PASS" and "GOP" in check[2], check

    def test_drop_frame_display(self):
        assert cc.timecode_text(107892, 30, True) == "01:00:00;00"
        assert cc.timecode_text(1800, 30, True) == "00:01:00;02"
        assert cc.timecode_text(17982, 30, True) == "00:10:00;00"
        assert cc._timecode_frames(0, 1, 0, 2, 30, True) == 1800


# ---------------------------------------------------------------------------
# Codec Profile
# ---------------------------------------------------------------------------

class TestCodecProfile:
    @pytest.mark.parametrize("codec,size,fps,level", [
        ("h264", (1920, 1080), 30, 40), ("h264", (1920, 1080), 60, 42),
        ("h264", (3840, 2160), 24000 / 1001, 51), ("h264", (3840, 3840), 30, 60),
        ("hevc", (1920, 1080), 30, 120), ("hevc", (3840, 2160), 30, 150),
        ("hevc", (5760, 2880), 30, 180), ("hevc", (8192, 4096), 60, 183),
    ])
    def test_required_level_tables(self, codec, size, fps, level):
        assert cc.required_level(codec, *size, fps) == level

    def test_clean_clip_passes_with_its_level(self, media):
        check = _row(_scan(_clean(media)), "Codec Profile")
        assert check[1] == "PASS" and "Level" in check[2], check

    def test_underdeclared_level_warns(self, tmp_path):
        path = tmp_path / "l10.mp4"
        make_clip(path, duration=2, extra=("-level:v", "1.0"))
        check = _row(_scan(path), "Codec Profile")
        assert check[1] == "WARN" and "needs Level" in check[2], check

    def test_pix_fmt_and_profile_allowlists(self, tmp_path):
        path = tmp_path / "444.mp4"
        make_clip(path, duration=2, extra=("-pix_fmt", "yuv444p", "-profile:v", "high444"))
        assert _row(_scan(path), "Codec Profile")[1] == "PASS"
        fmt = _row(_scan(path, allowed_pix_fmts=["yuv420p"]), "Codec Profile")
        assert fmt[1] == "FAIL" and "yuv444p" in fmt[2], fmt
        prof = _row(_scan(path, allowed_video_profiles=["high", "main"]), "Codec Profile")
        assert prof[1] == "FAIL" and "profile" in prof[2], prof

    def test_decoder_ceiling(self, media):
        assert _row(_scan(_clean(media), max_frame_pixels=320 * 240), "Codec Profile")[1] == "PASS"
        assert _row(_scan(_clean(media), max_frame_pixels=320 * 239), "Codec Profile")[1] == "FAIL"
        assert _row(_scan(_clean(media), max_pixel_rate=320 * 240 * 29), "Codec Profile")[1] == "FAIL"


# ---------------------------------------------------------------------------
# Track Layout, disabled checks, unknown audio layouts
# ---------------------------------------------------------------------------

class TestTrackLayoutAndSwitches:
    def test_inventory_and_extra_picture_stream(self, media, tmp_path):
        assert _row(_scan(_clean(media)), "Track Layout")[1] == "PASS"
        two = tmp_path / "two_video.mp4"
        ffmpeg("-i", _clean(media), "-i", _clean(media), "-map", "0:v", "-map", "1:v", "-map", "0:a",
               "-c", "copy", str(two))
        check = _row(_scan(two), "Track Layout")
        assert check[1] == "WARN" and "2 picture streams" in check[2], check

    def test_audio_track_limit(self, media, tmp_path):
        two = tmp_path / "two_audio.mp4"
        ffmpeg("-i", _clean(media), "-map", "0:v", "-map", "0:a", "-map", "0:a", "-c", "copy", str(two))
        assert _row(_scan(two), "Track Layout")[1] == "PASS"
        assert _row(_scan(two, max_audio_tracks=1), "Track Layout")[1] == "WARN"

    def test_disabled_checks_are_skipped(self, media):
        result = _scan(_clean(media), disabled_checks=["Edit List", "Track Layout"])
        assert _row(result, "Edit List")[1] == "SKIP"
        assert _row(result, "Track Layout")[1] == "SKIP"

    @pytest.mark.parametrize("channels", [6, 11])
    def test_unknown_layout_short_file_keeps_frame_level_checks(self, tmp_path, channels):
        """The 0.8.3 ambisonic abort (unknown layout, under 30 s) must not return."""
        labels = "".join(f"[s{i}]" for i in range(channels))
        path = tmp_path / f"u{channels}.mov"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
               "-filter_complex", f"[1:a]asplit={channels}{labels};{labels}amerge=inputs={channels}[a]",
               "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "pcm_s16le", str(path))
        data = bytearray(path.read_bytes())
        at = data.find(b"chan")
        if at > 0:
            data[at:at + 4] = b"free"  # drop the channel layout box: layout becomes unknown
        path.write_bytes(bytes(data))
        result = _scan(path, quick=False, loudness_mode="report")
        for name in ("File Integrity", "Audio Silence", "Dead Channel", "Audio Loudness"):
            check = _row(result, name)
            assert not check[2].startswith("Not verified"), check
