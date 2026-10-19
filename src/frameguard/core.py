"""
FrameGuard Desktop - Core Scan Engine
Lead Developer: Cassian Aléria
Copyright (c) 2026 Cassian Aléria. MIT License.
"""

# Single source of truth for the version; this module used to hard-code
# "0.4.4" while the app reported 0.5.x, which made the updater think every
# build was out of date.
import frameguard as _package
__version__ = _package.__version__

import subprocess
import codecs
import json
import math
import os
import re
import threading
from collections import deque
from typing import Optional, List, Dict, Tuple, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

# Live handles remain observable for diagnostics, but cancellation never reads
# this global set. Every cancellable scan subprocess is registered explicitly
# with the ScanWorker that owns it.
_live_procs: set = set()
_live_procs_lock = threading.Lock()


def _track_proc(proc) -> None:
    if proc is None:
        return
    with _live_procs_lock:
        _live_procs.add(proc)


def _untrack_proc(proc) -> None:
    if proc is None:
        return
    with _live_procs_lock:
        _live_procs.discard(proc)


def get_live_procs() -> list:
    """Snapshot of live scan subprocesses for diagnostics only."""
    with _live_procs_lock:
        return list(_live_procs)


def _register_owned_proc(proc, process_owner=None) -> None:
    _track_proc(proc)
    if process_owner is not None:
        process_owner.register_proc(proc)


def _unregister_owned_proc(proc, process_owner=None) -> None:
    _untrack_proc(proc)
    if process_owner is not None:
        process_owner.unregister_proc(proc)


def _run_owned_subprocess(cmd: list, timeout: float, process_owner=None,
                          cancel_event: Optional[threading.Event] = None):
    """Run a capture subprocess registered to one scan owner."""
    if cancel_event is not None and cancel_event.is_set():
        return None
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_get_clean_env(), **_subprocess_kwargs())
        _register_owned_proc(proc, process_owner)
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        raise
    finally:
        _unregister_owned_proc(proc, process_owner)

# Tool detection (optional — graceful fallback if tools.py not available)
try:
    from frameguard.tools import (
        get_ffmpeg_path, get_ffprobe_path,
        get_mediaconch_path, get_mediainfo_path,
        detect_ffmpeg_capabilities, get_xr_policy_path
    )
    _HAS_TOOLS_MODULE = True
except ImportError:
    _HAS_TOOLS_MODULE = False

    # Provide minimal fallbacks so callers can still use these APIs.
    def get_ffmpeg_path():
        return "ffmpeg"

    def get_ffprobe_path():
        return "ffprobe"

    def get_mediaconch_path():
        return None

    def get_mediainfo_path():
        return None

    def detect_ffmpeg_capabilities(*args, **kwargs):
        return {}

    def get_xr_policy_path():
        return ""


# ============================================================================
# PRESET PROFILES
# ============================================================================

PRESETS = {
    "4k30": {"label": "4K UHD @ 30fps", "width": 3840, "height": 2160, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 20},
    "4k60": {"label": "4K UHD @ 60fps", "width": 3840, "height": 2160, "fps": 60.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 30},
    "4k24": {"label": "4K UHD @ 24fps (Cinematic)", "width": 3840, "height": 2160, "fps": 24.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 18},
    "1080p30": {"label": "1080p Full HD @ 30fps", "width": 1920, "height": 1080, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 8},
    "1080p60": {"label": "1080p Full HD @ 60fps", "width": 1920, "height": 1080, "fps": 60.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 12},
    "1080p24": {"label": "1080p Full HD @ 24fps (Cinematic)", "width": 1920, "height": 1080, "fps": 24.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 6},
    "720p30": {"label": "720p HD @ 30fps (Proxy/Review)", "width": 1280, "height": 720, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 4},
    "5.7k30": {"label": "5.7K @ 30fps (360 VR Capture)", "width": 5760, "height": 2880, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 40},
    "8k30": {"label": "8K UHD @ 30fps (High-End VR)", "width": 7680, "height": 4320, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 60},
}

# ---------------------------------------------------------------------------
# CHECK CLASSIFICATION (v0.4.4)
# ---------------------------------------------------------------------------
# Essential checks determine overall PASS/MISMATCH status.
# Advisory checks show as yellow caution but don't affect overall status.
# Removed checks are disabled but code is preserved for future re-enablement.

ESSENTIAL_CHECKS = {
    "Container Format", "Video Codec", "Resolution", "Frame Rate",
    "CFR Check", "Bitrate", "Audio Stream", "Audio Codec",
    "Audio Channels", "A/V Duration",
    "Audio Silence",
}

ADVISORY_CHECKS = {
    "Decode Integrity", "Frame Count", "Black Frames",
    "Freeze Frames",
    # Raised when part of the file could not be scanned. Previously in neither
    # set, which meant overall_status() ignored it and a partially-scanned file
    # was indistinguishable from a fully clean one.
    "Deep Scan", "Filter Scan",
}

# Checks removed in v0.4.4 (can be re-enabled via config if needed):
# - "Color Space": validates rec709/bt2020 color metadata
# - "Pixel Aspect Ratio": validates SAR is 1:1 (square pixels)
# - "Audio Sample Rate": validates 48kHz sample rate
# - "MediaConch Policy": validates against XR delivery XML policy
REMOVED_CHECKS = {"Color Space", "Pixel Aspect Ratio", "Audio Sample Rate", "MediaConch Policy", "Naming Convention",
}

# ---------------------------------------------------------------------------
# CHECK SEVERITY WEIGHTS (visual hierarchy)
# ---------------------------------------------------------------------------
# A corrupted/unreadable file must read as more urgent than a file whose only
# problem is an off-spec framerate. overall_status() collapses everything to
# three buckets (MISMATCH / WARN / PASS), so the GUI sorts within those
# buckets by this weight x confidence. Weights are 0-100; higher = more
# severe. Checks not listed here default to 50.
CHECK_SEVERITY_WEIGHTS = {
    # Unreadable / corrupt: nothing else matters until this is fixed.
    "Container Format": 100,
    "FFprobe": 100,
    "Internal Error": 95,
    "Decode Integrity": 85,
    # Structural conformance: fails delivery spec.
    "Video Codec": 75,
    "Resolution": 70,
    "A/V Duration": 65,
    "Bitrate": 60,
    "Audio Stream": 60,
    "Audio Channels": 55,
    "Frame Count": 50,
    "CFR Check": 45,
    "Filter Scan": 45,
    "Deep Scan": 40,
    # Content advisories: visible defects, usually fixable in edit.
    "Freeze Frames": 38,
    "Black Frames": 35,
    "Audio Silence": 30,
    "Audio Codec": 25,
    "Frame Rate": 20,
}


def check_severity(check_name: str, status: str, confidence: int = 100) -> float:
    """Severity score for one check entry: weight x confidence.

    FAIL counts full, WARN counts half (same defect, less certainty about
    impact), PASS/SKIP count zero. Lets a confident Decode Integrity FAIL
    outrank a marginal Frame Rate WARN instead of sharing one bucket.
    """
    base = CHECK_SEVERITY_WEIGHTS.get(check_name, 50)
    conf = max(0, min(100, confidence or 0)) / 100.0
    if status == "FAIL":
        return base * conf
    if status == "WARN":
        return base * conf * 0.5
    return 0.0


def result_severity(result: "QAResult") -> tuple:
    """(score, top_check_name) for a whole file: max over its checks.

    The GUI sorts failures-first, then by this score, so a corrupted file
    (weight 100) always lists above a framerate-only file (weight 20).
    """
    best = 0.0
    top = ""
    for entry in getattr(result, "checks", []):
        name = entry[0] if len(entry) > 0 else ""
        status = entry[1] if len(entry) > 1 else ""
        conf = entry[3] if len(entry) > 3 else 100
        score = check_severity(name, status, conf)
        if score > best:
            best, top = score, name
    return round(best, 1), top




# ============================================================================
# DEFAULT CONFIGURATION
# ============================================================================

