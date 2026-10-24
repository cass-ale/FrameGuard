"""
FrameGuard Desktop -- Preset Management System

Presets use the .fg file extension (FrameGuard Preset format).
Internally they're JSON, but .fg makes them:
- Instantly recognizable as FrameGuard files
- Drag-and-droppable onto the app
- Associatable with FrameGuard in Windows (double-click to import)

Handles:
- Export scan profiles as .fg files
- Import .fg files (and legacy .json)
- Persist user-imported presets in ~/.frameguard/presets/
- List all available presets (bundled + user-imported)
"""

import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional, Dict, List

from frameguard import audio_checks

# Preset file format
PRESET_FORMAT_VERSION = 2
FG_EXTENSION = ".fg"
LEGACY_EXTENSION = ".json"
MAX_PRESET_BYTES = 1_000_000
MAX_PRESET_NESTING = 64

# The scan parameters a preset is allowed to carry, keyed exactly as
# ``core.DEFAULT_CONFIG`` keys them. Anything not listed here (themes, window
# geometry, watch-folder paths) is deliberately excluded so that sharing a
# preset with a teammate never overwrites their local preferences.
PRESET_CONFIG_KEYS = (
    "target_width",
    "target_height",
    "allow_larger_resolution",
    "target_fps",
    "fps_tolerance",
    "allow_fps_multiples",
    "min_bitrate_mbps",
    "accepted_codecs",
    "accepted_containers",
    "accepted_audio_codecs",
    "audio_required",
    "min_audio_sample_rate",
    "require_cfr",
    "require_progressive",
    "min_bit_depth",
    "required_transfer",
    "required_projection",
    "required_stereo_mode",
    "max_decode_errors",
    "silence_duration",
    "silence_noise_db",
    "loudness_mode",
    "target_loudness_lufs",
    "loudness_tolerance_lu",
    "true_peak_max_dbtp",
    # Container / XR / conformance (core.DEFAULT_CONFIG documents each).
    "allow_ntsc_rate_match",
    "duration_mismatch_threshold",
    "av_short_tolerance_frames",
    "av_long_tolerance_s",
    "av_offset_warn_ms",
    "av_offset_max_early_ms",
    "av_offset_max_late_ms",
    "allow_display_rotation",
    "spatial_audio",
    "require_square_pixels",
    "warn_untagged_color",
    "allowed_video_profiles",
    "allowed_pix_fmts",
    "max_frame_pixels",
    "max_pixel_rate",
    "require_faststart",
    "allow_trim_edit_lists",
    "max_audio_tracks",
    "disabled_checks",
    # Picture checks (video_checks.py, 0.9.1).
    "black_frames_enabled",
    "black_detect_duration",
    "black_detect_pix_threshold",
    "black_detect_picture_threshold",
    "black_short_min_frames",
    "freeze_frames_enabled",
    "freeze_detect_duration",
    "freeze_hold_allowance",
    "signal_levels_mode",
    "flash_check_enabled",
    "interlace_check_enabled",
    # Compatibility-only (see core.COMPAT_ONLY_CONFIG_KEYS): still accepted so
    # presets and saved settings written by 0.8.x round-trip unchanged.
    "use_hwaccel",
    "segment_enabled",
    "segment_count",
)
# Audio signal QC keys (loudness profile, per-check switches, thresholds).
PRESET_CONFIG_KEYS = PRESET_CONFIG_KEYS + audio_checks.PRESET_KEYS

# v1 presets (and anything exported by FrameGuard <= 0.5.1) used a different
# spelling for a few keys. Map them onto the engine names on import.
_LEGACY_KEY_ALIASES = {
    "delivery_width": "target_width",
    "delivery_height": "target_height",
    "delivery_fps_target": "target_fps",
    "delivery_fps_tolerance": "fps_tolerance",
    "delivery_min_bitrate_mbps": "min_bitrate_mbps",
    "audio_silence_duration_s": "silence_duration",
    "deep_hardware_accel": "use_hwaccel",
    "deep_segmented_scan": "segment_enabled",
    "deep_segment_count": "segment_count",
    "accepted_video_codecs": "accepted_codecs",
}

