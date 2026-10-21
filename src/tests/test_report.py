"""PDF report generation tests.

The reported bug: any report spanning more than one page failed to generate,
because the continuation-page header contained a literal U+2022 bullet and
fpdf2's built-in Helvetica is latin-1 only. Single-file reports fit on one
page and worked, so it only ever broke on real batch scans.
"""

import os

import pytest

from frameguard.core import QAResult
from frameguard.report import generate_report, get_default_output_dir
from frameguard.theme import THEMES, set_current_theme

from conftest import make_result


@pytest.fixture(autouse=True)
def _default_theme():
    set_current_theme("Blue Hour")
    yield
    set_current_theme("Blue Hour")


def _pdf_is_valid(path):
    assert os.path.exists(path), f"{path} was not written"
    with open(path, "rb") as f:
        assert f.read(5) == b"%PDF-", "output is not a PDF"
    assert os.path.getsize(path) > 500


class TestMultiPage:
    @pytest.mark.parametrize("count", [1, 2, 25, 120])
    def test_report_of_n_results(self, tmp_path, count):
        results = [make_result(f"clip_{i:03}.mov", ["pass", "warn", "fail"][i % 3])
                   for i in range(count)]
        out = str(tmp_path / f"r{count}.pdf")
        generate_report(results, out, preset_label="4K UHD @ 30fps")
        _pdf_is_valid(out)

    def test_multipage_with_unicode_preset_label(self, tmp_path):
        """The continuation-page header interpolates the preset label."""
        out = str(tmp_path / "u.pdf")
        generate_report([make_result(f"c{i}.mov") for i in range(60)], out,
                        preset_label="4K \u2014 \u201cVR\u201d \u00b5-spec \u2022 v2")
        _pdf_is_valid(out)

    def test_multipage_with_unicode_editor(self, tmp_path):
        out = str(tmp_path / "e.pdf")
        generate_report([make_result(f"c{i}.mov") for i in range(60)], out,
                        preset_label="P", editor="Jos\u00e9 Mu\u00f1oz \u2014 TPG")
        _pdf_is_valid(out)


class TestHostileInput:
    @pytest.mark.parametrize("filename", [
        "Caf\u00e9\u2014Reel_\u201cv2\u201d.mov",
        "\u65e5\u672c\u8a9e_\u30c6\u30b9\u30c8.mov",
        "emoji_\U0001F3AC_clip.mov",
        "A" * 200 + ".mov",
        "tabs\tand\nnewlines.mov",
        "\u00b5-spec_\u00b1_2\u00b0.mov",
    ])
    def test_filenames(self, tmp_path, filename):
        out = str(tmp_path / "f.pdf")
        generate_report([make_result(filename)], out)
        _pdf_is_valid(out)

    def test_very_long_check_detail(self, tmp_path):
        r = QAResult("long.mov")
        r.add_fail("Bitrate", "extremely verbose failure explanation. " * 60)
        r.finalize()
        out = str(tmp_path / "l.pdf")
        generate_report([r], out)
        _pdf_is_valid(out)

    def test_result_with_no_checks(self, tmp_path):
        r = QAResult("bare.mov")
        r.finalize()
        out = str(tmp_path / "b.pdf")
        generate_report([r], out)
        _pdf_is_valid(out)

    def test_unfinalized_result(self, tmp_path):
        """Cancelled scans can produce results that never had finalize() called."""
        r = QAResult("cancelled.mov")
        r.add_fail("Bitrate", "low")
        assert r.status is None
        out = str(tmp_path / "nf.pdf")
        generate_report([r], out)
        _pdf_is_valid(out)

    def test_empty_results_raises(self, tmp_path):
        with pytest.raises(ValueError):
            generate_report([], str(tmp_path / "empty.pdf"))

    def test_none_preset_label_is_tolerated(self, tmp_path):
        out = str(tmp_path / "n.pdf")
        generate_report([make_result()], out, preset_label=None)
        _pdf_is_valid(out)


