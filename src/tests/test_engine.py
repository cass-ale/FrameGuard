"""Scan engine tests.

The headline bug these cover: the deep scan ran FFmpeg at `-v error`, but
blackdetect/freezedetect/silencedetect all report their findings at info level.
Every deep scan therefore found nothing and reported a clean pass.
"""

import subprocess
from copy import deepcopy
from unittest import mock

import pytest

from frameguard.core import (
    DEFAULT_CONFIG,
    QAResult,
    _DecodeAccounting,
    _is_decode_evidence,
    _pair_detect_events,
    run_qa_checks,
)
from frameguard.tools import get_ffmpeg_path

FFMPEG = get_ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg not available")


@pytest.fixture(scope="session")
def defect_clip(tmp_path_factory):
    """40s 1080p30 clip: black + silent between 10s and 16s."""
    if not FFMPEG:
        pytest.skip("FFmpeg not available")
    out = tmp_path_factory.mktemp("media") / "defects.mp4"
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=40,"
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=40",
        "-filter_complex",
        "[0:v]drawbox=x=0:y=0:w=640:h=360:color=gray:t=fill:"
        "enable='not(between(t,10,16))'[v];"
        "[1:a]volume=enable='between(t,10,16)':volume=0[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2M",
        "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-c:a", "aac", "-b:a", "128k", str(out),
    ], check=True, capture_output=True)
    return str(out)


@pytest.fixture(scope="session")
def clean_clip(tmp_path_factory):
    """40s 1080p30 clip with no defects."""
    if not FFMPEG:
        pytest.skip("FFmpeg not available")
    out = tmp_path_factory.mktemp("media") / "clean.mp4"
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=40,"
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=40",
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2M",
        "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-c:a", "aac", "-b:a", "128k", str(out),
    ], check=True, capture_output=True)
    return str(out)


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"target_width": 640, "target_height": 360, "target_fps": 30.0,
                "min_bitrate_mbps": 1})
    cfg.update(overrides)
    return cfg


def _check(result, name):
    for c in result.checks:
        if c[0] == name:
            return {"status": c[1], "detail": c[2]}
    return None


class TestQAResult:
    def test_add_warn_accepts_confidence(self):
        """add_warn used to reject confidence=, and the resulting TypeError was
        laundered into an 'Internal Error' MISMATCH for the whole file."""
        r = QAResult("x.mov")
        r.add_warn("Bitrate", "slightly low", confidence=75)
        assert r.checks[0] == ("Bitrate", "WARN", "slightly low", 75)

    def test_status_is_none_until_finalized(self):
        r = QAResult("x.mov")
        assert r.status is None
        r.finalize()
        assert r.status == "PASS"

    @pytest.mark.parametrize("build,expected", [
        (lambda r: None, "PASS"),
        # Must be an ADVISORY_CHECKS name; a WARN on a removed check like
        # "Color Space" is intentionally ignored by overall_status().
        (lambda r: r.add_warn("Black Frames", "1 sequence, 0.8s"), "PASS_WITH_WARNINGS"),
        (lambda r: r.add_fail("Frame Rate", "24 != 30"), "MISMATCH"),
        (lambda r: r.add_fail("Internal Error", "boom"), "MISMATCH"),
    ])
    def test_overall_status(self, build, expected):
        r = QAResult("x.mov")
        build(r)
        r.finalize()
        assert r.status == expected

    def test_unknown_check_warning_is_never_dropped(self):
        """0.9.0 deleted the disabled checks; any WARN now reaches the verdict."""
        r = QAResult("x.mov")
        r.add_warn("Color Space", "bt601")
        r.finalize()
        assert r.status == "PASS_WITH_WARNINGS"


