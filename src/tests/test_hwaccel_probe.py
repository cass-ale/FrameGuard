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

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
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
# Tool registry and FFmpeg capability cache boundaries
# ---------------------------------------------------------------------------

class _ToolResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _capability_run(args, **_kwargs):
    if "-filters" in args:
        return _ToolResult("libvmaf signalstats ebur128")
    return _ToolResult("Hardware acceleration methods:\nvideotoolbox\n")


class TestToolCapabilityCache:
    @pytest.fixture(autouse=True)
    def _clean_tool_state(self):
        from frameguard import tools

        old_registry = tools._registry
        tools._registry = None
        tools._caps_cache.clear()
        yield
        tools._registry = old_registry
        tools._caps_cache.clear()

    def test_nested_capability_values_are_defensive_copies(self, monkeypatch):
        from frameguard import tools

        monkeypatch.setattr(tools.subprocess, "run", _capability_run)
        first = tools.detect_ffmpeg_capabilities("/fake/ffmpeg")
        first["hwaccel_methods"].append("caller-mutation")

        second = tools.detect_ffmpeg_capabilities("/fake/ffmpeg")
        assert second["hwaccel_methods"] == ["videotoolbox"]

    def test_nonzero_probe_is_transient_and_not_cached(self, monkeypatch):
        from frameguard import tools

        failed = True
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if failed:
                return _ToolResult(returncode=1)
            return _capability_run(args, **kwargs)

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        first = tools.detect_ffmpeg_capabilities("/fake/ffmpeg")
        assert first["has_libvmaf"] is False
        assert tools._caps_cache == {}

        failed = False
        second = tools.detect_ffmpeg_capabilities("/fake/ffmpeg")
        assert second["has_libvmaf"] is True
        assert len(calls) >= 3

    def test_force_refresh_finds_tool_that_appears_mid_session(self, monkeypatch):
        from frameguard import tools

        present = False

        def fake_detect(name, required=True):
            info = tools.ToolInfo(name, required=required)
            if present:
                info.found = True
                info.path = f"/new/{name}"
                info.version = "1.0"
                info.version_tuple = (1, 0)
                info.meets_minimum = True
            return info

        monkeypatch.setattr(tools, "_detect_tool", fake_detect)
        assert tools.check_all_tools().ffmpeg.path is None
        present = True
        refreshed = tools.check_all_tools(force_refresh=True)
        assert refreshed.ffmpeg.path == "/new/ffmpeg"

    def test_binary_replacement_invalidates_capability_cache(self, monkeypatch, tmp_path):
        from frameguard import tools

        binary = tmp_path / "ffmpeg"
        binary.write_bytes(b"first")
        binary.chmod(0o755)
        generation = {"filters": "signalstats"}
        calls = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            if "-filters" in args:
                return _ToolResult(generation["filters"])
            return _ToolResult("Hardware acceleration methods:\n")

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        first = tools.detect_ffmpeg_capabilities(str(binary))
        assert first["has_signalstats"] is True
        assert first["has_libvmaf"] is False

        binary.write_bytes(b"second-generation-is-different")
        generation["filters"] = "libvmaf"
        second = tools.detect_ffmpeg_capabilities(str(binary))
        assert second["has_libvmaf"] is True
        assert len(calls) == 4

    @pytest.mark.parametrize("name", ["ffmpeg", "mediaconch", "mediainfo"])
    def test_empty_version_output_is_a_failed_probe_not_an_exception(
            self, monkeypatch, name):
        from frameguard import tools

        monkeypatch.setattr(
            tools.subprocess, "run", lambda *args, **kwargs: _ToolResult(""))
        assert tools._get_tool_version(f"/fake/{name}", name) == (None, ())

    def test_concurrent_capability_requests_share_one_probe(self, monkeypatch):
        from frameguard import tools

        calls = []
        calls_lock = threading.Lock()

        def slow_run(args, **kwargs):
            with calls_lock:
                calls.append(args)
            time.sleep(0.02)
            return _capability_run(args, **kwargs)

        monkeypatch.setattr(tools.subprocess, "run", slow_run)
        barrier = threading.Barrier(6)
        results = []

        def probe():
            barrier.wait()
            results.append(tools.detect_ffmpeg_capabilities("/fake/ffmpeg"))

        threads = [threading.Thread(target=probe) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        assert len(results) == 6
        assert len(calls) == 2
        assert all(r == results[0] for r in results)


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
        """Patch capability detection + probing for one selection call.

        get_ffmpeg_path is patched to a dummy path as well: selection only
        consults the probe when it can resolve an ffmpeg binary, so on a
        clean machine with no ffmpeg installed (the Windows CI runner) the
        mocked probe was silently bypassed and the compile-time list was
        trusted — the exact failure these tests exist to pin. Patching the
        path makes the tests hermetic in both environments.
        """
        caps = mock.patch.object(
            core, "detect_ffmpeg_capabilities",
            return_value={"hwaccel_methods": methods})
        probe = mock.patch.object(
            core, "_probe_hwaccel",
            side_effect=lambda path, method, sample: probe_results[method])
        path = mock.patch.object(
            core, "get_ffmpeg_path", return_value="/fake/ffmpeg")
        return caps, probe, path

    def test_compiled_but_deviceless_method_is_skipped(self):
        """cuda is compiled in but the probe decode fails -> next method."""
        caps, probe, path = self._with_caps(
            ["cuda", "vaapi"], {"cuda": False, "vaapi": True})
        with caps, probe, path:
            args = _get_optimal_hwaccel(_cfg(), probe_file="clip.mp4")
        assert args == ["-hwaccel", "vaapi"]

    def test_all_methods_failing_falls_back_to_software(self):
        """Every candidate fails its probe -> no hwaccel args at all."""
        caps, probe, path = self._with_caps(
            ["cuda", "vaapi"], {"cuda": False, "vaapi": False, "auto": False})
        with caps, probe, path:
            args = _get_optimal_hwaccel(_cfg(), probe_file="clip.mp4")
        assert args == []

    def test_known_failed_method_is_never_reoffered(self):
        core._hwaccel_failed_methods.add("cuda")
        caps, probe, path = self._with_caps(["cuda", "vaapi"], {"vaapi": True})
        with caps, probe, path:
            args = _get_optimal_hwaccel(_cfg(), probe_file="clip.mp4")
        assert args == ["-hwaccel", "vaapi"]

    def test_explicit_method_that_cannot_decode_falls_back_to_software(self):
        with mock.patch.object(core, "get_ffmpeg_path",
                               return_value="/fake/ffmpeg"), \
             mock.patch.object(core, "_probe_hwaccel", return_value=False):
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
        self.stderr = io.BytesIO(stderr.encode("utf-8"))
        self.pid = 4242

    def wait(self, timeout=None):
        return self.returncode

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
