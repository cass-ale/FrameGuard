"""0.9.0 (D4): JSON/CSV exports and the PDF audit trail."""

import json
import os

import pytest

from frameguard import core
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import MediaLibrary, make_clip, needs_ffmpeg


def _scan(path, **overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=240, min_bitrate_mbps=0, loudness_mode="off")
    cfg.update(overrides)
    result = run_qa_checks(path, cfg=cfg)
    result.finalize()
    return result


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg090report")))


# ---------------------------------------------------------------------------
# D4: PDF spec table and data exports without media
# ---------------------------------------------------------------------------

class TestReportAuditTrail:
    def test_spec_rows_summarise_the_effective_config(self):
        from frameguard.report import spec_rows
        rows = dict(spec_rows(core.config_snapshot(dict(core.DEFAULT_CONFIG,
                                                        required_projection="equirectangular"))))
        assert rows["Resolution"] == "3840x2160 exactly"
        assert rows["Projection / stereo"] == "equirectangular / not enforced"
        assert rows["Decode errors allowed"] == "0"
        assert rows["Loudness"] == "measured and reported, not enforced"  # default "report"

    def test_pdf_and_exports_render_with_and_without_audit_data(self, tmp_path):
        from conftest import make_result
        from frameguard.report import generate_report, write_data_exports
        audited = make_result("a.mov", "warn")
        audited.config = core.config_snapshot(core.DEFAULT_CONFIG)
        audited.tool_versions = {"ffmpeg": {"path": "/x/ffmpeg", "version": "ffmpeg version 9.0"}}
        legacy = make_result("b.mov", "fail")  # built without config (older callers)
        pdf = generate_report([audited, legacy], str(tmp_path / "r.pdf"))
        assert open(pdf, "rb").read(5) == b"%PDF-"
        json_path, csv_path = write_data_exports([audited, legacy], pdf, "Spec")
        assert os.path.getsize(json_path) > 0 and os.path.getsize(csv_path) > 0


# ---------------------------------------------------------------------------
# D4: JSON / CSV export
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestDataExports:
    """D4: reports were PDF only and could not be audited."""

    def test_json_and_csv_carry_checks_config_and_versions(self, media, tmp_path):
        import csv
        from frameguard.report import generate_report, write_data_exports
        clip = media.get("clean_h264.mp4", lambda p: make_clip(p))
        result = _scan(clip, max_decode_errors=3)
        pdf = generate_report([result], str(tmp_path / "r.pdf"), preset_label="Test spec")
        json_path, csv_path = write_data_exports([result], pdf, "Test spec")

        document = json.loads(open(json_path, encoding="utf-8").read())
        record = document["results"][0]
        assert record["file"] == os.path.abspath(clip)
        assert record["config"]["max_decode_errors"] == 3
        assert "use_hwaccel" not in record["config"]
        assert record["tool_versions"]["ffmpeg"].startswith("ffmpeg version")
        assert record["scan_started"] and record["scan_finished"]
        assert {c["name"] for c in record["checks"]} >= {"File Integrity", "CFR Check"}
        rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
        assert {r["record_type"] for r in rows} == {"check", "metadata", "config"}
        assert all(r["file"] == os.path.abspath(clip) for r in rows)
        assert not [p for p in os.listdir(tmp_path) if p.endswith(".part")]
