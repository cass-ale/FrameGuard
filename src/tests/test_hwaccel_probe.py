"""Regression tests for the October 2026 bug sweep.

Headline defect: ``ffmpeg -hwaccels`` lists the methods the binary was
COMPILED with, not the devices present. On a machine whose FFmpeg lists
``cuda`` but which has no NVIDIA GPU, the deep scan selected
``-hwaccel cuda``, FFmpeg exited 255 having decoded zero frames, the
failure lines were scrubbed as "hwaccel noise", and every frame-level
check reported a clean pass. These tests pin the probe-based selection,
the return-code backstops, the update-manifest path-escape refusal, and
the version validator's coverage of every version literal that ships.
"""

import json
import os
import re
import subprocess
import sys
from unittest import mock

import pytest

import frameguard.core as core
from frameguard.core import (
    DEFAULT_CONFIG,
    QAResult,
    _get_optimal_hwaccel,
    _run_segment_filter_scan,
    _stderr_shows_device_failure,
    _stream_rotation_degrees,
    check_av_duration_mismatch,
    check_resolution,
)
from frameguard import manifest as M

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    return cfg


@pytest.fixture(autouse=True)
def _clean_hwaccel_state():
    core._hwaccel_failed_methods.clear()
    core._hwaccel_probe_cache.clear()
    yield
    core._hwaccel_failed_methods.clear()
    core._hwaccel_probe_cache.clear()


# ---------------------------------------------------------------------------
# Device-failure signatures
# ---------------------------------------------------------------------------

class TestDeviceFailureSignatures:
    def test_cuda_failure_is_recognised(self):
        stderr = ("[vist#0:0/h264 @ 0x0] [dec:h264 @ 0x0] No device available "
                  "for decoder: device type cuda needed for codec h264.\n"
                  "Device creation failed: -1.\n")
        assert _stderr_shows_device_failure(stderr)

    def test_clean_stderr_is_not_a_device_failure(self):
        assert not _stderr_shows_device_failure(
            "[blackdetect @ 0x0] black_start:1 black_end:2 black_duration:1")
        assert not _stderr_shows_device_failure("")


# ---------------------------------------------------------------------------
# Hwaccel selection: compile-time list must not be trusted
# ---------------------------------------------------------------------------

class TestHwaccelSelection:
    def _with_caps(self, methods, probe_results):
        """Patch capability detection + probing for one selection call."""
        caps = mock.patch.object(
            core, "detect_ffmpeg_capabilities",
            return_value={"hwaccel_methods": methods})
        probe = mock.patch.object(
            core, "_probe_hwaccel",
            side_effect=lambda path, method, sample: probe_results[method])
        return caps, probe

    def test_compiled_but_deviceless_method_is_skipped(self):
        """cuda is compiled in but the probe decode fails -> next method."""
        caps, probe = self._with_caps(
            ["cuda", "vaapi"], {"cuda": False, "vaapi": True})
        with caps, probe:
            args = _get_optimal_hwaccel(_cfg(), probe_file="clip.mp4")
        assert args == ["-hwaccel", "vaapi"]

    def test_all_methods_failing_falls_back_to_software(self):
        """Every candidate fails its probe -> no hwaccel args at all."""
        caps, probe = self._with_caps(
            ["cuda", "vaapi"], {"cuda": False, "vaapi": False, "auto": False})
        with caps, probe:
            args = _get_optimal_hwaccel(_cfg(), probe_file="clip.mp4")
        assert args == []

    def test_known_failed_method_is_never_reoffered(self):
        core._hwaccel_failed_methods.add("cuda")
        caps, probe = self._with_caps(["cuda", "vaapi"], {"vaapi": True})
        with caps, probe:
            args = _get_optimal_hwaccel(_cfg(), probe_file="clip.mp4")
        assert args == ["-hwaccel", "vaapi"]

    def test_explicit_method_that_cannot_decode_falls_back_to_software(self):
        with mock.patch.object(core, "_probe_hwaccel", return_value=False):
            args = _get_optimal_hwaccel(
                _cfg(hwaccel_method="cuda"), probe_file="clip.mp4")
        assert args == []

    def test_hwaccel_disabled_stays_disabled(self):
        assert _get_optimal_hwaccel(_cfg(use_hwaccel=False),
                                    probe_file="clip.mp4") == []


