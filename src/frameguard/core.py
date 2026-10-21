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
from collections import Counter, deque
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Callable

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
        get_ffmpeg_path, get_ffprobe_path, get_mediainfo_path,
    )
    _HAS_TOOLS_MODULE = True
except ImportError:
    _HAS_TOOLS_MODULE = False

    # Provide minimal fallbacks so callers can still use these APIs.
    def get_ffmpeg_path():
        return "ffmpeg"

    def get_ffprobe_path():
        return "ffprobe"

    def get_mediainfo_path():
        return None


# ============================================================================
# PRESET PROFILES
# ============================================================================
# Keys other than label/width/height/fps/fps_tolerance/min_bitrate_mbps are
# engine config keys applied verbatim by ScanWorker.configure(). Only presets
# that are unambiguously 360 carry a projection requirement: "8K High-End VR"
# is 16:9 and may be flat or stereo, so it states nothing about projection.

PRESETS = {
    "4k30": {"label": "4K UHD @ 30fps", "width": 3840, "height": 2160, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 20},
    "4k60": {"label": "4K UHD @ 60fps", "width": 3840, "height": 2160, "fps": 60.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 30},
    "4k24": {"label": "4K UHD @ 24fps (Cinematic)", "width": 3840, "height": 2160, "fps": 24.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 18},
    "1080p30": {"label": "1080p Full HD @ 30fps", "width": 1920, "height": 1080, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 8},
    "1080p60": {"label": "1080p Full HD @ 60fps", "width": 1920, "height": 1080, "fps": 60.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 12},
    "1080p24": {"label": "1080p Full HD @ 24fps (Cinematic)", "width": 1920, "height": 1080, "fps": 24.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 6},
    "720p30": {"label": "720p HD @ 30fps (Proxy/Review)", "width": 1280, "height": 720, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 4},
    "5.7k30": {"label": "5.7K @ 30fps (360 VR Capture)", "width": 5760, "height": 2880, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 40,
               "required_projection": "equirectangular"},
    "8k30": {"label": "8K UHD @ 30fps (High-End VR)", "width": 7680, "height": 4320, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 60},
}

# Preset fields that are not engine config keys under their own name.
PRESET_FIELD_TO_CONFIG = {
    "width": "target_width",
    "height": "target_height",
    "fps": "target_fps",
    "fps_tolerance": "fps_tolerance",
    "min_bitrate_mbps": "min_bitrate_mbps",
}


def preset_config(preset_key: str) -> dict:
    """Engine config overrides for a built-in preset (raises KeyError)."""
    entry = PRESETS[preset_key]
    config = {}
    for field, value in entry.items():
        if field == "label":
            continue
        config[PRESET_FIELD_TO_CONFIG.get(field, field)] = value
    return config


# ---------------------------------------------------------------------------
# CHECK CLASSIFICATION
# ---------------------------------------------------------------------------
# A confident FAIL on an essential check makes the file a MISMATCH. Checks
# whose enforcement is optional (Color / HDR, XR Metadata, Audio Loudness)
# are essential by name but only ever FAIL when the spec asks for enforcement;
# otherwise they report PASS (informational) or WARN (advisory).

ESSENTIAL_CHECKS = {
    "Container Format", "Video Codec", "Resolution", "Aspect Ratio",
    "Frame Rate", "CFR Check", "Scan Type", "Color / HDR", "XR Metadata",
    "Bitrate", "Audio Stream", "Audio Codec", "Audio Channels",
    "Audio Sample Rate", "A/V Duration", "File Integrity", "Audio Silence",
    "Dead Channel", "Audio Loudness",
}

ADVISORY_CHECKS = {
    "Frame Count", "Black Frames", "Freeze Frames",
    # Raised when part of the analysis could not run.
    "Deep Scan",
}

# The checks produced by the full-decode pass. When that pass cannot run,
# every one of them is reported "Not verified" rather than left out.
FRAME_LEVEL_CHECKS = (
    "File Integrity", "Black Frames", "Freeze Frames", "Audio Silence",
    "Dead Channel", "Audio Loudness",
)

NOT_VERIFIED_PREFIX = "Not verified: "

# ---------------------------------------------------------------------------
# CHECK SEVERITY WEIGHTS (visual hierarchy)
# ---------------------------------------------------------------------------
# overall_status() collapses everything to three buckets, so the GUI sorts
# within those buckets by weight x confidence. Weights are 0-100; checks not
# listed default to 50.
CHECK_SEVERITY_WEIGHTS = {
    "Container Format": 100,
    "FFprobe": 100,
    "Internal Error": 95,
    "File Integrity": 90,
    "Video Codec": 75,
    "Resolution": 70,
    "Aspect Ratio": 70,
    "A/V Duration": 65,
    "XR Metadata": 60,
    "Bitrate": 60,
    "Audio Stream": 60,
    "Scan Type": 55,
    "Audio Channels": 55,
    "Color / HDR": 50,
    "Frame Count": 50,
    "Audio Sample Rate": 45,
    "CFR Check": 45,
    "Dead Channel": 40,
    "Deep Scan": 40,
    "Freeze Frames": 38,
    "Black Frames": 35,
    "Audio Silence": 30,
    "Audio Codec": 25,
    "Audio Loudness": 25,
    "Frame Rate": 20,
}


def check_severity(check_name: str, status: str, confidence: int = 100) -> float:
    """Severity score for one check entry: weight x confidence.

    FAIL counts full, WARN counts half (same defect, less certainty about
    impact), PASS/SKIP count zero.
    """
    base = CHECK_SEVERITY_WEIGHTS.get(check_name, 50)
    conf = max(0, min(100, confidence or 0)) / 100.0
    if status == "FAIL":
        return base * conf
    if status == "WARN":
        return base * conf * 0.5
    return 0.0


def result_severity(result: "QAResult") -> tuple:
    """(score, top_check_name) for a whole file: max over its checks."""
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

# Enumerated spec values. "any" means "report, do not enforce".
BIT_DEPTH_CHOICES = ("any", 8, 10, 12, 16)
TRANSFER_CHOICES = ("any", "sdr", "pq", "hlg", "hdr")
PROJECTION_CHOICES = (
    "any", "none", "equirectangular", "cubemap", "fisheye",
    "half_equirectangular", "tiled_equirectangular", "rectilinear",
)
STEREO_CHOICES = ("any", "mono", "top_bottom", "side_by_side")
LOUDNESS_MODES = ("off", "advisory", "essential")

