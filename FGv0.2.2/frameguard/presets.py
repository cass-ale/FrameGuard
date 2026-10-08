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
import os
import re
import sys
from pathlib import Path
from typing import Optional, Dict, List

# Preset file format
PRESET_FORMAT_VERSION = 2
FG_EXTENSION = ".fg"
LEGACY_EXTENSION = ".json"

# The scan parameters a preset is allowed to carry, keyed exactly as
# ``core.DEFAULT_CONFIG`` keys them. Anything not listed here (themes, window
# geometry, watch-folder paths) is deliberately excluded so that sharing a
# preset with a teammate never overwrites their local preferences.
PRESET_CONFIG_KEYS = (
    "target_width",
    "target_height",
    "target_fps",
    "fps_tolerance",
    "min_bitrate_mbps",
    "accepted_codecs",
    "accepted_containers",
    "accepted_audio_codecs",
    "audio_required",
    "min_audio_sample_rate",
    "require_cfr",
    "silence_duration",
    "silence_noise_db",
    "use_hwaccel",
    "segment_enabled",
    "segment_count",
)

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


def normalize_preset_config(config: dict) -> dict:
    """Translate a preset's ``config`` block into engine (``DEFAULT_CONFIG``) keys.

    Accepts both the current schema and the legacy ``delivery_*``/``deep_*``
    spellings, and drops anything that isn't a shareable scan parameter.
    """
    if not isinstance(config, dict):
        return {}
    out = {}
    for key, value in config.items():
        key = _LEGACY_KEY_ALIASES.get(key, key)
        if key in _EXCLUDED_KEYS or key not in PRESET_CONFIG_KEYS:
            continue
        out[key] = value
    # Container extensions are compared against os.path.splitext() output, which
    # always includes the leading dot. Older presets stored them without one.
    if isinstance(out.get("accepted_containers"), list):
        out["accepted_containers"] = [
            ext if ext.startswith(".") else f".{ext}"
            for ext in out["accepted_containers"]
            if isinstance(ext, str) and ext
        ]
    return out


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
    """Convert a preset name to a safe filename."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name.lower()).strip("_")


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
        safe_name = _sanitize_filename(name)
        output_path = str(get_user_presets_dir() / f"{safe_name}{FG_EXTENSION}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(preset_data, f, indent=2)

    return output_path


def import_preset(filepath: str) -> Optional[Dict]:
    """Import a preset from a .fg or .json file.

    Returns:
        Dict with 'name', 'config', 'description', 'format_version' keys,
        or None if invalid/unreadable. 'config' is always normalized to
        engine key names.
    """
    try:
        # Reject absurdly large files (> 1MB — presets should be < 10KB)
        if os.path.getsize(filepath) > 1_000_000:
            return None

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
            return None

        config = normalize_preset_config(data["config"])
        if not config:
            return None

        return {
            "name": str(data.get("name") or Path(filepath).stem),
            "config": config,
            "description": data.get("description", ""),
            "format_version": data.get("format_version", 0),
            "source_path": filepath,
        }
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
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

    safe_name = _sanitize_filename(preset["name"]) or "preset"
    dest = get_user_presets_dir() / f"{safe_name}{FG_EXTENSION}"

    # Always re-export rather than copying: this upgrades legacy key spellings
    # in place and guarantees the stored copy matches the current schema.
    if str(Path(filepath).resolve()) != str(dest.resolve()):
        export_preset(preset["name"], preset["config"], str(dest))

    preset["path"] = str(dest)
    return preset


def list_available_presets() -> List[Dict]:
    """List all preset files from BOTH bundled and user directories.
    
    User presets take priority if names conflict with bundled ones.
    
    Returns:
        List of dicts with 'name', 'path', 'config', 'source' for each preset.
    """
    presets = {}  # keyed by name (deduplication)
    
    # Load bundled presets first
    bundled_dir = get_bundled_presets_dir()
    if bundled_dir.exists():
        for preset_file in sorted(bundled_dir.glob(f"*{LEGACY_EXTENSION}")) + \
                           sorted(bundled_dir.glob(f"*{FG_EXTENSION}")):
            preset = import_preset(str(preset_file))
            if preset:
                preset["path"] = str(preset_file)
                preset["source"] = "bundled"
                presets[preset["name"]] = preset
    
    # Load user presets (override bundled if same name)
    user_dir = get_user_presets_dir()
    for preset_file in sorted(user_dir.glob(f"*{FG_EXTENSION}")) + \
                       sorted(user_dir.glob(f"*{LEGACY_EXTENSION}")):
        preset = import_preset(str(preset_file))
        if preset:
            preset["path"] = str(preset_file)
            preset["source"] = "imported"
            presets[preset["name"]] = preset
    
    return list(presets.values())


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


def detect_preset_from_filename(filename: str, presets: dict) -> Optional[str]:
    """Auto-detect which preset to use based on the video filename."""
    name_lower = filename.lower()
    
    if any(x in name_lower for x in ["4k", "uhd", "3840", "2160"]):
        if any(x in name_lower for x in ["60p", "60fps", "_60_"]):
            return "4k60"
        elif any(x in name_lower for x in ["24p", "24fps", "_24_"]):
            return "4k24"
        return "4k30"
    elif any(x in name_lower for x in ["1080", "fhd", "fullhd"]):
        return "1080p30"
    elif any(x in name_lower for x in ["5.7k", "5760", "vr", "360"]):
        return "5.7k30"
    elif any(x in name_lower for x in ["8k", "7680"]):
        return "8k30"
    return None