# Keys that must never travel inside a preset even if a hand-edited file has
# them — they are machine- or user-specific, not delivery-spec.
_EXCLUDED_KEYS = frozenset({
    "theme", "delivery_resolution_preset", "deep_strict_mode",
    "watch_folder", "video_extensions", "max_workers", "filter_threads",
})


class PresetValidationError(ValueError):
    """A preset's config block is invalid; ``problems`` lists every bad key."""

    def __init__(self, problems: List[str], unknown: List[str]):
        self.problems = list(problems)
        self.unknown = list(unknown)
        parts = []
        if self.problems:
            parts.append("invalid values: " + "; ".join(self.problems))
        if self.unknown:
            parts.append("unknown keys: " + ", ".join(sorted(self.unknown)))
        super().__init__("Preset rejected — " + " | ".join(parts))


def _ignored_key(key: str) -> bool:
    """Keys that are known but deliberately not applied (never an error)."""
    from frameguard.core import RETIRED_CONFIG_KEYS
    return key in _EXCLUDED_KEYS or key in RETIRED_CONFIG_KEYS


def validate_preset_config(config: dict) -> dict:
    """Strictly normalise a preset ``config`` block or raise PresetValidationError.

    Out-of-range values used to be clamped (``target_fps: 0`` became 1 fps,
    ``fps_tolerance: 60`` disabled the frame-rate check) and misspelt keys were
    dropped silently, so an imported spec could be looser than its author
    wrote without anyone being told. Every problem is now collected and the
    preset is rejected with the full list. Retired and machine-local keys are
    ignored, not rejected, so older presets still load.
    """
    if not isinstance(config, dict):
        raise PresetValidationError(["config block is not an object"], [])
    out, problems, unknown = {}, [], []
    for raw_key, value in config.items():
        key = _LEGACY_KEY_ALIASES.get(raw_key, raw_key)
        if key not in PRESET_CONFIG_KEYS:
            if not _ignored_key(key):
                unknown.append(str(raw_key))
            continue
        coerced, problem = _coerce_preset_value(key, value)
        if problem:
            problems.append(f"{raw_key}={value!r} ({problem})")
        else:
            out[key] = coerced
    if problems or unknown:
        raise PresetValidationError(problems, unknown)
    return audio_checks.normalize_custom_profile(out)


def normalize_preset_config(config: dict) -> dict:
    """Leniently translate a config block into engine (``DEFAULT_CONFIG``) keys.

    Used for the app's own saved settings, where one stale or retired value
    must not throw away the whole configuration: invalid values and unknown
    keys are dropped (never clamped, so nothing is silently loosened), and
    the engine default applies for those keys. Imported preset files go
    through validate_preset_config instead, which rejects them.
    """
    if not isinstance(config, dict):
        return {}
    out = {}
    for key, value in config.items():
        key = _LEGACY_KEY_ALIASES.get(key, key)
        if key not in PRESET_CONFIG_KEYS:
            continue
        coerced, problem = _coerce_preset_value(key, value)
        if not problem:
            out[key] = coerced
    return audio_checks.normalize_custom_profile(out)