class TestReportColors:
    @pytest.mark.parametrize("value", [
        None,
        "",
        "#12345",
        "#1234567",
        "#12GG34",
        123456,
        object(),
    ])
    def test_malformed_colors_fall_back_without_raising(self, value):
        from frameguard.report import _hex_to_rgb

        assert _hex_to_rgb(value) == (128, 128, 128)

    @pytest.mark.parametrize("value, expected", [
        ("#12aBcD", (18, 171, 205)),
        ("12ABCD", (18, 171, 205)),
        ("#12ABCD80", (18, 171, 205)),
    ])
    def test_supported_colors_are_parsed_exactly(self, value, expected):
        from frameguard.report import _hex_to_rgb

        assert _hex_to_rgb(value) == expected

    @pytest.mark.parametrize("theme_name", sorted(THEMES))
    def test_every_theme_produces_valid_report_rgb_values(self, theme_name):
        from frameguard.report import _get_report_colors

        colors = _get_report_colors(THEMES[theme_name])
        assert colors
        assert all(
            isinstance(rgb, tuple)
            and len(rgb) == 3
            and all(isinstance(channel, int) and 0 <= channel <= 255
                    for channel in rgb)
            for rgb in colors.values()
        )


class TestThemes:
    @pytest.mark.parametrize("theme_name", sorted(THEMES))
    def test_every_theme_renders_multipage(self, tmp_path, theme_name):
        set_current_theme(theme_name)
        out = str(tmp_path / "t.pdf")
        generate_report([make_result(f"c{i}.mov") for i in range(40)], out,
                        preset_label="4K30")
        _pdf_is_valid(out)


class TestOutputPaths:
    def test_nested_directory_is_created(self, tmp_path):
        out = str(tmp_path / "a" / "b" / "c" / "r.pdf")
        generate_report([make_result()], out)
        _pdf_is_valid(out)

    def test_bare_filename_lands_in_documents(self, isolated_user_dirs):
        path = generate_report([make_result()], "bare.pdf")
        assert os.path.dirname(path) == get_default_output_dir()
        _pdf_is_valid(path)

    def test_returns_the_written_path(self, tmp_path):
        out = str(tmp_path / "r.pdf")
        assert generate_report([make_result()], out) == out


class TestLayout:
    """Geometry guarantees. These catch silent regressions that only show up
    when a human looks at the rendered page."""

    def test_table_widths_fill_the_usable_width_exactly(self):
        """Columns are proportions of USABLE_W, so they can never overrun."""
        from frameguard.report import USABLE_W

        summary = [w * USABLE_W for w in (0.36, 0.09, 0.11, 0.12, 0.32)]
        checks = [w * USABLE_W for w in (0.13, 0.24, 0.63)]
        assert sum(summary) == pytest.approx(USABLE_W)
        assert sum(checks) == pytest.approx(USABLE_W)

    def test_margins_leave_room_for_the_footer(self):
        from frameguard.report import MARGIN_B, HandoffReport

        pdf = HandoffReport()
        pdf.add_page()
        # Footer sits at -13; content must stop above it.
        assert pdf.content_bottom <= pdf.h - 13
        assert MARGIN_B >= 13

    def test_text_height_matches_rendered_lines(self):
        """The painted row background is sized from this; an approximation
        here is what made summary text overflow its band."""
        from frameguard.report import HandoffReport

        pdf = HandoffReport()
        pdf.add_page()
        pdf.set_font("Helvetica", "", 8)

        one = pdf.text_height(80, "short", 4.0)
        many = pdf.text_height(80, "word " * 60, 4.0)
        assert one == pytest.approx(4.0)
        assert many > one
        assert many % 4.0 == pytest.approx(0.0)

    def test_empty_text_still_reserves_one_line(self):
        from frameguard.report import HandoffReport

        pdf = HandoffReport()
        pdf.add_page()
        pdf.set_font("Helvetica", "", 8)
        assert pdf.text_height(80, "", 4.0) == pytest.approx(4.0)

    def test_long_filenames_do_not_break_the_layout(self, tmp_path):
        results = [make_result("A_VERY_LONG_PRODUCTION_FILENAME_" * 4 + ".mov")]
        out = str(tmp_path / "long.pdf")
        generate_report(results, out)
        _pdf_is_valid(out)

    def test_long_details_wrap_without_looping(self, tmp_path):
        r = QAResult("verbose.mov")
        r.add_fail("Bitrate", "detailed explanation of the failure. " * 200)
        r.finalize()
        out = str(tmp_path / "verbose.pdf")
        generate_report([r], out)
        _pdf_is_valid(out)

    def test_page_count_is_reasonable(self, tmp_path):
        """Guards against a layout bug that emits one page per row."""
        out = str(tmp_path / "many.pdf")
        generate_report([make_result(f"c{i}.mov", "pass") for i in range(30)], out)
        with open(out, "rb") as f:
            data = f.read()
        pages = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
        assert 1 <= pages <= 20, f"30 clean results produced {pages} pages"

    def test_header_text_is_latin1_safe(self, tmp_path):
        """The continuation-page header is the exact thing that used to crash
        every multi-page report."""
        from frameguard.report import HandoffReport

        pdf = HandoffReport(preset_label="4K \u2014 \u201cVR\u201d \u2022 \u00b5")
        pdf.add_page()
        pdf.add_page()  # triggers header()
        pdf.output(str(tmp_path / "hdr.pdf"))


