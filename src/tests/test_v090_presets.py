"""0.9.0: preset validation (M3) and worker config handling (O1)."""

import pytest

from frameguard import core
from frameguard.presets import (
    PresetValidationError, export_preset, import_preset, normalize_preset_config,
    validate_preset_config,
)


# ---------------------------------------------------------------------------
# M3: preset validation
# ---------------------------------------------------------------------------

class TestPresetValidation:
    def test_out_of_range_values_are_rejected_with_every_bad_key_listed(self):
        with pytest.raises(PresetValidationError) as caught:
            validate_preset_config({"target_fps": 0, "min_bitrate_mbps": -5,
                                    "fps_tolerance": 60, "target_width": 3840})
        message = str(caught.value)
        for key in ("target_fps", "min_bitrate_mbps", "fps_tolerance"):
            assert key in message
        assert "target_width" not in message

    def test_unknown_keys_are_reported(self):
        with pytest.raises(PresetValidationError) as caught:
            validate_preset_config({"target_widht": 3840, "target_height": 2160})
        assert caught.value.unknown == ["target_widht"]

    @pytest.mark.parametrize("key", ["accepted_codecs", "accepted_containers",
                                     "accepted_audio_codecs"])
    def test_empty_acceptance_lists_are_rejected(self, key):
        with pytest.raises(PresetValidationError) as caught:
            validate_preset_config({key: []})
        assert key in str(caught.value)

    def test_retired_and_machine_local_keys_are_ignored_not_rejected(self):
        config = validate_preset_config({"target_width": 1920, "max_workers": 8,
                                         "naming_pattern": "x", "theme": "dark"})
        assert config == {"target_width": 1920}

    def test_new_spec_keys_validate(self):
        config = validate_preset_config({
            "required_projection": "Equirectangular", "required_stereo_mode": "top-bottom",
            "min_bit_depth": "10", "required_transfer": "PQ", "loudness_mode": "essential",
            "allow_larger_resolution": True, "max_decode_errors": 2,
        })
        assert config["required_projection"] == "equirectangular"
        assert config["required_stereo_mode"] == "top_bottom"
        assert config["min_bit_depth"] == 10 and config["required_transfer"] == "pq"
        with pytest.raises(PresetValidationError):
            validate_preset_config({"required_transfer": "dolby"})

    @pytest.mark.parametrize("mode", ["off", "report", "advisory", "essential"])
    def test_every_loudness_mode_validates(self, mode):
        assert validate_preset_config({"loudness_mode": mode.upper()}) == {"loudness_mode": mode}

    def test_unknown_loudness_mode_is_rejected(self):
        with pytest.raises(PresetValidationError, match="loudness_mode"):
            validate_preset_config({"loudness_mode": "warn"})

    def test_import_rejects_and_can_explain(self, tmp_path):
        path = tmp_path / "bad.fg"
        export_preset("ok", {"target_width": 1920}, str(path))
        text = path.read_text(encoding="utf-8").replace('"target_width": 1920', '"target_width": 0')
        path.write_text(text, encoding="utf-8")
        assert import_preset(str(path)) is None
        with pytest.raises(PresetValidationError, match="target_width"):
            import_preset(str(path), raise_errors=True)

    def test_lenient_normalisation_drops_instead_of_clamping(self):
        assert normalize_preset_config({"fps_tolerance": 60, "target_fps": 0}) == {}

    def test_every_bundled_preset_still_validates(self):
        from frameguard.presets import get_bundled_presets_dir
        files = sorted(get_bundled_presets_dir().glob("*.json"))
        assert files
        for path in files:
            assert import_preset(str(path), raise_errors=True)

    def test_bundled_360_preset_requires_equirect(self):
        from frameguard.presets import get_bundled_presets_dir
        preset = import_preset(str(get_bundled_presets_dir() / "vr_360_5.7k.json"))
        assert preset["config"]["required_projection"] == "equirectangular"


# ---------------------------------------------------------------------------
# O1: worker config handling
# ---------------------------------------------------------------------------

class TestWorkerConfig:
    def test_retired_keys_from_old_settings_are_ignored(self, qapp):
        from frameguard.worker import ScanWorker
        worker = ScanWorker()
        worker.configure(files=["x.mov"], cfg_overrides={"max_workers": 8, "hwaccel_method": "cuda",
                                                         "target_width": 1920})
        assert worker._cfg["target_width"] == 1920
        assert "max_workers" not in worker._cfg

    def test_unknown_and_invalid_values_raise(self, qapp):
        from frameguard.worker import ScanWorker
        worker = ScanWorker()
        with pytest.raises(ValueError, match="target_widht"):
            worker.configure(files=["x.mov"], cfg_overrides={"target_widht": 1})
        with pytest.raises(ValueError, match="fps_tolerance"):
            worker.configure(files=["x.mov"], cfg_overrides={"fps_tolerance": 60})

    def test_dead_config_keys_are_gone(self):
        for key in ("naming_pattern", "use_signalstats", "use_mediaconch", "hwaccel_method",
                    "deep_scan_max_file_gb", "min_duration_fail", "quality_ymin_floor"):
            assert key not in core.DEFAULT_CONFIG
            assert key in core.RETIRED_CONFIG_KEYS
        for name in ("run_signalstats_check", "check_naming", "check_duration",
                     "check_pixel_aspect_ratio", "_segment_boundaries", "get_file_duration"):
            assert not hasattr(core, name), name