class TestMalformedMetadata:
    def _probe(self):
        return {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264",
                    "width": 640, "height": 360,
                    "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
                    "nb_frames": "1200", "duration": "40.0",
                    "bit_rate": "2000000",
                },
                {
                    "codec_type": "audio", "codec_name": "aac",
                    "sample_rate": "48000", "channels": "2",
                    "channel_layout": "stereo", "duration": "40.0",
                    "bit_rate": "128000",
                },
            ],
            "format": {"duration": "40.0", "bit_rate": "2128000"},
        }

    def _scan(self, tmp_path, probe):
        target = tmp_path / "metadata.mp4"
        target.write_bytes(b"not-empty")
        with mock.patch("frameguard.core.run_ffprobe", return_value=probe), \
             mock.patch("frameguard.core.run_deep_scan"), \
             mock.patch("frameguard.core.run_mediainfo_enrichment"):
            result = run_qa_checks(str(target), cfg=_cfg())
        result.finalize()
        return result

    @pytest.mark.parametrize("mutate,check_name", [
        (lambda p: p["streams"][0].update(r_frame_rate=[], avg_frame_rate={}),
         "Frame Rate"),
        (lambda p: p["streams"][0].update(nb_frames={}), "Frame Count"),
        (lambda p: p["streams"][0].pop("nb_frames"), "Frame Count"),
        (lambda p: (p["streams"][0].update(duration={}),
                    p["format"].update(duration={})), "Frame Count"),
        (lambda p: p["streams"][1].update(channels="stereo"), "Audio Channels"),
        (lambda p: p["streams"][0].update(bit_rate={"bad": 1}), "Bitrate"),
        (lambda p: p["streams"][1].update(duration={}), "A/V Duration"),
        (lambda p: p["streams"][1].pop("duration"), "A/V Duration"),
    ], ids=[
        "wrong-type-frame-rate", "wrong-type-frame-count", "missing-frame-count",
        "wrong-type-duration", "wrong-type-audio-channels", "wrong-type-bitrate",
        "wrong-type-av-duration", "missing-av-duration",
    ])
    def test_malformed_critical_metadata_is_flagged_without_internal_error(
            self, tmp_path, mutate, check_name):
        probe = self._probe()
        mutate(probe)
        result = self._scan(tmp_path, probe)

        assert _check(result, "Internal Error") is None, result.checks
        check = _check(result, check_name)
        assert check is not None and check["status"] in ("WARN", "FAIL"), result.checks
        assert result.status != "PASS", result.checks

    def test_valid_numeric_strings_keep_normal_outcomes(self, tmp_path):
        result = self._scan(tmp_path, deepcopy(self._probe()))

        assert _check(result, "Internal Error") is None
        assert _check(result, "Frame Rate")["status"] == "PASS"
        assert _check(result, "Audio Channels")["status"] == "PASS"
        assert _check(result, "A/V Duration")["status"] == "PASS"
        assert _check(result, "Frame Count")["status"] == "PASS"


class TestDetectEventPairing:
    """FFmpeg logs from several filter threads share one stderr pipe, and
    under threaded filtering the lines interleave mid-line: a detect
    token can land glued onto the tail of an ebur128 status line, its
    "[Parsed_blackdetect...]" prefix overwritten. The token payload
    survives intact, so the parser must not demand a word boundary (or
    a line start) before it. Regression: identical scans of one clip
    intermittently reported "No black frame sequences detected" for a
    6-second black segment when the glued line was dropped."""

    # Captured verbatim from a real losing scan (prefix eaten by the
    # ebur128 line it collided with).
    GLUED = ("[Parsed_ebur128_1 @ 0x7b51d8001c00] t: 15.899979  "
             "TARGET:-23 LUFS    M:-166.1 S:   nan     I: -21.8 LUFS       "
             "LRA:   4.8 LUblack_start:10 black_end:16.033333 "
             "black_duration:6.033333")

    def test_glued_black_event_is_paired(self):
        events = _pair_detect_events(self.GLUED, "black")
        assert len(events) == 1
        start, dur = events[0]
        assert start == 10.0
        assert abs(dur - 6.033333) < 0.001

    def test_glued_silence_event_is_paired(self):
        text = ("[Parsed_ebur128_1 @ 0x0] t: 9.9 LUFSsilence_start: 10.005333\n"
                "silence_end: 16.021333 | silence_duration: 6.016")
        events = _pair_detect_events(text, "silence")
        assert len(events) == 1
        start, dur = events[0]
        assert abs(start - 10.005333) < 0.0001
        assert abs(dur - 6.016) < 0.001


