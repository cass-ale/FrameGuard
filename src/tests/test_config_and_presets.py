"""Regression guards for the Settings <-> preset <-> engine config contract.

The bug these protect against: SettingsPanel emitted `delivery_width`,
`deep_hardware_accel`, etc., while the engine only ever reads `target_width`,
`use_hwaccel`, etc. Every override was silently discarded, so nothing in the
Settings panel had any effect on a scan, and exported presets contained
hardcoded defaults instead of the user's configuration.
"""

import json
from unittest import mock

import pytest

from frameguard import presets as P
from frameguard.core import DEFAULT_CONFIG, PRESETS
from frameguard.presets import (
    MAX_PRESET_BYTES,
    MAX_PRESET_NESTING,
    PRESET_CONFIG_KEYS,
    export_preset,
    import_preset,
    list_available_presets,
    normalize_preset_config,
    save_imported_preset,
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
        p.write_text("x" * (MAX_PRESET_BYTES + 1), encoding="utf-8")
        assert import_preset(str(p)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert import_preset(str(tmp_path / "nope.fg")) is None


class TestHostilePresetParsing:
    @staticmethod
    def _minimal_bytes():
        return json.dumps({
            "name": "Bounded",
            "config": {"target_width": 1920},
        }, separators=(",", ":")).encode("utf-8")

    def test_exact_byte_limit_is_accepted(self, tmp_path):
        raw = self._minimal_bytes()
        path = tmp_path / "exact.fg"
        path.write_bytes(raw + b" " * (MAX_PRESET_BYTES - len(raw)))

        assert import_preset(str(path))["config"]["target_width"] == 1920

    def test_one_byte_over_limit_is_rejected(self, tmp_path):
        raw = self._minimal_bytes()
        path = tmp_path / "over.fg"
        path.write_bytes(raw + b" " * (MAX_PRESET_BYTES + 1 - len(raw)))

        assert import_preset(str(path)) is None

    def test_growth_during_read_is_bounded(self, monkeypatch):
        requested = []

        class GrowingFile:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                requested.append(size)
                return b"x" * size

        monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: GrowingFile())

        assert import_preset("growing.fg") is None
        assert requested == [MAX_PRESET_BYTES + 1]

    def test_excessive_nesting_is_rejected_without_recursion(self, tmp_path):
        nested = "h264"
        for _ in range(MAX_PRESET_NESTING + 1):
            nested = [nested]
        path = tmp_path / "deep.fg"
        path.write_text(json.dumps({
            "name": "Deep",
            "config": {"accepted_codecs": nested},
        }), encoding="utf-8")

        assert import_preset(str(path)) is None

    @pytest.mark.parametrize("root", [[], [1], "preset", 42, None])
    def test_non_dict_roots_are_rejected(self, tmp_path, root):
        path = tmp_path / "root.fg"
        path.write_text(json.dumps(root), encoding="utf-8")
        assert import_preset(str(path)) is None

    def test_non_utf8_input_is_rejected(self, tmp_path):
        path = tmp_path / "binary.fg"
        path.write_bytes(b"{\xff\xfe}")
        assert import_preset(str(path)) is None

    def test_unknown_key_only_config_is_rejected(self, tmp_path):
        path = tmp_path / "unknown.fg"
        path.write_text(json.dumps({
            "name": "Unknown",
            "config": {"launch_command": "do-not-run"},
        }), encoding="utf-8")
        assert import_preset(str(path)) is None

    def test_duplicate_json_keys_are_rejected(self, tmp_path):
        path = tmp_path / "duplicate.fg"
        path.write_text(
            '{"name":"Duplicate","config":'
            '{"target_width":1920,"target_width":1}}',
            encoding="utf-8",
        )
        assert import_preset(str(path)) is None

    def test_parser_recursion_failure_is_invalid_not_an_exception(
            self, tmp_path, monkeypatch):
        path = tmp_path / "parser.fg"
        path.write_bytes(self._minimal_bytes())
        monkeypatch.setattr(P.json, "loads", mock.Mock(side_effect=RecursionError))

        assert import_preset(str(path)) is None


class TestPresetStorageBoundaries:
    @staticmethod
    def _source(tmp_path, filename, name, width):
        return export_preset(
            name,
            {"target_width": width},
            str(tmp_path / filename),
        )

    @pytest.mark.parametrize("names", [
        ("A/B", "A:B"),
        ("Review", "review"),
        ("Caf\u00e9", "Cafe\u0301"),
    ], ids=["sanitized", "case", "unicode-normalized"])
    def test_name_collisions_preserve_both_presets(
            self, tmp_path, monkeypatch, names):
        incoming = tmp_path / "incoming"
        user_dir = tmp_path / "user"
        incoming.mkdir()
        user_dir.mkdir()
        monkeypatch.setattr(P, "get_user_presets_dir", lambda: user_dir)
        first_source = self._source(incoming, "first.fg", names[0], 1920)
        second_source = self._source(incoming, "second.fg", names[1], 3840)

        first = save_imported_preset(first_source)
        second = save_imported_preset(second_source)

        assert first is not None and second is not None
        assert first["path"] != second["path"]
        assert P.Path(first["path"]).is_file()
        assert P.Path(second["path"]).is_file()
        assert import_preset(first["path"])["config"]["target_width"] == 1920
        assert import_preset(second["path"])["config"]["target_width"] == 3840

    def test_automatic_export_never_silently_overwrites(self, tmp_path, monkeypatch):
        monkeypatch.setattr(P, "get_user_presets_dir", lambda: tmp_path)

        first = export_preset("Same Name", {"target_width": 1920})
        second = export_preset("Same Name", {"target_width": 3840})

        assert first != second
        assert import_preset(first)["config"]["target_width"] == 1920
        assert import_preset(second)["config"]["target_width"] == 3840

    def test_listing_preserves_distinct_colliding_names(
            self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        self._source(user, "one.fg", "Review", 1920)
        self._source(user, "two.fg", "review", 3840)
        monkeypatch.setattr(P, "get_bundled_presets_dir", lambda: bundled)
        monkeypatch.setattr(P, "get_user_presets_dir", lambda: user)

        listed = list_available_presets()

        assert [preset["name"] for preset in listed] == ["Review", "review"]

    def test_exact_user_name_still_overrides_bundled_preset(
            self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled"
        user = tmp_path / "user"
        bundled.mkdir()
        user.mkdir()
        self._source(bundled, "team.json", "Team", 1920)
        self._source(user, "team.fg", "Team", 3840)
        monkeypatch.setattr(P, "get_bundled_presets_dir", lambda: bundled)
        monkeypatch.setattr(P, "get_user_presets_dir", lambda: user)

        listed = list_available_presets()

        assert len(listed) == 1
        assert listed[0]["source"] == "imported"
        assert listed[0]["config"]["target_width"] == 3840

    def test_unavailable_user_directory_is_a_safe_import_failure(
            self, tmp_path, monkeypatch):
        source = self._source(tmp_path, "source.fg", "Read Only", 1920)
        monkeypatch.setattr(
            P, "get_user_presets_dir",
            mock.Mock(side_effect=PermissionError("read-only")),
        )

        assert save_imported_preset(source) is None

    def test_listing_survives_unavailable_user_directory(
            self, tmp_path, monkeypatch):
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        self._source(bundled, "default.json", "Bundled", 1920)
        monkeypatch.setattr(P, "get_bundled_presets_dir", lambda: bundled)
        monkeypatch.setattr(
            P, "get_user_presets_dir",
            mock.Mock(side_effect=PermissionError("read-only")),
        )

        listed = list_available_presets()

        assert [preset["name"] for preset in listed] == ["Bundled"]

    def test_file_instead_of_user_directory_fails_safely(
            self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        frameguard_dir = home / ".frameguard"
        frameguard_dir.mkdir(parents=True)
        (frameguard_dir / "presets").write_text("not a directory", encoding="utf-8")
        source = self._source(tmp_path, "source.fg", "Blocked", 1920)
        monkeypatch.setattr(P.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(P, "get_bundled_presets_dir", lambda: tmp_path / "missing")

        assert save_imported_preset(source) is None
        assert list_available_presets() == []