DEFAULT_CONFIG = {
    # Resolution & frame rate
    "target_width": 3840,
    "target_height": 2160,
    "target_fps": 30.0,
    "fps_tolerance": 0.5,
    "min_bitrate_mbps": 20,

    # Codec acceptance lists
    "accepted_codecs": ["h264", "hevc", "h265", "av1", "prores", "prores_ks", "prores_aw"],
    "accepted_containers": [".mp4", ".mov", ".mkv", ".mxf"],
    "accepted_audio_codecs": ["aac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_s24be", "pcm_s32be", "pcm_f32le", "opus", "flac"],

    # Audio
    "audio_required": True,
    "min_audio_sample_rate": 44100,

    # CFR / VFR
    "require_cfr": True,
    "vfr_threshold": 0.02,

    # Naming convention
    "naming_pattern": r"^[A-Z]{2,5}_[A-Za-z0-9]+_[A-Z]+_\d{8}_C\d{3,4}\.\w{3,4}$",
    "naming_example": "PROJ_CAM01_TAKE_20260319_C004.mp4",

    # File discovery
    "video_extensions": {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm"},

    # Performance
    "max_workers": min(os.cpu_count() or 4, 4),
    "use_hwaccel": True,
    "hwaccel_method": "auto",

    # Segment-based parallel deep scan
    "segment_enabled": True,
    "segment_min_duration": 30.0,
    # Capped at 4: 8 concurrent 4K ffmpeg decodes OOM'd / crashed Windows.
    # See run_deep_scan max_workers cap below.
    "segment_count": min(os.cpu_count() or 4, 4),

    # Deep-scan safety bounds (Phase A hardening)
    "deep_scan_max_file_gb": 8.0,
    "deep_scan_max_duration_s": 7200.0,
    "deep_scan_max_stderr_lines": 5000,
    "deep_scan_max_stderr_chars": 512 * 1024,

    # Deep scan thresholds
    "black_detect_duration": 0.5,
    "black_detect_pix_threshold": 0.10,
    "freeze_detect_noise": 0.003,
    "freeze_detect_duration": 2.0,
    "silence_noise_db": -50,
    "silence_duration": 2.0,

    # Loudness (EBU R128)
    "target_loudness_lufs": -23.0,
    "loudness_tolerance_lu": 2.0,
    "true_peak_max_dbtp": -1.0,

    # Duration thresholds
    "min_duration_fail": 5.0,
    "min_duration_warn": 30.0,

    # A/V sync
    "duration_mismatch_threshold": 0.5,

    # Timeouts
    "timeout_base_seconds": 120,
    "timeout_multiplier": 3.0,
    "timeout_max_seconds": 7200,

    # Multi-tool integration (v0.3)
    "use_mediaconch": True,
    "use_mediainfo": True,
    "use_signalstats": True,
    "signalstats_sample_seconds": 10,
    "signalstats_decimate_fps": 5,
    "signalstats_num_samples": 3,
    "quality_ymin_floor": 16,
    "quality_ymax_ceil": 235,
    "quality_satavg_min": 5,

    # v0.4.2 Performance tuning
    "filter_threads": min(os.cpu_count() or 4, 8),
}

# Cached FFmpeg capabilities (populated on first use)
_ffmpeg_caps_cache: Optional[dict] = None


def _get_clean_env() -> dict:
    """Return a sanitized environment for subprocess calls.

    PyInstaller modifies LD_LIBRARY_PATH (Linux), DYLD_LIBRARY_PATH (macOS),
    and DLL search paths (Windows) to prefer bundled libs. External programs
    like FFmpeg, MediaConch, MediaInfo need their own libs, so we restore
    the original environment variables.
    """
    import sys as _sys
    env = os.environ.copy()
    if getattr(_sys, 'frozen', False):
        # Linux: restore original LD_LIBRARY_PATH
        if 'LD_LIBRARY_PATH_ORIG' in env:
            env['LD_LIBRARY_PATH'] = env['LD_LIBRARY_PATH_ORIG']
        elif 'LD_LIBRARY_PATH' in env:
            del env['LD_LIBRARY_PATH']
        # macOS: restore original DYLD_LIBRARY_PATH
        if 'DYLD_LIBRARY_PATH_ORIG' in env:
            env['DYLD_LIBRARY_PATH'] = env['DYLD_LIBRARY_PATH_ORIG']
        elif 'DYLD_LIBRARY_PATH' in env:
            del env['DYLD_LIBRARY_PATH']
    return env


def _subprocess_kwargs() -> dict:
    """Return the kwargs every FFmpeg/FFprobe/MediaInfo invocation needs.

    - Hides the console window on Windows (no flashing black terminals).
    - Forces UTF-8 decoding with replacement. FFmpeg emits UTF-8, but text=True
      defaults to the locale encoding (cp1252 on most Windows installs) with
      strict errors, so a non-ASCII filename or a '©' in the banner raises
      UnicodeDecodeError — which escapes as an "Internal Error" mid-scan, or
      crashes the app outright during the startup FFmpeg check.
    """
    kwargs = {"encoding": "utf-8", "errors": "replace"}
    if os.name == 'nt':  # Windows
        # CREATE_NO_WINDOW = 0x08000000
        kwargs['creationflags'] = 0x08000000
        # Belt-and-suspenders: also set startupinfo
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs['startupinfo'] = si
    return kwargs


# Hwaccel noise patterns to filter from decode stderr.
# Keep these SPECIFIC: bare tokens like "display", "cuda" or "Cannot open" also
# match genuine decode errors ("Cannot open file", "Discarding invalid packet"),
# which would be silently dropped and reported as a clean decode.
HWACCEL_NOISE_PATTERNS = [
    "Cannot load", "Could not dynamically load", "Device creation failed",
    "Failed to initialise", "Failed to initialize", "No device available",
    "hwaccel", "hw_device", "device_create",
    "libcuda", "libnvidia", "libvdpau", "libva",
    "No VAAPI", "No CUDA", "No VA display",
    "vaInitialize failed", "AVHWDeviceContext", "init_device_type",
    "Cannot open display", "X11 display", "DRM render node", "dri/renderD",
    "Cannot open the device", "Failed to create", "Failed to open",
    "does not support hwaccel", "hardware acceleration",
    "Error creating a NVDEC", "Cannot init CUDA", "Cannot open VDPAU",
    "vulkan", "opencl", "qsv device", "d3d11va", "dxva2", "videotoolbox device",
]


# ============================================================================
# QA RESULT
# ============================================================================

class QAResult:
    """Stores the QA result for a single video file."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self._engine = "FrameGuard by Cassian Aléria"
        self.checks: List[Tuple[str, str, str]] = []
        self.passed = True
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.metadata: Dict = {}
        self._lock = threading.Lock()
        self.status = None  # Set by finalize() after all checks complete
        self.severity_score = 0.0  # Set by finalize(); max weight x confidence
        self.severity_reason = ""  # Check name driving the score
        # Strict mode ("treat warnings as failures"). Recorded here — not
        # passed through finalize() — because the worker calls finalize()
        # with no arguments after run_qa_checks() returns.
        self.strict = False

    def add_pass(self, check_name: str, detail: str = "", confidence: int = 100):
        """confidence: 0-100, how certain this result is correct."""
        with self._lock:
            self.checks.append((check_name, "PASS", detail, confidence))

    def add_fail(self, check_name: str, detail: str = "", confidence: int = 100):
        """confidence: 0-100. <70 = marginal/borderline result."""
        with self._lock:
            self.checks.append((check_name, "FAIL", detail, confidence))
            self.errors.append(f"{check_name}: {detail}")
            self.passed = False

    def add_warn(self, check_name: str, detail: str = "", confidence: int = 100):
        """confidence: 0-100, how certain this result is correct."""
        with self._lock:
            self.checks.append((check_name, "WARN", detail, confidence))
            self.warnings.append(f"{check_name}: {detail}")

    def add_skip(self, check_name: str, detail: str = "", confidence: int = 100):
        with self._lock:
            self.checks.append((check_name, "SKIP", detail, confidence))

    def set_metadata(self, key, value):
        with self._lock:
            self.metadata[key] = value

    def plain_summary(self) -> str:
        """Return a brief, jargon-free one-line summary of what's wrong.
        Designed for operators who need quick, actionable feedback.
        """
        if self.passed and not self.warnings:
            return "All checks passed."

        # Map technical check names to plain-language descriptions
        # M3: Keep dictionary aligned with actual check names used in this module.
        _PLAIN = {
            "Container Format": "wrong file type",
            "Video Codec": "video uses an unsupported format",
            "Resolution": "video resolution doesn't match the target",
            "Frame Rate": "frame rate is off",
            "CFR Check": "frame rate is inconsistent (variable)",
            "CFR": "frame rate is inconsistent (variable)",  # legacy alias
            "Bitrate": "video quality (bitrate) is too low",
            "Audio Codec": "audio uses an unsupported format",
            "Audio Sample Rate": "audio sample rate is too low",
            "Audio Channels": "audio is missing or has no channels",
            "Audio Stream": "audio is missing",
            # "Duration"  # REMOVED v0.4.4: "video is too short",
            "Color Space": "color settings may look wrong on screen",
            "Naming Convention": "file name doesn't follow the naming rule",
            "A/V Duration": "audio and video lengths don't match",
            "A/V Duration Mismatch": "audio and video lengths don't match",  # legacy alias
            "Pixel Aspect Ratio": "pixels are non-square (image may look stretched)",
            "Frame Count": "frame count doesn't match the expected number",
            "Decode Integrity": "file may have encoding defects (dropped frames, decode errors) - does not always mean visible issues during playback",
            "Black Frames": "unexpected black frames detected",
            "Freeze Frames": "video freezes detected",
            "Audio Silence": "long stretches of silence found",
            "Audio Loudness": "audio levels are outside the target range",
            "MediaConch Policy": "file doesn't meet delivery format rules",
            # "Signal Quality"  # REMOVED v0.4.4: "video signal analysis detected anomalies (black levels, freeze frames, noise) - may not be visible during normal playback",
            "FFprobe": "file could not be read (may be corrupt)",
            "Internal Error": "an unexpected error occurred during the scan",
        }

        issues = []
        for check_name, status, *_ in self.checks:
            if status == "FAIL":
                plain = _PLAIN.get(check_name, check_name.lower() + " issue")
                issues.append(plain)

        if not issues:
            # Warnings only
            warn_names = []
            for check_name, status, *_ in self.checks:
                if status == "WARN":
                    plain = _PLAIN.get(check_name, check_name.lower() + " concern")
                    warn_names.append(plain)
            if warn_names:
                return "Heads up: " + ", ".join(warn_names) + "."
            return "Passed with minor notes."

        if len(issues) == 1:
            return issues[0][0].upper() + issues[0][1:] + "."
        else:
            return ", ".join(i.capitalize() for i in issues) + "."

    def overall_status(self, strict: bool = False) -> str:
        """Return the file's overall verdict.

        Returns:
            "PASS" - nothing flagged anything
            "PASS_WITH_WARNINGS" - something was flagged, but nothing disqualifying
            "MISMATCH" - an essential check failed with confidence

        Only a confident FAIL on an ESSENTIAL check (or a hard FFprobe/Internal
        Error) disqualifies a file. Everything else that is not a clean PASS
        downgrades to PASS_WITH_WARNINGS — including:

          * a WARN on an essential check. A 2000x2140 master warned against a
            4K target and this method ignored it entirely, so the file was
            reported as a clean PASS.
          * a low-confidence essential FAIL, which is advisory by design.
          * a FAIL or WARN on a check in neither classification set, such as
            the "Deep Scan" warning raised when segments time out. Those were
            silently dropped, so a scan that covered half the file looked
            identical to one that covered all of it.

        Checks in REMOVED_CHECKS are ignored: they are disabled features whose
        code is retained, and they must leave no trace in the verdict.

        When strict is true ("treat any warning as a failure"), any flagged
        check escalates to MISMATCH instead of PASS_WITH_WARNINGS.
        """
        strict = strict or bool(getattr(self, "strict", False))
        flagged = False

        for check_tuple in self.checks:
            name, status = check_tuple[0], check_tuple[1]
            confidence = check_tuple[3] if len(check_tuple) > 3 else 100

            if status in ("PASS", "SKIP") or name in REMOVED_CHECKS:
                continue

            if name in ("FFprobe", "Internal Error") and status == "FAIL":
                return "MISMATCH"

            if name in ESSENTIAL_CHECKS and status == "FAIL" and confidence >= 70:
                return "MISMATCH"

            if strict:
                return "MISMATCH"

            flagged = True

        return "PASS_WITH_WARNINGS" if flagged else "PASS"

    def finalize(self, strict: bool = False):
        """Compute and cache the overall status as a plain attribute.

        Call this AFTER all checks are complete. Avoids property descriptor
        issues when passing QAResult across PySide6 Signal threads.

        strict overrides the ``strict`` attribute recorded by
        run_qa_checks(); both exist because the worker finalizes with no
        arguments after the engine has already returned.
        """
        if strict:
            self.strict = True
        self.status = self.overall_status()  # Call method, cache as plain attr cache as plain attr
        try:
            score, top = result_severity(self)
        except Exception:
            score, top = 0.0, ""
        self.severity_score = score
        self.severity_reason = top
        self.set_metadata("severity_score", score)
        if top:
            self.set_metadata("severity_reason", top)


    
    def has_advisory_warnings(self) -> bool:
        """True if any advisory checks flagged issues."""
        return any(name in ADVISORY_CHECKS and status in ("FAIL", "WARN")
                   for name, status, *_ in self.checks)

# ============================================================================
# FFPROBE UTILITIES
# ============================================================================



def run_ffprobe(filepath: str, process_owner=None,
                cancel_event: Optional[threading.Event] = None) -> Optional[dict]:
    """Run FFprobe on a file and return parsed JSON output."""
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        return None
    # start_time feeds the A/V duration check (streams must be aligned by
    # their start offsets before their ends are compared), and the side-data
    # rotation / legacy rotate tag feed the resolution check (a stored
    # 1920x1080 frame with 90-degree rotation displays as 1080x1920).
    entries = (
        "stream=codec_name,codec_type,width,height,r_frame_rate,"
        "avg_frame_rate,nb_frames,duration,start_time,bit_rate,sample_rate,"
        "channels,channel_layout,pix_fmt,color_space,color_transfer,"
        "color_primaries,field_order,sample_aspect_ratio,display_aspect_ratio"
        ":stream_side_data=side_data_type,rotation"
        ":stream_tags=rotate"
    )
    legacy_entries = (
        "stream=codec_name,codec_type,width,height,r_frame_rate,"
        "avg_frame_rate,nb_frames,duration,bit_rate,sample_rate,"
        "channels,channel_layout,pix_fmt,color_space,color_transfer,"
        "color_primaries,field_order,sample_aspect_ratio,display_aspect_ratio"
    )
    base_cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", "-show_entries",
    ]
    try:
        result = _run_owned_subprocess(
            base_cmd + [entries, filepath], 120, process_owner, cancel_event)
        if result is None:
            return None
        if result.returncode != 0:
            # An ffprobe too old to know the side-data section must not take
            # probing down with it: retry with the original field list.
            result = _run_owned_subprocess(
                base_cmd + [legacy_entries, filepath], 120,
                process_owner, cancel_event)
        if result is None or result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, TypeError, OSError):
        return None


def _safe_float(value, default=None):
    """Return a finite float for scalar metadata, otherwise ``default``."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value, default=None):
    """Return an integer for scalar metadata, otherwise ``default``."""
    parsed = _safe_float(value)
    if parsed is None or not parsed.is_integer():
        return default
    try:
        return int(parsed)
    except (ValueError, OverflowError):
        return default


def parse_frame_rate(rate_str: str) -> float:
    if not rate_str or rate_str == "0/0":
        return 0.0
    if isinstance(rate_str, str):
        parts = rate_str.split("/")
        if len(parts) == 2:
            num = _safe_float(parts[0])
            den = _safe_float(parts[1])
            if num is not None and den not in (None, 0):
                return round(num / den, 4)
    return _safe_float(rate_str, 0.0)


def get_file_duration(filepath: str, process_owner=None,
                      cancel_event: Optional[threading.Event] = None) -> float:
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        return 0.0
    cmd = [
        ffprobe, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        filepath
    ]
    try:
        result = _run_owned_subprocess(
            cmd, 60, process_owner, cancel_event)
        if result is None or result.returncode != 0:
            return 0.0
        return _safe_float(result.stdout.strip(), 0.0)
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError, TypeError, OSError):
        return 0.0


def _calc_timeout(duration: float, cfg: dict) -> float:
    timeout = cfg["timeout_base_seconds"] + (cfg["timeout_multiplier"] * duration)
    return min(timeout, cfg["timeout_max_seconds"])


def check_ffmpeg_available() -> Tuple[bool, str]:
    """Check if FFmpeg and FFprobe are available. Returns (ok, message)."""
    tools = [get_ffmpeg_path(), get_ffprobe_path()]
    for tool in tools:
        if not tool:
            return False, "FFmpeg or FFprobe not found. Reinstall or relink the analysis tools."
        try:
            result = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=10, env=_get_clean_env(), **_subprocess_kwargs())
            if result.returncode != 0:
                return False, f"{tool} found but returned error"
        except FileNotFoundError:
            return False, f"{tool} not found in PATH"
        except subprocess.TimeoutExpired:
            return False, f"{tool} timed out"
        except (TypeError, OSError) as e:
            return False, f"{tool} could not be launched: {e}"
    return True, "FFmpeg and FFprobe available"


# ============================================================================
# METADATA QA CHECKS (1-13)
# ============================================================================

def check_container_format(filepath: str, result: QAResult, cfg: dict):
    """Check 1: Validate container format and file integrity."""
    if not os.path.isfile(filepath):
        result.add_fail("Container Format", f"File not found: {filepath}")
        return
    file_size = os.path.getsize(filepath)
    file_size_mb = file_size / (1024 * 1024)
    ext = os.path.splitext(filepath)[1].lower()
    result.set_metadata("file_size_mb", round(file_size_mb, 2))
    result.set_metadata("container", ext)
    if file_size == 0:
        result.add_fail("Container Format", "File is empty (0 bytes).")
        return
    if ext not in cfg["accepted_containers"]:
        result.add_fail("Container Format", f"Container '{ext}' not in accepted list {cfg['accepted_containers']}.")
        return
    result.add_pass("Container Format", f"{_format_size(file_size_mb)}, container: {ext}")


