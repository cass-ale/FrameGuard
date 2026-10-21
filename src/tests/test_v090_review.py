"""Fixes from the 0.9.0 release review, against the REAL FFmpeg/ffprobe.

H1 lossless trims, H2 loudness/strict/summary, M2 blank secondary tracks,
L3 sampled VFR windows, L4 export paths, L5 rotation wording. (L6, the
watch-folder lock message, needs no media: test_v090_watchfolder.py.)
"""

import json
import os

import pytest

from frameguard import core
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import (
    MediaLibrary, corrupt_middle, ffmpeg, make_clip, make_open_gop_source,
    needs_ffmpeg, stream_copy_trim, truncate,
)

pytestmark = needs_ffmpeg


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=240, target_fps=30.0,
               min_bitrate_mbps=0, accepted_containers=[".mp4", ".mov", ".mkv", ".ts"])
    cfg.update(overrides)
    return cfg


def _scan(path, strict=False, quick=False, **overrides):
    result = run_qa_checks(path, cfg=_cfg(**overrides), strict=strict, quick=quick)
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _problems(result):
    return [c[:3] for c in result.checks if c[1] in ("FAIL", "WARN")]


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg090review")))


# ---------------------------------------------------------------------------
# H1: losslessly trimmed files are not "truncated"
# ---------------------------------------------------------------------------

TRIMS = [
    # (codec, container, rate, cut start, cut length)
    ("h264", ".mp4", "30", 3.37, 6),
    ("h264", ".mp4", "30", 4.1, 3),
    ("hevc", ".mov", "24000/1001", 2.6, 6),
    ("hevc", ".mov", "24000/1001", 3.3, 3),
]


class TestLosslessTrims:
    """A stream-copy cut stores the GOP before the cut point and hides it
    with an edit list: nb_frames counted 498 frames where 452 are shown, so
    the file was called truncated (review H1)."""

    @pytest.mark.parametrize("vcodec,ext,rate,start,length", TRIMS,
                             ids=[f"{c}{e}-{s}+{n}" for c, e, _, s, n in TRIMS])
    def test_trimmed_file_is_clean(self, media, vcodec, ext, rate, start, length):
        source = media.get(f"opengop_{vcodec}{ext}",
                           lambda p: make_open_gop_source(p + ext, vcodec=vcodec, rate=rate))
        trimmed = media.get(f"trim_{vcodec}_{start}_{length}{ext}",
                            lambda p: stream_copy_trim(source, p + ext, start, length))
        fps = 30.0 if rate == "30" else 23.976
        result = _scan(trimmed, target_fps=fps)
        integrity = _row(result, "File Integrity")
        assert integrity[1] == "PASS", integrity
        assert result.metadata.get("edit_list_hidden_frames", 0) > 0, result.metadata
        assert _row(result, "Frame Count")[1] == "PASS", _row(result, "Frame Count")
        assert _row(result, "CFR Check")[1] == "PASS", _row(result, "CFR Check")
        assert result.status == "PASS", _problems(result)

    def test_truncating_a_trimmed_file_still_fails(self, media, tmp_path):
        source = media.get("opengop_h264.mp4",
                           lambda p: make_open_gop_source(p + ".mp4"))
        trimmed = stream_copy_trim(source, tmp_path / "trim_fs.mp4", 3.37, 6)
        faststart = str(tmp_path / "trim_faststart.mp4")
        ffmpeg("-i", trimmed, "-c", "copy", "-movflags", "+faststart", faststart)
        cut = truncate(faststart, tmp_path / "trim_cut.mp4", 0.5)
        integrity = _row(_scan(cut), "File Integrity")
        assert integrity[1] == "FAIL" and "Truncated" in integrity[2], integrity

    def test_truncated_mkv_fails(self, media, tmp_path):
        source = media.get("clean10.mkv", lambda p: make_clip(p + ".mkv", duration=10))
        cut = truncate(source, tmp_path / "cut.mkv", 0.5)
        result = _scan(cut)
        integrity = _row(result, "File Integrity")
        assert integrity[1] == "FAIL" and "Truncated" in integrity[2], integrity
        assert result.status == "MISMATCH"

    def test_mid_file_corruption_still_fails(self, media, tmp_path):
        source = media.get("clean10.mp4", lambda p: make_clip(p + ".mp4", duration=10))
        damaged = corrupt_middle(source, tmp_path / "damaged.mp4")
        assert _row(_scan(damaged), "File Integrity")[1] == "FAIL"

    def test_expected_frames_rule(self):
        """min(nb_frames minus hidden lead-in, round(duration x fps))."""
        probe = {"streams": [{"codec_type": "video", "index": 0, "nb_frames": "498",
                              "duration": "15.133333", "avg_frame_rate": "30/1"}],
                 "format": {"duration": "15.133333"}}
        assert core._ScanPlan(probe, _cfg()).video_expected_frames() == 454
        assert core._ScanPlan(probe, _cfg(), hidden_frames=46).video_expected_frames() == 452


# ---------------------------------------------------------------------------
# L3: sampled VFR windows on MPEG-TS with B-frames
# ---------------------------------------------------------------------------

class TestSampledWindows:
    def test_clean_25p_ts_with_b_frames_has_no_irregular_intervals(self, media):
        def build(path):
            path += ".ts"
            ffmpeg("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=75",
                   "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=75",
                   "-c:v", "libx264", "-preset", "fast",
                   "-x264-params", "keyint=50:bframes=3:b-pyramid=normal",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", path)
            return path
        clip = media.get("b25", build)
        result = _scan(clip, vfr_full_scan_max_mb=0, target_width=160, target_height=120,
                       target_fps=25.0)
        cfr = _row(result, "CFR Check")
        assert cfr[1] == "PASS" and "sampled 12 windows" in cfr[2], cfr
        assert result.metadata["irregular_frame_intervals"] == 0