DEFAULT_CONFIG = {
    # Picture geometry and timing. Resolution must match exactly unless
    # allow_larger_resolution; the frame rate must match within tolerance
    # (integer multiples only when allow_fps_multiples).
    "target_width": 3840,
    "target_height": 2160,
    "allow_larger_resolution": False,
    "target_fps": 30.0,
    "fps_tolerance": 0.5,
    "allow_fps_multiples": False,
    "min_bitrate_mbps": 20,

    # Codec acceptance lists
    "accepted_codecs": ["h264", "hevc", "h265", "av1", "prores", "prores_ks", "prores_aw"],
    "accepted_containers": [".mp4", ".mov", ".mkv", ".mxf"],
    "accepted_audio_codecs": ["aac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_s24be", "pcm_s32be", "pcm_f32le", "opus", "flac"],

    # Audio
    "audio_required": True,
    "min_audio_sample_rate": 44100,

    # Frame timing: fraction of frame intervals allowed to deviate by more
    # than half a frame from the dominant interval before the file is VFR.
    "require_cfr": True,
    "vfr_threshold": 0.01,
    # Files larger than this get a sampled (windowed) packet-timestamp scan
    # so the VFR check never reads a 100 GB master end to end.
    "vfr_full_scan_max_mb": 2048,

    # Picture format
    "require_progressive": True,
    "min_bit_depth": "any",
    "required_transfer": "any",

    # XR metadata
    "required_projection": "any",
    "required_stereo_mode": "any",

    # File integrity (full software decode)
    "max_decode_errors": 0,

    # File discovery
    "video_extensions": {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm"},

    # Deep-scan output bounds
    "deep_scan_max_stderr_lines": 5000,
    "deep_scan_max_stderr_chars": 512 * 1024,

    # Deep scan thresholds
    "black_detect_duration": 0.5,
    "black_detect_pix_threshold": 0.10,
    "freeze_detect_noise": 0.003,
    "freeze_detect_duration": 2.0,
    "silence_noise_db": -50,
    "silence_duration": 2.0,
    # A channel (or a whole track) silent for at least this fraction of its
    # length is dead.
    "dead_channel_ratio": 0.95,

    # Loudness (EBU R128). "off" skips ebur128 entirely.
    "loudness_mode": "advisory",
    "target_loudness_lufs": -23.0,
    "loudness_tolerance_lu": 2.0,
    "true_peak_max_dbtp": -1.0,

    # A/V sync
    "duration_mismatch_threshold": 0.5,

    # Timeouts
    "timeout_base_seconds": 120,
    "timeout_multiplier": 3.0,
    "timeout_max_seconds": 7200,

    # Optional enrichment
    "use_mediainfo": True,

    # Threads for the deep-scan filter graph.
    "filter_threads": min(os.cpu_count() or 4, 8),

    # Accepted for settings/preset compatibility only; the engine never reads
    # them. The integrity scan always decodes in software in one pass (see
    # run_deep_scan), so these cannot change a verdict.
    "use_hwaccel": False,
    "segment_enabled": True,
    "segment_count": min(os.cpu_count() or 4, 4),
}

COMPAT_ONLY_CONFIG_KEYS = frozenset({"use_hwaccel", "segment_enabled", "segment_count"})

# Keys removed in 0.9.0. Old saved settings and presets may still carry them;
# they are ignored rather than rejected so those files keep loading.
RETIRED_CONFIG_KEYS = frozenset({
    "hwaccel_method", "max_workers", "segment_min_duration",
    "deep_scan_max_file_gb", "deep_scan_max_duration_s",
    "min_duration_fail", "min_duration_warn",
    "naming_pattern", "naming_example",
    "use_mediaconch", "use_signalstats", "signalstats_sample_seconds",
    "signalstats_decimate_fps", "signalstats_num_samples",
    "quality_ymin_floor", "quality_ymax_ceil", "quality_satavg_min",
})


def _get_clean_env() -> dict:
    """Return a sanitized environment for subprocess calls.

    PyInstaller modifies LD_LIBRARY_PATH (Linux), DYLD_LIBRARY_PATH (macOS),
    and DLL search paths (Windows) to prefer bundled libs. External programs
    like FFmpeg and MediaInfo need their own libs, so we restore the original
    environment variables.
    """
    import sys as _sys
    env = os.environ.copy()
    if getattr(_sys, 'frozen', False):
        if 'LD_LIBRARY_PATH_ORIG' in env:
            env['LD_LIBRARY_PATH'] = env['LD_LIBRARY_PATH_ORIG']
        elif 'LD_LIBRARY_PATH' in env:
            del env['LD_LIBRARY_PATH']
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
      strict errors, so a non-ASCII filename raised UnicodeDecodeError.
    """
    kwargs = {"encoding": "utf-8", "errors": "replace"}
    if os.name == 'nt':  # Windows
        kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs['startupinfo'] = si
    return kwargs


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
        self.severity_score = 0.0
        self.severity_reason = ""
        # Strict mode ("treat warnings as failures"). Recorded here because the
        # worker calls finalize() with no arguments after run_qa_checks().
        self.strict = False
        # Audit trail for exports: the exact settings and tools that produced
        # this verdict, so a report can be reproduced later.
        self.config: Dict = {}
        self.tool_versions: Dict = {}
        self.engine_version = __version__
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

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
        """A check that does not apply to this file (e.g. silence with no audio)."""
        with self._lock:
            self.checks.append((check_name, "SKIP", detail, confidence))

    def add_not_verified(self, check_name: str, reason: str):
        """Record an analysis that should have run but could not.

        SKIP is ignored by the verdict, so an unrun integrity scan used to read
        exactly like a clean one. "Not verified" is a WARN with an explicit
        label: the file can never come out a clean PASS on unverified ground.
        """
        with self._lock:
            detail = f"{NOT_VERIFIED_PREFIX}{reason}"
            self.checks.append((check_name, "WARN", detail, 100))
            self.warnings.append(f"{check_name}: {detail}")
            self.metadata.setdefault("not_verified", [])
            if check_name not in self.metadata["not_verified"]:
                self.metadata["not_verified"].append(check_name)

    def set_metadata(self, key, value):
        with self._lock:
            self.metadata[key] = value

    def plain_summary(self) -> str:
        """Return a brief, jargon-free one-line summary of what's wrong."""
        if self.passed and not self.warnings:
            return "All checks passed."

        _PLAIN = {
            "Container Format": "wrong file type",
            "Video Codec": "video uses an unsupported format",
            "Resolution": "video resolution doesn't match the target",
            "Aspect Ratio": "picture shape doesn't match the target",
            "Frame Rate": "frame rate doesn't match the target",
            "CFR Check": "frame rate is inconsistent (variable)",
            "Scan Type": "video is interlaced",
            "Color / HDR": "bit depth or HDR format doesn't match the spec",
            "XR Metadata": "360/stereo metadata is missing or wrong",
            "Bitrate": "video quality (bitrate) is too low",
            "Audio Codec": "audio uses an unsupported format",
            "Audio Sample Rate": "audio sample rate is too low",
            "Audio Channels": "audio is missing or has no channels",
            "Audio Stream": "audio is missing",
            "A/V Duration": "audio and video lengths don't match",
            "Frame Count": "frame count doesn't match the expected number",
            "File Integrity": "file is incomplete or damaged",
            "Black Frames": "unexpected black frames detected",
            "Freeze Frames": "video freezes detected",
            "Audio Silence": "long stretches of silence found",
            "Dead Channel": "an audio channel is silent",
            "Audio Loudness": "audio levels are outside the target range",
            "FFprobe": "file could not be read (may be corrupt)",
            "Internal Error": "an unexpected error occurred during the scan",
        }

        issues = []
        for check_name, status, *_ in self.checks:
            if status == "FAIL":
                issues.append(_PLAIN.get(check_name, check_name.lower() + " issue"))

        if not issues:
            warn_names = []
            for check_name, status, detail, *_ in self.checks:
                if status != "WARN":
                    continue
                if str(detail).startswith(NOT_VERIFIED_PREFIX):
                    warn_names.append(f"{check_name.lower()} not verified")
                else:
                    warn_names.append(_PLAIN.get(check_name, check_name.lower() + " concern"))
            if warn_names:
                unique = list(dict.fromkeys(warn_names))
                return "Heads up: " + ", ".join(unique) + "."
            return "Passed with minor notes."

        if len(issues) == 1:
            return issues[0][0].upper() + issues[0][1:] + "."
        return ", ".join(i.capitalize() for i in issues) + "."

    def overall_status(self, strict: bool = False) -> str:
        """Return the file's overall verdict.

        Returns:
            "PASS" - nothing flagged anything
            "PASS_WITH_WARNINGS" - something was flagged (or not verified),
                                   but nothing disqualifying
            "MISMATCH" - an essential check failed with confidence

        Only a confident FAIL on an ESSENTIAL check (or a hard FFprobe/Internal
        Error) disqualifies a file. Every other non-PASS entry, including any
        "Not verified" result and checks in neither set, downgrades the file to
        PASS_WITH_WARNINGS so partial analysis never reads as a clean pass.

        When strict is true ("treat any warning as a failure"), any flagged
        check escalates to MISMATCH instead of PASS_WITH_WARNINGS.
        """
        strict = strict or bool(getattr(self, "strict", False))
        flagged = False

        for check_tuple in self.checks:
            name, status = check_tuple[0], check_tuple[1]
            confidence = check_tuple[3] if len(check_tuple) > 3 else 100

            if status in ("PASS", "SKIP"):
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
        """
        if strict:
            self.strict = True
        self.status = self.overall_status()
        score, top = result_severity(self)
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
    """Run FFprobe on a file and return its full format/stream description.

    Every stream field, tag and side-data entry is requested. An earlier
    field allow-list (``:stream_tags=rotate``) silently discarded the
    Matroska ``DURATION`` tags, so A/V duration was never checked on MKV, and
    never asked for the spherical / stereo-3D side data XR deliverables need.
    """
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", filepath]
    try:
        result = _run_owned_subprocess(cmd, 120, process_owner, cancel_event)
        if result is None or result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, TypeError, OSError):
        return None


def probe_first_video_frame(filepath: str, stream_index: int, process_owner=None,
                            cancel_event: Optional[threading.Event] = None) -> dict:
    """Decode one frame of ``stream_index`` and return its ffprobe frame entry.

    Stream headers omit things that only the bitstream carries: H.264 frame
    packing (stereo 3D) SEI, HDR mastering metadata, an un-flagged interlaced
    picture, colour tags absent from the container. One frame costs ~30 ms.
    Returns {} when the frame cannot be read; callers treat that as "no
    extra information", never as evidence the file is fine.
    """
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        return {}
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json",
           "-select_streams", str(stream_index), "-read_intervals", "%+#1",
           "-show_frames", "-show_entries",
           "frame=pix_fmt,color_transfer,color_primaries,color_space,"
           "interlaced_frame,top_field_first:frame_side_data:frame_tags",
           filepath]
    try:
        result = _run_owned_subprocess(cmd, 60, process_owner, cancel_event)
        if result is None or result.returncode != 0:
            return {}
        frames = json.loads(result.stdout).get("frames") or []
        return frames[0] if frames and isinstance(frames[0], dict) else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError,
            TypeError, OSError, AttributeError):
        return {}


class FrameTiming:
    """Presentation timestamps of a video stream, read from packets only.

    ``windows`` hold only presented frames (sorted pts inside the stream's
    presentation range). ``hidden_leadin`` counts packets before the stream
    start: frames an edit list hides, as left by a lossless (stream-copy)
    trim that had to keep the preceding GOP so the first shown frame can be
    decoded. They are stored and counted in nb_frames but never displayed.
    """

    def __init__(self, windows: List[List[float]], sampled: bool, description: str,
                 hidden_leadin: Optional[int] = None):
        self.windows = windows
        self.sampled = sampled
        self.description = description
        self.hidden_leadin = hidden_leadin

    @property
    def packet_count(self) -> int:
        return sum(len(w) for w in self.windows)


def _timing_windows(duration: Optional[float], file_size: int, cfg: dict) -> List[Optional[str]]:
    """``-read_intervals`` specs for the packet-timestamp scan.

    Reading packets still reads every byte of the file, so a full pass over a
    100 GB master on a network share is not acceptable as a pre-check. Files
    above ``vfr_full_scan_max_mb`` are sampled in 12 windows of 5 s spread
    evenly across the duration. [None] means "whole file".
    """
    max_bytes = float(cfg.get("vfr_full_scan_max_mb", 2048)) * 1024 * 1024
    if file_size <= max_bytes:
        return [None]
    if not duration or duration <= 0:
        return ["%+60"]
    count, length = 12, 5.0
    if duration <= count * length:
        return [None]
    step = (duration - length) / (count - 1)
    return [f"{i * step:.3f}%+{length:g}" for i in range(count)]


def probe_frame_timing(filepath: str, stream_index: int, duration: Optional[float],
                       cfg: dict, process_owner=None,
                       cancel_event: Optional[threading.Event] = None,
                       start_time: float = 0.0,
                       stream_duration: Optional[float] = None) -> Optional[FrameTiming]:
    """Read packet timestamps for one video stream (no decoding).

    Packets outside [start_time, start_time + stream_duration] are not
    presented (edit-list lead-in before, reorder spill-over after a cut), so
    they are counted as hidden lead-in or dropped rather than read as gaps.
    Returns None when ffprobe fails, so the CFR check can say "not verified"
    instead of passing on no evidence.
    """
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        return None
    try:
        file_size = os.path.getsize(filepath)
    except OSError:
        return None
    specs = _timing_windows(duration, file_size, cfg)
    windows = []
    hidden_leadin = None
    epsilon = 1e-4
    end_time = (start_time + stream_duration) if stream_duration else None
    for spec in specs:
        cmd = [ffprobe, "-v", "error", "-select_streams", str(stream_index)]
        if spec:
            cmd += ["-read_intervals", spec]
        cmd += ["-show_entries", "packet=pts_time", "-of", "csv=p=0", filepath]
        try:
            proc = _run_owned_subprocess(cmd, 300, process_owner, cancel_event)
        except (subprocess.TimeoutExpired, FileNotFoundError, TypeError, OSError):
            return None
        if proc is None or proc.returncode != 0:
            return None
        pts = []
        for line in proc.stdout.splitlines():
            value = _safe_float(line.strip().strip(","))
            if value is not None:
                pts.append(value)
        if spec is None or spec.startswith("0.000%") or spec.startswith("%"):
            # Only a window read from the very start sees the lead-in.
            hidden_leadin = sum(1 for p in pts if p < start_time - epsilon)
        presented = [p for p in pts if p >= start_time - epsilon
                     and (end_time is None or p < end_time - epsilon)]
        windows.append(sorted(presented))
    sampled = specs != [None]
    if sampled:
        description = (f"sampled {len(specs)} windows "
                       f"(file is {_format_size(file_size / 1024 ** 2)})")
    else:
        description = "all packets"
    return FrameTiming(windows, sampled, description, hidden_leadin)


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


def _calc_timeout(duration: Optional[float], cfg: dict) -> float:
    """Deep-scan timeout; the configured ceiling when the duration is unknown."""
    if not duration or duration <= 0:
        return float(cfg["timeout_max_seconds"])
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


_tool_version_cache: Dict[tuple, Dict] = {}
_tool_version_lock = threading.Lock()


def get_tool_versions() -> Dict[str, Dict[str, Optional[str]]]:
    """Path and ``-version`` banner of ffmpeg/ffprobe, cached per binary.

    Exports record these so a verdict can be reproduced: a decoder fix in a
    newer FFmpeg can change what counts as a decode error.
    """
    paths = (("ffmpeg", get_ffmpeg_path()), ("ffprobe", get_ffprobe_path()))
    key = tuple(p for _, p in paths)
    with _tool_version_lock:
        if key in _tool_version_cache:
            return dict(_tool_version_cache[key])
    versions = {}
    for name, path in paths:
        version = None
        if path:
            try:
                proc = subprocess.run([path, "-version"], capture_output=True, text=True,
                                      timeout=10, env=_get_clean_env(), **_subprocess_kwargs())
                if proc.returncode == 0 and proc.stdout:
                    version = proc.stdout.splitlines()[0].strip()
            except (subprocess.TimeoutExpired, OSError, TypeError) as exc:
                version = f"unavailable ({type(exc).__name__})"
        versions[name] = {"path": path, "version": version}
    with _tool_version_lock:
        _tool_version_cache[key] = versions
    return dict(versions)


# ============================================================================
# STREAM HELPERS
# ============================================================================

def _primary_video_stream(probe_data: dict) -> Optional[dict]:
    """The picture stream that is the deliverable.

    Cover art is stored as a video stream flagged ``attached_pic``; when it
    is listed first, judging "the first video stream" checked a PNG.
    """
    for stream in (probe_data or {}).get("streams", []) or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return stream
    return None


def _audio_streams(probe_data: dict) -> List[dict]:
    return [s for s in (probe_data or {}).get("streams", []) or []
            if s.get("codec_type") == "audio"]


def _track_label(position: int, stream: dict, total: int) -> str:
    """'Track 2 (aac 5.1)' for multi-track files, '' when there is one track."""
    if total <= 1:
        return ""
    codec = stream.get("codec_name", "?")
    layout = stream.get("channel_layout") or f"{_safe_int(stream.get('channels'), 0)}ch"
    return f"Track {position + 1} ({codec} {layout})"


def _with_track(label: str, text: str) -> str:
    return f"{label}: {text}" if label else text


def _format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _probed_duration(probe_data: dict) -> Optional[float]:
    value = _safe_float(((probe_data or {}).get("format") or {}).get("duration"))
    return value if value and value > 0 else None


def _stream_duration_text(stream: dict) -> Optional[str]:
    """Per-stream duration in seconds as text, including Matroska's tag form.

    MKV and WebM streams carry no ``duration`` field; the length lives in a
    ``DURATION`` tag formatted ``HH:MM:SS.nnnnnnnnn``.
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


def _stream_duration(stream: dict) -> Optional[float]:
    value = _safe_float(_stream_duration_text(stream or {}))
    return value if value and value > 0 else None


def _stream_rotation_degrees(stream: dict) -> int:
    """Rotation a player applies to this stream, in degrees.

    ffprobe reports it in the Display Matrix side data (negated: a video
    displayed 90 degrees clockwise carries -90); older files may carry only
    the QuickTime ``rotate`` tag. 0 when neither is present.
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


def _side_data(entries, kind: str) -> Optional[dict]:
    for entry in entries or []:
        if isinstance(entry, dict) and str(entry.get("side_data_type", "")).lower() == kind.lower():
            return entry
    return None


def _format_size(size_mb: float) -> str:
    """'40.3 MB' ('4.8 GB' from 1 GB), or KB below 0.1 MB: a 7 KB file read as '0.0 MB'."""
    if size_mb >= 1024:
        return f"{size_mb / 1024:.1f} GB"
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


# ============================================================================
# METADATA QA CHECKS
# ============================================================================

def check_container_format(filepath: str, result: QAResult, cfg: dict):
    """Validate the container extension and that the file is non-empty."""
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
    """Validate the primary video stream's codec."""
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_fail("Video Codec", "No video stream found.")
        return
    codec = str(video.get("codec_name", "unknown")).lower()
    result.set_metadata("video_codec", codec)
    if codec in cfg["accepted_codecs"]:
        result.add_pass("Video Codec", f"Codec: {codec}")
    else:
        result.add_fail("Video Codec", f"Codec '{codec}' not in accepted list {cfg['accepted_codecs']}.")


def _displayed_size(video: dict) -> Tuple[int, int, int]:
    """(width, height, rotation) of the picture as a player shows it."""
    width = _safe_int(video.get("width"), 0) or 0
    height = _safe_int(video.get("height"), 0) or 0
    rotation = _stream_rotation_degrees(video)
    if abs(rotation) % 180 == 90 and width and height:
        width, height = height, width
    return width, height, rotation


def check_resolution(probe_data: dict, result: QAResult, cfg: dict):
    """The displayed frame must equal the target exactly.

    Larger frames are accepted only with ``allow_larger_resolution``: a 4K
    master passed a 1080 spec, and a 5760x2880 equirect passed a 16:9 UHD
    target, because the old rule was a floor. Anything smaller would need
    upscaling and always fails. A 90/270-degree rotation flag swaps the
    displayed axes, so a portrait phone capture stored as 1920x1080 is judged
    as 1080x1920.
    """
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_fail("Resolution", "No video stream found.")
        return
    stored = f"{video.get('width', 0)}x{video.get('height', 0)}"
    width, height, rotation = _displayed_size(video)
    if rotation:
        result.set_metadata("rotation", rotation)
        result.set_metadata("stored_resolution", stored)
    result.set_metadata("resolution", f"{width}x{height}")
    result.set_metadata("width", width)
    result.set_metadata("height", height)
    tw, th = int(cfg["target_width"]), int(cfg["target_height"])
    rot_note = f" (displayed after {abs(rotation)}° rotation)" if rotation else ""

    if (width, height) == (tw, th):
        result.add_pass("Resolution", f"{width}x{height} (matches {tw}x{th} target){rot_note}")
    elif width >= tw and height >= th and cfg.get("allow_larger_resolution", False):
        result.add_pass("Resolution", f"{width}x{height} exceeds the {tw}x{th} target "
                                      f"(larger frames allowed by this spec){rot_note}")
    elif width >= tw and height >= th:
        result.add_fail("Resolution", f"{width}x{height} does not match the {tw}x{th} target{rot_note}. "
                                      f"Larger frames are not allowed by this spec.", confidence=95)
    else:
        result.add_fail("Resolution", f"{width}x{height} is smaller than the {tw}x{th} target{rot_note}. "
                                      f"Conforming would upscale the image.", confidence=95)


def _ratio(text) -> Optional[float]:
    """'16:9' or '16/9' as a float, None when absent/invalid/zero."""
    if not text or not isinstance(text, str):
        return None
    parts = re.split(r"[:/]", text)
    if len(parts) != 2:
        return None
    num, den = _safe_float(parts[0]), _safe_float(parts[1])
    if not num or not den:
        return None
    return num / den


def check_aspect_ratio(probe_data: dict, result: QAResult, cfg: dict):
    """The displayed picture shape must match the target's shape.

    Uses the sample aspect ratio, so an anamorphic 1440x1080 frame is judged
    by its 16:9 display shape. With ``allow_larger_resolution`` this is what
    stops a larger frame of the wrong shape (4096x2160 for UHD) passing.
    """
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_skip("Aspect Ratio", "No video stream found.")
        return
    width, height, _ = _displayed_size(video)
    tw, th = int(cfg["target_width"]), int(cfg["target_height"])
    if not width or not height or not tw or not th:
        result.add_not_verified("Aspect Ratio", "frame size is unknown.")
        return
    sar = _ratio(video.get("sample_aspect_ratio")) or 1.0
    if abs(_stream_rotation_degrees(video)) % 180 == 90:
        sar = 1.0 / sar
    display = width * sar / height
    target = tw / th
    result.set_metadata("display_aspect_ratio", round(display, 4))
    sar_note = f", SAR {video.get('sample_aspect_ratio')}" if abs(sar - 1.0) > 1e-3 else ""
    if abs(display - target) / target <= 0.005:
        result.add_pass("Aspect Ratio", f"{display:.3f}:1 matches the target {target:.3f}:1{sar_note}")
    else:
        result.add_fail("Aspect Ratio", f"Displays at {display:.3f}:1, target is {target:.3f}:1 "
                                        f"({tw}x{th}){sar_note}.", confidence=95)


def check_frame_rate(probe_data: dict, result: QAResult, cfg: dict):
    """The frame rate must match the target within ``fps_tolerance``.

    25p against a 24p spec used to be a warning; conforming it needs a speed
    change, so any mismatch now fails. Integer multiples (60p for a 30p
    spec) pass only with ``allow_fps_multiples``.
    """
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_fail("Frame Rate", "No video stream found.")
        return
    r_fps = parse_frame_rate(video.get("r_frame_rate", "0/0"))
    avg_fps = parse_frame_rate(video.get("avg_frame_rate", "0/0"))
    result.set_metadata("r_frame_rate", r_fps)
    result.set_metadata("avg_frame_rate", avg_fps)
    fps = avg_fps if avg_fps > 0 else r_fps
    result.set_metadata("frame_rate", fps)
    if fps <= 0:
        result.add_warn("Frame Rate", "Could not determine the frame rate from the container.",
                        confidence=50)
        return
    target = float(cfg["target_fps"])
    tolerance = float(cfg["fps_tolerance"])
    if abs(fps - target) <= tolerance:
        result.add_pass("Frame Rate", f"{fps:.3f} fps (target: {target:g} ± {tolerance:g} fps)")
        return
    multiple = round(fps / target) if target > 0 else 0
    if multiple >= 2 and abs(fps / multiple - target) <= tolerance:
        if cfg.get("allow_fps_multiples", False):
            result.add_pass("Frame Rate", f"{fps:.3f} fps is {multiple}x the {target:g} fps target "
                                          f"(integer multiples allowed by this spec)")
            return
        result.add_fail("Frame Rate", f"{fps:.3f} fps is {multiple}x the {target:g} fps target; "
                                      f"integer multiples are not allowed by this spec.")
        return
    result.add_fail("Frame Rate", f"{fps:.3f} fps does not match the {target:g} ± {tolerance:g} fps target.")


# Intervals ignored at each edge of every packet window. Within a window
# read from a seek point (or cut by a lossless trim) the first and last few
# presentation timestamps are incomplete: B-frames whose packets fall
# outside the window leave holes up to the reorder depth, and the stream's
# own first/last intervals absorb edit-list and priming offsets (a
# fragmented MP4's first frame lasts 55 ms instead of 33 ms). None of that
# says anything about the cadence; real VFR runs through the middle.
WINDOW_EDGE_INTERVALS = 8


def _trim_window_edges(window_gaps: list) -> list:
    edge = WINDOW_EDGE_INTERVALS
    if len(window_gaps) > 4 * edge:
        return window_gaps[edge:-edge]
    return window_gaps[1:-1]


def _interval_stats(timing: FrameTiming) -> Tuple[Optional[float], int, int, List[Tuple[float, float]]]:
    """(dominant interval, total intervals, irregular count, irregular samples).

    The dominant interval is the most common gap between consecutive
    presentation timestamps (rounded to 0.1 ms, which absorbs Matroska's
    millisecond rounding of 33/34 ms). A gap more than half a frame away
    from it is irregular: a dropped/held frame or a genuine rate change.
    """
    gaps = []
    for window in timing.windows:
        gaps.extend(_trim_window_edges([(b, b - a) for a, b in zip(window, window[1:])]))
    if not gaps:
        return None, 0, 0, []
    counts = Counter(round(delta, 4) for _, delta in gaps if delta > 0)
    if not counts:
        return None, len(gaps), len(gaps), gaps[:6]
    bucket = counts.most_common(1)[0][0]
    # Average the intervals near the most common one: Matroska stores 30 fps
    # as alternating 33/34 ms, whose mean is the true 33.33 ms.
    near = [d for _, d in gaps if abs(d - bucket) <= bucket / 4]
    dominant = sum(near) / len(near)
    irregular = [(t, d) for t, d in gaps if abs(d - dominant) > dominant / 2]
    return dominant, len(gaps), len(irregular), irregular


def check_cfr(probe_data: dict, result: QAResult, cfg: dict,
              timing: Optional[FrameTiming] = None):
    """Detect variable frame rate from real packet timestamps.

    The old check compared two header fields (r_frame_rate vs
    avg_frame_rate), which agree on most VFR files: a clip that switched from
    30 to 15 fps halfway, and one that dropped a frame every 2 s, both passed.
    Fails when more than ``vfr_threshold`` of intervals are irregular (and at
    least two are). A single gap is a warning: one dropped or held frame.
    """
    if _primary_video_stream(probe_data) is None:
        result.add_skip("CFR Check", "No video stream found.")
        return
    if timing is None:
        result.add_not_verified("CFR Check", "packet timestamps could not be read.")
        return
    dominant, total, irregular, samples = _interval_stats(timing)
    if dominant is None or total < 2:
        result.add_not_verified("CFR Check", f"too few frames to measure timing ({timing.packet_count} packets).")
        return
    measured_fps = 1.0 / dominant
    ratio = irregular / total
    result.set_metadata("measured_frame_interval_ms", round(dominant * 1000, 3))
    result.set_metadata("measured_fps", round(measured_fps, 3))
    result.set_metadata("irregular_frame_intervals", irregular)
    result.set_metadata("fps_deviation", round(ratio, 4))
    scope = f"; {timing.description}" if timing.sampled else ""
    where = ", ".join(f"{_timecode(t)} ({d * 1000:.0f} ms)" for t, d in samples[:6])
    if irregular == 0:
        result.add_pass("CFR Check", f"Constant frame rate: {total} intervals of "
                                     f"{dominant * 1000:.2f} ms ({measured_fps:.3f} fps){scope}")
        return
    if irregular == 1:
        result.add_warn("CFR Check", f"1 irregular frame interval at {where} against "
                                     f"{dominant * 1000:.2f} ms (possible dropped or held frame){scope}",
                        confidence=80)
        return
    result.set_metadata("vfr_detected", ratio > float(cfg.get("vfr_threshold", 0.01)))
    detail = (f"{irregular} of {total} frame intervals ({ratio * 100:.1f}%) deviate from "
              f"{dominant * 1000:.2f} ms ({measured_fps:.3f} fps). First at: {where}{scope}")
    if ratio <= float(cfg.get("vfr_threshold", 0.01)):
        result.add_warn("CFR Check", f"Minor timing irregularity: {detail}", confidence=75)
    elif cfg.get("require_cfr", True):
        result.add_fail("CFR Check", f"Variable frame rate: {detail}")
    else:
        result.add_warn("CFR Check", f"Variable frame rate (allowed by this spec): {detail}")


_INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}