def check_video_codec(probe_data: dict, result: QAResult, cfg: dict):
    """Check 2: Validate video codec."""
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        result.add_fail("Video Codec", "No video stream found.")
        return
    codec = video_streams[0].get("codec_name", "unknown").lower()
    result.set_metadata("video_codec", codec)
    if codec in cfg["accepted_codecs"]:
        result.add_pass("Video Codec", f"Codec: {codec}")
    else:
        result.add_fail("Video Codec", f"Codec '{codec}' not in accepted list {cfg['accepted_codecs']}.")


def _stream_rotation_degrees(stream: dict) -> int:
    """Rotation a player applies to this stream, in degrees.

    ffprobe reports it in the Display Matrix side data (negated: a video
    displayed 90 degrees clockwise carries -90); older files may carry
    only the QuickTime ``rotate`` tag. 0 when neither is present. Only
    the magnitude matters to callers: 90/270 swap the displayed axes.
    """
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return int(round(float(side_data["rotation"])))
            except (TypeError, ValueError):
                continue
    rotate_tag = (stream.get("tags") or {}).get("rotate")
    if rotate_tag:
        try:
            return int(round(float(rotate_tag)))
        except (TypeError, ValueError):
            pass
    return 0


def check_resolution(probe_data: dict, result: QAResult, cfg: dict):
    """Check 3: Validate resolution meets target.

    A delivery spec is a floor, not a suggestion: a frame smaller than the
    target in either dimension cannot be conformed up without resampling, so
    it fails. The old rule accepted anything above 50% of the target, which
    passed a 2000x2140 master against a 3840x2160 spec.

    The narrow band above is for mod-2 padding and rounding only (a 3840x2158
    encode), not for genuinely undersized footage.
    """
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        result.add_fail("Resolution", "No video stream found.")
        return
    width = video_streams[0].get("width", 0)
    height = video_streams[0].get("height", 0)
    # Judge the frame the audience sees, not the frame as stored: a
    # 90/270-degree rotation flag swaps the displayed axes, so a portrait
    # phone capture stored as 1920x1080 must not pass a landscape spec.
    rotation = _stream_rotation_degrees(video_streams[0])
    if rotation:
        result.set_metadata("rotation", rotation)
        result.set_metadata("stored_resolution", f"{width}x{height}")
    if abs(rotation) % 180 == 90 and width and height:
        width, height = height, width
    result.set_metadata("resolution", f"{width}x{height}")
    result.set_metadata("width", width)
    result.set_metadata("height", height)
    tw, th = cfg["target_width"], cfg["target_height"]

    rot_note = f" (displayed after {abs(rotation)}° rotation)" if rotation else ""
    if width >= tw and height >= th:
        if width > tw or height > th:
            result.add_pass("Resolution", f"{width}x{height} (exceeds {tw}x{th} target){rot_note}")
        else:
            result.add_pass("Resolution", f"{width}x{height} (matches {tw}x{th} target){rot_note}")
        return

    shortfall_pct = max(
        (tw - width) / tw * 100 if tw > 0 else 0,
        (th - height) / th * 100 if th > 0 else 0,
    )

    if shortfall_pct <= 1.0:
        result.add_warn(
            "Resolution",
            f"{width}x{height} is {shortfall_pct:.1f}% under the {tw}x{th} target{rot_note}. "
            f"Within rounding/mod-2 tolerance — verify this is intentional.",
            confidence=75,
        )
    else:
        result.add_fail(
            "Resolution",
            f"{width}x{height} does not meet the {tw}x{th} target{rot_note} "
            f"({shortfall_pct:.0f}% short). Upscaling to spec would resample the image.",
            confidence=95,
        )


def _format_size(size_mb: float) -> str:
    """'40.3 MB', or KB below 0.1 MB: a 7 KB file read as '0.0 MB'."""
    if size_mb < 0.1:
        return f"{max(size_mb * 1024, 0.0):.0f} KB"
    return f"{size_mb:.1f} MB"


def _format_mbps(mbps: float) -> str:
    """One decimal normally; more for sub-1 Mbps so 0.05 is not '0.0'."""
    if mbps >= 1:
        return f"{mbps:.1f}"
    if mbps >= 0.01:
        return f"{mbps:.2f}"
    return "<0.01"


def _format_pct(pct: float) -> str:
    """A deficit that rounds to 100% but is not 100% reads '>99%'."""
    if pct >= 99.5 and pct < 100:
        return ">99%"
    return f"{pct:.0f}%"


def check_frame_rate(probe_data: dict, result: QAResult, cfg: dict):
    """Check 4: Validate frame rate."""
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        result.add_fail("Frame Rate", "No video stream found.")
        return
    r_fps = parse_frame_rate(video_streams[0].get("r_frame_rate", "0/0"))
    avg_fps = parse_frame_rate(video_streams[0].get("avg_frame_rate", "0/0"))
    result.set_metadata("r_frame_rate", r_fps)
    result.set_metadata("avg_frame_rate", avg_fps)
    fps = avg_fps if avg_fps > 0 else r_fps
    result.set_metadata("frame_rate", fps)
    if fps <= 0:
        result.add_warn("Frame Rate", "Could not determine the frame rate from the container.",
                        confidence=50)
        return
    if abs(fps - cfg["target_fps"]) <= cfg["fps_tolerance"]:
        result.add_pass("Frame Rate", f"{fps:.2f} fps (target: {cfg['target_fps']} fps)")
    elif fps > cfg["target_fps"]:
        result.add_warn("Frame Rate", f"{fps:.2f} fps — above {cfg['target_fps']} fps target (higher frame rate is generally acceptable; can be conformed if needed)")
    else:
        result.add_fail("Frame Rate", f"{fps:.2f} fps (below target {cfg['target_fps']} fps)")


def check_cfr(probe_data: dict, result: QAResult, cfg: dict):
    """Check 5: Detect variable frame rate."""
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        result.add_skip("CFR Check", "No video stream found.")
        return
    r_fps = parse_frame_rate(video_streams[0].get("r_frame_rate", "0/0"))
    avg_fps = parse_frame_rate(video_streams[0].get("avg_frame_rate", "0/0"))
    if r_fps == 0 or avg_fps == 0:
        result.add_warn("CFR Check", "Could not determine frame rate type.")
        return
    # FIX: Some containers (MKV) report r_frame_rate as 1000/1 or 90000/1
    # which is a container default, not actual VFR. Skip CFR comparison.
    if r_fps in (1000.0, 90000.0):
        result.add_pass("CFR Check", f"Container r_frame_rate={r_fps:.0f} (default); using avg={avg_fps:.2f} fps")
        return
    deviation = abs(r_fps - avg_fps) / max(r_fps, avg_fps)
    result.set_metadata("fps_deviation", round(deviation, 4))
    if deviation <= cfg["vfr_threshold"]:
        result.add_pass("CFR Check", f"Constant frame rate (r={r_fps:.2f}, avg={avg_fps:.2f})")
    elif cfg["require_cfr"]:
        result.add_fail("CFR Check", f"Variable frame rate detected (r={r_fps:.2f}, avg={avg_fps:.2f})")
    else:
        result.add_warn("CFR Check", f"Variable frame rate detected (r={r_fps:.2f}, avg={avg_fps:.2f}). File is usable but may need conform for timeline stability.")


def check_bitrate(probe_data: dict, result: QAResult, cfg: dict):
    """Check 6: Validate video bitrate meets minimum."""
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    bitrate_str = None
    if video_streams:
        bitrate_str = video_streams[0].get("bit_rate")
    is_format_level = False
    if not bitrate_str:
        bitrate_str = probe_data.get("format", {}).get("bit_rate")
        is_format_level = True
    if not bitrate_str:
        result.add_warn("Bitrate", "Could not determine bitrate.")
        return
    bitrate_bps = _safe_int(bitrate_str)
    if bitrate_bps is None or bitrate_bps < 0:
        result.add_warn("Bitrate", f"Could not parse bitrate: {bitrate_str}")
        return
    # v1.6 FIX: Subtract audio bitrate when using format-level total
    if is_format_level:
        audio_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "audio"]
        total_audio_bps = 0
        for a_stream in audio_streams:
            audio_bps = _safe_int(a_stream.get("bit_rate"), 0)
            if audio_bps > 0:
                total_audio_bps += audio_bps
        if total_audio_bps > 0:
            bitrate_bps = max(bitrate_bps - total_audio_bps, 0)
    bitrate_mbps = bitrate_bps / 1_000_000
    result.set_metadata("video_bitrate_mbps", round(bitrate_mbps, 2))
    min_br = cfg["min_bitrate_mbps"]
    # FIX: Sanity check for PCM audio subtraction artifacts
    # If format-level subtraction yielded implausibly low video bitrate,
    # but original format bitrate was above minimum, issue WARN not FAIL
    if is_format_level and bitrate_mbps < 1.0:
        orig_bps = _safe_int(probe_data.get("format", {}).get("bit_rate"), 0)
        orig_mbps = orig_bps / 1_000_000
        if orig_mbps >= min_br:
            result.add_warn("Bitrate", f"Video bitrate ~{bitrate_mbps:.1f} Mbps after subtracting audio "
                           f"(format total: {orig_mbps:.1f} Mbps). PCM audio may skew calculation.")
            return
    if bitrate_mbps >= min_br:
        result.add_pass("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps (min: {min_br} Mbps)")
    else:
        # Percentage-based tolerance: VBR encoding commonly varies 10-15% below target
        deficit_pct = ((min_br - bitrate_mbps) / min_br * 100) if min_br > 0 else 0
        if deficit_pct <= 10:
            # Within 10% - marginal pass (normal VBR variance)
            result.add_pass("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps ({deficit_pct:.0f}% below target {min_br} Mbps - within VBR tolerance)", confidence=70)
        elif deficit_pct <= 25:
            # 10-25% below - warn but don't fail
            result.add_warn("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps — {deficit_pct:.0f}% below {min_br} Mbps target. Likely still usable; verify no visible compression artifacts.", confidence=75)
        else:
            # >25% below - definite fail
            result.add_fail("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps is {_format_pct(deficit_pct)} below minimum {min_br} Mbps. Likely visible compression artifacts.", confidence=95)


def check_audio(probe_data: dict, result: QAResult, cfg: dict):
    """Check 7: Validate audio stream (3 sub-checks matching CLI v1.6.1)."""
    audio_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        if cfg["audio_required"]:
            result.add_fail("Audio Stream", "No audio stream found (audio is required).")
        else:
            result.add_pass("Audio Stream", "No audio stream (audio not required).")
        return
    audio = audio_streams[0]
    codec = audio.get("codec_name", "unknown").lower()
    sample_rate = _safe_int(audio.get("sample_rate"), 0)
    channels = _safe_int(audio.get("channels"), 0)
    channel_layout = audio.get("channel_layout", "unknown")
    result.set_metadata("audio_codec", codec)
    result.set_metadata("audio_sample_rate", sample_rate)
    result.set_metadata("audio_channels", channels)

    # Sub-check 7a: Audio codec
    if codec in cfg["accepted_audio_codecs"]:
        result.add_pass("Audio Codec", f"Codec: {codec}")
    else:
        result.add_warn("Audio Codec", f"Codec '{codec}' — not in the expected codec list but may still be compatible.")

    # Sub-check 7b: Sample rate
    if sample_rate >= cfg["min_audio_sample_rate"]:
        # REMOVED v0.4.4: result.add_pass("Audio Sample Rate", f"{sample_rate} Hz (minimum: {cfg['min_audio_sample_rate']} Hz)")
        pass
    else:
        # REMOVED v0.4.4: result.add_fail("Audio Sample Rate", f"{sample_rate} Hz (below minimum {cfg['min_audio_sample_rate']} Hz)")
        pass

    # Sub-check 7c: Channels
    if channels > 0:
        result.add_pass("Audio Channels", f"{channels} channel(s), layout: {channel_layout}")
    else:
        result.add_warn("Audio Channels", "Could not determine audio channel configuration.")


