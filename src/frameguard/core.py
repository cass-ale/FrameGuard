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
import shutil
import tempfile
import threading
from collections import Counter, deque
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Callable

from frameguard import audio_checks, container_checks, video_checks

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
# that are unambiguously 360 carry a projection requirement. "8k30" is 8K UHD
# (16:9, flat); an 8K 360 equirect is 7680x3840 (2:1) and has its own preset.

# Meta Quest 360 delivery (Meta Media Studio 360/180 video requirements; Meta
# Horizon OS media requirements): MP4, H.264/HEVC, "Yuv 420p", SAR 1:1,
# 30 or 60 fps, hardware decode limit 8192x4096 at 60 fps. Fast-start is
# YouTube's and every streaming player's requirement.
_QUEST_360_DELIVERY = {
    "required_projection": "equirectangular",
    "accepted_containers": [".mp4"],
    "accepted_codecs": ["h264", "hevc"],
    "allowed_pix_fmts": ["yuv420p"],
    "require_square_pixels": True,
    "require_faststart": True,
    "allow_fps_multiples": True,
    "max_frame_pixels": 8192 * 4096,
    "max_pixel_rate": 8192 * 4096 * 60,
}

PRESETS = {
    "4k30": {"label": "4K UHD @ 30fps", "width": 3840, "height": 2160, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 20},
    "4k60": {"label": "4K UHD @ 60fps", "width": 3840, "height": 2160, "fps": 60.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 30},
    "4k24": {"label": "4K UHD @ 24fps (Cinematic)", "width": 3840, "height": 2160, "fps": 24.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 18},
    "1080p30": {"label": "1080p Full HD @ 30fps", "width": 1920, "height": 1080, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 8},
    "1080p60": {"label": "1080p Full HD @ 60fps", "width": 1920, "height": 1080, "fps": 60.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 12},
    "1080p24": {"label": "1080p Full HD @ 24fps (Cinematic)", "width": 1920, "height": 1080, "fps": 24.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 6},
    "720p30": {"label": "720p HD @ 30fps (Proxy/Review)", "width": 1280, "height": 720, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 4},
    "5.7k30": {"label": "5.7K @ 30fps (360 VR Capture)", "width": 5760, "height": 2880, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 40,
               "required_projection": "equirectangular", "require_square_pixels": True},
    "8k30": {"label": "8K UHD @ 30fps (7680x4320, flat)", "width": 7680, "height": 4320, "fps": 30.0, "fps_tolerance": 0.5, "min_bitrate_mbps": 60},
    "8k360": {"label": "8K 360° mono @ 30fps (7680x3840, Quest)", "width": 7680, "height": 3840, "fps": 30.0, "fps_tolerance": 0.5,
              "min_bitrate_mbps": 60, "required_stereo_mode": "mono", **_QUEST_360_DELIVERY},
    "360tb5760": {"label": "5.7K 360° 3D top-bottom @ 30fps (5760x5760, Quest)", "width": 5760, "height": 5760, "fps": 30.0,
                  "fps_tolerance": 0.5, "min_bitrate_mbps": 40, "required_stereo_mode": "top_bottom", **_QUEST_360_DELIVERY},
    "360tb3840": {"label": "4K 360° 3D top-bottom @ 30fps (3840x3840, Quest)", "width": 3840, "height": 3840, "fps": 30.0,
                  "fps_tolerance": 0.5, "min_bitrate_mbps": 25, "required_stereo_mode": "top_bottom", **_QUEST_360_DELIVERY},
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
    # container_checks.py: Codec Profile and Spatial Audio fail only on what
    # the spec sets; A/V Sync Offset fails beyond the EBU R37 chain budget.
    "Codec Profile", "Spatial Audio", "A/V Sync Offset",
    # Like Audio Loudness: only FAILs when signal_levels_mode is "essential".
    "Signal Levels",
}

ADVISORY_CHECKS = {
    "Frame Count", "Black Frames", "Freeze Frames",
    "Container Structure", "Edit List", "Timecode", "Track Layout",
    # Content-dependent screening (see video_checks): warnings, never FAIL.
    "Flash Risk", "Interlace Detection",
    # Raised when part of the analysis could not run.
    "Deep Scan",
}

