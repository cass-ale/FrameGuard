"""0.9.0 engine regressions, run against the REAL FFmpeg/ffprobe.

Each class pins one audit finding (ids in the class docstrings). Earlier
suites fed hand-made ffprobe dicts to the checks, which is how a truncated
file, Matroska A/V drift, a silent second audio track and real VFR all got
through: the dicts never looked like what the tools actually print.
"""

import os
import subprocess
import threading
import time

import pytest

from frameguard import core
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import (
    FFMPEG, MediaLibrary, corrupt_middle, ffmpeg, inject_spherical, make_clip,
    needs_ffmpeg, truncate,
)

pytestmark = needs_ffmpeg


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=240, target_fps=30.0,
               min_bitrate_mbps=0, loudness_mode="off",
               accepted_containers=[".mp4", ".mov", ".mkv"])
    cfg.update(overrides)
    return cfg


def _scan(path, **overrides):
    result = run_qa_checks(path, cfg=_cfg(**overrides))
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _not_verified(check):
    return check[1] == "WARN" and check[2].startswith(core.NOT_VERIFIED_PREFIX)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg090")))


# ---------------------------------------------------------------------------
# C1: file integrity
# ---------------------------------------------------------------------------

CLEAN_VARIANTS = [
    ("h264", ".mp4"), ("h264", ".mov"), ("h264", ".mkv"),
    ("hevc", ".mp4"), ("hevc", ".mkv"), ("prores", ".mov"),
]


class TestFileIntegrity:
    """C1: truncated/corrupt files must FAIL an essential check."""

    @pytest.mark.parametrize("vcodec,ext", CLEAN_VARIANTS,
                             ids=[f"{c}{e}" for c, e in CLEAN_VARIANTS])
    def test_clean_files_have_zero_decoder_errors(self, media, vcodec, ext):
        clip = media.get(f"clean_{vcodec}{ext}", lambda p: make_clip(p, vcodec=vcodec))
        result = _scan(clip)
        integrity = _row(result, "File Integrity")
        assert integrity[1] == "PASS", integrity
        assert result.metadata["decode_errors"] == 0
        assert result.status == "PASS", [c for c in result.checks if c[1] != "PASS"]

    def test_truncated_copy_fails(self, media, tmp_path):
        source = media.get("faststart.mp4", lambda p: make_clip(
            p, duration=10, extra=("-movflags", "+faststart")))
        cut = truncate(source, tmp_path / "cut40.mp4", 0.40)
        result = _scan(cut)
        integrity = _row(result, "File Integrity")
        assert integrity[1] == "FAIL" and "Truncated" in integrity[2], integrity
        assert result.status == "MISMATCH"
        # The header still claims the full length: that is why it used to pass.
        assert _row(result, "A/V Duration")[1] == "PASS"

    def test_mid_file_corruption_fails_and_threshold_is_configurable(self, media, tmp_path):
        source = media.get("plain.mp4", lambda p: make_clip(p, duration=8))
        damaged = corrupt_middle(source, tmp_path / "damaged.mp4")
        strict = _scan(damaged)
        assert _row(strict, "File Integrity")[1] == "FAIL"
        errors = strict.metadata["decode_errors"]
        assert errors > 0
        lenient = _scan(damaged, max_decode_errors=errors)
        assert _row(lenient, "File Integrity")[1] == "PASS"

    def test_error_count_matches_ffmpegs_own_single_pass_accounting(self, media, tmp_path):
        """L1: overlapping segments double counted errors (175 vs 141)."""
        source = media.get("plain.mp4", lambda p: make_clip(p, duration=8))
        damaged = corrupt_middle(source, tmp_path / "damaged.mp4")
        proc = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-nostats",
                               "-loglevel", "level+verbose", "-i", damaged, "-f", "null", "-"],
                              capture_output=True, text=True, errors="replace")
        independent = core._DecodeAccounting()
        for line in proc.stderr.splitlines():
            independent.add(line)
        assert _scan(damaged).metadata["decode_errors"] == independent.total_errors


# ---------------------------------------------------------------------------
# H1: Matroska stream durations
# ---------------------------------------------------------------------------

class TestMatroskaDurations:
    """H1: ``:stream_tags=rotate`` discarded MKV DURATION tags."""

    def test_short_audio_in_mkv_fails(self, media):
        clip = media.get("avshort.mkv", _avshort)
        assert _row(_scan(clip), "A/V Duration")[1] == "FAIL"

    def test_clean_mkv_has_per_stream_durations(self, media):
        clip = media.get("clean_h264.mkv", lambda p: make_clip(p))
        assert _row(_scan(clip), "A/V Duration")[1] == "PASS"


def _avshort(path):
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=8",
           "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=4",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", path)
    return path