def check_scan_type(probe_data: dict, result: QAResult, cfg: dict,
                    first_frame: Optional[dict] = None):
    """Fail interlaced video when the spec requires progressive.

    ``field_order`` was probed and ignored, so an interlaced master passed.
    When the container does not declare a field order, the first decoded
    frame's interlaced flag is used.
    """
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_skip("Scan Type", "No video stream found.")
        return
    field_order = str(video.get("field_order") or "unknown").lower()
    frame_flag = (first_frame or {}).get("interlaced_frame")
    if field_order in _INTERLACED_FIELD_ORDERS:
        interlaced, source = True, f"field order '{field_order}'"
    elif field_order == "progressive":
        interlaced, source = False, "declared progressive"
    elif frame_flag in (1, "1", True):
        interlaced, source = True, "first frame is flagged interlaced"
    else:
        interlaced, source = False, "not flagged interlaced (field order not declared)"
    result.set_metadata("scan_type", "interlaced" if interlaced else "progressive")
    if not interlaced:
        confidence = 100 if field_order == "progressive" else 85
        result.add_pass("Scan Type", f"Progressive ({source})", confidence=confidence)
    elif cfg.get("require_progressive", True):
        result.add_fail("Scan Type", f"Interlaced video ({source}); this spec requires progressive.")
    else:
        result.add_pass("Scan Type", f"Interlaced ({source}); allowed by this spec.")