# The checks produced by the full-decode pass. When that pass cannot run,
# every one of them is reported "Not verified" rather than left out.
FRAME_LEVEL_CHECKS = (
    "File Integrity", "Black Frames", "Freeze Frames", "Signal Levels",
    "Flash Risk", "Interlace Detection", "Audio Silence",
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
    "Signal Levels": 36,
    "Black Frames": 35,
    "Interlace Detection": 34,
    "Flash Risk": 32,
    "Audio Silence": 30,
    "Audio Codec": 25,
    "Audio Loudness": 25,
    "Frame Rate": 20,
    "Codec Profile": 65,
    "A/V Sync Offset": 60,
    "Spatial Audio": 55,
    "Container Structure": 55,
    "Edit List": 35,
    "Timecode": 30,
    "Track Layout": 25,
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
# "report" measures and shows loudness without judging it: camera originals
# and phone recordings routinely sit far from broadcast targets (-23 LUFS,
# -1 dBTP) and must not be flagged unless the spec asks for it.
LOUDNESS_MODES = ("off", "report", "advisory", "essential")
# "required" fails a file without a consistent SA3D ambisonic track.
SPATIAL_AUDIO_CHOICES = ("any", "required")
SIGNAL_LEVELS_MODES = video_checks.LEVELS_MODES

DEFAULT_CONFIG = {
    # Picture geometry and timing. Resolution must match exactly unless
    # allow_larger_resolution; the frame rate must match within tolerance
    # (integer multiples only when allow_fps_multiples).
    "target_width": 3840,
    "target_height": 2160,
    "allow_larger_resolution": False,
    "target_fps": 30.0,
    # fps_tolerance absorbs measurement jitter only. NTSC rates (x1000/1001:
    # 23.976, 29.97, 59.94) never match their integer rate through it; that
    # takes allow_ntsc_rate_match.
    "fps_tolerance": 0.5,
    "allow_fps_multiples": False,
    "allow_ntsc_rate_match": False,
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

    # XR metadata. Display rotation on a spherical file is a failure unless
    # allowed; on a flat file only 180 degrees / mirroring warn (90/270 are
    # judged through the displayed resolution).
    "required_projection": "any",
    "required_stereo_mode": "any",
    "allow_display_rotation": False,
    # "required" fails a file without consistent SA3D ambisonics.
    "spatial_audio": "any",

    # Picture and codec conformance ("any" = report only). Pixel limits of 0
    # mean no decoder ceiling.
    "require_square_pixels": False,
    "warn_untagged_color": True,
    "allowed_video_profiles": ["any"],
    "allowed_pix_fmts": ["any"],
    "max_frame_pixels": 0,
    "max_pixel_rate": 0,

    # Container structure and tracks. A lossless trim's single edit (hidden
    # lead-in) is normal unless allow_trim_edit_lists is off.
    "require_faststart": False,
    "allow_trim_edit_lists": True,
    "max_audio_tracks": 0,
    # Optional container/XR checks a spec may switch off by name.
    "disabled_checks": [],

    # File integrity (full software decode)
    "max_decode_errors": 0,

    # File discovery
    "video_extensions": {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".ts", ".webm"},

    # Deep-scan output bounds
    "deep_scan_max_stderr_lines": 5000,
    "deep_scan_max_stderr_chars": 512 * 1024,

    # Picture checks (video_checks.py). Black: a picture with at least
    # black_detect_picture_threshold of its pixels at or below the level
    # black_detect_pix_threshold (0.02 = Y <= 20 at 8-bit limited range) for
    # black_detect_duration seconds; bursts from black_short_min_frames
    # frames up to that are reported only when cut in hard (0 disables).
    "black_frames_enabled": True,
    "black_detect_duration": 0.3,
    "black_detect_pix_threshold": 0.02,
    "black_detect_picture_threshold": 0.98,
    "black_short_min_frames": 3,
    # Freeze: a held picture at least this long (EBU QC 0044B example: 25 frames).
    "freeze_frames_enabled": True,
    "freeze_detect_duration": 1.0,
    # EBU R 103 v3.0 luma/RGB tolerance: off, report, advisory or essential
    # (essential is for broadcast delivery specs; camera originals routinely
    # exceed broadcast levels).
    "signal_levels_mode": "advisory",
    # ITU-R BT.1702-3 flash screening (warning only; not a certification).
    "flash_check_enabled": True,
    # idet combing in progressive video / field order vs metadata.
    "interlace_check_enabled": True,
    "silence_noise_db": -50,
    "silence_duration": 2.0,
    # A channel (or a whole track) silent for at least this fraction of its
    # length is dead.
    "dead_channel_ratio": 0.95,

    # Loudness (EBU R128): "off" skips ebur128 entirely, "report" measures
    # without enforcing, "advisory" warns and "essential" fails off-target.
    "loudness_mode": "report",
    "target_loudness_lufs": -23.0,
    "loudness_tolerance_lu": 2.0,
    "true_peak_max_dbtp": -1.0,

    # A/V sync. Ends (Telestream FC-108): audio may end up to
    # av_short_tolerance_frames before the last frame and up to
    # av_long_tolerance_s after it; audio short by more than
    # duration_mismatch_threshold fails. Starts: DPP v5 2.8 file tolerance
    # (warn) and the EBU R37 chain budget, 40 ms early / 60 ms late (fail).
    "duration_mismatch_threshold": 0.5,
    "av_short_tolerance_frames": 2,
    "av_long_tolerance_s": 2.0,
    "av_offset_warn_ms": 5.0,
    "av_offset_max_early_ms": 40.0,
    "av_offset_max_late_ms": 60.0,

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

# --- Audio signal QC (frameguard.audio_checks) ------------------------------
# Loudness profiles, true peak, clipping, dropouts, phase, dual mono, DC and
# hum: their names, verdict classes, weights and defaults live in that module.
ESSENTIAL_CHECKS |= audio_checks.ESSENTIAL_CHECKS
ADVISORY_CHECKS |= audio_checks.ADVISORY_CHECKS
FRAME_LEVEL_CHECKS += tuple(sorted(audio_checks.ESSENTIAL_CHECKS | audio_checks.ADVISORY_CHECKS))
CHECK_SEVERITY_WEIGHTS.update(audio_checks.SEVERITY_WEIGHTS)
DEFAULT_CONFIG.update(audio_checks.DEFAULT_CONFIG)
LOUDNESS_PROFILES = audio_checks.LOUDNESS_PROFILE_CHOICES
# --- end audio signal QC -----------------------------------------------------

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
    # 0.9.1: freezedetect was replaced by a frame-difference run detector.
    "freeze_detect_noise",
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

    def add_not_verified(self, check_name: str, reason: str, by_choice: bool = False):
        """Record an analysis that did not run.

        SKIP is ignored by the verdict, so an unrun integrity scan used to read
        exactly like a clean one. "Not verified" is a WARN with an explicit
        label: the file can never come out a clean PASS on unverified ground.

        ``by_choice`` marks analysis the operator chose not to run (a quick
        scan). Strict Mode escalates genuine failures to analyse, but not a
        choice: otherwise every quick + strict scan was a FAIL.
        """
        with self._lock:
            detail = f"{NOT_VERIFIED_PREFIX}{reason}"
            self.checks.append((check_name, "WARN", detail, 100))
            self.warnings.append(f"{check_name}: {detail}")
            key = "not_run_by_choice" if by_choice else "not_verified"
            self.metadata.setdefault(key, [])
            if check_name not in self.metadata[key]:
                self.metadata[key].append(check_name)

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
            "Signal Levels": "picture levels or colours are outside broadcast range",
            "Flash Risk": "possible flashing (photosensitivity) risk",
            "Interlace Detection": "interlacing or field-order problem in the picture",
            "Audio Silence": "long stretches of silence found",
            "Dead Channel": "an audio channel is silent",
            "Audio Loudness": "audio levels are outside the target range",
            "Codec Profile": "codec profile, level or pixel format doesn't match the spec",
            "A/V Sync Offset": "audio and video don't start together",
            "Spatial Audio": "spatial (ambisonic) audio metadata is missing or wrong",
            "Container Structure": "file structure is damaged or not streaming-ready",
            "Edit List": "an edit list changes what plays",
            "Timecode": "timecode is discontinuous",
            "Track Layout": "unexpected track layout",
            "FFprobe": "file could not be read (may be corrupt)",
            "Internal Error": "an unexpected error occurred during the scan",
        }
        _PLAIN.update(audio_checks.PLAIN_SUMMARIES)

        issues = []
        for check_name, status, *_ in self.checks:
            if status == "FAIL":
                issues.append(_PLAIN.get(check_name, check_name.lower() + " issue"))
        if issues:
            if len(issues) == 1:
                return issues[0][0].upper() + issues[0][1:] + "."
            return ", ".join(i.capitalize() for i in issues) + "."

        notes = []
        for check_name, status, detail, *_ in self.checks:
            if status != "WARN":
                continue
            if str(detail).startswith(NOT_VERIFIED_PREFIX):
                notes.append(f"{check_name.lower()} not verified")
            else:
                notes.append(_PLAIN.get(check_name, check_name.lower() + " concern"))
        notes = list(dict.fromkeys(notes))
        if not notes:
            return "Passed with minor notes."
        # The wording follows the final verdict: under Strict Mode warnings
        # are failures, and "Heads up" on a FAIL row contradicted it.
        verdict = self.status or self.overall_status()
        if verdict == "MISMATCH":
            return "Failed in Strict Mode: " + ", ".join(notes) + "."
        return "Heads up: " + ", ".join(notes) + "."

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
        by_choice = set(self.metadata.get("not_run_by_choice") or ())

        for check_tuple in self.checks:
            name, status = check_tuple[0], check_tuple[1]
            confidence = check_tuple[3] if len(check_tuple) > 3 else 100

            if status in ("PASS", "SKIP"):
                continue

            if name in ("FFprobe", "Internal Error") and status == "FAIL":
                return "MISMATCH"

            if name in ESSENTIAL_CHECKS and status == "FAIL" and confidence >= 70:
                return "MISMATCH"

            chosen = (status == "WARN" and name in by_choice
                      and str(check_tuple[2]).startswith(NOT_VERIFIED_PREFIX))
            if strict and not chosen:
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
    """Validate the container extension, its real signature and that the file is non-empty.

    The extension used to be the only evidence, so a Matroska or MPEG-TS
    file renamed .mp4 passed (container_checks.extension_mismatch).
    """
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
    mismatch = container_checks.extension_mismatch(filepath, ext)
    if mismatch:
        result.add_fail("Container Format", mismatch)
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


def _orientation_name(width: int, height: int) -> str:
    return "portrait" if height > width else "landscape" if width > height else "square"


def _orientation_mismatch(width: int, height: int, tw: int, th: int, rotation: int) -> Optional[str]:
    """A clear message when the picture is turned the other way from the target.

    A portrait phone clip against a UHD spec used to read "smaller than the
    target... would upscale", which sent operators looking for a scaling
    problem instead of a rotation one.
    """
    actual, wanted = _orientation_name(width, height), _orientation_name(tw, th)
    if "square" in (actual, wanted) or actual == wanted:
        return None
    turned = f" (rotated {abs(rotation)}°)" if abs(rotation) % 180 == 90 else ""
    return (f"{actual.capitalize()} orientation{turned}: {width}x{height} displayed vs "
            f"{tw}x{th} {wanted} target.")


def _flat_display_transform_warning(video: dict, cfg: dict) -> str:
    """Advisory text for a flat picture shown upside down or mirrored by its matrix.

    90/270-degree flags are ordinary portrait captures and are judged through
    the displayed size; 180 degrees and mirroring leave the size unchanged,
    so an NLE or player that ignores the display matrix shows the picture
    upside down or reversed with nothing else to warn the operator. Spherical
    files are judged in XR Metadata instead.
    """
    if cfg.get("allow_display_rotation", False) or _side_data(video.get("side_data_list"), "Spherical Mapping"):
        return ""
    rotation, mirrored = container_checks.display_transform(video)
    parts = (["rotated 180°"] if abs(rotation) % 360 == 180 else []) + (["mirrored"] if mirrored else [])
    if not parts:
        return ""
    return (f"The display matrix shows the picture {' and '.join(parts)}: players and editors that ignore "
            f"it present the stored picture as is.")


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

    orientation = _orientation_mismatch(width, height, tw, th, rotation)
    turned = _flat_display_transform_warning(video, cfg)
    if (width, height) == (tw, th) and turned:
        result.add_warn("Resolution", f"{width}x{height} (matches {tw}x{th} target){rot_note}. {turned}",
                        confidence=70)
    elif (width, height) == (tw, th):
        result.add_pass("Resolution", f"{width}x{height} (matches {tw}x{th} target){rot_note}")
    elif orientation:
        result.add_fail("Resolution", orientation, confidence=95)
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
    width, height, rotation = _displayed_size(video)
    tw, th = int(cfg["target_width"]), int(cfg["target_height"])
    if not width or not height or not tw or not th:
        result.add_not_verified("Aspect Ratio", "frame size is unknown.")
        return
    if _orientation_mismatch(width, height, tw, th, rotation):
        # Resolution already fails a picture turned the other way; judging
        # its shape again would report one cause twice.
        result.add_skip("Aspect Ratio", f"Not judged separately: the picture is {_orientation_name(width, height)} "
                                        f"and the target {_orientation_name(tw, th)} (see Resolution).")
        return
    sar = _ratio(video.get("sample_aspect_ratio")) or 1.0
    if abs(_stream_rotation_degrees(video)) % 180 == 90:
        sar = 1.0 / sar
    display = width * sar / height
    target = tw / th
    result.set_metadata("display_aspect_ratio", round(display, 4))
    sar_note = f", SAR {video.get('sample_aspect_ratio')}" if abs(sar - 1.0) > 1e-3 else ""
    shape_ok = abs(display - target) / target <= 0.005
    if sar_note and cfg.get("require_square_pixels", False):
        # Meta Media Studio 360/180 requirements: "Pixel aspect ratio (SAR): 1:1".
        result.add_fail("Aspect Ratio", f"Non-square pixels{sar_note}: this spec requires square pixels (SAR 1:1); "
                                        f"displays at {display:.3f}:1, target {target:.3f}:1.", confidence=95)
    elif shape_ok and sar_note:
        result.add_warn("Aspect Ratio", f"{display:.3f}:1 matches the target {target:.3f}:1 only through non-square "
                                        f"pixels{sar_note}: platforms that ignore the pixel aspect show it "
                                        f"stretched.", confidence=70)
    elif shape_ok:
        result.add_pass("Aspect Ratio", f"{display:.3f}:1 matches the target {target:.3f}:1{sar_note}")
    else:
        result.add_fail("Aspect Ratio", f"Displays at {display:.3f}:1, target is {target:.3f}:1 "
                                        f"({tw}x{th}){sar_note}.", confidence=95)


# NTSC-family rates are N x 1000/1001 (23.976, 29.97, 59.94...). They are
# distinct rates, not roundings of 24/30/60: conforming one to the other needs
# a 0.1% speed change (and pitch shift) or pulldown.
_NTSC_BASES = (24, 30, 48, 60, 120, 240)


def _exact_target_rate(rate: float) -> float:
    """A typed target such as 29.97 or 23.976 as its exact NTSC rational."""
    for base in _NTSC_BASES:
        ntsc = base * 1000 / 1001
        if abs(rate - ntsc) < 0.006:
            return ntsc
    return rate


def _same_rate(a: float, b: float) -> bool:
    """Equal within 0.02%: container timestamp rounding, far below the 0.1% NTSC gap."""
    return abs(a - b) <= max(a, b) * 2e-4


def _rate_relation(fps: float, target: float, tolerance: float) -> str:
    """'exact', 'ntsc' (the x1000/1001 counterpart), 'tolerance' or 'none'."""
    if _same_rate(fps, target):
        return "exact"
    if _same_rate(fps, target * 1000 / 1001) or _same_rate(fps, target * 1001 / 1000):
        return "ntsc"
    if abs(fps - target) <= tolerance:
        return "tolerance"
    return "none"


def _rate_text(fps: float) -> str:
    for base in _NTSC_BASES:
        if _same_rate(fps, base * 1000 / 1001):
            return f"{fps:.3f} fps (NTSC {base * 1000}/1001)"
    return f"{fps:.3f} fps"


def check_frame_rate(probe_data: dict, result: QAResult, cfg: dict):
    """The frame rate must equal the target rate exactly, as a rational.

    29.97 (30000/1001) passed a 30p spec silently because ``fps_tolerance``
    (0.5) bridged the 0.1% NTSC gap. An NTSC rate against its integer
    counterpart (or the reverse) is now a confident WARN — Strict Mode turns
    it into a failure — and passes only with ``allow_ntsc_rate_match``. Not a
    FAIL by default: camera "24p" is 23.976 almost everywhere and many specs
    say 24 meaning it, so failing would turn whole shoots red; but the rates
    really differ (3.6 s drift per hour), so it is never a clean pass.
    ``fps_tolerance`` still absorbs measurement jitter between other rates. 25p against a 24p spec fails (conforming
    needs a speed change). Integer multiples (60p for a 30p spec) pass only
    with ``allow_fps_multiples``.
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
    typed = float(cfg["target_fps"])
    target = _exact_target_rate(typed)
    tolerance = float(cfg["fps_tolerance"])
    allow_ntsc = bool(cfg.get("allow_ntsc_rate_match", False))
    relation = _rate_relation(fps, target, tolerance)
    ntsc_fail = (f"{_rate_text(fps)} is the NTSC counterpart of the {typed:g} fps target, not the same rate: "
                 f"they drift 3.6 s per hour, and conforming needs a 0.1% speed change or pulldown. "
                 f"Set the target to the exact rate, or allow NTSC rate matching if the spec treats them as equal.")
    if relation == "exact":
        result.add_pass("Frame Rate", f"{_rate_text(fps)} (matches the {typed:g} fps target)")
        return
    if relation == "ntsc":
        if allow_ntsc:
            result.add_pass("Frame Rate", f"{_rate_text(fps)} is the NTSC counterpart of the {typed:g} fps "
                                          f"target (NTSC rate matching allowed by this spec)")
        else:
            result.add_warn("Frame Rate", ntsc_fail, confidence=90)
        return
    if relation == "tolerance":
        result.add_pass("Frame Rate", f"{fps:.3f} fps (target: {typed:g} ± {tolerance:g} fps)", confidence=90)
        return
    multiple = round(fps / target) if target > 0 else 0
    base = _rate_relation(fps / multiple, target, tolerance) if multiple >= 2 else "none"
    if base != "none":
        # A multiple is judged as a multiple first: 59.94 against a 30 fps
        # spec that forbids multiples is the wrong rate (FAIL), not a mere
        # NTSC difference. Only when multiples are allowed does the NTSC gap
        # of the base rate decide between PASS and the NTSC warning.
        if not cfg.get("allow_fps_multiples", False):
            result.add_fail("Frame Rate", f"{_rate_text(fps)} is {multiple}x the {typed:g} fps target; "
                                          f"integer multiples are not allowed by this spec.")
        elif base == "ntsc" and not allow_ntsc:
            result.add_warn("Frame Rate", f"{_rate_text(fps)} is {multiple}x the NTSC counterpart of the "
                                          f"{typed:g} fps target, not an exact multiple: the rates drift "
                                          f"3.6 s per hour. Allow NTSC rate matching if the spec treats "
                                          f"them as equal.", confidence=90)
        else:
            result.add_pass("Frame Rate", f"{_rate_text(fps)} is {multiple}x the {typed:g} fps target "
                                          f"(integer multiples allowed by this spec)")
        return
    result.add_fail("Frame Rate", f"{_rate_text(fps)} does not match the {typed:g} ± {tolerance:g} fps target.")


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

    hdr_gaps = _hdr_metadata_gaps(video, first_frame, transfer)
    if hdr_gaps and required in ("pq", "hdr"):
        problems.append(hdr_gaps)
    advisories = _colour_signalling_advisories(video, first_frame, transfer, depth, cfg)
    if hdr_gaps and required not in ("pq", "hdr"):
        advisories.append(hdr_gaps)

    if problems:
        result.add_fail("Color / HDR", f"{summary}. " + "; ".join(problems + advisories) + ".")
    elif advisories:
        result.add_warn("Color / HDR", f"{summary}. " + "; ".join(advisories) + ".", confidence=80)
    else:
        enforced = [n for n, v in (("bit depth", min_depth), ("transfer", required)) if v not in ("any", None)]
        note = f" (meets spec: {', '.join(enforced)})" if enforced else " (not enforced by this spec)"
        result.add_pass("Color / HDR", summary + note)


_UNSIGNALLED = ("", "unknown", "unspecified", "reserved", "none")


def _hdr_metadata_gaps(video: dict, first_frame: dict, transfer: str) -> str:
    """What a PQ (HDR10) stream lacks of its static metadata, or ''.

    PQ is display-referred: without SMPTE ST 2086 mastering-display metadata
    and a real MaxCLL/MaxFALL (CTA-861.3; 0 means "unknown") a display
    tone-maps blindly, and HDR10 delivery specs (Netflix, Apple, Amazon)
    require both. HLG is scene-referred (ITU-R BT.2100) and needs neither.
    """
    if transfer != "pq":
        return ""
    sides = list(video.get("side_data_list") or []) + list((first_frame or {}).get("side_data_list") or [])
    missing = []
    if not _side_data(sides, "Mastering display metadata"):
        missing.append("SMPTE ST 2086 mastering-display metadata")
    light = _side_data(sides, "Content light level metadata")
    if not light:
        missing.append("MaxCLL/MaxFALL")
    elif not _safe_int(light.get("max_content"), 0):
        missing.append("a real MaxCLL (it is 0, meaning unknown)")
    if not missing:
        return ""
    return f"PQ HDR without {' or '.join(missing)}: displays tone-map without knowing the grade"


def _colour_signalling_advisories(video: dict, first_frame: dict, transfer: str,
                                  depth: Optional[int], cfg: dict) -> List[str]:
    """Colour tags that are absent or contradict the picture (advisory; ITU-T H.273)."""
    first_frame = first_frame or {}
    matrix = str(video.get("color_space") or first_frame.get("color_space") or "").lower()
    primaries = str(video.get("color_primaries") or first_frame.get("color_primaries") or "").lower()
    width = _safe_int(video.get("width"), 0) or 0
    out = []
    if (cfg.get("warn_untagged_color", True) and matrix in _UNSIGNALLED and primaries in _UNSIGNALLED
            and transfer == "unspecified"):
        out.append("colour primaries, transfer and matrix are all unsignalled, so players guess "
                   "(BT.601 or BT.709 by frame size) and colours can differ between players")
    if width >= 1280 and matrix in ("smpte170m", "bt470bg"):
        out.append(f"a BT.601 (SD) matrix is signalled on a {width}-wide HD picture; HD and UHD SDR use "
                   f"BT.709, so colours decode shifted if the tag is wrong")
    if transfer in ("pq", "hlg") and depth is not None and depth < 10:
        out.append(f"{transfer.upper()} HDR is signalled on {depth}-bit video; ITU-R BT.2100 requires 10 or "
                   f"12 bits, so this is most likely SDR tagged as HDR")
    return out


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
                      first_frame: Optional[dict] = None,
                      structure: Optional["container_checks.Structure"] = None,
                      grabber: Optional["container_checks.FrameGrabber"] = None):
    """Require the spherical / stereo-3D signalling a 360 or VR spec declares.

    A 360 file without spherical metadata plays flat on every headset and
    web player. With "any" the findings are reported but not enforced.
    ``structure`` (the box reader's view) adds what ffprobe merges away:
    V1-only or conflicting V1/V2 metadata, per-eye geometry, display
    rotation on a spherical picture (container_checks.xr_metadata_findings).
    ``grabber`` lets a spherical file's picture be compared with its stereo
    tag (advisory content check, two downscaled frames).
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

    hard, soft, notes = container_checks.xr_metadata_findings(
        video, projection, stereo, structure, cfg, grabber, _stream_duration(video) or _probed_duration(probe_data))
    problems += hard
    note = f" ({'; '.join(notes)})" if notes else ""
    if problems:
        result.add_fail("XR Metadata", f"{found[0].upper()}{found[1:]}. " + "; ".join(problems + soft) + "." + note)
    elif soft:
        content_only = all("content check" in text for text in soft)
        result.add_warn("XR Metadata", f"{found[0].upper()}{found[1:]}. " + "; ".join(soft) + "." + note,
                        confidence=60 if content_only else 80)
    else:
        enforced = want_projection != "any" or want_stereo != "any"
        result.add_pass("XR Metadata", f"{found[0].upper()}{found[1:]}"
                        + (" (matches spec)" if enforced else " (not enforced by this spec)") + note)


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
        profile_rate = audio_checks.sample_rate_entry(rate, cfg)
        if rate <= 0:
            rates.append(("WARN", _with_track(label, f"{NOT_VERIFIED_PREFIX}sample rate unknown.")))
        elif rate < min_rate:
            rates.append(("FAIL", _with_track(label, f"{rate} Hz is below the {min_rate} Hz minimum")))
        elif profile_rate:
            # An enforced delivery profile (EBU R 128, A/85, Netflix) needs 48 kHz exactly.
            rates.append((profile_rate[0], _with_track(label, profile_rate[1])))
        else:
            rates.append(("PASS", _with_track(label, f"{rate} Hz (minimum {min_rate} Hz)")))

    _add_aggregate(result, "Audio Codec", codecs)
    _add_aggregate(result, "Audio Channels", channels)
    _add_aggregate(result, "Audio Sample Rate", rates)


def _av_end_entry(signed: float, frame: float, cfg: dict) -> Tuple[str, str]:
    """(status, note) for audio ending ``signed`` seconds after (+) or before (-) the picture.

    Telestream Qualify FC-108: audio may be no more than 2 frames shorter
    than the video and no more than 2 s longer. Short audio up to
    ``duration_mismatch_threshold`` is a warning (encoder tail padding varies
    by an AAC frame or two); beyond it the end of the programme is silent.
    """
    short_tol = int(cfg.get("av_short_tolerance_frames", 2)) * frame
    long_tol = float(cfg.get("av_long_tolerance_s", 2.0))
    fail_short = max(float(cfg["duration_mismatch_threshold"]), short_tol)
    frames = abs(signed) / frame if frame else 0.0
    if signed < 0:
        gap = -signed
        text = f"audio ends {gap:.3f} s ({frames:.1f} frames) before the last frame"
        if gap <= short_tol + 1e-3:
            return "PASS", ""
        if gap <= fail_short:
            return "WARN", f"; {text} (Telestream FC-108 allows {short_tol / frame:g} frames)"
        return "FAIL", f"; {text}: the end of the programme has no sound"
    if signed <= short_tol + 1e-3:
        return "PASS", ""
    if signed <= long_tol:
        return "PASS", f"; audio runs {signed:.3f} s past the last frame (within {long_tol:g} s)"
    return "FAIL", f"; audio runs {signed:.3f} s past the last frame (more than {long_tol:g} s)"


def check_av_duration_mismatch(probe_data: dict, result: QAResult, cfg: dict,
                               ends: Optional[Dict[int, float]] = None):
    """Compare each audio track's end against the video's end (asymmetric, Telestream FC-108).

    A symmetric 0.5 s window passed audio that stopped 0.3 s early and
    warned on audio that ran 1 s long, the opposite of what QC cares about:
    short audio leaves the programme's end silent, long audio is trimmed.
    ``ends`` (stream index -> measured end of its last packet) replaces the
    estimated durations of MPEG-TS/PS (container_checks.measured_stream_ends).
    """
    video = _primary_video_stream(probe_data)
    tracks = _audio_streams(probe_data)
    if video is None or not tracks:
        result.add_skip("A/V Duration", "Need both video and audio streams to compare.")
        return
    v_dur = _safe_float(_stream_duration_text(video))

    def _start_of(stream):
        return _safe_float(stream.get("start_time"), 0.0) or 0.0

    fps = (parse_frame_rate(video.get("avg_frame_rate", "0/0"))
           or parse_frame_rate(video.get("r_frame_rate", "0/0")))
    frame = 1.0 / fps if fps > 0 else 1.0 / 30
    ends = ends or {}
    measured = _safe_int(video.get("index")) in ends
    if measured:
        v_dur = ends[_safe_int(video.get("index"))] - _start_of(video)
    entries = []
    worst_diff = 0.0
    for position, track in enumerate(tracks):
        label = _track_label(position, track, len(tracks))
        a_dur = _safe_float(_stream_duration_text(track))
        if measured and _safe_int(track.get("index")) in ends:
            a_dur = ends[_safe_int(track.get("index"))] - _start_of(track)
        if v_dur is None or a_dur is None or v_dur <= 0 or a_dur <= 0:
            entries.append(("WARN", _with_track(label, "Per-stream duration not available.")))
            continue
        # Compare stream ENDS: streams routinely start at different offsets
        # (the start offset itself is A/V Sync Offset's verdict).
        signed = (_start_of(track) + a_dur) - (_start_of(video) + v_dur)
        diff = abs(signed)
        worst_diff = max(worst_diff, diff)
        status, note = _av_end_entry(signed, frame, cfg)
        text = f"Video: {v_dur:.2f}s, Audio: {a_dur:.2f}s (diff: {diff:.3f}s){note}"
        entries.append((status, _with_track(label, text)))
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
    meaningless) and for Matroska and MPEG-TS/PS, which record no frame count.
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
        elif container_checks._estimated_durations(probe_data):
            # MPEG-TS/PS index nothing: there is no declared count to check
            # (File Integrity still counts every decoded frame).
            result.add_skip("Frame Count", "Frame count is not recorded in MPEG transport/program streams.")
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
    if audio_checks.is_file_routed_line(line):
        # Read from the scan's measurement files (audio_checks), not the log.
        return ()
    categories = []
    if _is_decode_evidence(line):
        categories.append("decode")
    if _LOUDNESS_LINE_RE.search(line):
        categories.append("loudness")
    if audio_checks.is_evidence_line(line):
        categories.append("audio")
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
        if category in ("decode", "loudness", "audio"):
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
                peak_pending_chars: int, evidence_dropped: int = 0):
        value = super().__new__(cls, text)
        value.truncated_records = truncated_records
        # Evidence records (detections, summaries) the capture had to drop:
        # a verdict parsed from this text is then incomplete.
        value.evidence_dropped = evidence_dropped
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
        self.evidence_dropped = 0
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
                self.evidence_dropped += 1
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
            self.evidence_dropped,
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
    r"|Integrated loudness:|True peak:|Sample peak:|Loudness range:")
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
        self.video_checks = video_checks.VideoCheckConfig.from_cfg(cfg)
        # Set by run_deep_scan for the duration of the decode (temp files).
        self.video_analysis: Optional["video_checks.VideoAnalysis"] = None
        self.audio = _audio_streams(probe_data)
        self.duration = _probed_duration(probe_data)
        self.loudness = (cfg.get("loudness_mode", "report") != "off") and bool(self.audio)
        self.cfg = cfg
        # Audio (audio_checks), set by run_deep_scan: the loudness meter for
        # 3+ channel tracks once the bundled ebur128 is self-tested, the
        # tracks to treat as ambisonics, and the scan's private directory for
        # measurement files and filter-graph scripts.
        self.multichannel_meter = audio_checks.METER_EBUR128
        self.ambisonic = frozenset(
            i for i, t in enumerate(self.audio) if audio_checks.is_ambisonic(t))
        self.audio_files: Optional["audio_checks.MeasurementFiles"] = None
        self.graph_dir: Optional[str] = None
        self.graph_option = GRAPH_FILE_OPTION
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
            names += self.video_checks.enabled_names()
        if self.audio:
            names += ["Audio Silence", "Dead Channel"]
            if self.loudness:
                names.append("Audio Loudness")
            names += audio_checks.frame_check_names(self.cfg, self.loudness)
        return names