class TestDecodeErrorClassifier:
    """Errors are identified by FFmpeg's own severity tag and accounting."""

    def test_ffmpeg_banner_is_not_a_decode_error(self):
        noise = [
            "[info] ffmpeg version 8.1.2-essentials_build-www.gyan.dev Copyright (c) 2000-2025",
            "[info]   configuration: --enable-gpl --enable-libx264",
            "[info] Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'corrupt_error_clip.mov':",
            "[info]   Duration: 00:00:40.00, start: 0.000000, bitrate: 12000 kb/s",
            "[info] Stream mapping:",
            "[out#0/null @ 0000] [info] video:1024KiB audio:0KiB",
        ]
        assert not any(_is_decode_evidence(line) for line in noise)

    def test_filter_output_is_not_a_decode_error(self):
        accounting = _DecodeAccounting()
        accounting.add("[blackdetect@v @ 000001] [info] black_start:10 black_end:16 black_duration:6")
        accounting.add("[silencedetect@a0 @ 000002] [info] silence_start: 10.0")
        assert accounting.total_errors == 0 and accounting.messages == []

    @pytest.mark.parametrize("line", [
        "[h264 @ 0000] [error] error while decoding MB 12 34, bytestream -7",
        "[h264 @ 0000] [error] Invalid NAL unit size (-1 > 100).",
        "[vist#0:0/h264 @ 0000] [dec:h264 @ 0001] [warning] corrupt decoded frame",
        "[in#0/mov @ 0000] [warning] corrupt input packet in stream 0",
    ])
    def test_real_errors_are_detected(self, line):
        assert _is_decode_evidence(line)


@needs_ffmpeg
class TestDeepScanEndToEnd:
    def test_black_frames_and_silence_are_detected(self, defect_clip):
        r = run_qa_checks(defect_clip, cfg=_cfg())
        r.finalize()

        black = _check(r, "Black Frames")
        assert black and black["status"] in ("FAIL", "WARN"), \
            f"black frames not detected: {black}"
        # Injected at 10s for 6s; the reported time must be absolute, and
        # every occurrence must be listed, not just the first.
        assert "00:00:10" in r.metadata["black_frame_timestamps"], r.metadata
        assert r.metadata["black_frame_sequences"] == 1
        assert 5.0 <= r.metadata["black_frame_total_seconds"] <= 7.0

        silence = _check(r, "Audio Silence")
        assert silence and silence["status"] in ("FAIL", "WARN"), \
            f"silence not detected: {silence}"
        assert "00:00:10" in r.metadata["silence_timestamps"], r.metadata

    def test_clean_file_has_no_false_positives(self, clean_clip):
        r = run_qa_checks(clean_clip, cfg=_cfg())
        r.finalize()

        assert r.status == "PASS", [c for c in r.checks if c[1] in ("FAIL", "WARN")]
        assert _check(r, "Black Frames")["status"] == "PASS"
        assert _check(r, "Audio Silence")["status"] == "PASS"
        assert _check(r, "File Integrity")["status"] == "PASS"

    def test_settings_actually_change_the_verdict(self, clean_clip):
        """The whole point of the config fix: overrides must reach the engine."""
        passing = run_qa_checks(clean_clip, cfg=_cfg(target_fps=30.0))
        passing.finalize()
        assert _check(passing, "Frame Rate")["status"] == "PASS"

        failing = run_qa_checks(clean_clip, cfg=_cfg(target_fps=60.0))
        failing.finalize()
        assert _check(failing, "Frame Rate")["status"] == "FAIL"
        assert failing.status == "MISMATCH"

    def test_resolution_override_is_honoured(self, clean_clip):
        r = run_qa_checks(clean_clip, cfg=_cfg(target_width=3840, target_height=2160))
        r.finalize()
        assert _check(r, "Resolution")["status"] in ("FAIL", "WARN")

    def test_bitrate_near_threshold_does_not_error(self, clean_clip):
        """A 10-25% bitrate deficit used to raise TypeError -> Internal Error."""
        r = run_qa_checks(clean_clip, cfg=_cfg(min_bitrate_mbps=3))
        r.finalize()
        assert _check(r, "Internal Error") is None, "scan raised an internal error"
        assert _check(r, "Bitrate") is not None

    def test_unicode_filename_scans(self, clean_clip, tmp_path):
        target = tmp_path / "caf\u00e9_\u65e5\u672c_\u2014_test.mp4"
        with open(clean_clip, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
        r = run_qa_checks(str(target), cfg=_cfg())
        r.finalize()
        assert _check(r, "Internal Error") is None
        assert r.status in ("PASS", "PASS_WITH_WARNINGS", "MISMATCH")

    def test_missing_file_is_handled(self, tmp_path):
        r = run_qa_checks(str(tmp_path / "nope.mov"), cfg=_cfg())
        r.finalize()
        assert r.status == "MISMATCH"