# ---------------------------------------------------------------------------
# H2: frame-level analysis is never silently skipped
# ---------------------------------------------------------------------------

class TestNeverSilentlySkipped:
    """H2: unknown duration, quick mode and failed decodes are visible."""

    def test_unknown_duration_still_scans_a_corrupt_file(self, media, tmp_path, monkeypatch):
        source = media.get("plain.mp4", lambda p: make_clip(p, duration=8))
        damaged = corrupt_middle(source, tmp_path / "damaged.mp4")
        real_probe = core.run_ffprobe

        def probe_without_durations(*args, **kwargs):
            data = real_probe(*args, **kwargs)
            data["format"].pop("duration", None)
            for stream in data["streams"]:
                stream.pop("duration", None)
                stream.pop("nb_frames", None)
            return data

        monkeypatch.setattr(core, "run_ffprobe", probe_without_durations)
        result = _scan(damaged)
        assert _row(result, "File Integrity")[1] == "FAIL"
        assert result.status == "MISMATCH"

    def test_clean_file_without_duration_is_not_verified_not_pass(self, media):
        def build(path):
            ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=6",
                   "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=6",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-f", "matroska", "-live", "1", path)
            return path
        clip = media.get("live.mkv", build)
        result = _scan(clip)
        integrity = _row(result, "File Integrity")
        if core._probed_duration(core.run_ffprobe(clip)):
            pytest.skip("this FFmpeg wrote a duration even in live mode")
        assert _not_verified(integrity), integrity
        assert result.status == "PASS_WITH_WARNINGS"

    def test_quick_scan_is_labelled_not_verified(self, media):
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        result = run_qa_checks(clip, cfg=_cfg(), quick=True)
        result.finalize()
        for name in ("File Integrity", "Black Frames", "Freeze Frames", "Audio Silence"):
            assert _not_verified(_row(result, name)), name
        assert result.status == "PASS_WITH_WARNINGS"

    def test_ffmpeg_failure_marks_every_frame_check_not_verified(self, media, monkeypatch):
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        monkeypatch.setattr(core, "_build_scan_command",
                            lambda ffmpeg_path, filepath, plan, cfg:
                            [ffmpeg_path, "-loglevel", "level+verbose", "-i", filepath,
                             "-filter_complex", "nosuchfilter", "-f", "null", "-"])
        result = _scan(clip)
        for name in ("File Integrity", "Black Frames", "Freeze Frames", "Audio Silence"):
            assert _not_verified(_row(result, name)), _row(result, name)
        assert result.status == "PASS_WITH_WARNINGS"

    def test_large_files_are_not_gated(self, media, monkeypatch):
        """The 8 GB gate skipped frame analysis for ProRes masters."""
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        real_getsize = os.path.getsize
        monkeypatch.setattr(core.os.path, "getsize",
                            lambda p: 50 * 1024 ** 3 if p == clip else real_getsize(p))
        assert _row(_scan(clip), "File Integrity")[1] == "PASS"


# ---------------------------------------------------------------------------
# H3: software decoding only
# ---------------------------------------------------------------------------

class TestSoftwareDecoding:
    """H3: hardware decoding changed verdicts; the setting is now inert."""

    def test_hwaccel_setting_cannot_change_the_verdict(self, media, tmp_path):
        source = media.get("plain.mp4", lambda p: make_clip(p, duration=8))
        damaged = corrupt_middle(source, tmp_path / "damaged.mp4")
        on = _scan(damaged, use_hwaccel=True)
        off = _scan(damaged, use_hwaccel=False)
        assert [c[:2] for c in on.checks] == [c[:2] for c in off.checks]
        assert on.metadata["decode_errors"] == off.metadata["decode_errors"] > 0


# ---------------------------------------------------------------------------
# H4: every audio track
# ---------------------------------------------------------------------------

class TestEveryAudioTrack:
    """H4: only the first audio track was analysed."""

    def test_silent_second_track_is_reported(self, media):
        """Track 2 used to go unchecked. A blank secondary track is reported
        but only warns (review M2: intentional blank tracks exist)."""
        clip = media.get("silent51.mp4", lambda p: make_clip(p, audio=("stereo", "silent-5.1")))
        result = _scan(clip)
        silence = _row(result, "Audio Silence")
        assert silence[1] == "WARN", silence
        assert "Track 2 is silent - intentional blank track?" in silence[2]
        assert result.status == "PASS_WITH_WARNINGS"

    def test_silent_main_track_fails(self, media):
        clip = media.get("silentmain.mp4", lambda p: make_clip(p, audio=("silent-stereo", "5.1")))
        result = _scan(clip)
        silence = _row(result, "Audio Silence")
        assert silence[1] == "FAIL" and "Track 1" in silence[2], silence
        assert result.status == "MISMATCH"

    def test_low_sample_rate_second_track_fails(self, media):
        clip = media.get("track8k.mp4", lambda p: make_clip(p, audio=("stereo", "8k")))
        rate = _row(_scan(clip), "Audio Sample Rate")
        assert rate[1] == "FAIL" and "Track 2" in rate[2] and "8000 Hz" in rate[2], rate

    def test_dead_lfe_is_not_a_failure(self, media):
        """AAC low-passes the LFE, so a 440 Hz test tone leaves it silent."""
        clip = media.get("surround.mp4", lambda p: make_clip(p, audio=("5.1",)))
        dead = _row(_scan(clip), "Dead Channel")
        assert dead[1] == "PASS", dead