# Values are interpolated into FFmpeg filter graphs (e.g. silence_noise_db
# becomes "silencedetect=n=<value>dB"), so a string that slipped through could
# append arbitrary filters - including ones that write files. Every value is
# therefore coerced to the type of its engine default, or rejected.
_PRESET_VALUE_RANGES = {
    "target_width": (16, 16384),
    "target_height": (16, 16384),
    "target_fps": (1.0, 1000.0),
    # The Settings slider tops out at 2.0; anything wider stops the frame-rate
    # check meaning anything (24 vs 25 fps would pass).
    "fps_tolerance": (0.0, 2.0),
    "min_bitrate_mbps": (0.0, 100000.0),
    "min_audio_sample_rate": (1000, 768000),
    "max_decode_errors": (0, 1_000_000),
    "silence_duration": (0.1, 3600.0),
    "silence_noise_db": (-120.0, 0.0),
    "target_loudness_lufs": (-70.0, 0.0),
    "loudness_tolerance_lu": (0.0, 20.0),
    "true_peak_max_dbtp": (-20.0, 3.0),
    # Picture checks. Black level is a fraction of the nominal range (0.02 =
    # Y <= 20 at 8-bit limited); the picture share is a fraction of pixels.
    "black_detect_duration": (0.04, 60.0),
    "black_detect_pix_threshold": (0.0, 0.25),
    "black_detect_picture_threshold": (0.5, 1.0),
    "black_short_min_frames": (0, 100),
    "freeze_detect_duration": (0.2, 600.0),
    "freeze_hold_allowance": (0.0, 600.0),
    "segment_count": (1, 16),
    "duration_mismatch_threshold": (0.0, 60.0),
    "av_short_tolerance_frames": (0, 1000),
    "av_long_tolerance_s": (0.0, 60.0),
    "av_offset_warn_ms": (0.0, 1000.0),
    "av_offset_max_early_ms": (0.0, 10000.0),
    "av_offset_max_late_ms": (0.0, 10000.0),
    # 0 = no ceiling; 16384 x 16384 x 1000 fps bounds the rest.
    "max_frame_pixels": (0, 16384 * 16384),
    "max_pixel_rate": (0, 16384 * 16384 * 1000),
    "max_audio_tracks": (0, 64),
}
_PRESET_VALUE_RANGES.update(audio_checks.VALUE_RANGES)
_LOWERCASE_LIST_KEYS = ("accepted_codecs", "accepted_audio_codecs", "accepted_containers",
                        "allowed_video_profiles", "allowed_pix_fmts")
# Lists that may legitimately be empty (an empty acceptance list would fail
# every file, but an empty disabled-check list just runs every check).
_EMPTY_OK_LIST_KEYS = frozenset({"disabled_checks"})
# Only these are genuinely whole numbers. Everything else numeric accepts a
# fraction even when its default happens to be written as an int.
_INTEGER_PRESET_KEYS = frozenset({
    "target_width", "target_height", "min_audio_sample_rate", "segment_count",
    "max_decode_errors", "av_short_tolerance_frames", "max_frame_pixels",
    "max_pixel_rate", "max_audio_tracks", "black_short_min_frames",
}) | audio_checks.INTEGER_KEYS


def _enum_choices(key: str):
    from frameguard import core
    return {
        "min_bit_depth": core.BIT_DEPTH_CHOICES,
        "required_transfer": core.TRANSFER_CHOICES,
        "required_projection": core.PROJECTION_CHOICES,
        "required_stereo_mode": core.STEREO_CHOICES,
        "loudness_mode": core.LOUDNESS_MODES,
        "spatial_audio": core.SPATIAL_AUDIO_CHOICES,
        **audio_checks.ENUM_CHOICES,
        "signal_levels_mode": core.SIGNAL_LEVELS_MODES,
    }.get(key)


def _coerce_enum(key: str, value, choices):
    if isinstance(value, bool):
        return None, f"must be one of {', '.join(map(str, choices))}"
    if isinstance(value, str):
        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        for choice in choices:
            if str(choice) == text:
                return choice, None
    elif isinstance(value, (int, float)) and value in choices:
        return int(value), None
    return None, f"must be one of {', '.join(map(str, choices))}"