def _bit_depth(video: dict, first_frame: dict) -> Optional[int]:
    """Luma bit depth from bits_per_raw_sample, else from the pixel format name."""
    raw = _safe_int(video.get("bits_per_raw_sample"))
    if raw and raw > 0:
        return raw
    pix_fmt = str(video.get("pix_fmt") or (first_frame or {}).get("pix_fmt") or "").lower()
    if not pix_fmt:
        return None
    match = re.search(r"(?:p|le|be|f)?(\d{2})(?:le|be)$", pix_fmt) or re.search(r"p0?(\d{2})$", pix_fmt)
    if match and int(match.group(1)) in (9, 10, 12, 14, 16):
        return int(match.group(1))
    if pix_fmt in ("rgb48le", "rgb48be", "rgba64le", "rgba64be"):
        return 16
    return 8


_TRANSFER_NAMES = {"smpte2084": "pq", "arib-std-b67": "hlg"}


def _transfer(video: dict, first_frame: dict) -> Tuple[str, str]:
    """(class, raw name): class is 'pq', 'hlg', 'sdr' or 'unspecified'."""
    raw = video.get("color_transfer") or (first_frame or {}).get("color_transfer") or ""
    raw = str(raw).lower()
    if raw in ("", "unknown", "unspecified", "reserved"):
        return "unspecified", raw or "unspecified"
    return _TRANSFER_NAMES.get(raw, "sdr"), raw


def check_color_hdr(probe_data: dict, result: QAResult, cfg: dict,
                    first_frame: Optional[dict] = None):
    """Report bit depth and transfer (SDR/PQ/HLG); enforce only what the spec sets.

    ``min_bit_depth`` and ``required_transfer`` default to "any" (report
    only). An untagged file counts as SDR for an SDR spec, but can never
    satisfy an HDR spec: PQ/HLG must be signalled to display correctly.
    """
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_skip("Color / HDR", "No video stream found.")
        return
    first_frame = first_frame or {}
    depth = _bit_depth(video, first_frame)
    transfer, raw = _transfer(video, first_frame)
    primaries = video.get("color_primaries") or first_frame.get("color_primaries") or "unspecified"
    side = first_frame.get("side_data_list") or []
    hdr_meta = [name for name, key in (("mastering display", "Mastering display metadata"),
                                        ("content light level", "Content light level metadata"))
                if _side_data(side, key)]
    result.set_metadata("bit_depth", depth)
    result.set_metadata("color_transfer", raw)
    result.set_metadata("color_primaries", primaries)
    label = {"pq": "HDR PQ (SMPTE ST 2084)", "hlg": "HDR HLG (ARIB STD-B67)",
             "sdr": f"SDR ({raw})", "unspecified": "SDR (transfer not signalled)"}[transfer]
    summary = (f"{depth if depth else '?'}-bit, {label}, primaries {primaries}"
               + (f", {' + '.join(hdr_meta)} metadata" if hdr_meta else ""))

    problems = []
    min_depth = cfg.get("min_bit_depth", "any")
    if min_depth not in ("any", None):
        if depth is None:
            problems.append(f"bit depth unknown (spec requires {min_depth}-bit or more)")
        elif depth < int(min_depth):
            problems.append(f"{depth}-bit is below the required {min_depth}-bit")
    required = cfg.get("required_transfer", "any")
    if required not in ("any", None):
        allowed = {"sdr": {"sdr", "unspecified"}, "pq": {"pq"}, "hlg": {"hlg"},
                   "hdr": {"pq", "hlg"}}[required]
        if transfer not in allowed:
            problems.append(f"transfer is {label}, spec requires {required.upper()}")

    if problems:
        result.add_fail("Color / HDR", f"{summary}. " + "; ".join(problems) + ".")
    else:
        enforced = [n for n, v in (("bit depth", min_depth), ("transfer", required)) if v not in ("any", None)]
        note = f" (meets spec: {', '.join(enforced)})" if enforced else " (not enforced by this spec)"
        result.add_pass("Color / HDR", summary + note)


def _normalise_projection(name) -> str:
    return re.sub(r"[\s-]+", "_", str(name or "").strip().lower())


def _normalise_stereo(name) -> str:
    text = _normalise_projection(name)
    aliases = {
        "2d": "mono", "mono": "mono", "none": "mono",
        "top_and_bottom": "top_bottom", "top_bottom": "top_bottom", "tb": "top_bottom",
        "bottom_top": "top_bottom", "over_under": "top_bottom",
        "side_by_side": "side_by_side", "left_right": "side_by_side", "sbs": "side_by_side",
        "right_left": "side_by_side",
    }
    return aliases.get(text, text)


def _xr_metadata(video: dict, first_frame: dict) -> Tuple[Optional[str], Optional[str]]:
    """(projection, stereo layout) from stream side data, SEI or Matroska tags."""
    sources = (video.get("side_data_list") or [], (first_frame or {}).get("side_data_list") or [])
    projection = stereo = None
    for entries in sources:
        spherical = _side_data(entries, "Spherical Mapping")
        if spherical and projection is None:
            projection = _normalise_projection(spherical.get("projection") or "unknown")
        stereo3d = _side_data(entries, "Stereo 3D")
        if stereo3d and stereo is None:
            stereo = _normalise_stereo(stereo3d.get("type") or "unknown")
    if stereo is None:
        tags = {str(k).lower(): v for k, v in (video.get("tags") or {}).items()}
        tags.update({str(k).lower(): v for k, v in ((first_frame or {}).get("tags") or {}).items()})
        if tags.get("stereo_mode"):
            stereo = _normalise_stereo(tags["stereo_mode"])
    return projection, stereo


def check_xr_metadata(probe_data: dict, result: QAResult, cfg: dict,
                      first_frame: Optional[dict] = None):
    """Require the spherical / stereo-3D signalling a 360 or VR spec declares.

    A 360 file without spherical metadata plays flat on every headset and
    web player. With "any" the findings are reported but not enforced.
    """
    video = _primary_video_stream(probe_data)
    if video is None:
        result.add_skip("XR Metadata", "No video stream found.")
        return
    projection, stereo = _xr_metadata(video, first_frame or {})
    result.set_metadata("projection", projection or "none")
    result.set_metadata("stereo_mode", stereo or "mono")
    found = (f"projection {projection}" if projection else "no spherical metadata (flat)")
    found += f", stereo {stereo}" if stereo else ", no stereo metadata (mono)"

    problems = []
    want_projection = _normalise_projection(cfg.get("required_projection", "any") or "any")
    if want_projection == "none" and projection:
        problems.append(f"spec requires a flat (non-360) picture but the file declares {projection}")
    elif want_projection not in ("any", "none") and projection != want_projection:
        problems.append(f"spec requires {want_projection} projection metadata"
                        + ("; players will show this file flat" if not projection else f", file declares {projection}"))
    want_stereo = _normalise_stereo(cfg.get("required_stereo_mode", "any") or "any")
    actual_stereo = stereo or "mono"
    if want_stereo != "any" and actual_stereo != want_stereo:
        problems.append(f"spec requires stereo layout {want_stereo}, file declares {actual_stereo}")

    if problems:
        result.add_fail("XR Metadata", f"{found[0].upper()}{found[1:]}. " + "; ".join(problems) + ".")
    else:
        enforced = want_projection != "any" or want_stereo != "any"
        result.add_pass("XR Metadata", f"{found[0].upper()}{found[1:]}"
                        + (" (matches spec)" if enforced else " (not enforced by this spec)"))


def check_bitrate(probe_data: dict, result: QAResult, cfg: dict):
    """Validate video bitrate meets minimum."""
    video = _primary_video_stream(probe_data)
    bitrate_str = video.get("bit_rate") if video else None
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
    if is_format_level:
        # A container total includes every audio track.
        total_audio_bps = sum(max(0, _safe_int(a.get("bit_rate"), 0) or 0)
                              for a in _audio_streams(probe_data))
        if total_audio_bps > 0:
            bitrate_bps = max(bitrate_bps - total_audio_bps, 0)
    bitrate_mbps = bitrate_bps / 1_000_000
    result.set_metadata("video_bitrate_mbps", round(bitrate_mbps, 2))
    min_br = cfg["min_bitrate_mbps"]
    # Subtracting a large PCM track can push a container total under the
    # floor; only then is the estimate too uncertain to fail on.
    if is_format_level and bitrate_mbps < 1.0 and bitrate_mbps < min_br:
        orig_mbps = (_safe_int(probe_data.get("format", {}).get("bit_rate"), 0) or 0) / 1_000_000
        if orig_mbps >= min_br:
            result.add_warn("Bitrate", f"Video bitrate ~{bitrate_mbps:.1f} Mbps after subtracting audio "
                                       f"(format total: {orig_mbps:.1f} Mbps). PCM audio may skew calculation.")
            return
    if bitrate_mbps >= min_br:
        result.add_pass("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps (min: {min_br} Mbps)")
        return
    deficit_pct = ((min_br - bitrate_mbps) / min_br * 100) if min_br > 0 else 0
    if deficit_pct <= 10:
        result.add_pass("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps ({deficit_pct:.0f}% below target {min_br} Mbps - within VBR tolerance)", confidence=70)
    elif deficit_pct <= 25:
        result.add_warn("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps — {deficit_pct:.0f}% below {min_br} Mbps target. Likely still usable; verify no visible compression artifacts.", confidence=75)
    else:
        result.add_fail("Bitrate", f"{_format_mbps(bitrate_mbps)} Mbps is {_format_pct(deficit_pct)} below minimum {min_br} Mbps. Likely visible compression artifacts.", confidence=95)


_STATUS_RANK = {"FAIL": 3, "WARN": 2, "PASS": 1, "SKIP": 0}


def _add_aggregate(result: QAResult, name: str, entries: List[Tuple[str, str]], confidence: int = 100):
    """Record one row for a per-track check: the worst status, every track's detail.

    The checks matrix keys cells by check name, so one row per track would
    keep only the last track and could hide a failing one.
    """
    if not entries:
        return
    status = max((s for s, _ in entries), key=lambda s: _STATUS_RANK.get(s, 0))
    detail = "; ".join(d for _, d in entries)
    {"FAIL": result.add_fail, "WARN": result.add_warn,
     "PASS": result.add_pass, "SKIP": result.add_skip}[status](name, detail, confidence)