_EBUR128_MULTICHANNEL_OK: Dict[str, bool] = {}
_EBUR128_REFERENCE_RE = re.compile(r"^\s*I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", re.MULTILINE)


def _ebur128_multichannel_ok(ffmpeg: str, process_owner=None,
                             cancel_event: Optional[threading.Event] = None) -> Optional[bool]:
    """Known-answer test of the bundled ebur128 on three channels.

    1 kHz at -23 dBFS in L and R of a 3.0 layout is -23.0 LUFS by EBU Tech
    3341 case 1. The darwin-arm64 FFmpeg 9.0.2 build reads -5.0 (its >2
    channel K-weighting is broken; stereo is exact), the x86_64 build of the
    same release reads -23.0. Only a definite reading is cached; a run that
    fails, times out or prints no summary says nothing about the binary, so
    this scan uses the independent fallback meter and the next one retries.
    The process belongs to the scan, so Cancel stops it. None: cancelled.
    """
    if ffmpeg in _EBUR128_MULTICHANNEL_OK:
        return _EBUR128_MULTICHANNEL_OK[ffmpeg]
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-f", "lavfi",
           "-i", "sine=f=1000:d=2:sample_rate=48000,volume=0.5663568",
           "-af", "pan=3.0|c0=c0|c1=c0,ebur128", "-f", "null", "-"]
    try:
        proc = _run_owned_subprocess(cmd, 30, process_owner, cancel_event)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False
    if proc is None or (cancel_event is not None and cancel_event.is_set()):
        return None
    match = _EBUR128_REFERENCE_RE.search(proc.stderr or "")
    if proc.returncode != 0 or not match:
        return False
    ok = abs(float(match.group(1)) + 23.0) <= 0.3
    _EBUR128_MULTICHANNEL_OK[ffmpeg] = ok
    return ok