def _coerce_preset_value(key: str, value):
    """``(value converted to the engine type for key, None)`` or ``(None, problem)``."""
    from frameguard.core import DEFAULT_CONFIG

    choices = _enum_choices(key)
    if choices is not None:
        return _coerce_enum(key, value, choices)
    default = DEFAULT_CONFIG.get(key)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value, None
        if isinstance(value, int) and value in (0, 1):
            return bool(value), None
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true", None
        return None, "must be true or false"
    if isinstance(default, (int, float)):
        return _coerce_number(key, value, default)
    if isinstance(default, list):
        return _coerce_list(key, value)
    if isinstance(default, str):
        return (value, None) if isinstance(value, str) else (None, "must be text")
    return None, "unsupported type"


def _coerce_number(key: str, value, default):
    if isinstance(value, bool):
        return None, "must be a number"
    try:
        number = float(value.strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError, AttributeError):
        return None, "must be a number"
    if not math.isfinite(number):
        return None, "must be a finite number"
    low, high = _PRESET_VALUE_RANGES.get(key, (-math.inf, math.inf))
    if not low <= number <= high:
        return None, f"out of range {low:g}..{high:g}"
    if key in _INTEGER_PRESET_KEYS:
        if number != int(number):
            return None, "must be a whole number"
        return int(number), None
    if isinstance(default, int) and number == int(number):
        return int(number), None
    return number, None


def _coerce_list(key: str, value):
    if not isinstance(value, list):
        return None, "must be a list"
    items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(items) != len(value):
        return None, "every entry must be non-empty text"
    if key == "disabled_checks":
        return _coerce_check_names(items)
    if not items and key not in _EMPTY_OK_LIST_KEYS:
        # An empty acceptance list fails every file.
        return None, "must not be empty"
    if key in _LOWERCASE_LIST_KEYS:
        # Probed codec names and os.path.splitext() output are lowercase.
        items = [item.lower() for item in items]
    if key == "accepted_containers":
        # Compared against splitext() output, which keeps the leading dot.
        items = [item if item.startswith(".") else f".{item}" for item in items]
    return items, None


def _coerce_check_names(items: List[str]):
    """Only the optional container/XR checks may be switched off, by exact name."""
    from frameguard.container_checks import SWITCHABLE_CHECKS
    by_lower = {name.lower(): name for name in SWITCHABLE_CHECKS}
    unknown = [item for item in items if item.lower() not in by_lower]
    if unknown:
        return None, f"unknown check(s) {', '.join(unknown)}; allowed: {', '.join(SWITCHABLE_CHECKS)}"
    return list(dict.fromkeys(by_lower[item.lower()] for item in items)), None


def get_bundled_presets_dir() -> Path:
    """Get the bundled presets directory (read-only, shipped with app)."""
    if getattr(sys, "frozen", False):
        roots = [
            Path(getattr(sys, "_MEIPASS", "")),
            Path(sys.executable).parent / "_internal",
            Path(sys.executable).parent,
        ]
    else:
        roots = [Path(__file__).resolve().parent.parent]
    for root in roots:
        if not str(root):
            continue
        bundled = root / "presets"
        if bundled.is_dir():
            return bundled
    return Path("")  # Empty path if not found


def get_user_presets_dir() -> Path:
    """Get the user presets directory (writable, persists imported presets).
    
    Location: ~/.frameguard/presets/
    This directory persists across app updates/reinstalls.
    """
    user_dir = Path.home() / ".frameguard" / "presets"
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def get_presets_dir() -> Path:
    """Get the primary presets directory (user-writable).
    Alias for get_user_presets_dir for backward compatibility.
    """
    return get_user_presets_dir()


def _sanitize_filename(name: str) -> str:
    """Convert a normalized preset name to a safe, portable filename."""
    normalized = unicodedata.normalize("NFKC", name)
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", normalized.lower()).strip("_")


def _filename_key(path: Path) -> str:
    """Model case/Unicode-insensitive filesystems on every platform."""
    return unicodedata.normalize("NFKC", path.name).casefold()