# ---------------------------------------------------------------------------
# H5: real VFR from packet timestamps
# ---------------------------------------------------------------------------

class TestVariableFrameRate:
    """H5: r_frame_rate vs avg_frame_rate missed real VFR."""

    def test_dropped_frame_every_second_fails(self, media):
        clip = media.get("vfr_drop.mp4", lambda p: make_clip(
            p, duration=8, video_filter="select='mod(n\\,30)-29'",
            extra=("-fps_mode", "vfr")))
        cfr = _row(_scan(clip), "CFR Check")
        assert cfr[1] == "FAIL", cfr

    def test_rate_change_midway_fails_in_mkv(self, media):
        def build(path):
            ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=8",
                   "-vf", "select='lt(t\\,4)+eq(mod(n\\,2)\\,0)'", "-fps_mode", "vfr",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", path)
            return path
        clip = media.get("vfr_half.mkv", build)
        result = _scan(clip, audio_required=False)
        assert _row(result, "CFR Check")[1] == "FAIL"
        # Duration x nominal rate predicts the wrong frame count for VFR; the
        # integrity check must judge by time and not call this truncated.
        assert _row(result, "File Integrity")[1] == "PASS", _row(result, "File Integrity")

    def test_sampled_scan_still_detects_vfr(self, media):
        """Files above vfr_full_scan_max_mb are read in 12 x 5 s windows."""
        clip = media.get("vfr_drop_long.mp4", lambda p: make_clip(
            p, size="160x120", duration=75, audio=(),
            video_filter="select='mod(n\\,30)-29'", extra=("-fps_mode", "vfr")))
        cfr = _row(_scan(clip, vfr_full_scan_max_mb=0, audio_required=False,
                         target_width=160, target_height=120), "CFR Check")
        assert cfr[1] == "FAIL" and "sampled 12 windows" in cfr[2], cfr

    def test_constant_rate_passes(self, media):
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        cfr = _row(_scan(clip), "CFR Check")
        assert cfr[1] == "PASS" and "30.000 fps" in cfr[2], cfr


# ---------------------------------------------------------------------------
# M1 / M2: frame rate, resolution, aspect ratio
# ---------------------------------------------------------------------------

class TestGeometryAndRate:
    """M1/M2: above-target rates and larger frames used to pass."""

    def test_25p_fails_a_24p_spec(self, media):
        clip = media.get("pal25.mp4", lambda p: make_clip(p, rate=25))
        assert _row(_scan(clip, target_fps=24.0), "Frame Rate")[1] == "FAIL"

    def test_integer_multiple_needs_the_escape_hatch(self, media):
        clip = media.get("p60.mp4", lambda p: make_clip(p, rate=60, duration=3))
        assert _row(_scan(clip), "Frame Rate")[1] == "FAIL"
        assert _row(_scan(clip, allow_fps_multiples=True), "Frame Rate")[1] == "PASS"

    def test_larger_frame_of_the_wrong_shape_fails_aspect(self, media):
        clip = media.get("wide.mp4", lambda p: make_clip(p, size="640x320", duration=3))
        result = _scan(clip, allow_larger_resolution=True)
        assert _row(result, "Resolution")[1] == "PASS"
        assert _row(result, "Aspect Ratio")[1] == "FAIL"
        assert _row(_scan(clip), "Resolution")[1] == "FAIL"


# ---------------------------------------------------------------------------
# D1: interlace, colour / HDR, dead channels
# ---------------------------------------------------------------------------