# ---------------------------------------------------------------------------
# Segment scan: a dead decode must be a failed segment, never a clean one
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self._stderr = stderr
        self.pid = 4242

    def communicate(self, timeout=None):
        return "", self._stderr

    def kill(self):
        pass


class TestSegmentReturnCodes:
    def _run_with_proc(self, proc, cfg=None):
        with mock.patch("frameguard.core.get_ffmpeg_path",
                        return_value="/usr/bin/ffmpeg"), \
             mock.patch("frameguard.core._probe_hwaccel", return_value=True), \
             mock.patch("frameguard.core.subprocess.Popen",
                        return_value=proc):
            return _run_segment_filter_scan(
                "clip.mp4", 0, 10, "blackdetect=d=0.5", None, False,
                cfg or _cfg(hwaccel_method="cuda"))

    def test_device_failure_returns_none_and_blacklists_method(self):
        proc = _FakeProc(255, "Device creation failed: -1.\n"
                              "No device available for decoder")
        assert self._run_with_proc(proc) is None
        assert "cuda" in core._hwaccel_failed_methods

    def test_zero_frames_decoded_returns_none(self):
        proc = _FakeProc(1, "Error opening input: Invalid data found")
        assert self._run_with_proc(proc) is None

    def test_partial_decode_keeps_its_findings(self):
        stderr = ("[blackdetect @ 0x0] black_start:1.0 black_end:3.0 "
                  "black_duration:2.0\nframe=  100 fps=30")
        proc = _FakeProc(1, stderr)
        assert self._run_with_proc(proc) == stderr

    def test_clean_segment_still_returns_stderr(self):
        proc = _FakeProc(0, "frame=  300 fps=30")
        assert self._run_with_proc(proc) == "frame=  300 fps=30"


# ---------------------------------------------------------------------------
# Update manifests: paths must stay inside the release tree
# ---------------------------------------------------------------------------

class TestManifestPathSafety:
    @pytest.mark.parametrize("rel", [
        "../evil.dll", "a/../../evil.dll", "/abs/path.dll",
        "C:/evil.dll", "a\\..\\evil.dll", "", "a//b.dll", "./a.dll",
    ])
    def test_unsafe_paths_are_rejected(self, rel):
        assert not M._is_safe_rel_path(rel)

    @pytest.mark.parametrize("rel", [
        "FrameGuard.exe", "_internal/PySide6/QtCore.pyd", "tools/ffmpeg.exe",
    ])
    def test_legitimate_paths_are_accepted(self, rel):
        assert M._is_safe_rel_path(rel)

    def test_load_manifest_refuses_escaping_paths(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({
            "manifest_version": 1,
            "files": {"../evil.dll": {"sha256": "0" * 64, "size": 1}},
        }), encoding="utf-8")
        assert M.load_manifest(str(path)) is None

    def test_load_manifest_accepts_a_normal_manifest(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps({
            "manifest_version": 1,
            "files": {"FrameGuard.exe": {"sha256": "0" * 64, "size": 1}},
        }), encoding="utf-8")
        assert M.load_manifest(str(path)) is not None


# ---------------------------------------------------------------------------
# Rotation: judge the displayed frame, not the stored frame
# (research finding: ffprobe reports stored dims; players autorotate)
# ---------------------------------------------------------------------------

def _status(result, name):
    for check in result.checks:
        if check[0] == name:
            return check[1]
    return None


class TestRotationAwareResolution:
    def _probe(self, w, h, **stream_kw):
        stream = {"codec_type": "video", "width": w, "height": h}
        stream.update(stream_kw)
        return {"streams": [stream]}

    def test_rotation_degrees_from_side_data_and_tag(self):
        assert _stream_rotation_degrees(
            {"side_data_list": [{"side_data_type": "Display Matrix",
                                 "rotation": -90}]}) == -90
        assert _stream_rotation_degrees({"tags": {"rotate": "90"}}) == 90
        assert _stream_rotation_degrees({}) == 0

    def test_rotated_portrait_capture_fails_a_landscape_target(self):
        """Stored 1920x1080 with -90 rotation displays as 1080x1920."""
        r = QAResult("phone.mov")
        probe = self._probe(1920, 1080, side_data_list=[
            {"side_data_type": "Display Matrix", "rotation": -90}])
        check_resolution(probe, r, _cfg(target_width=1920, target_height=1080))
        assert _status(r, "Resolution") == "FAIL"
        assert r.metadata["rotation"] == -90
        assert r.metadata["width"] == 1080

    def test_unrotated_landscape_still_passes(self):
        r = QAResult("master.mov")
        check_resolution(self._probe(1920, 1080), r,
                         _cfg(target_width=1920, target_height=1080))
        assert _status(r, "Resolution") == "PASS"

    def test_180_rotation_does_not_swap_axes(self):
        r = QAResult("flipped.mov")
        probe = self._probe(1920, 1080, side_data_list=[
            {"side_data_type": "Display Matrix", "rotation": 180}])
        check_resolution(probe, r, _cfg(target_width=1920, target_height=1080))
        assert _status(r, "Resolution") == "PASS"