def _available_preset_path(directory: Path, safe_name: str) -> Path:
    """Choose a deterministic non-colliding destination without overwriting."""
    existing = set()
    try:
        existing = {_filename_key(path) for path in directory.iterdir()}
    except FileNotFoundError:
        pass

    stem = safe_name or "preset"
    suffix = 1
    while True:
        numbered = stem if suffix == 1 else f"{stem}_{suffix}"
        candidate = directory / f"{numbered}{FG_EXTENSION}"
        if _filename_key(candidate) not in existing:
            return candidate
        suffix += 1


def _iter_preset_files(directory: Path) -> List[Path]:
    """Return preset files in deterministic cross-filesystem order."""
    if not directory.is_dir():
        return []
    files = [
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in (FG_EXTENSION, LEGACY_EXTENSION)
    ]
    return sorted(files, key=lambda path: (_filename_key(path), path.name))


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _within_nesting_limit(value, limit: int = MAX_PRESET_NESTING) -> bool:
    """Check JSON depth iteratively so validation cannot overflow Python's stack."""
    pending = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > limit:
            return False
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    return True


def _load_preset_json(filepath: str):
    """Read at most the preset byte ceiling and parse hostile JSON safely."""
    with open(filepath, "rb") as stream:
        raw = stream.read(MAX_PRESET_BYTES + 1)
    if len(raw) > MAX_PRESET_BYTES:
        return None
    text = raw.decode("utf-8")
    data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    return data if _within_nesting_limit(data) else None


def export_preset(name: str, config: dict, output_path: Optional[str] = None) -> str:
    """Export a scan configuration as a .fg preset file.

    Args:
        name: Human-readable preset name (e.g., "4K UHD @ 30fps")
        config: Scan parameters keyed as in ``core.DEFAULT_CONFIG``. Legacy
                ``delivery_*``/``deep_*`` keys are accepted and translated.
        output_path: Where to save. If None, writes into the user presets dir.

    Returns:
        Path to the saved preset file.
    """
    from frameguard.core import DEFAULT_CONFIG

    normalized = normalize_preset_config(config)
    # Fill any gaps from the engine defaults so a preset is always complete and
    # self-describing, rather than silently inheriting whatever the importer
    # happens to have configured locally.
    resolved = {
        key: normalized.get(key, DEFAULT_CONFIG.get(key))
        for key in PRESET_CONFIG_KEYS
        if key in normalized or key in DEFAULT_CONFIG
    }
    # DEFAULT_CONFIG stores extensions as a set in some fields; JSON needs lists.
    for key, value in list(resolved.items()):
        if isinstance(value, (set, tuple)):
            resolved[key] = sorted(value)

    preset_data = {
        "format_version": PRESET_FORMAT_VERSION,
        "type": "frameguard_preset",
        "name": name,
        "description": f"FrameGuard scan preset: {name}",
        "config": resolved,
    }

    if output_path is None:
        user_dir = get_user_presets_dir()
        output_path = str(_available_preset_path(
            user_dir, _sanitize_filename(name) or "preset"))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(preset_data, f, indent=2)

    return output_path


def import_preset(filepath: str, raise_errors: bool = False) -> Optional[Dict]:
    """Import a preset from a .fg or .json file.

    Returns:
        Dict with 'name', 'config', 'description', 'format_version' keys,
        or None if the file is unreadable or its config is invalid. 'config'
        is always normalized to engine key names.

    With ``raise_errors`` an invalid config raises PresetValidationError,
    whose message lists every bad and unknown key (for showing to the user).
    """
    try:
        data = _load_preset_json(filepath)
        if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
            if raise_errors:
                raise PresetValidationError(["file has no 'config' object"], [])
            return None
        config = validate_preset_config(data["config"])
        if not config:
            raise PresetValidationError(["config contains no scan settings"], [])
        return {
            "name": str(data.get("name") or Path(filepath).stem),
            "config": config,
            "description": data.get("description", ""),
            "format_version": data.get("format_version", 0),
            "source_path": filepath,
        }
    except PresetValidationError:
        if raise_errors:
            raise
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError,
            RecursionError, ValueError):
        if raise_errors:
            raise
        return None