class TestBackgroundOutputBoundaries:
    def test_runner_snapshots_report_colors_at_creation(self, monkeypatch):
        from frameguard import report as R
        from frameguard.gui import _PdfExportRunner

        set_current_theme("Blue Hour")
        expected = R._get_report_colors()
        runner = _PdfExportRunner([make_result()], "ignored.pdf", "4K30")
        set_current_theme("Clean Light")
        captured = {}

        def _capture(*_args, **kwargs):
            captured.update(kwargs["colors"])

        monkeypatch.setattr(R, "generate_report", _capture)
        runner.run()

        assert captured == expected
        assert captured != R._get_report_colors()

    def test_resetting_export_ui_does_not_release_unrelated_runners(self):
        from types import SimpleNamespace
        from frameguard.gui import MainWindow

        class _Button:
            def setText(self, _text):
                pass

            def setEnabled(self, _enabled):
                pass

        first = object()
        second = object()
        owner = SimpleNamespace(
            _export_runners=[first, second],
            results_panel=SimpleNamespace(export_btn=_Button(), _results=[1]),
        )

        MainWindow._reset_export_button(owner)

        assert owner._export_runners == [first, second]

    def test_auto_export_runner_is_released_after_success(
            self, monkeypatch, tmp_path):
        from types import SimpleNamespace
        from frameguard import gui as G
        from frameguard import report as R

        class _Owner:
            _release_export_runner = G.MainWindow._release_export_runner
            _on_auto_export_finished = G.MainWindow._on_auto_export_finished
            _on_auto_export_error = G.MainWindow._on_auto_export_error

            def __init__(self):
                self._export_runners = []
                self.scan_panel = SimpleNamespace(
                    preset_combo=SimpleNamespace(currentText=lambda: "4K30"))
                self.messages = []

            def _ensure_output_directory(self):
                return str(tmp_path)

            def _report_idle_status(self, text):
                self.messages.append(text)

        class _InlinePool:
            @staticmethod
            def start(runner):
                runner.run()

        owner = _Owner()
        monkeypatch.setattr(R, "generate_report", lambda *_a, **_k: None)
        monkeypatch.setattr(
            G.QThreadPool, "globalInstance", staticmethod(lambda: _InlinePool()))

        G.MainWindow._auto_export_report(owner, [make_result()])

        assert owner._export_runners == []
        assert owner.messages and owner.messages[-1].startswith("Report saved:")

    def test_delayed_runner_survives_gc_then_releases_exactly_once(
            self, monkeypatch, qapp):
        import gc
        import threading
        import weakref
        from frameguard import report as R
        from frameguard.gui import _PdfExportRunner

        started = threading.Event()
        finish = threading.Event()

        def _delayed(*_args, **_kwargs):
            started.set()
            assert finish.wait(5)

        monkeypatch.setattr(R, "generate_report", _delayed)
        runner = _PdfExportRunner([make_result()], "ignored.pdf", "4K30")
        retained = [runner]

        def _release(completed):
            if completed in retained:
                retained.remove(completed)

        runner.signals.terminal.connect(_release)
        reference = weakref.ref(runner)
        thread = threading.Thread(target=runner.run)
        thread.start()
        assert started.wait(5)
        del runner
        gc.collect()
        assert reference() is not None
        assert len(retained) == 1

        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        qapp.processEvents()
        assert retained == []
        del thread
        gc.collect()
        assert reference() is None

    def test_stale_positive_vlc_cache_is_invalidated(
            self, monkeypatch, tmp_path):
        from frameguard import gui as G

        executable = tmp_path / "vlc"
        executable.write_bytes(b"binary")
        monkeypatch.setattr(G.sys, "platform", "linux")
        monkeypatch.setattr(G.shutil, "which", lambda _name: str(executable))
        G._vlc_path_cache.clear()
        try:
            assert G.find_vlc() == str(executable)
            executable.unlink()
            monkeypatch.setattr(G.shutil, "which", lambda _name: None)
            assert G.find_vlc() is None
        finally:
            G._vlc_path_cache.clear()

    def test_changed_vlc_identity_forces_rediscovery(
            self, monkeypatch, tmp_path):
        from frameguard import gui as G

        executable = tmp_path / "vlc"
        executable.write_bytes(b"old")
        calls = []

        def _which(_name):
            calls.append(True)
            return str(executable)

        monkeypatch.setattr(G.sys, "platform", "linux")
        monkeypatch.setattr(G.shutil, "which", _which)
        G._vlc_path_cache.clear()
        try:
            assert G.find_vlc() == str(executable)
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"new binary")
            os.replace(replacement, executable)
            assert G.find_vlc() == str(executable)
            assert len(calls) == 2
        finally:
            G._vlc_path_cache.clear()

    def test_missing_reveal_target_never_launches_file_manager(
            self, monkeypatch, tmp_path):
        from frameguard import gui as G

        launched = []
        monkeypatch.setattr(G.sys, "platform", "darwin")
        monkeypatch.setattr(
            G.subprocess, "Popen", lambda command: launched.append(command))

        assert G._reveal_in_file_manager(str(tmp_path / "deleted.mov")) is False
        assert launched == []

    def test_slow_death_vlc_falls_back_to_default_player(
            self, monkeypatch, tmp_path):
        from frameguard import gui as G

        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"video")

        class _SlowDeath:
            def poll(self):
                return None

            def wait(self, timeout):
                assert timeout <= 1.0
                return 1

        monkeypatch.setattr(G, "find_vlc", lambda: str(tmp_path / "vlc"))
        monkeypatch.setattr(G.subprocess, "Popen", lambda *_a, **_k: _SlowDeath())
        monkeypatch.setattr(G, "_open_path", lambda _path: True)

        assert G.open_in_player(str(clip)) == "default"


class TestStatusConsistency:
    def test_badge_counts_match_result_statuses(self, tmp_path):
        """The PDF summary must agree with QAResult.status, not r.passed."""
        from frameguard.report import _result_status, _status_label

        results = [make_result("a.mov", "pass"),
                   make_result("b.mov", "warn"),
                   make_result("c.mov", "fail")]
        statuses = [_result_status(r) for r in results]
        assert statuses == ["PASS", "PASS_WITH_WARNINGS", "MISMATCH"]

        # Operator-facing wording matches the UI.
        assert [_status_label(s) for s in statuses] == ["PASS", "WARNING", "FAIL"]

        out = str(tmp_path / "s.pdf")
        generate_report(results, out)
        _pdf_is_valid(out)