def _multichannel_meter(ffmpeg: str, plan: "_ScanPlan", process_owner=None,
                        cancel_event: Optional[threading.Event] = None) -> Optional[str]:
    """The loudness meter for tracks of three or more channels (None: cancelled)."""
    needs = plan.loudness and any(plan.channels(t) > 2 and i not in plan.ambisonic
                                  for i, t in enumerate(plan.audio))
    if not needs:
        return audio_checks.METER_EBUR128
    ok = _ebur128_multichannel_ok(ffmpeg, process_owner, cancel_event)
    if ok is None:
        return None
    return audio_checks.METER_EBUR128 if ok else audio_checks.METER_LOUDNORM


# FFmpeg >= 7 reads any option's value from a file with "-/option file";
# older builds only know -filter_complex_script (removed in 8). Every bundled
# build is 9.0.2.
GRAPH_FILE_OPTION = "-/filter_complex"
LEGACY_GRAPH_FILE_OPTION = "-filter_complex_script"
_FFMPEG_MAJOR_RE = re.compile(r"ffmpeg version n?(\d+)\.")


def _graph_file_option(version_banner: Optional[str]) -> str:
    """How to pass a filter graph from a file to this FFmpeg. Unparseable
    banners (git builds, "N-12345-g...") are current."""
    match = _FFMPEG_MAJOR_RE.search(version_banner or "")
    if match and int(match.group(1)) < 7:
        return LEGACY_GRAPH_FILE_OPTION
    return GRAPH_FILE_OPTION


