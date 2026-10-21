"""0.9.0 (H6): file names only ever suggest a preset, by whole tokens."""

import pytest

from frameguard import core
from frameguard.presets import detect_preset_from_filename, suggest_preset_for_files


# ---------------------------------------------------------------------------
# H6: filename suggestions never override the user's spec
# ---------------------------------------------------------------------------

class TestFilenameSuggestions:
    @pytest.mark.parametrize("name", [
        "PROJ_CAM01_C360.mp4", "overview_vr_cut.mp4", "take_1080_v2.mov",
        "a4kb.mp4", "SHOW_4K_25p.mov", "interview_2160.mov",
    ])
    def test_ambiguous_or_partial_tokens_suggest_nothing(self, name):
        assert detect_preset_from_filename(name, core.PRESETS) is None

    @pytest.mark.parametrize("name,expected", [
        ("SHOW_4K_60p.mov", "4k60"), ("show_UHD_24fps.mov", "4k24"),
        ("clip_1080p.mp4", "1080p30"), ("x_5.7k_master.mp4", "5.7k30"),
        ("x_1920x1080_59.94.mov", "1080p60"),
    ])
    def test_whole_tokens_suggest(self, name, expected):
        assert detect_preset_from_filename(name, core.PRESETS) == expected

    def test_batch_must_agree(self):
        assert suggest_preset_for_files(["a_4K_30p.mov", "b_4K_30p.mov"], core.PRESETS) == "4k30"
        assert suggest_preset_for_files(["a_4K_30p.mov", "b_1080p.mov"], core.PRESETS) is None

    def test_selecting_files_never_changes_the_chosen_profile(self, qapp):
        from frameguard.gui import ScanPanel
        panel = ScanPanel()
        for index in range(panel.preset_combo.count()):
            if panel.preset_combo.itemData(index) == "custom":
                panel.preset_combo.setCurrentIndex(index)
        panel._set_files(["/x/SHOW_EP01_4K_60p.mov"])
        assert panel.preset_combo.currentData() == "custom"
        assert "not applied" in panel.file_count_label.text()
        assert "4K UHD @ 60fps" in panel.file_count_label.text()