def check_audio(probe_data: dict, result: QAResult, cfg: dict):
    """Validate every audio track: codec, channels and sample rate.

    Only the first track used to be read, so a delivery whose second track
    was a silent 5.1 stem (or an 8 kHz scratch track) passed.
    """
    tracks = _audio_streams(probe_data)
    if not tracks:
        if cfg["audio_required"]:
            result.add_fail("Audio Stream", "No audio stream found (audio is required).")
        else:
            result.add_pass("Audio Stream", "No audio stream (audio not required).")
        return
    result.add_pass("Audio Stream", f"{len(tracks)} audio track(s)")
    result.set_metadata("audio_tracks", len(tracks))
    first = tracks[0]
    result.set_metadata("audio_codec", str(first.get("codec_name", "unknown")).lower())
    result.set_metadata("audio_sample_rate", _safe_int(first.get("sample_rate"), 0))
    result.set_metadata("audio_channels", _safe_int(first.get("channels"), 0))

    codecs, channels, rates = [], [], []
    min_rate = int(cfg["min_audio_sample_rate"])
    for position, track in enumerate(tracks):
        label = _track_label(position, track, len(tracks))
        codec = str(track.get("codec_name", "unknown")).lower()
        if codec in cfg["accepted_audio_codecs"]:
            codecs.append(("PASS", _with_track(label, f"Codec: {codec}")))
        else:
            codecs.append(("WARN", _with_track(label, f"Codec '{codec}' — not in the expected codec list but may still be compatible.")))

        count = _safe_int(track.get("channels"), 0) or 0
        layout = track.get("channel_layout", "unknown")
        if count > 0:
            channels.append(("PASS", _with_track(label, f"{count} channel(s), layout: {layout}")))
        else:
            channels.append(("WARN", _with_track(label, "Could not determine audio channel configuration.")))

        rate = _safe_int(track.get("sample_rate"), 0) or 0
        if rate <= 0:
            rates.append(("WARN", _with_track(label, f"{NOT_VERIFIED_PREFIX}sample rate unknown.")))
        elif rate >= min_rate:
            rates.append(("PASS", _with_track(label, f"{rate} Hz (minimum {min_rate} Hz)")))
        else:
            rates.append(("FAIL", _with_track(label, f"{rate} Hz is below the {min_rate} Hz minimum")))

    _add_aggregate(result, "Audio Codec", codecs)
    _add_aggregate(result, "Audio Channels", channels)
    _add_aggregate(result, "Audio Sample Rate", rates)


def check_av_duration_mismatch(probe_data: dict, result: QAResult, cfg: dict):
    """Compare each audio track's end against the video's end, tiered."""
    video = _primary_video_stream(probe_data)
    tracks = _audio_streams(probe_data)
    if video is None or not tracks:
        result.add_skip("A/V Duration", "Need both video and audio streams to compare.")
        return
    v_dur = _safe_float(_stream_duration_text(video))

    def _start_of(stream):
        return _safe_float(stream.get("start_time"), 0.0) or 0.0

    threshold = cfg["duration_mismatch_threshold"]
    entries = []
    worst_diff = 0.0
    for position, track in enumerate(tracks):
        label = _track_label(position, track, len(tracks))
        a_dur = _safe_float(_stream_duration_text(track))
        if v_dur is None or a_dur is None or v_dur <= 0 or a_dur <= 0:
            entries.append(("WARN", _with_track(label, "Per-stream duration not available.")))
            continue
        # Compare stream ENDS: streams routinely start at different offsets.
        diff = abs((_start_of(video) + v_dur) - (_start_of(track) + a_dur))
        worst_diff = max(worst_diff, diff)
        text = f"Video: {v_dur:.2f}s, Audio: {a_dur:.2f}s (diff: {diff:.3f}s)"
        if diff <= threshold:
            entries.append(("PASS", _with_track(label, text)))
        elif diff <= 2.0:
            entries.append(("WARN", _with_track(label, text + ". Minor offset — usually safe but verify lip sync on dialogue.")))
        else:
            entries.append(("FAIL", _with_track(label, text + " -- significant mismatch")))
    if v_dur:
        result.set_metadata("video_duration", round(v_dur, 3))
    first_audio = _safe_float(_stream_duration_text(tracks[0]))
    if first_audio:
        result.set_metadata("audio_duration", round(first_audio, 3))
    result.set_metadata("av_duration_diff", round(worst_diff, 3))
    _add_aggregate(result, "A/V Duration", entries)


def check_frame_count(probe_data: dict, result: QAResult, cfg: dict):
    """Advisory: header frame count vs duration x frame rate.

    Skipped when the CFR check found variable timing (the expectation is
    meaningless) and for Matroska, which records no frame count.
    """
    vs = _primary_video_stream(probe_data)
    if vs is None:
        result.add_skip("Frame Count", "No video stream.")
        return
    if result.metadata.get("vfr_detected"):
        result.add_skip("Frame Count", "Skipped — variable frame rate detected (frame count unreliable for VFR).")
        return
    nb_frames_str = vs.get("nb_frames")
    if not nb_frames_str or nb_frames_str == "N/A":
        format_name = str((probe_data.get("format") or {}).get("format_name") or "")
        if "matroska" in format_name or "webm" in format_name:
            result.add_skip("Frame Count", "Frame count is not recorded in Matroska/WebM files.")
        else:
            result.add_warn("Frame Count", "nb_frames not available in container metadata.")
        return
    actual_frames = _safe_int(nb_frames_str)
    if actual_frames is None or actual_frames < 0:
        result.add_warn("Frame Count", f"Could not parse frame count: {nb_frames_str}")
        return
    hidden = int(result.metadata.get("edit_list_hidden_frames", 0) or 0)
    hidden_note = (f" ({hidden} lead-in frame(s) stored but hidden by an edit list, "
                   f"as left by a lossless trim)") if hidden else ""
    actual_frames = max(0, actual_frames - hidden)
    duration = _safe_float(vs.get("duration") or probe_data.get("format", {}).get("duration"))
    fps = parse_frame_rate(vs.get("r_frame_rate", "0/1"))
    if duration is None or duration <= 0 or fps <= 0:
        result.add_warn("Frame Count", "Cannot determine expected frame count.")
        return
    expected_frames = int(round(duration * fps))
    diff = abs(expected_frames - actual_frames)
    tolerance = max(2, int(fps * 0.1))
    if hidden:
        # A stream-copy cut also drops the B-frames at its tail whose
        # reference fell outside the cut: up to the reorder depth.
        tolerance = max(tolerance, 8)
    result.set_metadata("expected_frames", expected_frames)
    result.set_metadata("actual_frames", actual_frames)
    result.set_metadata("frame_count_diff", diff)
    if diff <= tolerance:
        result.add_pass("Frame Count", f"{actual_frames} frames (expected ~{expected_frames}, diff: {diff}){hidden_note}")
    elif diff <= expected_frames * 0.10:
        result.add_warn("Frame Count", f"{actual_frames} frames vs ~{expected_frames} (diff: {diff}, {diff/expected_frames*100:.1f}%). May be normal for static/slow-motion content.")
    else:
        result.add_fail("Frame Count", f"{actual_frames} frames vs ~{expected_frames} (diff: {diff}, {diff/expected_frames*100:.1f}%). Likely frame drops or encoding issue.")



# ============================================================================
# DEEP SCAN: LOG CAPTURE AND EVENT PARSING
# ============================================================================

_STDERR_EVIDENCE_TOKENS = {
    "black": ("black_start", "black_end", "black_duration"),
    "freeze": ("freeze_start", "freeze_end", "freeze_duration"),
    "silence": ("silence_start", "silence_end", "silence_duration"),
}
_STDERR_READ_CHUNK_BYTES = 64 * 1024
_STDERR_RECORD_FRAGMENT_CHARS = 4096
_STDERR_RECORD_OVERLAP_CHARS = 128


def _stderr_evidence_categories(line: str) -> tuple:
    """Semantic categories of one FFmpeg log record; () for plain context.

    Evidence records are always retained by _BoundedStderrCapture because the
    verdicts are parsed from them; everything else is a bounded tail.
    """
    categories = []
    if _is_decode_evidence(line):
        categories.append("decode")
    if _LOUDNESS_LINE_RE.search(line):
        categories.append("loudness")
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
        if category in ("decode", "loudness"):
            positions = [0]
        else:
            positions = [line.find(token)
                         for token in _STDERR_EVIDENCE_TOKENS[category]
                         if line.find(token) >= 0]
        start = max(0, min(positions) - 16) if positions else 0
        snippets.append(line[start:start + per_category])
    return " | ".join(snippets)[:limit]


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

    Detect filters can report one visual event as several touching regions
    (a region closed at EOF, a freeze interrupted for a single frame).
    Merging by interval collapses those into one event with the full
    duration while keeping genuinely separate events separate.

    ``join_gap`` absorbs sub-frame drift between adjacent reports.
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
    passed. The open region now ends where the decoded range ends.
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


# ---------------------------------------------------------------------------
# FFmpeg log records
#
# The integrity scan runs with ``-loglevel level+verbose``: every record then
# carries its severity tag ("[h264 @ 0x..] [error] ..."), and FFmpeg prints
# exact per-stream accounting at the end ("Input stream #0:0 (video): 1200
# packets read ...; 1198 frames decoded; 2 decode errors"). Verdicts come
# from that accounting and from the severity tags, never from free-text
# pattern matches, which used to trip on file names and metadata.
# ---------------------------------------------------------------------------

_LOG_LINE_RE = re.compile(
    r"^(?P<ctx>(?:\[[^\[\]\n]* @ [0-9A-Fa-fx]+\]\s*)*)"
    r"\[(?P<level>trace|debug|verbose|info|warning|error|fatal|panic)\]\s?(?P<msg>.*)$")
_CONTEXT_RE = re.compile(r"\[([^\[\]\n]*) @ [0-9A-Fa-fx]+\]")
_STREAM_STATS_RE = re.compile(
    r"Input stream #(\d+):(\d+) \((\w+)\): (\d+) packets read \((\d+) bytes\); "
    r"(\d+) frames decoded(?:; (\d+) decode errors)?(?: \((\d+) samples\))?")
_CORRUPT_FRAME_RE = re.compile(r"corrupt decoded frame")
_CORRUPT_PACKET_RE = re.compile(r"corrupt input packet in stream (\d+)")
_INPUT_STREAM_CONTEXT_RE = re.compile(r"^[vas]ist#\d+:(\d+)")
_LOUDNESS_LINE_RE = re.compile(
    r"\[ebur128@|^\s*(?:I|Peak|Threshold|LRA(?: low| high)?):\s"
    r"|Integrated loudness:|True peak:|Loudness range:")
_DECODER_UNAVAILABLE_RE = re.compile(
    r"no decoder|decoder \(codec [^)]*\) not found|unknown decoder|unsupported codec"
    r"|could not find codec|decoding requested, but no decoder", re.IGNORECASE)
_ERROR_LEVELS = ("error", "fatal", "panic")
MAX_ERROR_SAMPLES = 20
# Above this many channels per track the per-channel split is skipped (and
# reported as not verified) rather than building an enormous filter graph.
MAX_CHANNEL_SPLIT = 32
INTEGRITY_MIN_DECODED_RATIO = 0.98


def _parse_log_line(line: str) -> Tuple[List[str], Optional[str], str]:
    """(contexts, level, message) of one record; level None for continuations."""
    match = _LOG_LINE_RE.match(line)
    if not match:
        return [], None, line
    return _CONTEXT_RE.findall(match.group("ctx")), match.group("level"), match.group("msg")