# ---------------------------------------------------------------------------
# A/V duration: compare stream ends (start_time + duration), not durations
# (research finding: duration subtraction ignores per-stream start offsets
# and AAC priming, reading both as phantom mismatch)
# ---------------------------------------------------------------------------

class TestAVDurationAlignment:
    def _probe(self, v_dur, a_dur, v_start=None, a_start=None):
        video = {"codec_type": "video", "duration": str(v_dur)}
        audio = {"codec_type": "audio", "duration": str(a_dur)}
        if v_start is not None:
            video["start_time"] = str(v_start)
        if a_start is not None:
            audio["start_time"] = str(a_start)
        return {"streams": [video, audio]}

    def test_offset_starts_with_matching_ends_pass(self):
        """Audio starts 0.4s late and is 0.4s shorter: same end, in sync.
        The old duration-only comparison warned on this file."""
        r = QAResult("offset.mov")
        check_av_duration_mismatch(
            self._probe(10.0, 9.6, v_start=0.0, a_start=0.4), r, _cfg())
        assert _status(r, "A/V Duration") == "PASS"
        assert r.metadata["av_duration_diff"] == 0.0

    def test_equal_durations_with_shifted_audio_fail_to_warn_tier(self):
        """Equal durations but audio shifted 0.8s: ends differ by 0.8s,
        which the old comparison scored as a perfect match."""
        r = QAResult("shifted.mov")
        check_av_duration_mismatch(
            self._probe(10.0, 10.0, v_start=0.0, a_start=0.8), r, _cfg())
        assert _status(r, "A/V Duration") == "WARN"
        assert r.metadata["av_duration_diff"] == 0.8

    def test_missing_start_time_falls_back_to_duration_diff(self):
        r = QAResult("legacy.mov")
        check_av_duration_mismatch(self._probe(10.0, 10.0), r, _cfg())
        assert _status(r, "A/V Duration") == "PASS"


# ---------------------------------------------------------------------------
# Version validator: every literal that ships must agree
# ---------------------------------------------------------------------------

class TestVersionValidatorCoverage:
    def _run_validator(self, cwd):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "validate_version.py")],
            cwd=cwd, capture_output=True, text=True, timeout=60)

    def test_current_tree_is_consistent(self):
        result = self._run_validator(ROOT)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_stale_version_literal_is_caught(self, tmp_path):
        """0.5.5 shipped with installer labels still saying 0.5.4 while the
        validator only read AppVersion= and passed. The literal scan must
        catch that class of drift. (The original target, a VersionInfoVersion
        literal in installer.iss, no longer exists: installer versions are
        now routed through AppVersion via ISPP. The spec still carries
        literal FileVersion/ProductVersion strings, so the drift class is
        exercised there — located generically, so the test does not rot
        again at the next literal change.)"""
        (tmp_path / "frameguard").mkdir()
        for rel in ("frameguard/__init__.py", "frameguard.spec",
                    "installer.iss", "version.json"):
            src = os.path.join(ROOT, rel)
            dst = tmp_path / rel
            with open(src, encoding="utf-8") as f:
                text = f.read()
            if rel == "frameguard.spec":
                text, n = re.subn(r"\d+\.\d+\.\d+\.\d+", "9.9.9.9",
                                  text, count=1)
                assert n == 1, "spec carries no four-part version literal"
            dst.write_text(text, encoding="utf-8")
        result = self._run_validator(str(tmp_path))
        assert result.returncode == 1
        assert "9.9.9.9" in result.stdout