def save_imported_preset(filepath: str) -> Optional[Dict]:
    """Import a preset and save a normalized copy to the user presets directory.

    This makes the preset persistent — it will appear in the preset dropdown
    across app restarts.

    Returns:
        The imported preset dict, or None if import failed.
    """
    preset = import_preset(filepath)
    if not preset:
        return None

    try:
        safe_name = _sanitize_filename(preset["name"]) or "preset"
        user_dir = get_user_presets_dir()
        source = Path(filepath).resolve()
        base_dest = user_dir / f"{safe_name}{FG_EXTENSION}"
        dest = (base_dest if source == base_dest.resolve()
                else _available_preset_path(user_dir, safe_name))

        # Always re-export rather than copying: this upgrades legacy key
        # spellings and guarantees the stored copy matches the current schema.
        if source != dest.resolve():
            export_preset(preset["name"], preset["config"], str(dest))
    except (OSError, ValueError):
        return None

    preset["path"] = str(dest)
    return preset


def list_available_presets() -> List[Dict]:
    """List all preset files from BOTH bundled and user directories.
    
    User presets take priority if names conflict with bundled ones.
    
    Returns:
        List of dicts with 'name', 'path', 'config', 'source' for each preset.
    """
    presets = []
    by_name = {}

    def add_file(preset_file: Path, source: str):
        preset = import_preset(str(preset_file))
        if not preset:
            return
        preset["path"] = str(preset_file)
        preset["source"] = source
        name = preset["name"]
        previous = by_name.get(name)
        if previous is not None and source == "imported" \
                and presets[previous]["source"] == "bundled":
            # An intentionally same-named user preset overrides the shipped one.
            presets[previous] = preset
            return
        if previous is not None:
            # Preserve same-source collisions instead of silently hiding one.
            base = name
            number = 2
            while preset["name"] in by_name:
                preset["name"] = f"{base} ({number})"
                number += 1
            name = preset["name"]
        by_name[name] = len(presets)
        presets.append(preset)

    try:
        bundled_files = _iter_preset_files(get_bundled_presets_dir())
    except OSError:
        bundled_files = []
    for preset_file in bundled_files:
        add_file(preset_file, "bundled")

    try:
        user_files = _iter_preset_files(get_user_presets_dir())
    except OSError:
        user_files = []
    for preset_file in user_files:
        add_file(preset_file, "imported")

    return presets


def is_preset_file(filepath: str) -> bool:
    """Check whether a path is a FrameGuard preset.

    ``.fg`` is ours by definition. ``.json`` is only treated as a preset if it
    actually parses as one — otherwise an unrelated JSON sidecar dropped
    alongside footage would be swallowed as a preset instead of being reported.
    """
    ext = Path(filepath).suffix.lower()
    if ext == FG_EXTENSION:
        return True
    if ext == LEGACY_EXTENSION:
        return import_preset(filepath) is not None
    return False