def _is_decode_evidence(line: str) -> bool:
    """Records the integrity verdict depends on: errors and decoder accounting."""
    _, level, message = _parse_log_line(line)
    return (level in _ERROR_LEVELS
            or bool(_CORRUPT_FRAME_RE.search(message)
                    or _CORRUPT_PACKET_RE.search(message)
                    or _STREAM_STATS_RE.search(message)))


class _DecodeAccounting:
    """Exact running counts of decoder-reported damage.

    Counted while the log streams, independently of the bounded text capture,
    so a file with thousands of damaged frames is never under-counted.
    """

    def __init__(self):
        self.stream_stats: Dict[int, dict] = {}
        self.corrupt_frames: Counter = Counter()
        self.corrupt_packets: Counter = Counter()
        self.messages: List[str] = []

    def add(self, line: str) -> None:
        contexts, level, message = _parse_log_line(line)
        stats = _STREAM_STATS_RE.search(message)
        if stats:
            index = int(stats.group(2))
            self.stream_stats[index] = {
                "kind": stats.group(3),
                "packets": int(stats.group(4)),
                "frames": int(stats.group(6)),
                "errors": int(stats.group(7) or 0),
                "samples": int(stats.group(8)) if stats.group(8) else None,
            }
            return
        if _CORRUPT_FRAME_RE.search(message):
            self.corrupt_frames[self._stream_index(contexts)] += 1
            self._sample(contexts, message)
            return
        packet = _CORRUPT_PACKET_RE.search(message)
        if packet:
            self.corrupt_packets[int(packet.group(1))] += 1
            self._sample(contexts, message)
            return
        if level in _ERROR_LEVELS:
            self._sample(contexts, message)

    @staticmethod
    def _stream_index(contexts: List[str]) -> int:
        for context in contexts:
            match = _INPUT_STREAM_CONTEXT_RE.match(context)
            if match:
                return int(match.group(1))
        return -1

    def _sample(self, contexts: List[str], message: str) -> None:
        if len(self.messages) < MAX_ERROR_SAMPLES:
            source = contexts[-1] if contexts else "ffmpeg"
            self.messages.append(f"[{source}] {message.strip()}"[:200])

    @property
    def decode_failures(self) -> int:
        return sum(s["errors"] for s in self.stream_stats.values())

    @property
    def total_errors(self) -> int:
        """Undecodable packets + frames the decoder flagged corrupt + packets
        the demuxer flagged corrupt: all FFmpeg's own accounting."""
        return (self.decode_failures + sum(self.corrupt_frames.values())
                + sum(self.corrupt_packets.values()))

    def decoder_unavailable(self) -> bool:
        return any(_DECODER_UNAVAILABLE_RE.search(m) for m in self.messages)


class _ScanLog:
    """Feeds every record to the exact accounting and the bounded capture."""

    def __init__(self, cfg: dict):
        self.capture = _BoundedStderrCapture(cfg)
        self.accounting = _DecodeAccounting()

    def add(self, line: str) -> None:
        self.accounting.add(line)
        self.capture.add(line)

    def note_fragment_split(self) -> None:
        self.capture.note_fragment_split()


def _lines_with_context(text: str, name: str) -> str:
    """Records logged by the filter instance ``name`` (exact match)."""
    kept = []
    for line in text.splitlines():
        contexts, _, _ = _parse_log_line(line)
        if name in contexts:
            kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Scan plan
# ---------------------------------------------------------------------------

_LAYOUT_CHANNELS = {
    "mono": ["FC"], "stereo": ["FL", "FR"], "2.1": ["FL", "FR", "LFE"],
    "3.0": ["FL", "FR", "FC"], "quad": ["FL", "FR", "BL", "BR"],
    "4.0": ["FL", "FR", "FC", "BC"], "5.0": ["FL", "FR", "FC", "BL", "BR"],
    "5.0(side)": ["FL", "FR", "FC", "SL", "SR"],
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    "7.1(wide)": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLC", "FRC"],
}


def _channel_names(track: dict, count: int) -> List[str]:
    names = _LAYOUT_CHANNELS.get(str(track.get("channel_layout") or "").lower())
    if names and len(names) == count:
        return names
    return [f"ch{k + 1}" for k in range(count)]


class _ScanPlan:
    """What the integrity pass decodes and what it should find."""

    def __init__(self, probe_data: dict, cfg: dict, variable_rate: bool = False,
                 hidden_frames: int = 0):
        self.hidden_frames = max(0, int(hidden_frames or 0))
        self.video = _primary_video_stream(probe_data)
        self.audio = _audio_streams(probe_data)
        self.duration = _probed_duration(probe_data)
        self.loudness = (cfg.get("loudness_mode", "advisory") != "off") and bool(self.audio)
        self.variable_rate = variable_rate

    def video_frames_unreliable(self) -> bool:
        """True when no declared frame count exists and the timing is variable."""
        declared = _safe_int((self.video or {}).get("nb_frames"))
        return bool(self.variable_rate) and not (declared and declared > 0)

    def video_fps(self) -> float:
        if not self.video:
            return 0.0
        return (parse_frame_rate(self.video.get("avg_frame_rate", "0/0"))
                or parse_frame_rate(self.video.get("r_frame_rate", "0/0")))

    def video_expected_frames(self) -> Optional[int]:
        """Frames that should be presented.

        ``nb_frames`` counts stored packets, including the lead-in an edit
        list hides after a lossless trim (up to a GOP: a 15 s open-GOP H.264
        cut stores 498 frames and shows 452), so the hidden lead-in is
        subtracted and the result capped by duration x rate. A truncated
        copy keeps its original header, so both values still describe the
        full file and the shortfall is caught.
        """
        if not self.video:
            return None
        declared = _safe_int(self.video.get("nb_frames"))
        if declared is not None and declared > 0:
            declared = max(0, declared - self.hidden_frames)
        else:
            declared = None
        fps = self.video_fps()
        duration = _stream_duration(self.video) or self.duration
        by_time = int(round(duration * fps)) if fps > 0 and duration else None
        candidates = [v for v in (declared, by_time) if v]
        return min(candidates) if candidates else None

    @staticmethod
    def channels(track: dict) -> int:
        return _safe_int(track.get("channels"), 0) or 0

    def audio_expected_seconds(self, track: dict) -> Optional[float]:
        return _stream_duration(track) or self.duration

    def frame_check_names(self) -> List[str]:
        names = ["File Integrity"]
        if self.video:
            names += ["Black Frames", "Freeze Frames"]
        if self.audio:
            names += ["Audio Silence", "Dead Channel"]
            if self.loudness:
                names.append("Audio Loudness")
        return names


def _build_scan_command(ffmpeg: str, filepath: str, plan: _ScanPlan, cfg: dict) -> List[str]:
    """One software-decode pass over the whole file with every detector attached.

    No ``-hwaccel``: hardware decoders conceal damage differently (the same
    corrupt clip gave 16 errors and a phantom freeze on VideoToolbox, 175
    errors and no freeze in software), so the verdict depended on the machine
    and on which file happened to be scanned first.
    """
    noise = float(cfg["silence_noise_db"])
    silence = float(cfg["silence_duration"])
    chains, maps = [], []
    if plan.video is not None:
        chains.append(
            f"[0:{int(plan.video['index'])}]"
            f"blackdetect@v=d={float(cfg['black_detect_duration'])}"
            f":pix_th={float(cfg['black_detect_pix_threshold'])},"
            f"freezedetect@v=n={float(cfg['freeze_detect_noise'])}"
            f":d={float(cfg['freeze_detect_duration'])}[v]")
        maps += ["-map", "[v]"]
    for i, track in enumerate(plan.audio):
        source = f"[0:{int(track['index'])}]"
        count = plan.channels(track)
        if 1 < count <= MAX_CHANNEL_SPLIT:
            labels = [f"[a{i}m]"] + [f"[a{i}c{k}]" for k in range(count)]
            chains.append(f"{source}asplit={count + 1}{''.join(labels)}")
            source = f"[a{i}m]"
            for k in range(count):
                chains.append(f"[a{i}c{k}]pan=mono|c0=c{k},"
                              f"silencedetect@a{i}c{k}=n={noise}dB:d={silence},anullsink")
        chain = f"{source}silencedetect@a{i}=n={noise}dB:d={silence}"
        if plan.loudness:
            chain += f",ebur128@a{i}=peak=true:framelog=quiet"
        chains.append(chain + f"[a{i}]")
        maps += ["-map", f"[a{i}]"]
    threads = max(1, int(cfg.get("filter_threads", 4) or 4))
    return ([ffmpeg, "-nostdin", "-hide_banner", "-nostats",
             "-loglevel", "level+verbose", "-progress", "pipe:1",
             "-i", filepath, "-filter_complex_threads", str(threads),
             "-filter_complex", ";".join(chains)]
            + maps + ["-f", "null", "-"])


class _ScanOutcome:
    def __init__(self):
        self.returncode: Optional[int] = None
        self.timed_out = False
        self.timeout = 0.0
        self.reader_failed = False
        self.launch_error: Optional[str] = None
        self.last_out_time: Optional[float] = None
        self.log_text = ""
        self.accounting = _DecodeAccounting()


def _run_scan_process(cmd: List[str], timeout: float, duration: Optional[float], cfg: dict,
                      progress_callback: Optional[Callable],
                      cancel_event: Optional[threading.Event],
                      process_owner) -> Optional[_ScanOutcome]:
    """Run the integrity pass; None when cancelled."""
    if cancel_event is not None and cancel_event.is_set():
        return None
    outcome = _ScanOutcome()
    outcome.timeout = timeout
    log = _ScanLog(cfg)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=False, bufsize=0, env=_get_clean_env(),
                                **_subprocess_kwargs())
    except (OSError, ValueError) as exc:
        outcome.launch_error = f"{type(exc).__name__}: {exc}"
        return outcome
    _register_owned_proc(proc, process_owner)
    reader_errors = []

    def _read_stderr():
        try:
            log.capture.peak_pending_chars = _drain_binary_records(proc.stderr, log.add)
        except (OSError, ValueError, TypeError, AssertionError) as exc:
            reader_errors.append(exc)

    def _on_progress(line: str):
        line = line.strip()
        if not line.startswith("out_time_us="):
            return
        microseconds = _safe_int(line.split("=", 1)[1])
        if microseconds is None or microseconds < 0:
            return
        outcome.last_out_time = microseconds / 1_000_000
        if progress_callback:
            progress_callback("filter", outcome.last_out_time, duration or 0.0,
                              "Integrity scan (software decode)")

    def _read_stdout():
        try:
            _drain_binary_records(proc.stdout, _on_progress)
        except (OSError, ValueError, TypeError, AssertionError) as exc:
            reader_errors.append(exc)

    def _join(thread, stream) -> bool:
        thread.join(timeout=5)
        if thread.is_alive():
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            thread.join(timeout=1)
        return not thread.is_alive()

    try:
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()
        try:
            outcome.returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            outcome.timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
        readers_done = _join(stderr_thread, proc.stderr) and _join(stdout_thread, proc.stdout)
        outcome.reader_failed = not readers_done or bool(reader_errors)
    finally:
        _unregister_owned_proc(proc, process_owner)
    if cancel_event is not None and cancel_event.is_set():
        return None
    outcome.log_text = log.capture.result()
    outcome.accounting = log.accounting
    return outcome



# ---------------------------------------------------------------------------
# Integrity scan reporting
# ---------------------------------------------------------------------------

def _mark_not_verified(result: QAResult, names: List[str], reason: str) -> None:
    for name in names:
        result.add_not_verified(name, reason)


def _not_run_reason(outcome: _ScanOutcome, plan: _ScanPlan) -> Optional[str]:
    """Why the pass produced no usable evidence, or None if it did."""
    if outcome.launch_error:
        return f"FFmpeg could not be started ({outcome.launch_error})."
    if outcome.timed_out:
        reached = (f" after decoding {outcome.last_out_time:.1f} s" if outcome.last_out_time else "")
        of = f" of {plan.duration:.1f} s" if plan.duration else ""
        return f"the integrity scan timed out after {int(outcome.timeout)} s{reached}{of}."
    if outcome.reader_failed:
        return "FFmpeg output could not be read completely."
    accounting = outcome.accounting
    if not accounting.stream_stats and outcome.returncode:
        if accounting.decoder_unavailable():
            return "the bundled FFmpeg has no decoder for this codec."
        first = accounting.messages[0] if accounting.messages else "no error message"
        return f"FFmpeg exited with code {outcome.returncode} before decoding ({first})."
    return None