def _graph_arguments(graphs: List[str], plan: "_ScanPlan") -> List[str]:
    """Each filter graph as a file in the scan's private directory.

    Inline, a 16-track master's graphs exceed Windows' 32,767-character
    command line (CreateProcess fails and nothing is decoded); as files the
    command stays a few hundred characters whatever the track count.
    Without a directory (unit tests building a command) they stay inline.
    """
    if not plan.graph_dir:
        return [arg for graph in graphs for arg in ("-filter_complex", graph)]
    args = []
    for number, graph in enumerate(graphs):
        path = os.path.join(plan.graph_dir, f"graph{number}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(graph)
        args += [plan.graph_option, path]
    return args


def _build_scan_command(ffmpeg: str, filepath: str, plan: _ScanPlan, cfg: dict) -> List[str]:
    """One software-decode pass over the whole file with every detector attached.

    No ``-hwaccel``: hardware decoders conceal damage differently (the same
    corrupt clip gave 16 errors and a phantom freeze on VideoToolbox, 175
    errors and no freeze in software), so the verdict depended on the machine
    and on which file happened to be scanned first.

    The picture detectors come from ``plan.video_analysis`` (video_checks.py);
    it may add a second output (the flash grid) after the null output. The
    audio event detectors run in graphs of their own: FFmpeg runs each
    filter graph in its own thread, so a 16-track master is analysed in
    parallel (audio_checks.TrackGraphs). The graphs are passed as files
    (_graph_arguments).
    """
    graphs: List[str] = []
    maps: List[str] = []
    extra_outputs: List[str] = []
    if plan.video is not None:
        # --- video branch (video_checks.py) ---
        video_analysis = plan.video_analysis
        if video_analysis is not None:
            graphs.append(";".join(video_analysis.filter_chains("v")))
            for graph, label in video_analysis.extra_graphs():
                graphs.append(graph)
                if label:
                    maps += ["-map", label]
            extra_outputs = video_analysis.extra_outputs()
        else:
            graphs.append(f"[0:{int(plan.video['index'])}]"
                          f"{video_checks.black_filter(plan.video_checks)}[v]")
        maps += ["-map", "[v]"]
        # --- end video branch ---
    # --- audio branch (audio_checks.py) ---
    # Summary-printing filters join the first graph (as in 0.9.0); the
    # file-writing detectors get graphs of their own (see TrackGraphs).
    summary: List[str] = []
    units: List["audio_checks.MeasurementUnit"] = []
    for i, track in enumerate(plan.audio):
        names = _channel_names(track, plan.channels(track))
        parts = audio_checks.track_graphs(
            i, track, names, cfg, plan.loudness, MAX_CHANNEL_SPLIT, plan.multichannel_meter,
            ambisonic=i in plan.ambisonic, files=plan.audio_files)
        summary += parts.summary
        units += parts.units
        maps += ["-map", f"[a{i}]"]
    if summary:
        if graphs:
            graphs[0] = ";".join([graphs[0]] + summary)
        else:
            graphs.append(";".join(summary))
    for graph, stub in audio_checks.measurement_graphs(units):
        graphs.append(graph)
        maps += ["-map", stub]
    # --- end audio branch ---
    threads = max(1, int(cfg.get("filter_threads", 4) or 4))
    return ([ffmpeg, "-nostdin", "-hide_banner", "-nostats",
             "-loglevel", "level+verbose", "-progress", "pipe:1",
             # Coded orientation: rotating a 4K picture costs more than the
             # analysis, and interlace detection needs the coded lines.
             "-noautorotate",
             "-i", filepath, "-filter_complex_threads", str(threads)]
            + _graph_arguments(graphs, plan)
            + maps + ["-f", "null", "-"] + extra_outputs)


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


def _black_events(log_text: str, video_end: float) -> List[Tuple[float, float]]:
    """(start, end) blackdetect runs, an open run closed at the decoded end."""
    text = _close_open_regions(_lines_with_context(log_text, "blackdetect@v"), video_end)
    return [(start, start + duration) for start, duration in _pair_detect_events(text, "black")]


def _open_video_analysis(plan: _ScanPlan, cfg: dict,
                         cancel_event: Optional[threading.Event] = None):
    """(analysis, error) for the picture checks; attaches it to ``plan``.

    The analysis owns a private temporary directory; it is removed by
    close() or, on any early exit, when the object is released. Without
    temporary space the decode (integrity, black, audio) still runs and the
    per-frame picture checks are reported not verified.
    """
    if plan.video is None or "index" not in plan.video:
        return None, None
    analysis = video_checks.VideoAnalysis(plan.video, cfg, cancel_event)
    try:
        analysis.open()
    except OSError as exc:
        return None, f"no temporary directory for the measurements ({exc.strerror or exc})."
    plan.video_analysis = analysis
    return analysis, None


def _report_video(result: QAResult, plan: _ScanPlan, outcome: _ScanOutcome,
                  analysis: Optional["video_checks.VideoAnalysis"],
                  analysis_error: Optional[str] = None) -> None:
    """Every picture-check row from the one decode (video_checks.py)."""
    video_end = (_decoded_video_seconds(plan, outcome.accounting)
                 or _stream_duration(plan.video) or plan.duration or 0.0)
    stats = outcome.accounting.stream_stats.get(int(plan.video["index"]))
    video_checks.report_video(
        result, plan.video_checks, plan.video, plan.video_fps(),
        decoded_frames=stats["frames"] if stats else 0, video_end=video_end,
        black_events=(None if getattr(outcome.log_text, "evidence_dropped", 0)
                      else _black_events(outcome.log_text, video_end)),
        analysis=analysis, analysis_error=analysis_error)


def _silence_events_text(text: str, end_time: float,
                         join_gap: float = 0.25) -> List[Tuple[float, float]]:
    """Merged (start, duration) silencedetect regions from a log or from a
    measurement file (``lavfi.silence_start=...``); a region still open at
    the end closes at ``end_time``. Only for complete text: a file FFmpeg
    wrote, never a log the capture truncated."""
    text = _close_open_regions(text, end_time)
    events = _merge_event_intervals(_pair_detect_events(text, "silence"), join_gap)
    return [(start, dur if dur > 0 else max(0.0, end_time - start)) for start, dur in events]


def _silence_events(log_text: str, instance: str, end_time: float,
                    join_gap: float = 0.25) -> List[Tuple[float, float]]:
    return _silence_events_text(_lines_with_context(log_text, instance), end_time, join_gap)


def _primary_track_position(tracks: List[dict]) -> int:
    """The track a player picks: the first flagged default, else the first."""
    for position, track in enumerate(tracks):
        if (track.get("disposition") or {}).get("default"):
            return position
    return 0


def _track_silence_entry(label: str, events, duration: float, cfg: dict,
                         primary: bool = True, number: int = 1) -> Tuple[str, str]:
    """Status/detail for one track's silence, using the long-standing tiers.

    A primary track silent for at least ``dead_channel_ratio`` of its length
    carries no programme and fails whatever its length (the old >30 s
    condition let a completely silent 20 s clip through as a warning). A
    blank secondary track only warns: broadcast and archive layouts reserve
    empty tracks on purpose.
    """
    if not events:
        return "PASS", _with_track(label, "No extended silence detected.")
    total = sum(d for _, d in events)
    positions = _format_event_list(events)
    count = len(events)
    if duration > 0 and total >= duration * float(cfg.get("dead_channel_ratio", 0.95)):
        share = f"silent for {total:.1f}s of {duration:.1f}s ({total / duration * 100:.0f}%)"
        if not primary:
            return "WARN", f"Track {number} is silent - intentional blank track? ({share})"
        return "FAIL", _with_track(label, f"{share[0].upper()}{share[1:]}: the main track carries no audio.")
    if total > duration * 0.5 and duration > 30:
        return "FAIL", _with_track(label, f"{count} silence region(s), total {total:.1f}s. Extended silence in deliverable. At: {positions}")
    if total < 1.0:
        return "PASS", _with_track(label, f"{count} brief silence(s), total {total:.1f}s (natural pauses)")
    return "WARN", _with_track(label, f"{count} silence region(s), total {total:.1f}s. At: {positions}")


def _dead_channels(track: dict, position: int, evidence: "audio_checks.ScanEvidence",
                   duration: float, cfg: dict) -> List[int]:
    """Indices of channels silent (at ``channel_silence_noise_db``) for at
    least ``dead_channel_ratio`` of the track."""
    count = _ScanPlan.channels(track)
    if count <= 1 or count > MAX_CHANNEL_SPLIT or duration <= 0:
        return []
    ratio = float(cfg.get("dead_channel_ratio", 0.95))
    return [k for k in range(count)
            if sum(d for _, d in evidence.silence_events(f"silencedetect@a{position}c{k}", duration))
            >= duration * ratio]


def _dead_channel_entry(label: str, track: dict, dead_channels: List[int],
                        track_dead: bool) -> Tuple[str, str]:
    """Per-channel silence: one dead channel in a live track.

    silencedetect on a whole track only fires when every channel is silent,
    so a stereo file with a dead right channel passed. Channels are judged
    at Qualify's -70 dBFS per-channel default (``channel_silence_noise_db``).
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
    dead = [names[k] for k in dead_channels]
    if not dead:
        return "PASS", _with_track(label, f"All {count} channels carry signal.")
    live_dead = [n for n in dead if n != "LFE"]
    if not live_dead:
        return "PASS", _with_track(label, "LFE channel is silent (common when the mix has no low-frequency content); all other channels carry signal.")
    text = f"Silent channel(s): {', '.join(dead)} of {count} ({track.get('channel_layout') or 'unknown layout'})."
    if count == 2:
        return "FAIL", _with_track(label, text)
    return "WARN", _with_track(label, text + " Verify the mix is intentionally sparse.")


def _add_audio_row(result: QAResult, name: str, row) -> None:
    status, detail, confidence = row
    {"FAIL": result.add_fail, "WARN": result.add_warn,
     "PASS": result.add_pass, "SKIP": result.add_skip}[status](name, detail, confidence)


def _audio_evidence(log_text: str, files: Optional["audio_checks.MeasurementFiles"]
                    ) -> "audio_checks.ScanEvidence":
    """Silence events come from the measurement files, which FFmpeg writes in
    full; the log only supplies the end-of-scan summaries, and is marked
    incomplete when the bounded capture dropped evidence records."""
    def silence_events(instance: str, end: float, join_gap: float = 0.25):
        text = files.read(instance) if files is not None else None
        if text is None:
            raise audio_checks.EvidenceMissing(instance)
        return _silence_events_text(text, end, join_gap)

    dropped = int(getattr(log_text, "evidence_dropped", 0) or 0)
    return audio_checks.ScanEvidence(log_text=log_text, silence_events=silence_events,
                                     files=files, log_complete=dropped == 0,
                                     dropped_records=dropped)


def _record_loudness_metadata(result: QAResult, entry: Optional[dict], cfg: dict) -> None:
    if not entry:
        return
    result.set_metadata("integrated_loudness_lufs", entry.get("integrated"))
    result.set_metadata("true_peak_dbtp", entry.get("true_peak"))
    for source, key in (("lra", "loudness_range_lu"), ("sample_peak", "sample_peak_dbfs"),
                        ("meter", "loudness_meter")):
        if entry.get(source) is not None:
            result.set_metadata(key, entry[source])
    result.set_metadata("loudness_profile", audio_checks.resolve_profile(cfg).key)


def _with_channel_silence(status: str, detail: str, label: str,
                          alone: List[str]) -> Tuple[str, str]:
    """Add per-channel silent stretches (a WARN) to a track's silence entry."""
    if not alone:
        return status, detail
    extra = "; ".join(alone)
    if status == "PASS":
        return "WARN", _with_track(label, extra)
    return status, f"{detail}; {extra}"


def _audio_files_complete(files: Optional["audio_checks.MeasurementFiles"]) -> Optional[str]:
    """Why the measurement files cannot be trusted, or None."""
    if files is None:
        return "no temporary directory for the audio measurement files."
    missing = [name for name, path in files.paths.items() if not os.path.exists(path)]
    if missing:
        return f"FFmpeg did not write {len(missing)} audio measurement file(s) ({missing[0]})."
    return None


def _report_audio(result: QAResult, plan: _ScanPlan, outcome: _ScanOutcome, cfg: dict) -> None:
    """Audio rows of the integrity pass.

    Whole-track silence and dead channels keep their 0.9.0 rules; loudness,
    true peak and the per-channel signal checks come from audio_checks.
    Silence-type events are read from the measurement files; when those are
    missing, or the log lost evidence records, the affected rows are "Not
    verified" instead of judged from partial data.
    """
    files_problem = _audio_files_complete(plan.audio_files)
    evidence = _audio_evidence(outcome.log_text, None if files_problem else plan.audio_files)
    silence, dead, loudness, true_peak, contexts = [], [], [], [], []
    measured = audio_checks.parse_meters(outcome.log_text) if plan.loudness else {}
    total = len(plan.audio)
    primary = _primary_track_position(plan.audio)
    channel_warning = False
    for position, track in enumerate(plan.audio):
        label = _track_label(position, track, total)
        duration = (_decoded_audio_seconds(track, outcome.accounting)
                    or plan.audio_expected_seconds(track) or 0.0)
        ambisonic = position in plan.ambisonic
        ctx = audio_checks.TrackContext(
            position=position, track=track, names=_channel_names(track, plan.channels(track)),
            label=label, duration=duration, ambisonic=ambisonic,
            meter=audio_checks.loudness_meter(track, plan.channels(track),
                                              plan.multichannel_meter, ambisonic))
        contexts.append(ctx)
        if not files_problem:
            events = evidence.silence_events(f"silencedetect@a{position}", duration)
            status, detail = _track_silence_entry(label, events, duration, cfg,
                                                  primary=position == primary, number=position + 1)
            if position == 0 and events:
                result.set_metadata("silence_regions", len(events))
                result.set_metadata("silence_total_seconds", round(sum(d for _, d in events), 2))
                result.set_metadata("silence_timestamps", _format_event_list(events))
            ctx.track_silent = (duration > 0 and sum(d for _, d in events)
                                >= duration * float(cfg.get("dead_channel_ratio", 0.95)))
            ctx.dead_channels = _dead_channels(track, position, evidence, duration, cfg)
            alone = audio_checks.channel_silence_entries(evidence, ctx, cfg, events)
            channel_warning = channel_warning or bool(alone)
            silence.append(_with_channel_silence(status, detail, label, alone))
            dead.append(_dead_channel_entry(label, track, ctx.dead_channels, ctx.track_silent))
        if plan.loudness:
            entry = measured.get(position)
            loudness.append(audio_checks.loudness_row(entry, ctx, cfg))
            true_peak.append(audio_checks.true_peak_row(
                entry, ctx, cfg, audio_checks.track_sample_peak(evidence, ctx)))
            if position == 0 and evidence.log_complete:
                _record_loudness_metadata(result, entry, cfg)
    if files_problem:
        _mark_not_verified(result, ["Audio Silence", "Dead Channel"], files_problem)
    else:
        if any(s == "FAIL" for s, _ in silence):
            confidence = 90
        else:
            confidence = 80 if channel_warning else 100
        _add_aggregate(result, "Audio Silence", silence, confidence)
        _add_aggregate(result, "Dead Channel", dead)
    if not plan.loudness:
        result.add_skip("Audio Loudness", "Loudness not measured (disabled by this spec).")
        result.add_skip(audio_checks.TRUE_PEAK, "True peak not measured (loudness is off in this spec).")
    elif not evidence.log_complete:
        overflow = audio_checks.log_overflow_text(evidence)[len(NOT_VERIFIED_PREFIX):]
        _mark_not_verified(result, ["Audio Loudness", audio_checks.TRUE_PEAK], overflow)
    else:
        _add_audio_row(result, "Audio Loudness", audio_checks.aggregate(loudness))
        _add_audio_row(result, audio_checks.TRUE_PEAK, audio_checks.aggregate(true_peak))
    rows = audio_checks.signal_rows(evidence, contexts, cfg)
    rows.update(audio_checks.disabled_rows(cfg))
    for name in (audio_checks.CLIPPING, audio_checks.DROPOUTS, audio_checks.PHASE,
                 audio_checks.DUAL_MONO, audio_checks.DC_OFFSET, audio_checks.HUM):
        if name in rows:
            _add_audio_row(result, name, rows[name])


def _report_scan(result: QAResult, plan: _ScanPlan, outcome: _ScanOutcome, cfg: dict,
                 analysis: Optional["video_checks.VideoAnalysis"] = None,
                 analysis_error: Optional[str] = None) -> None:
    reason = _not_run_reason(outcome, plan)
    if reason:
        _mark_not_verified(result, plan.frame_check_names(), reason)
        return
    _report_file_integrity(result, plan, outcome, cfg)
    if plan.video is not None:
        _report_video(result, plan, outcome, analysis, analysis_error)
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
        for name in plan.frame_check_names():
            result.add_not_verified(name, "quick scan — the file was not decoded.", by_choice=True)
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

    analysis, analysis_error = _open_video_analysis(plan, cfg, cancel_event)
    scan_dir = _prepare_scan_files(plan, result)
    try:
        meter = _multichannel_meter(ffmpeg_path, plan, process_owner, cancel_event)
        if meter is None:
            return
        plan.multichannel_meter = meter
        cmd = _build_scan_command(ffmpeg_path, filepath, plan, cfg)
        if progress_callback:
            progress_callback("filter", 0, plan.duration or 0.0, "Integrity scan (software decode)")
        outcome = _run_scan_process(cmd, _calc_timeout(plan.duration, cfg), plan.duration, cfg,
                                    progress_callback, cancel_event, process_owner)
        if outcome is None:
            return
        if getattr(outcome.log_text, "truncated_records", 0):
            result.set_metadata("deep_scan_stderr_truncated_lines", outcome.log_text.truncated_records)
        if getattr(outcome.log_text, "evidence_dropped", 0):
            result.set_metadata("deep_scan_evidence_dropped", outcome.log_text.evidence_dropped)
        if progress_callback:
            end = plan.duration or outcome.last_out_time or 0.0
            progress_callback("filter", end, end, "Integrity scan (software decode)")
        _report_scan(result, plan, outcome, cfg, analysis, analysis_error)
    finally:
        if analysis is not None:
            analysis.close()
        if scan_dir:
            shutil.rmtree(scan_dir, ignore_errors=True)


def _prepare_scan_files(plan: _ScanPlan, result: QAResult) -> Optional[str]:
    """The scan's private directory for filter-graph scripts and the audio
    measurement files, and the plan settings that use it; None (graphs
    inline, file-based audio rows not verified) when none can be created."""
    plan.graph_option = _graph_file_option(
        (result.tool_versions.get("ffmpeg") or {}).get("version") if result.tool_versions else None)
    tracks = result.metadata.get("ambisonic_audio_tracks")
    if tracks is not None:
        plan.ambisonic = frozenset(int(n) - 1 for n in tracks)
    try:
        scan_dir = tempfile.mkdtemp(prefix="frameguard-scan-")
    except OSError:
        return None
    plan.graph_dir = scan_dir
    plan.audio_files = audio_checks.MeasurementFiles(scan_dir)
    return scan_dir



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
    # Box-level view of MP4/MOV files for the container/XR/conformance checks
    # (container_checks.py); bounded and never raises.
    structure = container_checks.read_structure(filepath)
    check_xr_metadata(probe_data, result, cfg, first_frame, structure,
                      container_checks.FrameGrabber(filepath, process_owner, cancel_event))
    check_bitrate(probe_data, result, cfg)
    check_audio(probe_data, result, cfg)
    check_av_duration_mismatch(probe_data, result, cfg, container_checks.measured_stream_ends(
        filepath, probe_data, process_owner, cancel_event))
    check_frame_count(probe_data, result, cfg)
    projection = result.metadata.get("projection")
    container_checks.run_structure_checks(filepath, structure, probe_data, result, cfg,
                                          None if projection in (None, "none") else projection)
    # --- audio: tracks analysed as ambisonics (audio_checks.ambisonic_positions) ---
    tracks = _audio_streams(probe_data)
    if tracks:
        positions = audio_checks.ambisonic_positions(
            tracks, [container_checks._sa3d_for(structure, t) for t in tracks],
            spherical=projection not in (None, "none"))
        result.set_metadata("ambisonic_audio_tracks", [p + 1 for p in positions])


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