class TestPictureAndChannelDepth:
    """D1: probed values that were never checked."""

    def test_interlaced_fails_when_progressive_is_required(self, media):
        clip = media.get("interlaced.mp4", lambda p: make_clip(
            p, extra=("-flags", "+ilme+ildct", "-x264opts", "tff=1")))
        assert _row(_scan(clip), "Scan Type")[1] == "FAIL"
        assert _row(_scan(clip, require_progressive=False), "Scan Type")[1] == "PASS"

    def test_pq_hdr_is_reported_and_enforced_only_when_specified(self, media):
        params = ("log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                  "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)"
                  "L(10000000,1):max-cll=1000,400")
        clip = media.get("pq.mp4", lambda p: make_clip(
            p, vcodec="hevc", extra=("-pix_fmt", "yuv420p10le", "-x265-params", params)))
        info = _row(_scan(clip), "Color / HDR")
        assert info[1] == "PASS" and "10-bit" in info[2] and "PQ" in info[2], info
        assert _row(_scan(clip, required_transfer="sdr"), "Color / HDR")[1] == "FAIL"
        assert _row(_scan(clip, required_transfer="hdr", min_bit_depth=10), "Color / HDR")[1] == "PASS"

    def test_8bit_fails_a_10bit_minimum(self, media):
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        assert _row(_scan(clip, min_bit_depth=10), "Color / HDR")[1] == "FAIL"

    def test_dead_right_channel_fails(self, media):
        clip = media.get("deadright.mp4", lambda p: make_clip(p, audio=("dead-right",)))
        dead = _row(_scan(clip), "Dead Channel")
        assert dead[1] == "FAIL" and "FR" in dead[2], dead


# ---------------------------------------------------------------------------
# D2: XR metadata
# ---------------------------------------------------------------------------

class TestXRMetadata:
    """D2: spherical/stereo signalling was never requested."""

    def test_missing_projection_fails_a_360_spec(self, media):
        clip = media.get("flat21.mp4", lambda p: make_clip(p, size="640x320", duration=3))
        xr = _row(_scan(clip, required_projection="equirectangular"), "XR Metadata")
        assert xr[1] == "FAIL" and "flat" in xr[2], xr

    def test_injected_equirect_top_bottom_is_found_and_matched(self, media):
        clip = media.get("eq_tb.mp4", lambda p: inject_spherical(
            make_clip(p, size="640x320", duration=3), stereo="top-bottom"))
        assert _row(_scan(clip), "XR Metadata")[1] == "PASS"
        matched = _row(_scan(clip, required_projection="equirectangular",
                             required_stereo_mode="top_bottom"), "XR Metadata")
        assert matched[1] == "PASS" and "equirectangular" in matched[2], matched
        assert _row(_scan(clip, required_stereo_mode="mono"), "XR Metadata")[1] == "FAIL"

    def test_builtin_360_preset_requires_equirect(self):
        from frameguard.worker import ScanWorker
        worker = ScanWorker()
        worker.configure(files=["x.mp4"], preset="5.7k30")
        assert worker._cfg["required_projection"] == "equirectangular"
        worker.configure(files=["x.mp4"], preset="8k30")
        assert worker._cfg["required_projection"] == "any"


# ---------------------------------------------------------------------------
# D3: loudness
# ---------------------------------------------------------------------------

class TestLoudness:
    """D3: ebur128 ran and its result was discarded. Enforcement is opt-in
    (review H2); the defaults are covered in test_v090_review.py."""

    def test_measured_loudness_is_checked(self, media):
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        result = _scan(clip, loudness_mode="advisory", target_loudness_lufs=-40.0)
        loud = _row(result, "Audio Loudness")
        assert loud[1] == "WARN" and "LUFS" in loud[2], loud
        assert result.metadata["integrated_loudness_lufs"] > -40
        assert result.status == "PASS_WITH_WARNINGS"
        essential = _scan(clip, loudness_mode="essential", target_loudness_lufs=-40.0)
        assert _row(essential, "Audio Loudness")[1] == "FAIL"

    def test_off_does_not_run_ebur128(self):
        plan = core._ScanPlan({"streams": [{"codec_type": "audio", "index": 0, "channels": 2}]},
                              _cfg(loudness_mode="off"))
        command = core._build_scan_command("ffmpeg", "x.mp4", plan, _cfg(loudness_mode="off"))
        assert not any("ebur128" in part for part in command)


# ---------------------------------------------------------------------------
# Cancellation of the single-pass scan
# ---------------------------------------------------------------------------

class TestCancellation:
    def test_cancel_stops_ffmpeg_promptly(self, media):
        from frameguard.worker import ScanWorker
        clip = media.get("long.mp4", lambda p: make_clip(p, size="1280x720", duration=60))
        worker = ScanWorker()
        worker.configure(files=[clip], cfg_overrides={"target_width": 1280, "target_height": 720})
        done = threading.Event()
        worker.scan_cancelled.connect(done.set)
        worker.scan_finished.connect(lambda _r: done.set())
        thread = threading.Thread(target=worker.run)
        thread.start()
        time.sleep(1.0)
        started = time.monotonic()
        worker.cancel()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert time.monotonic() - started < 5
        assert worker._own_procs == []
        assert not [p for p in core.get_live_procs() if p.poll() is None]