def _decoded_video_seconds(plan: _ScanPlan, accounting: _DecodeAccounting) -> Optional[float]:
    stats = accounting.stream_stats.get(int(plan.video["index"])) if plan.video else None
    fps = plan.video_fps()
    if stats is None or fps <= 0:
        return None
    return stats["frames"] / fps


def _decoded_audio_seconds(track: dict, accounting: _DecodeAccounting) -> Optional[float]:
    stats = accounting.stream_stats.get(int(track["index"]))
    rate = _safe_int(track.get("sample_rate"), 0) or 0
    if stats is None or stats.get("samples") is None or rate <= 0:
        return None
    return stats["samples"] / rate


def _coverage(plan: _ScanPlan, accounting: _DecodeAccounting,
              last_out_time: Optional[float]) -> Tuple[List[str], List[str], List[str]]:
    """(per-stream descriptions, shortfalls, streams whose completeness is unknown).

    A stream is short when it decoded under 98% of what its own header
    declares, with a small absolute slack (2 frames / 0.1 s) so a 1-second
    clip that loses its last frame to an edit list is not called truncated.
    A truncated copy keeps its original header, so this is the measurement
    that catches it: the old scan never compared decoded media with the
    probed duration.
    """
    described, short, unknown = [], [], []
    if plan.video is not None and plan.video_frames_unreliable():
        # VFR without a declared frame count: duration x rate predicts the
        # wrong number of frames, so judge the video by decoded time instead.
        expected_s = _stream_duration(plan.video) or plan.duration
        decoded_s = last_out_time or 0.0
        if not expected_s:
            unknown.append("video")
        else:
            described.append(f"{decoded_s:.2f}/{expected_s:.2f} s video (variable frame rate)")
            if decoded_s < expected_s * INTEGRITY_MIN_DECODED_RATIO - 0.1:
                short.append(f"decoding stopped at {decoded_s:.2f} of {expected_s:.2f} s "
                             f"({decoded_s / expected_s * 100:.0f}%)")
    elif plan.video is not None:
        stats = accounting.stream_stats.get(int(plan.video["index"]))
        expected = plan.video_expected_frames()
        decoded = stats["frames"] if stats else 0
        if not expected:
            unknown.append("video")
            described.append(f"{decoded} video frames")
        else:
            described.append(f"{decoded}/{expected} video frames")
            if decoded < expected * INTEGRITY_MIN_DECODED_RATIO - 2:
                short.append(f"video decoded {decoded} of {expected} frames "
                             f"({decoded / expected * 100:.0f}%)")
    total = len(plan.audio)
    for position, track in enumerate(plan.audio):
        name = f"audio track {position + 1}" if total > 1 else "audio"
        expected = plan.audio_expected_seconds(track)
        decoded = _decoded_audio_seconds(track, accounting)
        if decoded is None:
            decoded = 0.0 if accounting.stream_stats else None
        if decoded is None or not expected:
            unknown.append(name)
            if decoded is not None:
                described.append(f"{decoded:.2f} s {name}")
            continue
        described.append(f"{decoded:.2f}/{expected:.2f} s {name}")
        if decoded < expected * INTEGRITY_MIN_DECODED_RATIO - 0.1:
            short.append(f"{name} decoded {decoded:.2f} of {expected:.2f} s "
                         f"({decoded / expected * 100:.0f}%)")
    return described, short, unknown


def _report_file_integrity(result: QAResult, plan: _ScanPlan, outcome: _ScanOutcome, cfg: dict) -> None:
    """FAIL a truncated or damaged file; "Not verified" when completeness is unmeasurable."""
    accounting = outcome.accounting
    if not accounting.stream_stats:
        result.add_not_verified("File Integrity", "FFmpeg reported no decoder statistics.")
        return
    described, short, unknown = _coverage(plan, accounting, outcome.last_out_time)
    errors = accounting.total_errors
    allowed = int(cfg.get("max_decode_errors", 0))
    result.set_metadata("decode_errors", errors)
    if accounting.messages:
        result.set_metadata("decode_error_samples", accounting.messages[:5])
    decoded_video = _decoded_video_seconds(plan, accounting)
    if decoded_video is not None:
        result.set_metadata("decoded_video_seconds", round(decoded_video, 3))

    problems = []
    if short:
        problems.append("Truncated or incomplete: " + "; ".join(short))
    if errors > allowed:
        breakdown = []
        if accounting.decode_failures:
            breakdown.append(f"{accounting.decode_failures} undecodable packet(s)")
        if accounting.corrupt_frames:
            breakdown.append(f"{sum(accounting.corrupt_frames.values())} corrupt frame(s)")
        if accounting.corrupt_packets:
            breakdown.append(f"{sum(accounting.corrupt_packets.values())} corrupt packet(s)")
        sample = "; ".join(accounting.messages[:3])
        problems.append(f"{errors} decoder error(s), max allowed {allowed} "
                        f"({', '.join(breakdown)})" + (f". First: {sample}" if sample else ""))
    if problems:
        result.add_fail("File Integrity", ". ".join(problems) + ".")
        return
    summary = f"Software decode of {', '.join(described)}; {errors} decoder error(s)"
    if errors:
        summary += f" (within the allowed {allowed})"
    if unknown:
        result.add_not_verified(
            "File Integrity",
            f"completeness cannot be measured because the container declares no duration "
            f"for {', '.join(unknown)} ({summary}).")
        return
    result.add_pass("File Integrity", summary + ".")


def _report_black_frames(result: QAResult, events_text: str, duration: float) -> None:
    black_events = _merge_event_intervals(_pair_detect_events(events_text, "black"))
    # Trim the first and last second (legitimate fades) instead of discarding
    # whole events, so an entirely black file still fails.
    if duration > 3.0:
        trimmed = []
        for start, dur in black_events:
            begin = max(start, 1.0)
            finish = min(start + dur, duration - 1.0)
            if finish - begin > 0:
                trimmed.append((begin, finish - begin))
        black_events = trimmed
    if not black_events:
        result.add_pass("Black Frames", "No black frame sequences detected.")
        return
    total_black = sum(d for _, d in black_events)
    count = len(black_events)
    positions = _format_event_list(black_events)
    result.set_metadata("black_frame_sequences", count)
    result.set_metadata("black_frame_total_seconds", round(total_black, 2))
    result.set_metadata("black_frame_timestamps", positions)
    if total_black > duration * 0.1:
        if total_black < 4.0:
            result.add_warn("Black Frames", f"{count} black sequence(s), total {total_black:.1f}s (likely transitions/slates). At: {positions}", confidence=60)
        else:
            result.add_fail("Black Frames", f"{count} black sequence(s), total {total_black:.1f}s. At: {positions}", confidence=90)
    else:
        result.add_warn("Black Frames", f"{count} black sequence(s), total {total_black:.1f}s. At: {positions}", confidence=65)


def _report_freeze_frames(result: QAResult, events_text: str, duration: float) -> None:
    freeze_events = _merge_event_intervals(_pair_detect_events(events_text, "freeze"))
    if not freeze_events:
        result.add_pass("Freeze Frames", "No freeze events detected.")
        return
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


def _silence_events(log_text: str, instance: str, end_time: float) -> List[Tuple[float, float]]:
    text = _close_open_regions(_lines_with_context(log_text, instance), end_time)
    events = _merge_event_intervals(_pair_detect_events(text, "silence"))
    return [(start, dur if dur > 0 else max(0.0, end_time - start)) for start, dur in events]


def _track_silence_entry(label: str, events, duration: float, cfg: dict) -> Tuple[str, str]:
    """Status/detail for one track's silence, using the long-standing tiers.

    New: a track silent for at least ``dead_channel_ratio`` of its length is
    dead and fails whatever its length (the old >30 s condition let a
    completely silent 20 s clip through as a warning).
    """
    if not events:
        return "PASS", _with_track(label, "No extended silence detected.")
    total = sum(d for _, d in events)
    positions = _format_event_list(events)
    count = len(events)
    if duration > 0 and total >= duration * float(cfg.get("dead_channel_ratio", 0.95)):
        return "FAIL", _with_track(label, f"Silent for {total:.1f}s of {duration:.1f}s "
                                          f"({total / duration * 100:.0f}%): track carries no audio.")
    if total > duration * 0.5 and duration > 30:
        return "FAIL", _with_track(label, f"{count} silence region(s), total {total:.1f}s. Extended silence in deliverable. At: {positions}")
    if total < 1.0:
        return "PASS", _with_track(label, f"{count} brief silence(s), total {total:.1f}s (natural pauses)")
    return "WARN", _with_track(label, f"{count} silence region(s), total {total:.1f}s. At: {positions}")


def _dead_channel_entry(label: str, track: dict, position: int, log_text: str,
                        duration: float, track_dead: bool, cfg: dict) -> Tuple[str, str]:
    """Per-channel silence: one dead channel in a live track.

    silencedetect on a whole track only fires when every channel is silent,
    so a stereo file with a dead right channel passed.
    """
    count = _ScanPlan.channels(track)
    if count <= 1:
        return "SKIP", _with_track(label, "Single-channel track.")
    if count > MAX_CHANNEL_SPLIT:
        return "WARN", _with_track(label, f"{NOT_VERIFIED_PREFIX}{count} channels exceed the "
                                          f"{MAX_CHANNEL_SPLIT}-channel analysis limit.")
    if track_dead:
        return "SKIP", _with_track(label, "Whole track is silent (see Audio Silence).")
    names = _channel_names(track, count)
    ratio = float(cfg.get("dead_channel_ratio", 0.95))
    dead = []
    for k in range(count):
        events = _silence_events(log_text, f"silencedetect@a{position}c{k}", duration)
        if duration > 0 and sum(d for _, d in events) >= duration * ratio:
            dead.append(names[k])
    if not dead:
        return "PASS", _with_track(label, f"All {count} channels carry signal.")
    live_dead = [n for n in dead if n != "LFE"]
    if not live_dead:
        return "PASS", _with_track(label, "LFE channel is silent (common when the mix has no low-frequency content); all other channels carry signal.")
    text = f"Silent channel(s): {', '.join(dead)} of {count} ({track.get('channel_layout') or 'unknown layout'})."
    if count == 2:
        return "FAIL", _with_track(label, text)
    return "WARN", _with_track(label, text + " Verify the mix is intentionally sparse.")


def _parse_loudness(log_text: str) -> Dict[int, dict]:
    """Integrated loudness and true peak per ebur128@aN summary block."""
    results: Dict[int, dict] = {}
    current = None
    number = r"(-?inf|-?\d+(?:\.\d+)?)"
    for line in log_text.splitlines():
        header = re.search(r"\[ebur128@a(\d+) @ [0-9A-Fa-fx]+\].*Summary:", line)
        if header:
            current = int(header.group(1))
            results[current] = {}
            continue
        if current is None:
            continue
        integrated = re.match(rf"^\s*I:\s+{number}\s+LUFS", line)
        if integrated:
            results[current]["integrated"] = float(integrated.group(1))
            continue
        peak = re.match(rf"^\s*Peak:\s+{number}\s+dBFS", line)
        if peak:
            results[current]["true_peak"] = float(peak.group(1))
            current = None
    return results


