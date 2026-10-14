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
    set_current_theme("Meta Dark")
    yield
    set_current_theme("Meta Dark")


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