def check_duration(probe_data: dict, result: QAResult, cfg: dict):
    """Check 8: Report duration and flag short files."""
    duration_str = probe_data.get("format", {}).get("duration")
    if not duration_str:
        result.add_warn("Duration", "Could not determine file duration.")
        return
    duration = _safe_float(duration_str)
    if duration is None or duration < 0:
        result.add_warn("Duration", f"Could not parse duration: {duration_str}")
        return
    hours = int(duration // 3600)
    minutes = int((duration % 3600) // 60)
    seconds = int(duration % 60)
    result.set_metadata("duration_seconds", round(duration, 2))
    result.set_metadata("duration_display", f"{hours:02d}:{minutes:02d}:{seconds:02d}")
    dur_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    min_fail = cfg.get("min_duration_fail", 5.0)
    min_warn = cfg.get("min_duration_warn", 30.0)
    if duration < min_fail:
        result.add_fail("Duration", f"{dur_str} (less than {min_fail}s -- possible incomplete capture)")
    elif duration < min_warn:
        result.add_warn("Duration", f"{dur_str} (less than {min_warn}s -- verify intentional)")
    else:
        result.add_pass("Duration", f"{dur_str} ({duration:.1f}s)")


# Check 9 (Color Space) was removed in v0.4.4. Commenting out only its `def`
# line left the whole body indented into check_duration above, so this
# function silently recorded "Color Space" results too. The body is gone
# rather than re-commented; "Color Space" remains listed in REMOVED_CHECKS.


def check_naming(filepath: str, result: QAResult, cfg: dict):
    """Check 10: Validate file naming convention."""
    filename = os.path.basename(filepath)
    pattern = cfg["naming_pattern"]
    if re.match(pattern, filename):
        result.add_pass("Naming Convention", f"Matches pattern: {cfg['naming_example']}")
    else:
        result.add_warn("Naming Convention", f"Does not match expected pattern: {cfg['naming_example']}")


def _stream_duration_text(stream: dict) -> Optional[str]:
    """Per-stream duration in seconds as text, including Matroska's tag form.

    MKV and WebM streams carry no ``duration`` field; the length lives in a
    ``DURATION`` tag formatted ``HH:MM:SS.nnnnnnnnn``. Without this fallback
    every Matroska file with audio raised an A/V Duration warning.
    """
    value = stream.get("duration")
    if value and value != "N/A":
        return str(value)
    tags = stream.get("tags") or {}
    for key, raw in tags.items():
        if str(key).upper().startswith("DURATION") and isinstance(raw, str):
            match = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*", raw)
            if match:
                hours, minutes, seconds = match.groups()
                return str(int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    return None


def check_av_duration_mismatch(probe_data: dict, result: QAResult, cfg: dict):
    """Check 11: Detect video/audio duration mismatch with tiered severity."""
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    audio_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "audio"]
    if not video_streams or not audio_streams:
        result.add_skip("A/V Duration", "Need both video and audio streams to compare.")
        return
    v_dur_str = _stream_duration_text(video_streams[0])
    a_dur_str = _stream_duration_text(audio_streams[0])
    if not v_dur_str or not a_dur_str:
        result.add_warn("A/V Duration", "Per-stream duration not available.")
        return
    v_dur = _safe_float(v_dur_str)
    a_dur = _safe_float(a_dur_str)
    if v_dur is None or a_dur is None:
        result.add_warn("A/V Duration", "Could not parse durations.")
        return
    if v_dur <= 0 or a_dur <= 0:
        result.add_warn("A/V Duration", "Duration data unavailable.")
        return
    # Compare stream ENDS, not raw durations. Streams routinely start at
    # different offsets (an audio stream beginning 40 ms in, a video
    # stream at a negative PTS origin), and comparing durations alone
    # reads that offset as a mismatch — or hides a real one behind it.
    # Missing/unparseable start_time falls back to 0, the old behaviour.
    def _start_of(stream):
        try:
            return float(stream.get("start_time") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    v_start = _start_of(video_streams[0])
    a_start = _start_of(audio_streams[0])
    diff = abs((v_start + v_dur) - (a_start + a_dur))
    result.set_metadata("video_duration", round(v_dur, 3))
    result.set_metadata("audio_duration", round(a_dur, 3))
    if v_start or a_start:
        result.set_metadata("video_start_time", round(v_start, 3))
        result.set_metadata("audio_start_time", round(a_start, 3))
    result.set_metadata("av_duration_diff", round(diff, 3))
    threshold = cfg["duration_mismatch_threshold"]
    if diff <= threshold:
        result.add_pass("A/V Duration", f"Video: {v_dur:.2f}s, Audio: {a_dur:.2f}s (diff: {diff:.3f}s)")
    elif diff <= 2.0:
        result.add_warn("A/V Duration", f"Video: {v_dur:.2f}s, Audio: {a_dur:.2f}s (diff: {diff:.3f}s). Minor offset — usually safe but verify lip sync on dialogue.")
    else:
        result.add_fail("A/V Duration", f"Video: {v_dur:.2f}s, Audio: {a_dur:.2f}s (diff: {diff:.3f}s -- significant mismatch)")


def check_pixel_aspect_ratio(probe_data: dict, result: QAResult, cfg: dict):
    """Check 12: Validate pixel aspect ratio is square (1:1)."""
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        result.add_skip("Pixel Aspect Ratio", "No video stream.")
        return
    sar = video_streams[0].get("sample_aspect_ratio", "N/A")
    dar = video_streams[0].get("display_aspect_ratio", "N/A")
    result.set_metadata("sar", sar)
    result.set_metadata("display_aspect_ratio", dar)
    if sar in ("1:1", "N/A", None, "", "0:1"):
        result.add_pass("Pixel Aspect Ratio", f"SAR: {sar}, DAR: {dar}")
    else:
        result.add_warn("Pixel Aspect Ratio", f"Non-square pixels (SAR: {sar}, DAR: {dar}). File is usable but verify display renders at correct aspect.")


def check_frame_count(probe_data: dict, result: QAResult, cfg: dict):
    """Check 13: Validate actual frame count matches expected (duration * fps).
    Uses video stream duration (not format duration) per v1.6 fix.
    Skips when VFR is detected to prevent false failures.
    """
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        result.add_skip("Frame Count", "No video stream.")
        return
    # FIX: Skip frame count check when VFR was detected (fps_deviation stored by check_cfr)
    # Professional codecs report minor rate differences - increase tolerance
    _video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    _codec = _video_streams[0].get("codec_name", "").lower() if _video_streams else ""
    _vfr_thresh = cfg.get("vfr_threshold", 0.02)
    if _codec in ("prores", "dnxhd", "dnxhr", "cfhd", "v210", "rawvideo"):
        _vfr_thresh = max(_vfr_thresh, 0.05)  # More lenient for pro codecs
    
    if result.metadata.get("fps_deviation", 0) > _vfr_thresh:
        result.add_skip("Frame Count", "Skipped — variable frame rate detected (frame count unreliable for VFR).")
        return
    vs = video_streams[0]
    nb_frames_str = vs.get("nb_frames")
    if not nb_frames_str or nb_frames_str == "N/A":
        # Matroska and WebM never record a per-stream frame count, so its
        # absence there says nothing about the file; every .mkv used to carry
        # a Frame Count warning. Elsewhere a missing count stays a warning.
        format_name = str((probe_data.get("format") or {}).get("format_name") or "")
        if nb_frames_str in (None, "", "N/A") and (
                "matroska" in format_name or "webm" in format_name):
            result.add_skip("Frame Count", "Frame count is not recorded in Matroska/WebM files.")
        else:
            result.add_warn("Frame Count", "nb_frames not available in container metadata.")
        return
    actual_frames = _safe_int(nb_frames_str)
    if actual_frames is None or actual_frames < 0:
        result.add_warn("Frame Count", f"Could not parse frame count: {nb_frames_str}")
        return
    # v1.6: Use video stream duration first (not format duration)
    duration_str = vs.get("duration")
    if not duration_str:
        duration_str = probe_data.get("format", {}).get("duration")
    fps = parse_frame_rate(vs.get("r_frame_rate", "0/1"))
    duration = _safe_float(duration_str)
    if duration is None or duration <= 0 or fps <= 0:
        result.add_warn("Frame Count", "Cannot determine expected frame count.")
        return
    expected_frames = int(round(duration * fps))
    diff = abs(expected_frames - actual_frames)
    tolerance = max(2, int(fps * 0.1))
    result.set_metadata("expected_frames", expected_frames)
    result.set_metadata("actual_frames", actual_frames)
    result.set_metadata("frame_count_diff", diff)
    if diff <= tolerance:
        result.add_pass("Frame Count", f"{actual_frames} frames (expected ~{expected_frames}, diff: {diff})")
    elif diff <= expected_frames * 0.10:  # Up to 10% deviation is WARN (common for static/slow content)
        result.add_warn("Frame Count", f"{actual_frames} frames vs ~{expected_frames} (diff: {diff}, {diff/expected_frames*100:.1f}%). May be normal for static/slow-motion content.")
    else:
        result.add_fail("Frame Count", f"{actual_frames} frames vs ~{expected_frames} (diff: {diff}, {diff/expected_frames*100:.1f}%). Likely frame drops or encoding issue.")


# ============================================================================
# DEEP SCAN UTILITIES
# ============================================================================

def _filter_hwaccel_noise(stderr_text: str) -> str:
    """Remove hwaccel fallback messages from stderr to prevent false positives."""
    # Split without stripping the whole text: the first line's indentation
    # tells _extract_decode_errors whether it belongs to the metadata dump.
    lines = stderr_text.split("\n")
    filtered = []
    for line in lines:
        line_lower = line.lower()
        if any(p.lower() in line_lower for p in HWACCEL_NOISE_PATTERNS):
            continue
        if line.strip():
            filtered.append(line)
    return "\n".join(filtered)


# Signatures of genuine decode/demux problems in FFmpeg stderr.
#
# This is deliberately an ALLOW-list. The scan runs at `-v info` (required, or
# the detect filters log nothing), which also emits the banner, build config,
# stream mapping and progress lines. Treating "anything unrecognised" as an
# error — as this used to — reports hundreds of decode errors for every clean file.
DECODE_ERROR_PATTERNS = (
    "error while decoding",
    "Error while decoding",
    "Invalid data found when processing input",
    "invalid nal unit",
    "Invalid NAL unit",
    "missing picture in access unit",
    "concealing",
    "corrupt",
    "Corrupt",
    "Truncating packet",
    "no frame!",
    "decode_slice_header error",
    "illegal short term buffer state",
    "co located POCs unavailable",
    "mmco: unref short failure",
    "Invalid sample size",
    "Invalid frame size",
    "out of range",
    "Application provided invalid",
    "non-existing PPS",
    "non-existing SPS",
    "Frame parameters mismatch",
    "Could not find codec parameters",
    "Failed to decode",
)


_NON_DECODER_LINE_RE = re.compile(
    r"^(?:Input|Output) #\d|^Stream #\d|^Duration:|^Metadata:|^Chapters?:"
    r"|^[A-Za-z_][\w .-]*?\s{2,}:\s"   # padded "key        : value" dump rows
)


# "[h264 @ 0x7f..]" - present on decoder messages, including one glued onto
# an indented line by interleaved filter-thread output.
_DECODER_TAG_RE = re.compile(r"\[[^\]@\n]+ @ 0x[0-9a-fA-F]+\]")


def _extract_decode_errors(stderr_clean: str) -> list:
    """Return the lines in FFmpeg stderr that indicate a real decode problem.

    FFmpeg's input banner ("Input #0, mov,... from '/path/not_corrupt.mp4':")
    and metadata dump are not suppressed by -hide_banner, and they contain the
    file path and tags. Matching patterns such as "corrupt" against those
    lines failed Decode Integrity for a clean file purely because of its name.
    """
    errors = []
    for raw in stderr_clean.split("\n"):
        # The banner, metadata dump and stream list are indented; decoder
        # messages start at column 0 or with a "[codec @ 0x...]" prefix. This
        # also covers long keys whose padding collapses ("com.apple.quicktime
        # .description: corrupt take") and ": ..." continuation rows.
        if raw[:1] in (" ", "\t") and not _DECODER_TAG_RE.search(raw):
            continue
        line = raw.strip()
        if not line or _NON_DECODER_LINE_RE.match(line):
            continue
        if any(p in line for p in DECODE_ERROR_PATTERNS):
            errors.append(line)
    return errors


_STDERR_EVIDENCE_TOKENS = {
    "black": ("black_start", "black_end", "black_duration"),
    "freeze": ("freeze_start", "freeze_end", "freeze_duration"),
    "silence": ("silence_start", "silence_end", "silence_duration"),
}
_STDERR_READ_CHUNK_BYTES = 64 * 1024
_STDERR_RECORD_FRAGMENT_CHARS = 4096
_STDERR_RECORD_OVERLAP_CHARS = 128


def _stderr_evidence_categories(line: str) -> tuple:
    categories = []
    if _extract_decode_errors(line):
        categories.append("decode")
    for category in ("freeze", "silence", "black"):
        if any(token in line for token in _STDERR_EVIDENCE_TOKENS[category]):
            categories.append(category)
    return tuple(categories)


def _stderr_line_is_evidence(line: str) -> bool:
    return bool(_stderr_evidence_categories(line))


def _clip_stderr_line(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    marker = " ...[truncated]... "
    if limit <= len(marker):
        return line[:limit]
    head = (limit - len(marker)) // 2
    return line[:head] + marker + line[-(limit - len(marker) - head):]


def _clip_evidence_line(line: str, categories: tuple, limit: int) -> str:
    """Keep parseable snippets for every semantic category in a long record."""
    if len(line) <= limit:
        return line
    per_category = max(1, (limit - max(0, len(categories) - 1) * 3)
                       // max(1, len(categories)))
    snippets = []
    for category in categories:
        if category == "decode":
            positions = [line.find(pattern) for pattern in DECODE_ERROR_PATTERNS
                         if line.find(pattern) >= 0]
        else:
            positions = [line.find(token)
                         for token in _STDERR_EVIDENCE_TOKENS[category]
                         if line.find(token) >= 0]
        start = max(0, min(positions) - 16) if positions else 0
        snippets.append(line[start:start + per_category])
    return " | ".join(snippets)[:limit]


def _cap_stderr_text(text: str, cfg: dict) -> tuple:
    """Bound completed stderr with the same category allocation as capture."""
    max_lines = max(1, int(cfg.get("deep_scan_max_stderr_lines", 5000) or 5000))
    max_chars = max(1, int(cfg.get("deep_scan_max_stderr_chars", 512 * 1024)
                           or 512 * 1024))
    if not text:
        return "", 0
    lines = text.splitlines()
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text, 0
    capture = _BoundedStderrCapture(cfg)
    for line in lines:
        capture.add(line)
    capped = capture.result()
    return str(capped), capped.truncated_records


class _CapturedStderr(str):
    def __new__(cls, text: str, truncated_records: int,
                peak_lines: int, peak_chars: int,
                peak_pending_chars: int):
        value = super().__new__(cls, text)
        value.truncated_records = truncated_records
        value.peak_lines = peak_lines
        value.peak_chars = peak_chars
        value.peak_pending_chars = peak_pending_chars
        return value


class _BoundedStderrCapture:
    """Keep every detection record, plus a bounded tail of other context.

    Verdicts are parsed from this text, so detect-filter events and decode
    errors are data, not diagnostics. v0.8.0 kept only the first and most
    recent line per category: freezedetect writes three lines per event and
    silencedetect two, so the parser paired the first event's start with the
    last event's end and fabricated a single multi-minute silence or freeze
    (a false MISMATCH), black events beyond the second vanished, and repeated
    decode errors were collapsed into one. Event lines are now all retained up
    to a hard ceiling (``deep_scan_max_event_lines``) that only pathological
    streams reach; ordinary progress noise still shares the line and character
    budget, and is what gets discarded under pressure.
    """

    def __init__(self, cfg: dict):
        self.max_lines = max(
            1, int(cfg.get("deep_scan_max_stderr_lines", 5000) or 5000))
        self.max_chars = max(
            1, int(cfg.get("deep_scan_max_stderr_chars", 512 * 1024)
                   or 512 * 1024))
        self.max_event_lines = max(
            1, int(cfg.get("deep_scan_max_event_lines", 20000) or 20000))
        self.max_event_chars = max(
            1, int(cfg.get("deep_scan_max_event_chars", 4 * 1024 * 1024)
                   or 4 * 1024 * 1024))
        evidence_slots = max(1, min(self.max_lines, 8))
        separators = max(0, evidence_slots - 1)
        # A single event record is clipped to a parseable snippet: parsing
        # needs only the tokens, and unclipped multi-kilobyte decoder lines
        # let 20000 retained records reach tens of megabytes per segment.
        self._evidence_line_limit = min(512, max(
            256, (self.max_chars - separators) // evidence_slots))
        self._kept = []
        self._kept_chars = 0
        self._tail = deque()
        self._tail_chars = 0
        self._sequence = 0
        self.truncated_records = 0
        self.peak_lines = 0
        self.peak_chars = 0
        self.peak_pending_chars = 0

    def note_fragment_split(self) -> None:
        self.truncated_records += 1

    def _entries(self) -> list:
        return sorted([*self._kept, *self._tail])

    def _retained_count(self) -> int:
        return len(self._kept) + len(self._tail)

    def _joined_chars(self) -> int:
        count = self._retained_count()
        return self._kept_chars + self._tail_chars + max(0, count - 1)

    def add(self, line: str) -> None:
        line = line.rstrip("\r\n")
        categories = _stderr_evidence_categories(line)
        limit = self._evidence_line_limit if categories else self.max_chars
        clipped = (_clip_evidence_line(line, categories, limit)
                   if categories else _clip_stderr_line(line, limit))
        if len(clipped) < len(line):
            self.truncated_records += 1
        entry = (self._sequence, clipped)
        self._sequence += 1

        if categories:
            if (len(self._kept) >= self.max_event_lines
                    or self._kept_chars + len(clipped) > self.max_event_chars):
                self.truncated_records += 1
                return
            self._kept.append(entry)
            self._kept_chars += len(clipped)
        else:
            self._tail.append(entry)
            self._tail_chars += len(clipped)

        # The tail only gets what the retained evidence leaves of the budget.
        line_budget = max(0, self.max_lines - len(self._kept))
        char_budget = max(0, self.max_chars - self._kept_chars - len(self._kept))
        while self._tail and (len(self._tail) > line_budget
                              or self._tail_chars + len(self._tail) > char_budget):
            discarded = self._tail.popleft()
            self._tail_chars -= len(discarded[1])
            self.truncated_records += 1

        self.peak_lines = max(self.peak_lines, self._retained_count())
        self.peak_chars = max(self.peak_chars, self._joined_chars())

    def result(self) -> _CapturedStderr:
        entries = self._entries()
        return _CapturedStderr(
            "\n".join(line for _, line in entries),
            self.truncated_records,
            self.peak_lines,
            self.peak_chars,
            self.peak_pending_chars,
        )


def _drain_binary_records(stream, on_record: Callable[[str], None]) -> int:
    """Decode a binary pipe incrementally and split overlong records safely."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    peak_pending = 0

    def consume(text: str, final: bool = False) -> None:
        nonlocal pending, peak_pending
        pending += text
        peak_pending = max(peak_pending, len(pending))
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            on_record(line.rstrip("\r"))
        while len(pending) > _STDERR_RECORD_FRAGMENT_CHARS:
            on_record(pending[:_STDERR_RECORD_FRAGMENT_CHARS])
            pending = pending[
                _STDERR_RECORD_FRAGMENT_CHARS - _STDERR_RECORD_OVERLAP_CHARS:]
            if hasattr(on_record, "__self__") and hasattr(
                    on_record.__self__, "note_fragment_split"):
                on_record.__self__.note_fragment_split()
        if final and pending:
            on_record(pending.rstrip("\r"))
            pending = ""

    while True:
        chunk = stream.read(_STDERR_READ_CHUNK_BYTES)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        consume(decoder.decode(chunk))
    consume(decoder.decode(b"", final=True), final=True)
    return peak_pending


def _merge_event_intervals(events: list, join_gap: float = 0.25) -> list:
    """Merge overlapping/adjacent (start, duration) events into distinct ones.

    Segments are scanned with an overlap so an event straddling a boundary is
    seen whole by at least one of them, which means the same event is often
    reported twice — once truncated. Merging by interval (rather than by
    start-time proximity, as the old ``_dedup_events`` did) collapses those
    into a single event with the correct full duration, while keeping two
    genuinely separate events separate no matter how close together they are.

    ``join_gap`` absorbs the sub-frame drift between a segment's own report of
    an event and the overlapping segment's report of the same one.
    """
    intervals = []
    for e in events:
        if isinstance(e, tuple):
            start, duration = float(e[0]), float(e[1]) if len(e) > 1 else 0.0
        else:
            start, duration = float(e), 0.0
        intervals.append((start, max(0.0, duration)))

    if not intervals:
        return []

    intervals.sort()
    merged = [intervals[0]]
    for start, duration in intervals[1:]:
        prev_start, prev_duration = merged[-1]
        prev_end = prev_start + prev_duration
        if start <= prev_end + join_gap:
            merged[-1] = (prev_start, max(prev_end, start + duration) - prev_start)
        else:
            merged.append((start, duration))
    return merged


def _timecode(seconds: float) -> str:
    """Format a timeline position as HH:MM:SS.mmm for operator scrubbing.

    Operators could not locate reported black frames because the only position
    given was a raw seconds count for the first event.
    """
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def _format_event_list(events: list, limit: int = 6) -> str:
    """Render merged events as 'HH:MM:SS.mmm (1.2s)' for display."""
    shown = [f"{_timecode(start)} ({duration:.1f}s)" for start, duration in events[:limit]]
    if len(events) > limit:
        shown.append(f"+{len(events) - limit} more")
    return ", ".join(shown)


def _pair_detect_events(stderr_text: str, prefix: str) -> list:
    """Extract (start, duration) pairs from an FFmpeg detect filter's output.

    ``blackdetect``/``freezedetect``/``silencedetect`` emit ``<prefix>_start``,
    ``<prefix>_end`` and ``<prefix>_duration`` as independent tokens, and a
    region still open when the input ends produces a start with no end.

    The previous parser collected all starts and all durations into two flat
    lists and zipped them by index, so a single unterminated region shifted
    every subsequent pairing — every event after it was reported with another
    event's duration. Walking the tokens in document order instead keeps each
    start bound to its own end, and reconstructs a missing duration from the
    end timestamp when one is available.
    """
    # No leading \\b: FFmpeg logs from several filter threads share one
    # stderr pipe, and lines can interleave mid-line under threaded
    # filtering — a detect token glued onto the tail of an ebur128 line
    # ("...LRA:   4.8 LU" + "black_start:10 ...") has no word boundary
    # before it. Requiring one silently dropped real events (~3 in 8
    # identical scans of the same clip), reporting a clean PASS for a
    # file that was never clean. The tokens are distinctive enough to
    # match unanchored.
    token_re = re.compile(
        rf"{prefix}_(start|end|duration)\s*[:=]\s*(-?\d+(?:\.\d+)?)"
    )

    events = []
    open_start = None
    pending_duration = None

    for match in token_re.finditer(stderr_text):
        kind, raw = match.group(1), float(match.group(2))
        if kind == "start":
            if open_start is not None:
                # Previous region never closed; keep it with whatever we know.
                events.append((open_start, pending_duration or 0.0))
            open_start, pending_duration = raw, None
        elif kind == "duration":
            pending_duration = raw
        elif kind == "end":
            if open_start is None:
                continue
            duration = pending_duration if pending_duration is not None else max(0.0, raw - open_start)
            events.append((open_start, duration))
            open_start, pending_duration = None, None

    if open_start is not None:
        events.append((open_start, pending_duration or 0.0))

    return events


# Timestamps emitted by blackdetect / freezedetect / silencedetect. Durations
# ("black_duration", "silence_duration") are intervals, not points on the
# timeline, so they must NOT be shifted.
_EVENT_TIME_RE = re.compile(
    r"(black_start|black_end|freeze_start|freeze_end|silence_start|silence_end)"
    # The value may be negative: a region that began just before a segment's
    # seek point is reported as e.g. "silence_start: -0.0213". Leaving it
    # unshifted paired it with a shifted end and fabricated a silence
    # hundreds of seconds long.
    r"(\s*[:=]\s*)(-?\d+(?:\.\d+)?)"
)


_REGION_CLOSERS = {
    "freeze": "lavfi.freezedetect.freeze_end: {end:.3f}",
    "silence": "[silencedetect] silence_end: {end:.3f}",
    "black": "[blackdetect] black_end:{end:.3f}",
}


def _close_open_regions(stderr_text: str, end_time: float) -> str:
    """Close any detection region still open at ``end_time``.

    freezedetect never prints freeze_end for a freeze that lasts until the
    input ends, so a clip that froze for its last six seconds - or a whole
    static deliverable - was reported as "1 freeze event, total 0.0s" and
    passed. With segmented scans every segment of a frozen file ended that
    way, giving several 0.0s events instead of one long freeze. The open
    region now ends where the scanned range ends.
    """
    if not stderr_text or end_time is None:
        return stderr_text or ""
    additions = []
    for prefix, closer in _REGION_CLOSERS.items():
        last_kind = None
        for match in re.finditer(rf"{prefix}_(start|end)\s*[:=]", stderr_text):
            last_kind = match.group(1)
        if last_kind == "start":
            additions.append(closer.format(end=float(end_time)))
    if not additions:
        return stderr_text
    return stderr_text + "\n" + "\n".join(additions)


def _offset_event_times(stderr_text: str, offset: float) -> str:
    """Shift segment-relative event timestamps onto the whole-file timeline.

    ``-ss`` before ``-i`` resets output PTS to zero, so a black frame five
    minutes into the file is reported at 00:12 by the segment that starts at
    04:60. Without this, reported timestamps are wrong and ``_dedup_events``
    collapses genuine events from different segments.

    The original separator is preserved verbatim because the downstream
    parsers match on the exact ``black_start:12.345 black_end:...`` spacing.
    """
    if not stderr_text or not offset:
        return stderr_text or ""

    def _shift(match):
        return f"{match.group(1)}{match.group(2)}{float(match.group(3)) + offset:.3f}"

    return _EVENT_TIME_RE.sub(_shift, stderr_text)


def _segment_boundaries(duration: float, seg_count: int, overlap: float) -> List[Tuple[float, float]]:
    """Split a file into deterministic, overlapping scan segments.

    Two separate bugs lived in the old scheme, and together they made repeat
    scans of the same file disagree:

    1. Split points came from ``ffprobe`` keyframe probes with an 8s timeout.
       Under load a probe timed out and silently fell back to an even split,
       so the boundaries themselves changed from run to run.
    2. Segments were butt-jointed. ``freezedetect`` needs an uninterrupted run
       of ``freeze_detect_duration`` seconds, so a freeze lying across a
       boundary was cut into two shorter halves and detected by neither.

    Boundaries are now a pure function of (duration, seg_count), and each
    segment after the first starts ``overlap`` seconds early so any event
    shorter than ``overlap`` is seen whole by at least one segment.
    ``_merge_event_intervals`` collapses the resulting duplicates.
    """
    seg_count = max(1, int(seg_count))
    if duration <= 0:
        return [(0.0, 0.0)]

    step = duration / seg_count
    segments = []
    for i in range(seg_count):
        start = max(0.0, i * step - (overlap if i else 0.0))
        end = duration if i == seg_count - 1 else (i + 1) * step
        if end - start > 0.05:
            segments.append((start, end))
    return segments or [(0.0, duration)]


# ---------------------------------------------------------------------------
# Hardware acceleration selection
#
# ``ffmpeg -hwaccels`` lists the methods the binary was COMPILED with, not
# the methods that can open a device on this machine. A gyan.dev build lists
# ``cuda`` on a laptop with no NVIDIA GPU; ``-hwaccel cuda`` there does not
# fall back to software — device creation fails, FFmpeg exits 255 having
# decoded zero frames, and because the failure lines are exactly what
# HWACCEL_NOISE_PATTERNS scrubs from stderr, the deep scan then reported a
# clean pass for every frame-level check. Selection therefore probes each
# candidate with a real one-frame decode of the file being scanned (cached
# per process), and a method that fails device initialisation is never
# offered again.
# ---------------------------------------------------------------------------

_hwaccel_failed_methods: set = set()
_hwaccel_probe_cache: dict = {}

# Lines that mean "the hardware device could not be initialised", as opposed
# to "the file is damaged". A decode that dies this way produced no frames,
# so its (empty) findings must never be read as a clean result.
_HWACCEL_DEVICE_FAILURE_PATTERNS = (
    "Device creation failed",
    "No device available",
    "Hardware device setup failed",
    "Failed to initialise",
    "Failed to initialize",
    "Cannot init CUDA",
    "vaInitialize failed",
)


def _stderr_shows_device_failure(stderr_text: str) -> bool:
    if not stderr_text:
        return False
    return any(p in stderr_text for p in _HWACCEL_DEVICE_FAILURE_PATTERNS)


def _probe_hwaccel(ffmpeg_path: str, method: str, sample_file: str) -> bool:
    """True if `method` can really decode on this machine.

    One frame is enough: device creation happens before the first frame, so
    a method with no usable device fails immediately. Cached per
    (binary, method) — the answer cannot change while the process runs.
    """
    key = (ffmpeg_path, method)
    if key in _hwaccel_probe_cache:
        return _hwaccel_probe_cache[key]
    ok = False
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-nostdin", "-hide_banner", "-v", "error",
             "-hwaccel", method, "-i", sample_file,
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
            env=_get_clean_env(), **_subprocess_kwargs())
        ok = proc.returncode == 0
    except Exception:
        ok = False
    _hwaccel_probe_cache[key] = ok
    if not ok:
        _hwaccel_failed_methods.add(method)
    return ok


def _get_optimal_hwaccel(cfg: dict, probe_file: Optional[str] = None) -> List[str]:
    """Determine the best hwaccel method available on this system.
    Returns FFmpeg hwaccel args list, or empty list if disabled/software.

    A candidate is returned only if it is not already known to fail on this
    machine and — when `probe_file` is given — a one-frame probe decode of
    that file succeeds with it. With no probe file the compile-time list is
    trusted as before, minus any method already known to fail.
    """
    if not cfg.get("use_hwaccel", True):
        return []

    method = cfg.get("hwaccel_method", "auto")
    ffmpeg_path = get_ffmpeg_path() if _HAS_TOOLS_MODULE else "ffmpeg"

    def _usable(candidate: str) -> bool:
        if candidate in _hwaccel_failed_methods:
            return False
        if probe_file and ffmpeg_path:
            return _probe_hwaccel(ffmpeg_path, candidate, probe_file)
        return True

    if method != "auto":
        return ["-hwaccel", method] if _usable(method) else []

    # Auto-detect: try preferred methods in order
    if _HAS_TOOLS_MODULE:
        caps = detect_ffmpeg_capabilities()
        hw_methods = caps.get("hwaccel_methods", [])
        # Priority order: cuda > videotoolbox > vaapi > dxva2 > d3d11va
        for preferred in ["cuda", "videotoolbox", "vaapi", "dxva2", "d3d11va"]:
            if preferred in hw_methods and _usable(preferred):
                return ["-hwaccel", preferred]

    # FFmpeg's own "auto" probes devices itself and falls back to software
    # decoding when none initialises, so it is safe to offer unprobed.
    if _usable("auto"):
        return ["-hwaccel", "auto"]
    return []


def _run_segment_filter_scan(filepath: str, start_time: float, end_time: float,
                             vf_str: str, af_str: Optional[str], has_audio: bool,
                             cfg: dict,
                             cancel_event: Optional[threading.Event] = None,
                             process_owner=None) -> Optional[str]:
    """Run a filter scan on a time segment. Returns stderr text, or None on failure.

    Uses ``-ss`` before ``-i`` for fast input seeking, which resets output PTS
    to zero — callers must add ``start_time`` back to every parsed timestamp.

    Verbosity must stay at ``info``: blackdetect/freezedetect/silencedetect all
    report their findings at AV_LOG_INFO, so ``-v error`` would suppress every
    detection and make the deep scan silently pass everything.
    """
    if cancel_event is not None and cancel_event.is_set():
        return None
    duration = end_time - start_time
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        return None
    hwaccel_args = _get_optimal_hwaccel(cfg, probe_file=filepath)
    ft = str(cfg.get("filter_threads", 4))
    cmd = [ffmpeg_path, "-nostdin", "-hide_banner"] + hwaccel_args + [
        "-ss", str(start_time), "-t", str(duration),
        "-i", filepath,
        "-filter_threads", ft,
        "-v", "info",
        "-vf", vf_str
    ]
    if af_str and has_audio:
        cmd += ["-af", af_str]
    cmd += ["-f", "null", "-"]

    proc = None
    stderr_thread = None
    stderr_started = False
    try:
        # stdout is unused. Sending it to DEVNULL leaves one pipe to drain and
        # avoids the classic two-pipe deadlock while stderr is consumed live.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=False, bufsize=0, env=_get_clean_env(), **_subprocess_kwargs())
        _register_owned_proc(proc, process_owner)
        capture = _BoundedStderrCapture(cfg)
        reader_errors = []

        def _drain_stderr():
            try:
                capture.peak_pending_chars = _drain_binary_records(
                    proc.stderr, capture.add)
            except (OSError, ValueError, TypeError, AssertionError) as exc:
                reader_errors.append(exc)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        stderr_started = True

        def _join_stderr_reader() -> bool:
            stderr_thread.join(timeout=5)
            if stderr_thread.is_alive():
                # A completed/killed child must close its pipe. Refuse a
                # partial clean result if an abnormal stream stays blocked.
                try:
                    proc.stderr.close()
                except (OSError, ValueError):
                    pass
                stderr_thread.join(timeout=1)
            return not stderr_thread.is_alive()

        proc.wait(timeout=_calc_timeout(duration, cfg))
        if not _join_stderr_reader():
            return None
        if reader_errors and not (cancel_event and cancel_event.is_set()):
            return None

        stderr = capture.result()
        if cancel_event is not None and cancel_event.is_set():
            return None
        if proc.returncode:
            # A non-zero exit used to be ignored, so a decode that died at
            # device initialisation (zero frames) returned its error text
            # and was parsed as "no events found" — a clean pass.
            if hwaccel_args and _stderr_shows_device_failure(stderr):
                # Remember it so no later segment or file picks it again.
                _hwaccel_failed_methods.add(hwaccel_args[1])
                return None
            produced = stderr or ""
            if not any(tok in produced for tok in (
                    "black_start", "freeze_start", "silence_start", "frame=")):
                # Nothing was decoded and nothing was detected: this segment
                # failed, and the caller must count it as uncovered rather
                # than clean. (A segment that decoded part of the file before
                # failing keeps its real findings and its decode errors.)
                return None
        return stderr
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            if stderr_thread is not None and stderr_started:
                _join_stderr_reader()
        return None
    except (OSError, ValueError, TypeError):
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            if stderr_thread is not None and stderr_started:
                _join_stderr_reader()
        return None
    finally:
        # None (not "") so the caller can tell a failed segment apart from a
        # clean one — an empty string would look like "no events found".
        _unregister_owned_proc(proc, process_owner)


# ============================================================================
# DEEP SCAN (Checks 14-18)
# ============================================================================

def run_deep_scan(filepath: str, result: QAResult, cfg: dict,
                  progress_callback: Optional[Callable] = None,
                  cancel_event: Optional[threading.Event] = None,
                  quick: bool = False,
                  probe_data: Optional[dict] = None,
                  process_owner=None):
    """
    Run FFmpeg-based deep scan for frame-level defects.

    Single-pass decode+filter scan, executed as parallel segments for files
    long enough to benefit, with a parallel loudness pass.

    Segmentation is deterministic and overlapping (see ``_segment_boundaries``):
    repeat scans of the same file with the same settings produce the same
    findings, and an event lying across a segment join is still detected.
    """
    duration = get_file_duration(filepath, process_owner, cancel_event)
    if duration <= 0:
        result.add_skip("Deep Scan", "Could not determine file duration.")
        return

    # Safety gate: a 20GB+/multi-hour file decoded at full parallelism OOM'd
    # Windows. Skip frame-level analysis with a clear reason instead of
    # crashing; metadata checks above still ran.
    try:
        file_gb = os.path.getsize(filepath) / (1024 ** 3)
    except OSError:
        file_gb = 0.0
    max_gb = float(cfg.get("deep_scan_max_file_gb", 8.0) or 8.0)
    max_dur = float(cfg.get("deep_scan_max_duration_s", 7200.0) or 7200.0)
    if file_gb > max_gb or duration > max_dur:
        result.add_warn(
            "Deep Scan",
            f"Frame-level analysis not run: file is {file_gb:.1f} GB / "
            f"{duration:.0f}s (limits {max_gb:.0f} GB / {max_dur:.0f}s). "
            f"Metadata checks still apply.")
        result.set_metadata("deep_scan_skipped_reason", "size_or_duration_gate")
        return

    # Set duration metadata if not already set by check_duration
    if "duration_seconds" not in result.metadata:
        result.set_metadata("duration_seconds", round(duration, 2))
        dur_s = int(duration)
        result.set_metadata("duration_display", f"{dur_s // 3600:02d}:{(dur_s % 3600) // 60:02d}:{dur_s % 60:02d}")

    timeout = _calc_timeout(duration, cfg)

    # v0.4.2: Reuse probe_data from caller to avoid redundant FFprobe call
    if probe_data is None:
        probe_data = run_ffprobe(filepath, process_owner, cancel_event)
    audio_streams = [s for s in (probe_data or {}).get("streams", []) if s.get("codec_type") == "audio"]
    has_audio = len(audio_streams) > 0

    # Build hwaccel args (v0.3: adaptive detection). Probed against this
    # file: a compile-time hwaccel method with no device on this machine
    # must never be selected — its decode dies before the first frame and
    # the empty result used to be reported as a clean pass.
    # ---- Quick mode: skip deep scan entirely ----
    # Checked before hardware-decode probing: those probes spawn up to five
    # FFmpeg test decodes that quick mode never uses.
    if quick:
        result.add_skip("Decode Integrity", "Skipped (quick mode)")
        result.add_skip("Black Frames", "Skipped (quick mode)")
        result.add_skip("Freeze Frames", "Skipped (quick mode)")
        result.add_skip("Audio Silence", "Skipped (quick mode)")
        # REMOVED v0.4.4: result.add_skip("Audio Loudness", "Skipped (quick mode)")
        return

    hwaccel_args = _get_optimal_hwaccel(cfg, probe_file=filepath)

    # Build filter strings — video and audio SEPARATE
    vf_str = (
        f"blackdetect=d={cfg['black_detect_duration']}:pix_th={cfg['black_detect_pix_threshold']},"
        f"freezedetect=n={cfg['freeze_detect_noise']}:d={cfg['freeze_detect_duration']}"
    )

    if has_audio:
        af_str = (
            f"silencedetect=n={cfg['silence_noise_db']}dB:d={cfg['silence_duration']},"
            f"ebur128=peak=true"
        )
    else:
        af_str = None

    if progress_callback:
        progress_callback("filter", 0, duration, "Deep Scan [combined]")

    # Segment-based parallel scan for large files
    use_segments = (
        cfg.get("segment_enabled", True)
        and duration >= cfg.get("segment_min_duration", 30.0)
        and cfg.get("segment_count", 8) > 1
    )

    stderr_text = ""

    if use_segments:
        seg_count = cfg.get("segment_count", 8)

        # Each segment must overlap its predecessor by at least the longest
        # detection window, or an event straddling the join is split into two
        # sub-threshold halves and reported by neither segment.
        overlap = max(
            float(cfg.get("black_detect_duration", 0.5)),
            float(cfg.get("freeze_detect_duration", 2.0)),
            float(cfg.get("silence_duration", 2.0)),
        ) + 1.0
        segments = _segment_boundaries(duration, seg_count, overlap)

        # Segments use silence detection only (no ebur128 — needs full file)
        seg_af_str = f"silencedetect=n={cfg['silence_noise_db']}dB:d={cfg['silence_duration']}" if has_audio else None

        try:
            segment_results = []
            segment_capture_truncated = 0
            failed_segments = 0
            cancelled = False

            # Cap max_workers: up to 8 concurrent 4K ffmpeg decodes OOM'd /
            # crashed Windows. 2-4 workers saturate decode on most machines;
            # more only multiplies memory (each holds its own stderr).
            max_workers = min(len(segments), max(1, (os.cpu_count() or 4) // 2), 4)

            with ThreadPoolExecutor(max_workers=max_workers) as seg_executor:
                seg_futures = {
                    seg_executor.submit(
                        _run_segment_filter_scan, filepath,
                        seg_start, seg_end, vf_str, seg_af_str, has_audio, cfg,
                        cancel_event, process_owner
                    ): idx
                    for idx, (seg_start, seg_end) in enumerate(segments)
                }

                completed = 0
                for future in as_completed(seg_futures):
                    if cancel_event and cancel_event.is_set():
                        cancelled = True
                        for f in seg_futures:
                            if not f.done():
                                f.cancel()
                        break

                    idx = seg_futures[future]
                    try:
                        seg_stderr = future.result()
                    except Exception:
                        seg_stderr = None
                    if seg_stderr is None:
                        failed_segments += 1
                        seg_stderr = ""
                    else:
                        segment_capture_truncated += getattr(
                            seg_stderr, "truncated_records", 0)
                    # -ss before -i zeroes the segment's PTS, so shift every
                    # reported timestamp back onto the whole-file timeline.
                    segment_results.append((idx, _close_open_regions(
                        _offset_event_times(seg_stderr, segments[idx][0]),
                        segments[idx][1])))
                    completed += 1
                    if progress_callback:
                        progress_callback("filter", completed * (duration / len(segments)), duration, "Deep Scan [combined]")

            if cancelled:
                return

            if failed_segments:
                # Never let a failed segment masquerade as a clean one. "Deep
                # Scan" is in ADVISORY_CHECKS so this actually reaches the
                # verdict as PASS_WITH_WARNINGS instead of being discarded.
                covered_pct = (len(segments) - failed_segments) / len(segments) * 100
                result.set_metadata("deep_scan_coverage_pct", round(covered_pct, 1))
                result.add_warn(
                    "Deep Scan",
                    f"{failed_segments} of {len(segments)} segments failed to scan "
                    f"(timeout or decode error). Only ~{covered_pct:.0f}% of the file "
                    f"was analysed — findings below are not a full-coverage result.",
                    confidence=40,
                )

            if failed_segments and failed_segments >= len(segments):
                # Nothing was scanned at all. Parsing the empty stderr below
                # would still record clean PASSes ("Full decode completed
                # with no errors") for checks that never ran — the coverage
                # warning above is the whole verdict.
                return

            segment_results.sort(key=lambda x: x[0])
            stderr_text, truncated = _cap_stderr_text(
                "\n".join(r[1] for r in segment_results), cfg)
            total_truncated = segment_capture_truncated + truncated
            if total_truncated:
                result.set_metadata(
                    "deep_scan_stderr_truncated_lines", total_truncated)

        except Exception as e:
            result.add_warn("Filter Scan", f"Segment scan failed: {str(e)[:100]}. Falling back to single-pass.")
            use_segments = False

    if not use_segments:
        # Single-pass combined scan (decode + filters in one FFmpeg call).
        # -v info is required: the detect filters log their findings at info level.
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            result.add_skip("Deep Scan", "FFmpeg not found.")
            return
        ft = str(cfg.get("filter_threads", 4))
        filter_cmd = [ffmpeg_path, "-nostdin", "-hide_banner"] + hwaccel_args + [
            "-v", "info", "-progress", "pipe:1",
            "-i", filepath,
            "-filter_threads", ft,
            "-vf", vf_str
        ]
        if af_str:
            filter_cmd += ["-af", af_str]
        filter_cmd += ["-f", "null", "-"]

        try:
            proc = subprocess.Popen(
                filter_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, bufsize=0, env=_get_clean_env(),
                **_subprocess_kwargs()
            )
            _register_owned_proc(proc, process_owner)
            capture = _BoundedStderrCapture(cfg)
            stderr_errors = []
            stdout_errors = []
            decode_progressed = False

            def _read_stderr_combined():
                try:
                    capture.peak_pending_chars = _drain_binary_records(
                        proc.stderr, capture.add)
                except (OSError, ValueError, TypeError, AssertionError) as exc:
                    stderr_errors.append(exc)

            def _handle_progress_record(line: str):
                nonlocal decode_progressed
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                        if us > 0:
                            decode_progressed = True
                        if progress_callback:
                            progress_callback(
                                "filter", us / 1_000_000, duration,
                                "Deep Scan [combined]")
                    except (ValueError, IndexError):
                        pass

            def _read_stdout_progress():
                try:
                    _drain_binary_records(proc.stdout, _handle_progress_record)
                except (OSError, ValueError, TypeError, AssertionError) as exc:
                    stdout_errors.append(exc)

            stderr_thread = threading.Thread(
                target=_read_stderr_combined, daemon=True)
            stdout_thread = threading.Thread(
                target=_read_stdout_progress, daemon=True)
            stderr_thread.start()
            stdout_thread.start()

            def _join_reader(thread, stream) -> bool:
                thread.join(timeout=5)
                if thread.is_alive():
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
                    thread.join(timeout=1)
                return not thread.is_alive()

            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    pass
                _join_reader(stderr_thread, proc.stderr)
                _join_reader(stdout_thread, proc.stdout)
                result.add_warn(
                    "Deep Scan", f"Scan timed out after {int(timeout)}s.")
                if progress_callback:
                    progress_callback(
                        "filter", duration, duration, "Deep Scan [combined]")
                return

            readers_done = (
                _join_reader(stderr_thread, proc.stderr)
                and _join_reader(stdout_thread, proc.stdout)
            )
            if cancel_event and cancel_event.is_set():
                return
            if not readers_done or stderr_errors or stdout_errors:
                result.add_warn(
                    "Deep Scan",
                    "FFmpeg output could not be read completely; frame-level "
                    "checks were not treated as clean.",
                    confidence=40,
                )
                return

            stderr_text = capture.result()
            if stderr_text.truncated_records:
                result.set_metadata(
                    "deep_scan_stderr_truncated_lines",
                    stderr_text.truncated_records)

            if returncode and not decode_progressed and not any(
                    tok in stderr_text for tok in (
                        "black_start", "freeze_start", "silence_start")):
                # FFmpeg exited without decoding a single frame. Parsing the
                # (event-free) stderr below would report every frame-level
                # check as a clean PASS for a file that never decoded.
                if hwaccel_args and _stderr_shows_device_failure(stderr_text):
                    _hwaccel_failed_methods.add(hwaccel_args[1])
                result.add_warn(
                    "Deep Scan",
                    f"FFmpeg could not decode this file (exit code "
                    f"{returncode}); frame-level checks were not run. "
                    f"The file may be corrupt or use an unsupported codec.",
                    confidence=40,
                )
                return

        except FileNotFoundError:
            result.add_skip("Deep Scan", "FFmpeg not found.")
            return
        finally:
            if 'proc' in locals():
                _unregister_owned_proc(proc, process_owner)

    if progress_callback:
        progress_callback("filter", duration, duration, "Deep Scan [combined]")

    # ---- Parse decode errors from stderr (merged from Pass 1) ----
    stderr_clean = _filter_hwaccel_noise(stderr_text)
    # Regions still open when the input ended (see _close_open_regions).
    events_text = _close_open_regions(stderr_text, duration)
    decode_errors = _extract_decode_errors(stderr_clean)

    if not decode_errors:
        result.add_pass("Decode Integrity", "Full decode completed with no errors.")
    else:
        count = len(decode_errors)
        sample = "; ".join(e[:80] for e in decode_errors[:3])
        result.add_fail("Decode Integrity", f"{count} decode error(s). First 3: {sample}")
        result.set_metadata("decode_errors", count)

    # ---- Parse filter results from combined stderr ----

    # Check 15: Black frames
    black_events = _merge_event_intervals(_pair_detect_events(events_text, "black"))
    # FIX: Exclude black events in first 1s and last 1s (legitimate fade-in/fade-out)
    # Trim the first and last second instead of discarding whole events:
    # dropping any event that touched a margin let an entirely black file,
    # or one opening or ending on 20s of black, pass as "no black frames".
    if duration > 3.0:
        trimmed = []
        for start, dur in black_events:
            begin = max(start, 1.0)
            finish = min(start + dur, duration - 1.0)
            if finish - begin > 0:
                trimmed.append((begin, finish - begin))
        black_events = trimmed

    if black_events:
        total_black = sum(d for _, d in black_events)
        count = len(black_events)
        positions = _format_event_list(black_events)
        result.set_metadata("black_frame_sequences", count)
        result.set_metadata("black_frame_total_seconds", round(total_black, 2))
        # Operators reported being unable to find flagged black frames because
        # only the first event's raw seconds offset was given. Every position
        # is now recorded as a scrubbable timecode.
        result.set_metadata("black_frame_timestamps", positions)
        if total_black > duration * 0.1:
            # Contextual suppression: short total black is likely transitions/slates
            if total_black < 4.0:
                result.add_warn("Black Frames", f"{count} black sequence(s), total {total_black:.1f}s (likely transitions/slates). At: {positions}", confidence=60)
            else:
                result.add_fail("Black Frames", f"{count} black sequence(s), total {total_black:.1f}s. At: {positions}", confidence=90)
        else:
            result.add_warn("Black Frames", f"{count} black sequence(s), total {total_black:.1f}s. At: {positions}", confidence=65)
    else:
        result.add_pass("Black Frames", "No black frame sequences detected.")

    # Check 16: Freeze frames
    freeze_events = _merge_event_intervals(_pair_detect_events(events_text, "freeze"))

    if freeze_events:
        total_freeze = sum(d for _, d in freeze_events)
        count = len(freeze_events)
        positions = _format_event_list(freeze_events)
        result.set_metadata("freeze_frame_count", count)
        result.set_metadata("freeze_frame_total_seconds", round(total_freeze, 2))
        result.set_metadata("freeze_frame_timestamps", positions)
        if total_freeze > duration * 0.1:
            result.add_fail("Freeze Frames", f"{count} freeze event(s), total {total_freeze:.1f}s. At: {positions}")
        else:
            result.add_warn("Freeze Frames", f"{count} freeze event(s), total {total_freeze:.1f}s. At: {positions}")
    else:
        result.add_pass("Freeze Frames", "No freeze events detected.")

    # Check 17: Audio silence
    if has_audio:
        silence_events = _merge_event_intervals(_pair_detect_events(events_text, "silence"))

        if silence_events:
            count = len(silence_events)
            # A region still open at EOF has no end token; _pair_detect_events
            # gives it a zero duration, so close it against the file duration
            # rather than reporting a fabricated 0.0s.
            silence_events = [
                (start, dur if dur > 0 else max(0.0, duration - start))
                for start, dur in silence_events
            ]
            total_silence = sum(d for _, d in silence_events)
            positions = _format_event_list(silence_events)
            result.set_metadata("silence_regions", count)
            result.set_metadata("silence_total_seconds", round(total_silence, 2))
            result.set_metadata("silence_timestamps", positions)

            if total_silence > duration * 0.5 and duration > 30:
                # Long silence (>50% of file) - real issue
                result.add_fail("Audio Silence", f"{count} silence region(s), total {total_silence:.1f}s. Extended silence in deliverable. At: {positions}", confidence=90)
            elif total_silence < 1.0:
                # Contextual suppression: very short silence is normal
                result.add_pass("Audio Silence", f"{count} brief silence(s), total {total_silence:.1f}s (natural pauses)", confidence=75)
            else:
                result.add_warn("Audio Silence", f"{count} silence region(s), total {total_silence:.1f}s. At: {positions}", confidence=70)
        else:
            result.add_pass("Audio Silence", "No extended silence detected.")
    else:
        result.add_skip("Audio Silence", "No audio stream to analyze.")

    # Audio Loudness (EBU R128) was removed in v0.4.4. It used to record a SKIP
    # entry saying so, which meant every report and the Checks matrix still
    # listed a row for a check that no longer exists, reading as "N/A" to
    # operators. A removed check should leave no trace.


# ============================================================================
# MEDIAINFO ENRICHED METADATA (Check 20)
# ============================================================================

def run_mediainfo_enrichment(filepath: str, result: QAResult, cfg: dict,
                             process_owner=None,
                             cancel_event: Optional[threading.Event] = None):
    """Check 20: Use MediaInfo for enriched metadata extraction.
    Adds writing library, encoding settings, and commercial format names
    that FFprobe may not expose. Also cross-validates key fields.
    Gracefully skips if MediaInfo is not installed.
    """
    if not cfg.get("use_mediainfo", True):
        return

    if not _HAS_TOOLS_MODULE:
        return

    mi_path = get_mediainfo_path()
    if not mi_path:
        return

    try:
        cmd = [mi_path, "--Output=JSON", filepath]
        proc = _run_owned_subprocess(
            cmd, 60, process_owner, cancel_event)
        if proc is None or proc.returncode != 0:
            return

        mi_data = json.loads(proc.stdout)
        tracks = mi_data.get("media", {}).get("track", [])

        for track in tracks:
            track_type = track.get("@type", "")

            if track_type == "General":
                if track.get("Encoded_Application"):
                    result.set_metadata("encoding_app", track["Encoded_Application"])
                if track.get("Encoded_Library"):
                    result.set_metadata("encoding_library", track["Encoded_Library"])
                if track.get("Format_Commercial_IfAny"):
                    result.set_metadata("format_commercial", track["Format_Commercial_IfAny"])
                if track.get("FileSize"):
                    try:
                        size_mb = int(track["FileSize"]) / (1024 * 1024)
                        result.set_metadata("file_size_mb", round(size_mb, 1))
                    except (ValueError, TypeError):
                        pass
                if track.get("OverallBitRate"):
                    try:
                        overall_mbps = int(track["OverallBitRate"]) / 1_000_000
                        result.set_metadata("mediainfo_overall_bitrate_mbps", round(overall_mbps, 2))
                    except (KeyError, ValueError, TypeError):
                        pass
    except Exception:
        pass


def discover_video_files(path: str, extensions: set = None) -> List[str]:
    """Discover video files in a directory or return single file.

    Never raises on filesystem errors: a PermissionError from a locked /
    disconnected Drive folder used to escape into a Qt slot and crash the
    app via the global crash dialog. Callers get an empty list instead.
    """
    if extensions is None:
        extensions = DEFAULT_CONFIG["video_extensions"]

    try:
        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in extensions:
                return [path]
            return []

        if os.path.isdir(path):
            files = []
            try:
                entries = sorted(os.listdir(path))
            except (PermissionError, OSError):
                return []
            for f in entries:
                try:
                    ext = os.path.splitext(f)[1].lower()
                except Exception:
                    continue
                if ext in extensions:
                    files.append(os.path.join(path, f))
            return files
    except (PermissionError, OSError):
        return []

    return []


def run_qa_checks(filepath: str, cfg: dict = None,
                  strict: bool = False, naming_check: bool = False,
                  quick: bool = False,
                  progress_callback: Optional[Callable] = None,
                  cancel_event: Optional[threading.Event] = None,
                  process_owner=None) -> QAResult:
    """
    Run all QA checks on a single file.
    Wrapped in try/except for per-file error isolation (v1.4 parity).
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG.copy()

    result = QAResult(filepath)
    # Record the strict flag on the result itself: finalize() is called later
    # by the worker with no arguments, and only the verdict method knows how
    # to escalate warnings — flipping result.passed here left status as
    # PASS_WITH_WARNINGS because _result_status() only reads status/passed.
    result.strict = bool(strict)

    try:
        # Check for cancellation at start
        if cancel_event and cancel_event.is_set():
            return result

        # --- Metadata checks (fast) ---
        check_container_format(filepath, result, cfg)
        if not result.passed:
            return result

        if cancel_event and cancel_event.is_set():
            return result

        probe_data = run_ffprobe(filepath, process_owner, cancel_event)
        if probe_data is None:
            result.add_fail("FFprobe", "FFprobe failed to read the file. "
                           "Possible causes: file is corrupt, codec not supported by installed FFmpeg, "
                           "or file is still being written. Try opening in VLC to verify.")
            return result

        if cancel_event and cancel_event.is_set():
            return result

        check_video_codec(probe_data, result, cfg)
        check_resolution(probe_data, result, cfg)
        check_frame_rate(probe_data, result, cfg)
        check_cfr(probe_data, result, cfg)
        check_bitrate(probe_data, result, cfg)
        check_audio(probe_data, result, cfg)
        # REMOVED v0.4.4: check_duration(probe_data, result, cfg)
        # REMOVED v0.4.4: check_color_space(probe_data, result, cfg)

        if naming_check:
            if cancel_event and cancel_event.is_set():
                return result
            # REMOVED v0.4.4: check_naming(filepath, result, cfg)

        # --- Metadata-based structural checks ---
        if cancel_event and cancel_event.is_set():
            return result

        check_av_duration_mismatch(probe_data, result, cfg)
        # REMOVED v0.4.4: check_pixel_aspect_ratio(probe_data, result, cfg)
        check_frame_count(probe_data, result, cfg)

        # --- Deep scan checks (require full decode) ---
        if cancel_event and cancel_event.is_set():
            return result

        run_deep_scan(filepath, result, cfg,
                      progress_callback=progress_callback,
                      cancel_event=cancel_event,
                      quick=quick,
                      probe_data=probe_data,
                      process_owner=process_owner)

        # --- Multi-tool checks (v0.4.1 — run in parallel for speed) ---
        if cancel_event and cancel_event.is_set():
            return result
        with ThreadPoolExecutor(max_workers=2) as tool_executor:
            # REMOVED v0.4.4: mc_future = tool_executor.submit(run_mediaconch_check, filepath, result, cfg)
            mi_future = tool_executor.submit(
                run_mediainfo_enrichment, filepath, result, cfg,
                process_owner, cancel_event)
            # REMOVED v0.4.4: ss_future = tool_executor.submit(
                # REMOVED v0.4.4: run_signalstats_check, filepath, result, cfg,
                # REMOVED v0.4.4: file_duration, cancel_event
            # REMOVED v0.4.4: )
            # Wait for all to complete (they write to result directly)
            for f in [mi_future]:
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    f.result(timeout=120)
                except Exception:
                    pass

        # --- Strict mode enforcement ---
        # Handled by QAResult.overall_status() via result.strict (recorded
        # above): any flagged check escalates to MISMATCH at finalize() time.
        # The old code flipped result.passed here, which the verdict helpers
        # never read once status was cached — strict was a silent no-op.
        if strict:
            result.strict = True

    except KeyboardInterrupt:
        raise
    except Exception as e:
        result.add_fail("Internal Error", f"Unexpected error during scan: {type(e).__name__}: {str(e)[:200]}")

    return result


# ============================================================================
# FILE DISCOVERY
# ============================================================================

def run_signalstats_check(filepath: str, result: QAResult, cfg: dict,
                          duration: float = 0,
                          cancel_event: Optional[threading.Event] = None):
    """Check 21: No-reference video quality scoring via FFmpeg signalstats.
    Detects crushed blacks, clipped whites, low saturation, and banding.
    Samples evenly across the file for speed (default: 10s sample).
    Gracefully skips if signalstats filter is not available.
    """
    if not cfg.get("use_signalstats", True):
        result.add_skip("Signal Quality", "Disabled in settings.")
        return

    ffmpeg_path = "ffmpeg"
    if _HAS_TOOLS_MODULE:
        fp = get_ffmpeg_path()
        if fp:
            ffmpeg_path = fp

    # Check if signalstats filter is available (cached)
    global _ffmpeg_caps_cache
    if _HAS_TOOLS_MODULE:
        if _ffmpeg_caps_cache is None:
            _ffmpeg_caps_cache = detect_ffmpeg_capabilities(ffmpeg_path)
        if not _ffmpeg_caps_cache.get("has_signalstats", False):
            result.add_skip("Signal Quality", "FFmpeg signalstats filter not available in this build.")
            return

    if duration <= 0:
        result.add_skip("Signal Quality", "Could not determine file duration.")
        return

    # v0.4.2: Multi-sample signalstats with frame decimation
    sample_secs = cfg.get("signalstats_sample_seconds", 10)
    decimate_fps = cfg.get("signalstats_decimate_fps", 5)
    num_samples = cfg.get("signalstats_num_samples", 3)
    hwaccel_args = _get_optimal_hwaccel(cfg)
    ft = str(cfg.get("filter_threads", 4))

    # v0.4.2: fps=N decimation before signalstats reduces frames analyzed by ~6x
    vf_chain = f"fps={decimate_fps},signalstats=stat=tout+vrep+brng,metadata=mode=print"

    # Build sample points: evenly distributed across file duration
    if sample_secs <= 0 or duration <= sample_secs:
        sample_points = [(0, duration)]  # scan full file
    elif duration <= sample_secs * num_samples:
        sample_points = [(0, duration)]  # file too short for multi-sample
    else:
        # v0.4.2: 3 evenly-spaced samples (beginning, middle, end)
        spacing = duration / (num_samples + 1)
        sample_points = [
            (max(0, spacing * (i + 1) - sample_secs / 2), sample_secs)
            for i in range(num_samples)
        ]

    # Run all samples and collect stderr
    all_stderr = []
    for sp_start, sp_dur in sample_points:
        ss_args = ["-ss", str(sp_start)] if sp_start > 0 else []
        t_args = ["-t", str(sp_dur)] if sp_dur < duration else []

        cmd = [ffmpeg_path, "-nostdin"] + hwaccel_args + ss_args + [
            "-i", filepath
        ] + t_args + [
            "-filter_threads", ft,
            "-vf", vf_chain,
            "-an", "-f", "null", "-"
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=max(60, duration * 0.5), env=_get_clean_env(), **_subprocess_kwargs())
            all_stderr.append(proc.stderr)
        except (subprocess.TimeoutExpired, Exception):
            pass

    stderr = "\n".join(all_stderr)

    try:

        if cancel_event and cancel_event.is_set():
            return

        # Parse signalstats output
        ymin_vals = [float(m) for m in re.findall(r"YMIN=(\d+\.?\d*)", stderr)]
        ymax_vals = [float(m) for m in re.findall(r"YMAX=(\d+\.?\d*)", stderr)]
        yavg_vals = [float(m) for m in re.findall(r"YAVG=(\d+\.?\d*)", stderr)]
        satavg_vals = [float(m) for m in re.findall(r"SATAVG=(\d+\.?\d*)", stderr)]
        brng_vals = [float(m) for m in re.findall(r"BRNG=(\d+\.?\d*)", stderr)]
        tout_vals = [float(m) for m in re.findall(r"TOUT=(\d+\.?\d*)", stderr)]

        if not ymin_vals:
            result.add_skip("Signal Quality", "Could not parse signalstats output.")
            return

        # Calculate averages
        avg_ymin = sum(ymin_vals) / len(ymin_vals)
        avg_ymax = sum(ymax_vals) / len(ymax_vals) if ymax_vals else 235
        avg_yavg = sum(yavg_vals) / len(yavg_vals) if yavg_vals else 128
        avg_satavg = sum(satavg_vals) / len(satavg_vals) if satavg_vals else 50
        avg_brng = sum(brng_vals) / len(brng_vals) if brng_vals else 0
        avg_tout = sum(tout_vals) / len(tout_vals) if tout_vals else 0

        result.metadata["signal_ymin_avg"] = round(avg_ymin, 1)
        result.metadata["signal_ymax_avg"] = round(avg_ymax, 1)
        result.metadata["signal_yavg"] = round(avg_yavg, 1)
        result.metadata["signal_satavg"] = round(avg_satavg, 1)
        result.metadata["signal_brng_avg"] = round(avg_brng, 1)
        result.metadata["signal_tout_avg"] = round(avg_tout, 3)

        # Quality assessment
        issues = []
        ymin_floor = cfg.get("quality_ymin_floor", 16)
        ymax_ceil = cfg.get("quality_ymax_ceil", 235)
        satavg_min = cfg.get("quality_satavg_min", 5)

        # Check for crushed blacks (YMIN consistently at 0)
        crushed_pct = sum(1 for v in ymin_vals if v < ymin_floor) / len(ymin_vals) * 100
        if crushed_pct > 50:
            issues.append(f"crushed blacks in {crushed_pct:.0f}% of frames (YMIN<{ymin_floor})")

        # Check for clipped whites (YMAX consistently at 255)
        clipped_pct = sum(1 for v in ymax_vals if v > ymax_ceil) / len(ymax_vals) * 100 if ymax_vals else 0
        if clipped_pct > 50:
            issues.append(f"clipped whites in {clipped_pct:.0f}% of frames (YMAX>{ymax_ceil})")

        # Check for desaturation
        if avg_satavg < satavg_min:
            issues.append(f"very low saturation (avg={avg_satavg:.1f})")

        # Check for out-of-range values (banding/artifacts)
        if avg_brng > 5:
            issues.append(f"out-of-broadcast-range pixels detected (BRNG avg={avg_brng:.1f})")

        # Check for temporal outliers (compression artifacts)
        if avg_tout > 0.02:
            issues.append(f"temporal outliers detected (TOUT avg={avg_tout:.3f})")

        detail = (f"YMIN avg={avg_ymin:.0f}, YMAX avg={avg_ymax:.0f}, "
                  f"YAVG={avg_yavg:.0f}, SAT avg={avg_satavg:.0f}")

        if issues:
            result.add_warn("Signal Quality", f"{detail}. Issues: {'; '.join(issues)}")
        else:
            result.add_pass("Signal Quality", f"{detail}. No quality anomalies detected.")

    except subprocess.TimeoutExpired:
        result.add_warn("Signal Quality", "Signalstats analysis timed out.")
    except FileNotFoundError:
        result.add_skip("Signal Quality", "FFmpeg not found.")
    except Exception as e:
        result.add_skip("Signal Quality", f"Signalstats error: {str(e)[:100]}")


# ============================================================================
# ORCHESTRATION
# ============================================================================