def _loudness_entry(label: str, measured: Optional[dict], cfg: dict) -> Tuple[str, str]:
    if not measured or "integrated" not in measured:
        return "WARN", _with_track(label, f"{NOT_VERIFIED_PREFIX}loudness summary not reported by FFmpeg.")
    essential = cfg.get("loudness_mode") == "essential"
    target = float(cfg["target_loudness_lufs"])
    tolerance = float(cfg["loudness_tolerance_lu"])
    peak_max = float(cfg["true_peak_max_dbtp"])
    integrated = measured["integrated"]
    peak = measured.get("true_peak")
    issues = []
    if integrated <= -69.9:
        issues.append("no measurable programme loudness (silent)")
    elif abs(integrated - target) > tolerance:
        issues.append(f"integrated {integrated:.1f} LUFS is outside {target:g} ± {tolerance:g} LUFS")
    if peak is not None and peak > peak_max:
        issues.append(f"true peak {peak:.1f} dBTP exceeds {peak_max:g} dBTP")
    peak_text = f"{peak:.1f} dBTP" if peak is not None and math.isfinite(peak) else "-inf dBTP"
    text = f"I {integrated:.1f} LUFS (target {target:g} ± {tolerance:g}), true peak {peak_text} (max {peak_max:g})"
    if not issues:
        return "PASS", _with_track(label, text)
    status = "FAIL" if essential else "WARN"
    return status, _with_track(label, f"{text}: " + "; ".join(issues))


def _report_audio(result: QAResult, plan: _ScanPlan, outcome: _ScanOutcome, cfg: dict) -> None:
    silence, dead, loudness = [], [], []
    measured = _parse_loudness(outcome.log_text) if plan.loudness else {}
    total = len(plan.audio)
    for position, track in enumerate(plan.audio):
        label = _track_label(position, track, total)
        duration = (_decoded_audio_seconds(track, outcome.accounting)
                    or plan.audio_expected_seconds(track) or 0.0)
        events = _silence_events(outcome.log_text, f"silencedetect@a{position}", duration)
        status, detail = _track_silence_entry(label, events, duration, cfg)
        silence.append((status, detail))
        if position == 0 and events:
            result.set_metadata("silence_regions", len(events))
            result.set_metadata("silence_total_seconds", round(sum(d for _, d in events), 2))
            result.set_metadata("silence_timestamps", _format_event_list(events))
        track_dead = (duration > 0 and sum(d for _, d in events)
                      >= duration * float(cfg.get("dead_channel_ratio", 0.95)))
        dead.append(_dead_channel_entry(label, track, position, outcome.log_text,
                                        duration, track_dead, cfg))
        if plan.loudness:
            entry = measured.get(position)
            loudness.append(_loudness_entry(label, entry, cfg))
            if entry and position == 0:
                result.set_metadata("integrated_loudness_lufs", entry.get("integrated"))
                result.set_metadata("true_peak_dbtp", entry.get("true_peak"))
    confidence = 90 if any(s == "FAIL" for s, _ in silence) else 100
    _add_aggregate(result, "Audio Silence", silence, confidence)
    _add_aggregate(result, "Dead Channel", dead)
    if plan.loudness:
        _add_aggregate(result, "Audio Loudness", loudness)
    else:
        result.add_skip("Audio Loudness", "Loudness not measured (disabled by this spec).")


def _report_scan(result: QAResult, plan: _ScanPlan, outcome: _ScanOutcome, cfg: dict) -> None:
    reason = _not_run_reason(outcome, plan)
    if reason:
        _mark_not_verified(result, plan.frame_check_names(), reason)
        return
    _report_file_integrity(result, plan, outcome, cfg)
    if plan.video is not None:
        video_end = (_decoded_video_seconds(plan, outcome.accounting)
                     or _stream_duration(plan.video) or plan.duration or 0.0)
        _report_black_frames(result, _close_open_regions(
            _lines_with_context(outcome.log_text, "blackdetect@v"), video_end), video_end)
        _report_freeze_frames(result, _close_open_regions(
            _lines_with_context(outcome.log_text, "freezedetect@v"), video_end), video_end)
    if plan.audio:
        _report_audio(result, plan, outcome, cfg)
    else:
        result.add_skip("Audio Silence", "No audio stream to analyze.")


# ============================================================================
# DEEP SCAN
# ============================================================================

def run_deep_scan(filepath: str, result: QAResult, cfg: dict,
                  progress_callback: Optional[Callable] = None,
                  cancel_event: Optional[threading.Event] = None,
                  quick: bool = False,
                  probe_data: Optional[dict] = None,
                  process_owner=None):
    """Decode the whole file once, in software, with every detector attached.

    One pass, always:
      * it needs no duration, so OBS-style recordings with none are still
        scanned (they used to be skipped with an ignored SKIP);
      * memory does not grow with file size, so there is no size gate (the
        old 8 GB gate skipped nearly every ProRes mezzanine master);
      * decode errors are counted exactly once (overlapping segments counted
        175 errors where there were 141) and decoded coverage is exact;
      * measured on an M3 Max it is as fast or faster than the former
        4-way segmented scan for HEVC 10-bit and ProRes, and ~1.45x slower
        only for light 8-bit H.264.

    Anything that cannot run is reported "Not verified", never omitted.
    """
    if probe_data is None:
        probe_data = run_ffprobe(filepath, process_owner, cancel_event)
    plan = _ScanPlan(probe_data or {}, cfg,
                     variable_rate=bool(result.metadata.get("vfr_detected")),
                     hidden_frames=result.metadata.get("edit_list_hidden_frames", 0))
    if quick:
        _mark_not_verified(result, plan.frame_check_names(),
                           "quick scan — the file was not decoded.")
        return
    if probe_data is None:
        _mark_not_verified(result, plan.frame_check_names(), "the file could not be probed.")
        return
    if plan.video is None and not plan.audio:
        result.add_not_verified("File Integrity", "the file has no video or audio stream to decode.")
        return
    ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        _mark_not_verified(result, plan.frame_check_names(), "FFmpeg was not found.")
        return
    if plan.duration and "duration_seconds" not in result.metadata:
        result.set_metadata("duration_seconds", round(plan.duration, 2))
        result.set_metadata("duration_display", _format_duration(plan.duration))

    cmd = _build_scan_command(ffmpeg_path, filepath, plan, cfg)
    if progress_callback:
        progress_callback("filter", 0, plan.duration or 0.0, "Integrity scan (software decode)")
    outcome = _run_scan_process(cmd, _calc_timeout(plan.duration, cfg), plan.duration, cfg,
                                progress_callback, cancel_event, process_owner)
    if outcome is None:
        return
    if getattr(outcome.log_text, "truncated_records", 0):
        result.set_metadata("deep_scan_stderr_truncated_lines", outcome.log_text.truncated_records)
    if progress_callback:
        end = plan.duration or outcome.last_out_time or 0.0
        progress_callback("filter", end, end, "Integrity scan (software decode)")
    _report_scan(result, plan, outcome, cfg)



# ============================================================================
# MEDIAINFO ENRICHED METADATA
# ============================================================================

def run_mediainfo_enrichment(filepath: str, result: QAResult, cfg: dict,
                             process_owner=None,
                             cancel_event: Optional[threading.Event] = None):
    """Add writing application/library and commercial format names.

    Informational only: it never adds a check row. A failure is recorded in
    metadata (``mediainfo_error``) instead of being swallowed silently.
    """
    if not cfg.get("use_mediainfo", True) or not _HAS_TOOLS_MODULE:
        return
    mi_path = get_mediainfo_path()
    if not mi_path:
        return
    try:
        proc = _run_owned_subprocess([mi_path, "--Output=JSON", filepath], 60,
                                     process_owner, cancel_event)
    except (subprocess.TimeoutExpired, OSError, TypeError) as exc:
        result.set_metadata("mediainfo_error", f"{type(exc).__name__}: {exc}"[:200])
        return
    if proc is None:
        return
    if proc.returncode != 0:
        result.set_metadata("mediainfo_error", f"exit code {proc.returncode}")
        return
    try:
        tracks = json.loads(proc.stdout).get("media", {}).get("track", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        result.set_metadata("mediainfo_error", f"unreadable output: {exc}"[:200])
        return
    for track in tracks:
        if not isinstance(track, dict) or track.get("@type") != "General":
            continue
        for source, key in (("Encoded_Application", "encoding_app"),
                            ("Encoded_Library", "encoding_library"),
                            ("Format_Commercial_IfAny", "format_commercial")):
            if track.get(source):
                result.set_metadata(key, track[source])
        overall = _safe_float(track.get("OverallBitRate"))
        if overall:
            result.set_metadata("mediainfo_overall_bitrate_mbps", round(overall / 1_000_000, 2))


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
            return [path] if ext in extensions else []
        if os.path.isdir(path):
            try:
                entries = sorted(os.listdir(path))
            except (PermissionError, OSError):
                return []
            return [os.path.join(path, f) for f in entries
                    if os.path.splitext(f)[1].lower() in extensions]
    except (PermissionError, OSError):
        return []
    return []


def config_snapshot(cfg: dict) -> dict:
    """JSON-safe copy of the settings a scan actually used.

    Compatibility-only keys are left out: they have no effect and would
    suggest otherwise in an exported report.
    """
    snapshot = {}
    for key, value in cfg.items():
        if key in COMPAT_ONLY_CONFIG_KEYS:
            continue
        if isinstance(value, (set, frozenset, tuple)):
            value = sorted(value)
        elif isinstance(value, list):
            value = list(value)
        snapshot[key] = value
    return snapshot


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run_metadata_checks(probe_data: dict, result: QAResult, cfg: dict,
                         filepath: str, process_owner, cancel_event) -> None:
    """Every check answerable from the container and a few packet/frame reads."""
    video = _primary_video_stream(probe_data)
    first_frame, timing = {}, None
    if video is not None and "index" in video:
        first_frame = probe_first_video_frame(filepath, int(video["index"]),
                                              process_owner, cancel_event)
        timing = probe_frame_timing(filepath, int(video["index"]),
                                    _probed_duration(probe_data), cfg,
                                    process_owner, cancel_event,
                                    start_time=_safe_float(video.get("start_time"), 0.0) or 0.0,
                                    stream_duration=_stream_duration(video))
        if timing is not None and timing.hidden_leadin:
            result.set_metadata("edit_list_hidden_frames", timing.hidden_leadin)
    check_video_codec(probe_data, result, cfg)
    check_resolution(probe_data, result, cfg)
    check_aspect_ratio(probe_data, result, cfg)
    check_frame_rate(probe_data, result, cfg)
    check_cfr(probe_data, result, cfg, timing)
    check_scan_type(probe_data, result, cfg, first_frame)
    check_color_hdr(probe_data, result, cfg, first_frame)
    check_xr_metadata(probe_data, result, cfg, first_frame)
    check_bitrate(probe_data, result, cfg)
    check_audio(probe_data, result, cfg)
    check_av_duration_mismatch(probe_data, result, cfg)
    check_frame_count(probe_data, result, cfg)


def run_qa_checks(filepath: str, cfg: dict = None,
                  strict: bool = False,
                  quick: bool = False,
                  progress_callback: Optional[Callable] = None,
                  cancel_event: Optional[threading.Event] = None,
                  process_owner=None) -> QAResult:
    """Run all QA checks on a single file, isolating any unexpected error."""
    if cfg is None:
        cfg = DEFAULT_CONFIG.copy()

    result = QAResult(filepath)
    # Recorded on the result: finalize() is called later with no arguments.
    result.strict = bool(strict)
    result.config = config_snapshot(cfg)
    result.started_at = _now_iso()

    try:
        if cancel_event and cancel_event.is_set():
            return result
        result.tool_versions = get_tool_versions()

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

        _run_metadata_checks(probe_data, result, cfg, filepath, process_owner, cancel_event)
        if cancel_event and cancel_event.is_set():
            return result

        run_deep_scan(filepath, result, cfg,
                      progress_callback=progress_callback,
                      cancel_event=cancel_event,
                      quick=quick,
                      probe_data=probe_data,
                      process_owner=process_owner)
        if cancel_event and cancel_event.is_set():
            return result

        run_mediainfo_enrichment(filepath, result, cfg, process_owner, cancel_event)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        result.add_fail("Internal Error", f"Unexpected error during scan: {type(e).__name__}: {str(e)[:200]}")
    finally:
        result.finished_at = _now_iso()

    return result