_FPS_TOKENS = {
    24: ("24p", "24fps"),
    30: ("30p", "30fps"),
    60: ("60p", "60fps"),
}
# NTSC rates are not the integer rate (Frame Rate matches rationally), and no
# built-in preset targets them, so these tokens suppress any suggestion.
_NTSC_RATE_TOKENS = frozenset({"2398", "23.976", "23976", "2997", "29.97", "5994", "59.94"})
# 180-degree deliverables have no built-in preset.
_HALF_SPHERE_TOKENS = frozenset({"180", "vr180", "180vr"})
_RESOLUTION_TOKENS = {
    "4k": ("4k", "uhd", "2160p", "3840x2160"),
    "1080p": ("1080p", "fhd", "fullhd", "1920x1080"),
    "720p": ("720p", "1280x720"),
    "5.7k": ("5.7k", "5760x2880"),
    "8k": ("8k", "4320p", "7680x4320"),
    # Frame sizes that are only ever 360 deliverables.
    "8k360": ("7680x3840",),
    "tb5760": ("5760x5760",),
    "tb3840": ("3840x3840",),
}
# Whole tokens that mark a file as 360/VR. They never select a preset on
# their own (a clip named "C0360" or "overview_vr" is not a 360 deliverable);
# they only turn a bare "8k" into the 8K 360 preset.
_SPHERICAL_TOKENS = frozenset({"360", "vr", "vr360", "360vr", "equirect", "equirectangular", "spherical"})
# Stereo words make a 5.7K/8K name ambiguous (mono 2:1 or top-bottom 1:1).
_STEREO_TOKENS = frozenset({"tb", "ou", "overunder", "3d", "stereo", "sbs", "lr"})
# Resolution family + frame rate -> built-in preset. Combinations with no
# matching preset (e.g. 4K 25p) deliberately produce no suggestion.
_PRESET_BY_TOKENS = {
    ("4k", 30): "4k30", ("4k", 60): "4k60", ("4k", 24): "4k24",
    ("1080p", 30): "1080p30", ("1080p", 60): "1080p60", ("1080p", 24): "1080p24",
    ("720p", 30): "720p30", ("5.7k", 30): "5.7k30", ("8k", 30): "8k30",
    ("8k360", 30): "8k360", ("tb5760", 30): "360tb5760", ("tb3840", 30): "360tb3840",
}


def _filename_tokens(filename: str) -> List[str]:
    """Lower-case whole tokens of a file name, split on separators.

    Dots inside a token survive ("5.7k", "29.97"); the extension does not.
    """
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    return [t for t in re.split(r"[\s_\-\[\]\(\)+,]+", stem) if t]


def detect_preset_from_filename(filename: str, presets: dict = None) -> Optional[str]:
    """Suggest a built-in preset from whole tokens in a file name, or None.

    The old substring matcher read "_C360" and "overview_vr_cut" as 5.7K VR
    and any "1080" (a take number) as 1080p. Only unambiguous whole tokens
    count now: a resolution token ("4k", "uhd", "2160p", "1920x1080" ...),
    optionally with a frame-rate token ("24p", "59.94" ...; 30 when absent).
    Bare numbers, "vr" and "360" never match on their own; next to "8k" they
    select the 8K 360 preset (7680x3840) instead of 8K UHD, and a stereo word
    next to "5.7k"/"8k" makes the name ambiguous. This only suggests:
    callers must never replace a spec the user chose.
    """
    tokens = set(_filename_tokens(filename))
    if tokens & (_NTSC_RATE_TOKENS | _HALF_SPHERE_TOKENS):
        return None
    families = [fam for fam, words in _RESOLUTION_TOKENS.items() if tokens & set(words)]
    if families == ["8k"] and tokens & _SPHERICAL_TOKENS:
        families = ["8k360"]
    if len(families) == 1 and families[0] in ("5.7k", "8k", "8k360") and tokens & _STEREO_TOKENS:
        return None
    rates = {fps for fps, words in _FPS_TOKENS.items() if tokens & set(words)}
    known_rate_words = {w for words in _FPS_TOKENS.values() for w in words} | _NTSC_RATE_TOKENS
    for token in tokens - known_rate_words:
        rate = re.fullmatch(r"(\d{2,3}(?:\.\d+)?)(?:p|fps)", token)
        if rate and float(rate.group(1)) <= 240:
            return None  # a frame rate no built-in preset has (25p, 50p, 120fps)
    if len(families) != 1 or len(rates) > 1:
        return None
    rates = sorted(rates)
    key = _PRESET_BY_TOKENS.get((families[0], rates[0] if rates else 30))
    if key and presets is not None and key not in presets:
        return None
    return key


def suggest_preset_for_files(filenames: List[str], presets: dict = None) -> Optional[str]:
    """A suggestion only when every file name points at the same preset."""
    suggestions = {detect_preset_from_filename(name, presets) for name in filenames}
    if len(suggestions) == 1:
        return suggestions.pop()
    return None
