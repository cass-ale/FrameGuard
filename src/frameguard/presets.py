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
import unicodedata
from pathlib import Path
from typing import Optional, Dict, List

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


def import_preset(filepath: str) -> Optional[Dict]:
    """Import a preset from a .fg or .json file.

    Returns:
        Dict with 'name', 'config', 'description', 'format_version' keys,
        or None if invalid/unreadable. 'config' is always normalized to
        engine key names.
    """
    try:
        data = _load_preset_json(filepath)
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
    except (json.JSONDecodeError, OSError, UnicodeDecodeError,
            RecursionError, ValueError):
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
