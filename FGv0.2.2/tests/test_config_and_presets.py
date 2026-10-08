"""Regression guards for the Settings <-> preset <-> engine config contract.

The bug these protect against: SettingsPanel emitted `delivery_width`,
`deep_hardware_accel`, etc., while the engine only ever reads `target_width`,
`use_hwaccel`, etc. Every override was silently discarded, so nothing in the
Settings panel had any effect on a scan, and exported presets contained
hardcoded defaults instead of the user's configuration.
"""

import pytest

from frameguard.core import DEFAULT_CONFIG, PRESETS
from frameguard.presets import (
    PRESET_CONFIG_KEYS,
    export_preset,
    import_preset,
    normalize_preset_config,
)
from frameguard.worker import ScanWorker


class TestConfigKeyContract:
    def test_every_override_key_is_read_by_the_engine(self, qapp):
        """The core invariant. If this fails, Settings silently do nothing."""
        from frameguard.gui import SettingsPanel

        overrides = SettingsPanel().get_config_overrides()
        unknown = set(overrides) - set(DEFAULT_CONFIG)
        assert not unknown, (
            f"Settings emits key(s) the engine never reads: {sorted(unknown)}. "
            f"They would be silently discarded and the scan would use defaults."
        )

    def test_overrides_are_not_empty(self, qapp):
        from frameguard.gui import SettingsPanel

        overrides = SettingsPanel().get_config_overrides()
        for key in ("target_width", "target_height", "target_fps",
                    "min_bitrate_mbps", "audio_required"):
            assert key in overrides, f"{key} missing from Settings overrides"

    def test_no_ui_only_keys_leak_into_engine_config(self, qapp):
        from frameguard.gui import SettingsPanel

        overrides = SettingsPanel().get_config_overrides()
        for leaked in ("theme", "delivery_resolution_preset", "deep_strict_mode"):
            assert leaked not in overrides

    def test_preset_keys_are_all_engine_keys(self):
        unknown = set(PRESET_CONFIG_KEYS) - set(DEFAULT_CONFIG)
        assert not unknown, f"Preset schema has non-engine keys: {sorted(unknown)}"

    def test_worker_rejects_unknown_config_keys(self):
        """A silently-ignored key is what caused the original bug; fail loudly."""
        worker = ScanWorker()
        with pytest.raises(ValueError, match="Unknown scan config key"):
            worker.configure(files=["a.mov"], cfg_overrides={"delivery_width": 1920})

    def test_worker_rejects_unknown_preset(self):
        worker = ScanWorker()
        with pytest.raises(ValueError, match="Unknown scan preset"):
            worker.configure(files=["a.mov"], preset="imported:My Preset")

    @pytest.mark.parametrize("preset_key", sorted(PRESETS))
    def test_all_builtin_presets_apply(self, preset_key):
        worker = ScanWorker()
        worker.configure(files=["a.mov"], preset=preset_key)
        p = PRESETS[preset_key]
        assert worker._cfg["target_width"] == p["width"]
        assert worker._cfg["target_height"] == p["height"]
        assert worker._cfg["target_fps"] == p["fps"]
        assert worker._cfg["min_bitrate_mbps"] == p["min_bitrate_mbps"]


class TestPresetRoundTrip:
    def test_export_preserves_user_settings(self, tmp_path):
        """Previously every exported preset contained hardcoded defaults."""
        config = {
            "target_width": 1920,
            "target_height": 1080,
            "target_fps": 24.0,
            "fps_tolerance": 0.2,
            "min_bitrate_mbps": 6,
            "audio_required": False,
            "use_hwaccel": False,
            "segment_enabled": False,
            "segment_count": 2,
            "silence_duration": 7.5,
        }
        path = export_preset("Team 1080p24", config, str(tmp_path / "p.fg"))
        loaded = import_preset(path)

        assert loaded is not None
        for key, value in config.items():
            assert loaded["config"][key] == value, f"{key} was not round-tripped"

    def test_export_name_is_preserved(self, tmp_path):
        path = export_preset("4K \u2014 VR Master", {"target_width": 3840}, str(tmp_path / "p.fg"))
        assert import_preset(path)["name"] == "4K \u2014 VR Master"

    def test_settings_panel_round_trip(self, qapp, tmp_path):
        """Export from the real panel, re-import, and confirm nothing is lost."""
        from frameguard.gui import SettingsPanel

        panel = SettingsPanel()
        original = panel.get_config_overrides()
        path = export_preset("RoundTrip", original, str(tmp_path / "rt.fg"))
        loaded = import_preset(path)["config"]

        for key, value in original.items():
            assert loaded[key] == value, f"{key}: {original[key]!r} -> {loaded.get(key)!r}"

    def test_legacy_delivery_keys_are_migrated(self):
        """v0.5.1 and earlier wrote delivery_*/deep_* names."""
        legacy = {
            "delivery_width": 1280,
            "delivery_height": 720,
            "delivery_fps_target": 60.0,
            "delivery_min_bitrate_mbps": 4,
            "audio_silence_duration_s": 3.0,
            "deep_hardware_accel": False,
            "deep_segmented_scan": False,
            "deep_segment_count": 2,
            "accepted_video_codecs": ["h264"],
        }
        out = normalize_preset_config(legacy)
        assert out == {
            "target_width": 1280,
            "target_height": 720,
            "target_fps": 60.0,
            "min_bitrate_mbps": 4,
            "silence_duration": 3.0,
            "use_hwaccel": False,
            "segment_enabled": False,
            "segment_count": 2,
            "accepted_codecs": ["h264"],
        }

    def test_bundled_v1_presets_still_load(self):
        """The shipped presets/*.json are format_version 1."""
        from frameguard.presets import get_bundled_presets_dir

        bundled = get_bundled_presets_dir()
        files = sorted(bundled.glob("*.json")) if str(bundled) else []
        assert files, "bundled presets are missing from the repo"
        for f in files:
            preset = import_preset(str(f))
            assert preset is not None, f"{f.name} failed to import"
            assert preset["config"]["target_width"] > 0

    def test_container_extensions_get_a_leading_dot(self):
        """core compares against os.path.splitext() output, which includes '.'."""
        out = normalize_preset_config({"accepted_containers": ["mov", "mp4", ".mxf"]})
        assert out["accepted_containers"] == [".mov", ".mp4", ".mxf"]

    def test_ui_only_keys_are_stripped_from_presets(self):
        out = normalize_preset_config({"theme": "Nord Frost", "target_width": 1920})
        assert "theme" not in out
        assert out["target_width"] == 1920

    @pytest.mark.parametrize("bad", [
        '{"not": "a preset"}',
        '{"config": "not a dict"}',
        "definitely not json",
        "",
    ])
    def test_malformed_presets_return_none(self, tmp_path, bad):
        p = tmp_path / "bad.fg"
        p.write_text(bad, encoding="utf-8")
        assert import_preset(str(p)) is None

    def test_oversized_file_is_rejected(self, tmp_path):
        p = tmp_path / "huge.fg"
        p.write_text("x" * 1_000_001, encoding="utf-8")
        assert import_preset(str(p)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert import_preset(str(tmp_path / "nope.fg")) is None
