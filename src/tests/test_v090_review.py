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
        assert _row(result, "Edit List")[1] == "PASS", _row(result, "Edit List")
        # 0.9.1: A/V ends follow Telestream FC-108 (audio may stop at most 2
        # frames early). A stream-copy cut can leave the audio a few frames
        # short at its tail, which is now an advisory A/V Duration warning.
        others = [c for c in _problems(result) if c[0] != "A/V Duration"]
        assert not others and result.status in ("PASS", "PASS_WITH_WARNINGS"), _problems(result)
        assert _row(result, "A/V Duration")[1] in ("PASS", "WARN"), _row(result, "A/V Duration")

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
# H2: loudness, strict mode and the summary line
# ---------------------------------------------------------------------------

def _hot_clip(path):
    """Camera-original loudness: about -3.6 LUFS with a true peak above 0 dBTP."""
    path += ".mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=4",
           "-f", "lavfi", "-i", "sine=f=1000:sample_rate=48000:d=4,volume=8",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-colorspace", "bt709", "-c:a", "aac", path)
    return path


class TestLoudnessIsReportedNotJudgedByDefault:
    """Every Canon original tripped -23 LUFS / -1 dBTP and Strict Mode turned
    that into FAIL (review H2)."""

    def test_default_is_report(self):
        assert DEFAULT_CONFIG["loudness_mode"] == "report"
        assert core.LOUDNESS_MODES == ("off", "report", "advisory", "essential")

    def test_hot_original_passes_by_default_even_in_strict_mode(self, media):
        clip = media.get("hot", _hot_clip)
        for strict in (False, True):
            result = _scan(clip, strict=strict)
            loud = _row(result, "Audio Loudness")
            assert loud[1] == "PASS", loud
            assert "LUFS integrated" in loud[2] and "not enforced" in loud[2], loud
            # 0.9.1: true peak is its own row (separate limit and remedy).
            peak = _row(result, "Audio True Peak")
            assert peak[1] == "PASS" and "dBTP" in peak[2] and "not enforced" in peak[2], peak
            assert result.status == "PASS", (strict, _problems(result))
        assert result.metadata["true_peak_dbtp"] > -1

    def test_advisory_warns_and_essential_fails(self, media):
        clip = media.get("hot", _hot_clip)
        assert _row(_scan(clip, loudness_mode="advisory"), "Audio Loudness")[1] == "WARN"
        essential = _scan(clip, loudness_mode="essential")
        assert _row(essential, "Audio Loudness")[1] == "FAIL"
        assert essential.status == "MISMATCH"

    def test_off_skips_ebur128(self, media):
        clip = media.get("hot", _hot_clip)
        result = _scan(clip, loudness_mode="off")
        assert _row(result, "Audio Loudness")[1] == "SKIP"
        assert "integrated_loudness_lufs" not in result.metadata


class TestStrictMode:
    def test_quick_scan_is_not_a_strict_failure(self, media):
        """Quick scans skip frame analysis by choice; under Strict every file
        used to become a FAIL."""
        clip = media.get("clean.mp4", lambda p: make_clip(p + ".mp4", duration=3))
        result = _scan(clip, strict=True, quick=True)
        assert _row(result, "File Integrity")[2].startswith(core.NOT_VERIFIED_PREFIX)
        assert result.status == "PASS_WITH_WARNINGS", _problems(result)
        assert result.plain_summary().startswith("Heads up:")

    def test_analysis_that_genuinely_failed_still_escalates(self, media, monkeypatch):
        clip = media.get("clean.mp4", lambda p: make_clip(p + ".mp4", duration=3))
        monkeypatch.setattr(core, "_build_scan_command",
                            lambda ffmpeg_path, filepath, plan, cfg:
                            [ffmpeg_path, "-loglevel", "level+verbose", "-i", filepath,
                             "-filter_complex", "nosuchfilter", "-f", "null", "-"])
        assert _scan(clip).status == "PASS_WITH_WARNINGS"
        strict = _scan(clip, strict=True)
        assert strict.status == "MISMATCH"
        assert strict.plain_summary().startswith("Failed in Strict Mode:"), strict.plain_summary()

    def test_strict_warning_summary_follows_the_verdict(self, media):
        clip = media.get("hot", _hot_clip)
        strict = _scan(clip, strict=True, loudness_mode="advisory")
        assert strict.status == "MISMATCH"
        summary = strict.plain_summary()
        assert summary.startswith("Failed in Strict Mode:") and "Heads up" not in summary
        relaxed = _scan(clip, loudness_mode="advisory")
        assert relaxed.plain_summary().startswith("Heads up:")


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


# ---------------------------------------------------------------------------
# M2 lives in test_v090_engine.py (TestEveryAudioTrack); dead channels too.
# L4: exports carry tool versions, not install paths
# ---------------------------------------------------------------------------

class TestExportPaths:
    def test_media_path_kept_tool_paths_dropped(self, media, tmp_path):
        from frameguard.report import export_results_csv, export_results_json
        from frameguard.tools import get_ffmpeg_path, get_ffprobe_path
        clip = media.get("clean.mp4", lambda p: make_clip(p + ".mp4", duration=3))
        result = _scan(clip)
        json_text = open(export_results_json([result], str(tmp_path / "r.json")),
                         encoding="utf-8").read()
        csv_text = open(export_results_csv([result], str(tmp_path / "r.csv")),
                        encoding="utf-8-sig").read()
        record = json.loads(json_text)["results"][0]
        assert record["file"] == os.path.abspath(clip)
        assert set(record["tool_versions"]) == {"ffmpeg", "ffprobe"}
        assert all(v.startswith(("ffmpeg version", "ffprobe version"))
                   for v in record["tool_versions"].values())
        for tool in (get_ffmpeg_path(), get_ffprobe_path()):
            assert tool not in json_text and tool not in csv_text
            assert os.path.dirname(tool) not in json_text


# ---------------------------------------------------------------------------
# L5: rotated footage is an orientation mismatch, not "smaller"
# ---------------------------------------------------------------------------

class TestRotationWording:
    def test_portrait_rotation_is_named(self, media):
        source = media.get("clean.mp4", lambda p: make_clip(p + ".mp4", duration=3))

        def build(path):
            path += ".mp4"
            ffmpeg("-display_rotation", "90", "-i", source, "-c", "copy", path)
            return path
        rotated = media.get("rot90", build)
        check = _row(_scan(rotated), "Resolution")
        assert check[1] == "FAIL"
        assert check[2] == ("Portrait orientation (rotated 90°): 240x320 displayed vs "
                            "320x240 landscape target."), check
        assert "upscale" not in check[2]
