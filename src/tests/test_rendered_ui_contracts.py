"""Deterministic, platform-tolerant rendered UI contracts.

These tests verify render stability, non-blank/distinct application states, and
structural geometry. They intentionally do not claim golden-image regression
coverage: Qt font hinting and native-adjacent controls vary across OS patches.
Failures preserve repeat captures and diffs for review; the independent audit
gates clipping, overlap, reachability, semantics, focus, and accessibility.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget, QWidget

from frameguard.core import QAResult
from tools_dev.gui_audit import (
    Audit,
    SIZES,
    audit_state,
    check_accessibility,
    check_fixed_size_risks,
    check_headers,
    check_hit_areas,
    check_reachability,
    check_realized_text,
    check_sibling_overlaps,
)
from rendered_ui_helpers import (
    capture_application_content,
    settle,
    signature_distance,
    visual_signature,
    write_failure_artifacts,
)

pytestmark = pytest.mark.usefixtures("qapp")

BASELINE_PATH = Path(__file__).parent / "baselines" / "rendered_ui_contracts.json"
CONTRACT = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
SIGNATURE_CONTRACT = CONTRACT["signature"]


def _dense_results() -> list[QAResult]:
    outcomes = ("pass", "warn", "fail")
    results = []
    for index in range(12):
        outcome = outcomes[index % len(outcomes)]
        result = QAResult(
            "C:/FrameGuard QA/Long Delivery Root/"
            f"SHOW_EP{index:02}_SEQUENCE_WITH_A_DELIBERATELY_LONG_MASTER_FILENAME_v123.mov"
        )
        result.set_metadata("video_codec", "ProRes 422 HQ")
        result.set_metadata("resolution", "7680x4320")
        result.set_metadata("duration_display", "12:34:56")
        result.set_metadata("file_size_mb", 12345.6)
        result.add_pass("Container Format", "QuickTime/MOV accepted")
        result.add_pass("Audio Codec", "Linear PCM accepted")
        result.add_pass("Resolution", "7680x4320 delivery raster")
        result.add_pass("Frame Rate", "29.97 fps")
        if outcome != "pass":
            result.add_warn("Black Frames", "12 sequences detected", confidence=60)
        if outcome == "fail":
            result.add_fail("Bitrate", "below the 80 Mbps minimum", confidence=95)
        result.finalize()
        results.append(result)
    return results


@pytest.fixture
def visual_window(qapp):
    from frameguard.gui import MainWindow

    window = MainWindow()
    if getattr(window, "_watch_monitor", None) is not None:
        window._watch_monitor.stop()
    results = _dense_results()
    window.results_panel.set_results(results, append=True)
    window.checks_panel.set_results(window.results_panel._results)
    window.resize(1000, 700)
    window.show()
    settle(qapp)
    yield window
    window._cleanup_worker()
    if getattr(window, "_watch_monitor", None) is not None:
        window._watch_monitor.stop()
    window.hide()
    window.deleteLater()
    settle(qapp, 2)


def _artifact_dir(tmp_path: Path) -> Path:
    configured = os.environ.get("FRAMEGUARD_VISUAL_ARTIFACTS")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    return tmp_path / "visual-artifacts"


def _assert_render_quality(signature, state: str) -> None:
    assert signature.luma_span >= SIGNATURE_CONTRACT["minimum_luma_span"], (
        f"{state}: screenshot has only {signature.luma_span} luma levels of range; "
        "it may be blank or missing content"
    )
    assert signature.sampled_colors >= SIGNATURE_CONTRACT["minimum_sampled_colors"], (
        f"{state}: screenshot has only {signature.sampled_colors} sampled colours"
    )


def _capture_stable_render(window, qapp, state: str, artifacts: Path):
    settle(qapp)
    first = capture_application_content(window)
    settle(qapp, 3)
    repeat = capture_application_content(window)
    first_signature = visual_signature(first)
    repeat_signature = visual_signature(repeat)
    distance = signature_distance(first_signature, repeat_signature)
    if distance > SIGNATURE_CONTRACT["repeat_distance_max"]:
        paths = write_failure_artifacts(
            artifacts, state, first, repeat, first_signature, repeat_signature)
        pytest.fail(
            f"{state}: repeated deterministic render distance {distance:.5f} exceeds "
            f"{SIGNATURE_CONTRACT['repeat_distance_max']:.5f}; artifacts: {paths}"
        )
    _assert_render_quality(repeat_signature, state)
    return repeat_signature


def test_principal_pages_have_stable_distinct_light_and_dark_renders(
        visual_window, qapp, tmp_path):
    from frameguard import gui as G
    from frameguard.theme import THEMES

    window = visual_window
    artifacts = _artifact_dir(tmp_path)
    light = "Meta Light" if "Meta Light" in THEMES else "Clean Light"
    themes = [light, "Blue Hour"]
    panels = (
        (G.PANEL_SCAN, "Scan"),
        (G.PANEL_RESULTS, "Results"),
        (G.PANEL_CHECKS, "Checks"),
        (G.PANEL_WATCH, "Watch"),
        (G.PANEL_SETTINGS, "Settings"),
    )

    for theme in themes:
        window._apply_theme(theme)
        page_signatures = {}
        for index, page in panels:
            window._switch_panel(index)
            state = f"{theme}-{page}-1000x700"
            page_signatures[page] = _capture_stable_render(window, qapp, state, artifacts)
        for (first_name, first), (second_name, second) in itertools.combinations(
                page_signatures.items(), 2):
            distance = signature_distance(first, second)
            assert distance >= SIGNATURE_CONTRACT["minimum_distinct_page_distance"], (
                f"{theme}: {first_name} and {second_name} render nearly identically "
                f"({distance:.5f}); a page may have failed to populate"
            )


def test_all_themes_render_a_reviewable_populated_focus_state(
        visual_window, qapp, tmp_path):
    from frameguard import gui as G
    from frameguard.theme import THEMES

    assert len(THEMES) == 15, "the visual evidence matrix must cover all shipped themes"
    window = visual_window
    window.resize(1280, 800)
    window._switch_panel(G.PANEL_RESULTS)
    window.results_panel._more_btn.setFocus()
    artifacts = _artifact_dir(tmp_path)

    signatures = {}
    for theme in THEMES:
        window._apply_theme(theme)
        signatures[theme] = _capture_stable_render(
            window, qapp, f"theme-sheet-{theme}", artifacts)

    # A theme application bug that leaves every palette unchanged should not
    # pass merely because each individual image is non-blank.
    quantized = {
        tuple(value // 8 for value in signature.luma_tiles + signature.chroma_tiles)
        for signature in signatures.values()
    }
    assert len(quantized) >= 8, (
        f"only {len(quantized)} perceptually distinct theme signatures for 15 themes"
    )


@pytest.mark.parametrize("width,height", SIZES)
def test_results_and_checks_worst_case_pass_structural_audit(
        visual_window, qapp, tmp_path, width, height):
    from frameguard import gui as G

    window = visual_window
    window.resize(width, height)
    artifacts = _artifact_dir(tmp_path) / "audit"
    audit = Audit()
    for index, page in ((G.PANEL_RESULTS, "Results"), (G.PANEL_CHECKS, "Checks")):
        window._switch_panel(index)
        settle(qapp)
        state = f"{page}-{width}x{height}"
        audit_state(window, window.stack.widget(index), qapp, audit, state, artifacts)

    report = audit.report(float(os.environ.get("QT_SCALE_FACTOR", "1")))
    if audit.error_count:
        artifacts.mkdir(parents=True, exist_ok=True)
        report_path = artifacts / f"structural-{width}x{height}.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.fail(
            f"{audit.error_count} structural visual error(s) at {width}x{height}; "
            f"report: {report_path}"
        )


def test_overlap_and_accessibility_checks_detect_actionable_defects(qapp):
    root = QWidget()
    root.resize(220, 100)
    first = QPushButton("First", root)
    first.setGeometry(10, 10, 100, 40)
    second = QPushButton("…", root)
    second.setGeometry(80, 20, 100, 40)
    root.show()
    settle(qapp)

    audit = Audit()
    check_sibling_overlaps(root, audit, "synthetic-overlap")
    check_accessibility(root, audit, "synthetic-accessibility")
    categories = {finding.category for finding in audit.findings}
    assert "sibling-overlap" in categories
    assert "accessible-name" in categories
    assert all(finding.widget and finding.message for finding in audit.findings)
    root.hide()
    root.deleteLater()


def test_rendered_geometry_checks_detect_clipping_headers_reachability_and_hit_areas(qapp):
    root = QWidget()
    root.resize(300, 180)

    clipped = QLabel("A deliberately long realized label", root)
    clipped.setGeometry(4, 4, 40, 20)
    clipped.setFixedSize(40, 20)

    tiny = QPushButton("Tiny", root)
    tiny.setProperty("fgRole", "secondary")
    tiny.setGeometry(4, 32, 20, 20)

    outside = QPushButton("Outside", root)
    outside.setProperty("fgRole", "secondary")
    outside.setGeometry(270, 60, 80, 32)

    table = QTableWidget(root)
    table.setGeometry(4, 100, 150, 70)
    table.setColumnCount(1)
    table.setHorizontalHeaderLabels(["A header that cannot fit"])
    table.setColumnWidth(0, 50)
    table.setAccessibleName("Synthetic table")

    root.show()
    settle(qapp)
    audit = Audit()
    state = "synthetic-geometry"
    check_realized_text(root, audit, state)
    check_fixed_size_risks(root, audit, state)
    check_headers(root, audit, state)
    check_reachability(root, audit, state)
    check_hit_areas(root, audit, state)
    categories = {finding.category for finding in audit.findings}
    assert {
        "text-clipped", "fixed-size-dpi-risk", "header-clipped", "unreachable", "hit-area"
    } <= categories
    assert audit.report(1.0)["summary"]["errors"] >= 4
    root.hide()
    root.deleteLater()


def test_render_failure_artifacts_include_repeat_diff_and_signatures(tmp_path):
    first = QImage(24, 24, QImage.Format_RGBA8888)
    repeat = QImage(24, 24, QImage.Format_RGBA8888)
    first.fill(QColor("#102030"))
    repeat.fill(QColor("#D02030"))
    first_signature = visual_signature(first)
    repeat_signature = visual_signature(repeat)

    paths = write_failure_artifacts(
        tmp_path, "deliberate mismatch", first, repeat,
        first_signature, repeat_signature)

    assert set(paths) == {"first", "repeat", "diff", "report"}
    assert all(path.is_file() and path.stat().st_size > 0 for path in paths.values())
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert report["distance"] > 0
    assert report["first"] != report["repeat"]


def test_unrealizable_window_states_are_skipped_not_audited():
    """A desktop too small for the requested logical size at a scale factor
    (1024x768 CI desktop, 1000x700 at 2x) yields a state below the app's own
    minimum; the audit records it as skipped instead of reporting clipping
    that no supported display can show."""
    from tools_dev.gui_audit import unrealizable_reason

    assert unrealizable_reason((1000, 700), (512, 375), (760, 520))
    assert unrealizable_reason((1000, 700), (1000, 700), (760, 520)) is None
    # Clamped but still at least the minimum: a real state, audited.
    assert unrealizable_reason((1920, 1080), (853, 640), (760, 520)) is None
    audit = Audit()
    audit.skip("Clean Light-1000x700-scale-2", "too small")
    report = audit.report(2.0)
    assert report["summary"]["skipped"] == 1 and report["skipped"][0]["reason"] == "too small"


def test_a_scale_that_checked_nothing_fails_the_matrix():
    """Skipping unrealizable states must never turn into a silent pass."""
    from tools_dev.gui_audit import SCALES, empty_scales, parse_scales

    reports = [{"scale_factor": 1.0, "summary": {"states": 30}},
               {"scale_factor": 1.5, "summary": {"states": 0, "skipped": 6}}]
    assert empty_scales(reports) == [1.5]
    assert parse_scales("all") == SCALES
    assert parse_scales("1,1.25") == (1.0, 1.25)
    with pytest.raises(ValueError):
        parse_scales("3")
