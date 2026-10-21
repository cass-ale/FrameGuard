"""0.9.1 XR, spatial-audio, colour, frame-rate, aspect, A/V-end and preset checks (REAL FFmpeg).

Spherical V2 (st3d/sv3d) and SA3D boxes are patched into FFmpeg-made MP4s
(container_fixtures) because the FFmpeg CLI cannot write them from scratch.
"""

import json

import pytest

from frameguard import container_checks as cc
from frameguard import presets
from frameguard.core import DEFAULT_CONFIG, PRESETS, preset_config, run_qa_checks
from media_fixtures import MediaLibrary, ffmpeg, inject_spherical, make_clip, needs_ffmpeg
from container_fixtures import add_to_sample_entry, sa3d, st3d, sv3d

pytestmark = needs_ffmpeg

XR_SPEC = {"required_projection": "equirectangular"}


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=160, target_fps=30.0, min_bitrate_mbps=0,
               loudness_mode="off", accepted_containers=[".mp4", ".mov", ".mkv"])
    cfg.update(overrides)
    return cfg


def _scan(path, **overrides):
    result = run_qa_checks(str(path), cfg=_cfg(**overrides), quick=True)
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _has_row(result, name):
    return any(check[0] == name for check in result.checks)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg091x")))


def _copy(src, dst):
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(a.read())
    return str(dst)


def _equirect(media, size="320x160", audio=("stereo",)):
    """A moov-last clip (boxes are patched in place)."""
    key = f"eq_{size}_{'-'.join(audio) or 'none'}.mp4"
    return media.get(key, lambda p: make_clip(p, duration=2, size=size, audio=audio) or p)


def _v2(media, tmp_path, name, mode=0, size="320x160", audio=("stereo",), extra=()):
    path = _copy(_equirect(media, size, audio), tmp_path / name)
    return add_to_sample_entry(path, b"vide", st3d(mode), sv3d(), *extra)


# ---------------------------------------------------------------------------
# XR Metadata extension
# ---------------------------------------------------------------------------

class TestSphericalGenerations:
    def test_v2_mono_passes_an_xr_spec(self, media, tmp_path):
        check = _row(_scan(_v2(media, tmp_path, "v2.mp4"), **XR_SPEC), "XR Metadata")
        assert check[1] == "PASS", check

    def test_v1_only_warns(self, media, tmp_path):
        path = inject_spherical(_copy(_equirect(media), tmp_path / "v1.mp4"))
        check = _row(_scan(path, **XR_SPEC), "XR Metadata")
        assert check[1] == "WARN" and "Spherical V1" in check[2], check

    def test_v1_v2_conflict_fails_an_xr_spec_and_warns_otherwise(self, media, tmp_path):
        path = inject_spherical(_v2(media, tmp_path, "conflict.mp4", mode=0), stereo="top-bottom")
        assert _row(_scan(path, **XR_SPEC), "XR Metadata")[1] == "FAIL"
        relaxed = _row(_scan(path), "XR Metadata")
        assert relaxed[1] == "WARN" and "players disagree" in relaxed[2], relaxed

    def test_flat_file_is_untouched(self, media):
        assert _row(_scan(_equirect(media)), "XR Metadata")[1] == "PASS"


class TestEquirectGeometry:
    def test_16x9_tagged_equirect(self, media, tmp_path):
        path = _v2(media, tmp_path, "wide.mp4", size="320x180")
        check = _row(_scan(path, target_height=180, **XR_SPEC), "XR Metadata")
        assert check[1] == "FAIL" and "1.78:1" in check[2], check
        assert _row(_scan(path, target_height=180), "XR Metadata")[1] == "WARN"

    def test_square_top_bottom_passes_a_tb_spec(self, media, tmp_path):
        path = _v2(media, tmp_path, "tb.mp4", mode=1, size="320x320")
        check = _row(_scan(path, target_height=320, required_stereo_mode="top_bottom", **XR_SPEC),
                     "XR Metadata")
        assert check[1] == "PASS", check

    def test_half_height_top_bottom(self, media, tmp_path):
        path = _v2(media, tmp_path, "tb_half.mp4", mode=1)
        noted = _row(_scan(path, **XR_SPEC), "XR Metadata")
        assert noted[1] == "PASS" and "half-height" in noted[2], noted
        quest = _row(_scan(path, required_stereo_mode="top_bottom", **XR_SPEC), "XR Metadata")
        assert quest[1] == "FAIL" and "square" in quest[2], quest

    def test_side_by_side_360_stereo(self, media, tmp_path):
        path = _v2(media, tmp_path, "sbs.mp4", mode=2, size="640x160")
        check = _row(_scan(path, target_width=640, **XR_SPEC), "XR Metadata")
        assert check[1] == "FAIL" and "top-bottom" in check[2], check

    def test_rotation_on_a_spherical_file(self, media, tmp_path):
        base = _v2(media, tmp_path, "rot_base.mp4")
        rotated = tmp_path / "rot.mp4"
        ffmpeg("-display_rotation", "180", "-i", base, "-c", "copy", "-strict", "unofficial", str(rotated))
        check = _row(_scan(rotated, **XR_SPEC), "XR Metadata")
        assert check[1] == "FAIL" and "rotated 180" in check[2], check
        allowed = _row(_scan(rotated, allow_display_rotation=True, **XR_SPEC), "XR Metadata")
        assert allowed[1] == "PASS", allowed


class TestContentLayout:
    """Advisory pixel check: a top-bottom picture tagged mono (the Quest-critical mislabel)."""

    def _stacked(self, media):
        def build(path):
            # Two 320x80 views stacked: a 2:1 frame, so only the content is wrong.
            make_clip(path, duration=3, size="320x80", video_filter="split[t][b];[t][b]vstack")
            return path
        return media.get("stacked.mp4", build)

    def test_top_bottom_picture_tagged_mono_warns(self, media, tmp_path):
        path = add_to_sample_entry(_copy(self._stacked(media), tmp_path / "tbmono.mp4"), b"vide", st3d(0), sv3d())
        check = _row(_scan(path, **XR_SPEC), "XR Metadata")
        assert check[1] == "WARN" and "looks top-bottom" in check[2] and check[3] < 70, check

    def test_correct_tags_and_mono_pictures_are_quiet(self, media, tmp_path):
        tagged = add_to_sample_entry(_copy(self._stacked(media), tmp_path / "tbok.mp4"), b"vide", st3d(1), sv3d())
        assert "looks top-bottom" not in _row(_scan(tagged, **XR_SPEC), "XR Metadata")[2]
        mono = _v2(media, tmp_path, "mono_content.mp4")
        assert "looks top-bottom" not in _row(_scan(mono, **XR_SPEC), "XR Metadata")[2]

    def test_flat_pictures_are_not_grabbed(self, media, monkeypatch):
        def fail(*args, **kwargs):
            raise AssertionError("no frame grab for a flat file")
        monkeypatch.setattr(cc.FrameGrabber, "gray", fail)
        assert _row(_scan(self._stacked(media)), "XR Metadata")[1] == "PASS"


# ---------------------------------------------------------------------------
# Spatial Audio
# ---------------------------------------------------------------------------

def _quad(media):
    def build(path):
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x160:r=30:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2",
               "-filter_complex", "[1:a]pan=4.0|c0=c0|c1=0.5*c0|c2=0*c0|c3=0.8*c0[a]", "-map", "0:v",
               "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", path)
        return path
    return media.get("quad.mp4", build)


class TestSpatialAudio:
    def test_foa_with_sa3d_passes(self, media, tmp_path):
        path = add_to_sample_entry(_v2_from(media, _quad(media), tmp_path / "foa.mp4"), b"soun", sa3d(4))
        check = _row(_scan(path, **XR_SPEC), "Spatial Audio")
        assert check[1] == "PASS" and "order 1" in check[2], check

    def test_four_channels_without_sa3d_warn(self, media, tmp_path):
        path = _v2_from(media, _quad(media), tmp_path / "nosa3d.mp4")
        check = _row(_scan(path, **XR_SPEC), "Spatial Audio")
        assert check[1] == "WARN" and "SA3D" in check[2], check

    def test_sa3d_channel_mismatch(self, media, tmp_path):
        path = add_to_sample_entry(_v2(media, tmp_path, "mismatch.mp4"), b"soun", sa3d(4))
        assert _row(_scan(path, **XR_SPEC), "Spatial Audio")[1] == "FAIL"
        relaxed = _row(_scan(path, required_projection="any"), "Spatial Audio")
        assert relaxed[1] == "WARN" and "has 2" in relaxed[2], relaxed

    def test_head_locked_and_bad_ordering(self, media, tmp_path):
        bad = add_to_sample_entry(_v2_from(media, _quad(media), tmp_path / "fuma.mp4"), b"soun",
                                  sa3d(4, ordering=1, normalization=1))
        check = _row(_scan(bad, **XR_SPEC), "Spatial Audio")
        assert check[1] == "FAIL" and "ACN" in check[2] and "SN3D" in check[2], check
        assert not cc.sa3d_problems(cc._parse_sa3d(sa3d(6, head_locked=True)[8:]), 6)

    def test_required_spatial_audio(self, media, tmp_path):
        path = _v2(media, tmp_path, "stereo_only.mp4")
        check = _row(_scan(path, spatial_audio="required", **XR_SPEC), "Spatial Audio")
        assert check[1] == "FAIL", check

    def test_flat_file_has_no_spatial_row(self, media):
        assert not _has_row(_scan(_equirect(media)), "Spatial Audio")


def _v2_from(media, src, dst):
    return add_to_sample_entry(_copy(src, dst), b"vide", st3d(0), sv3d())


# ---------------------------------------------------------------------------
# Frame rate (exact rational matching), A/V ends, aspect, colour, rotation
# ---------------------------------------------------------------------------

def _rate_clip(media, rate):
    return media.get(f"rate_{rate.replace('/', '_')}.mp4",
                     lambda p: make_clip(p, duration=2, size="320x160", rate=rate) or p)


class TestFrameRate:
    def test_ntsc_does_not_match_integer_rate(self, media):
        # Never a clean pass; a confident warning by default, a failure under
        # Strict Mode (camera 24p is 23.976 almost everywhere).
        result = _scan(_rate_clip(media, "30000/1001"))
        check = _row(result, "Frame Rate")
        assert check[1] == "WARN" and "NTSC" in check[2] and check[3] >= 90, check
        assert result.overall_status(strict=True) == "MISMATCH"

    def test_ntsc_allowance(self, media):
        check = _row(_scan(_rate_clip(media, "30000/1001"), allow_ntsc_rate_match=True), "Frame Rate")
        assert check[1] == "PASS" and "NTSC" in check[2], check

    def test_tolerance_never_bridges_ntsc(self, media):
        assert _row(_scan(_rate_clip(media, "30000/1001"), fps_tolerance=2.0), "Frame Rate")[1] == "WARN"

    def test_typed_ntsc_target_matches_exactly(self, media):
        check = _row(_scan(_rate_clip(media, "24000/1001"), target_fps=23.976), "Frame Rate")
        assert check[1] == "PASS" and check[3] == 100, check
        assert _row(_scan(_rate_clip(media, "30"), target_fps=29.97), "Frame Rate")[1] == "WARN"

    def test_other_mismatches_and_multiples(self, media):
        assert _row(_scan(_rate_clip(media, "25"), target_fps=24.0), "Frame Rate")[1] == "FAIL"
        assert _row(_scan(_rate_clip(media, "60"), allow_fps_multiples=True), "Frame Rate")[1] == "PASS"
        ntsc_double = _rate_clip(media, "60000/1001")
        assert _row(_scan(ntsc_double, allow_fps_multiples=True), "Frame Rate")[1] == "WARN"
        assert _row(_scan(ntsc_double, allow_fps_multiples=True, allow_ntsc_rate_match=True),
                    "Frame Rate")[1] == "PASS"


def _short_audio(media, seconds, longer=False):
    def build(path):
        video, audio = (2.0, 2.0 + seconds) if longer else (2.0, 2.0 - seconds)
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=320x160:r=30:d={video}", "-f", "lavfi",
               "-i", f"sine=f=440:sample_rate=48000:d={audio}", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "pcm_s16le", path)
        return path
    return media.get(f"av_{'long' if longer else 'short'}_{seconds}.mov", build)


class TestAVEnds:
    @pytest.mark.parametrize("seconds,expected", [(0.03, "PASS"), (0.1, "WARN"), (0.45, "WARN"),
                                                  (0.6, "FAIL")])
    def test_short_audio_tiers(self, media, seconds, expected):
        check = _row(_scan(_short_audio(media, seconds)), "A/V Duration")
        assert check[1] == expected, check

    @pytest.mark.parametrize("seconds,expected", [(1.0, "PASS"), (2.5, "FAIL")])
    def test_long_audio_tiers(self, media, seconds, expected):
        check = _row(_scan(_short_audio(media, seconds, longer=True)), "A/V Duration")
        assert check[1] == expected, check


class TestAspectAndRotation:
    def test_non_square_pixels(self, media, tmp_path):
        path = tmp_path / "sar.mp4"
        make_clip(path, duration=2, size="240x160", video_filter="setsar=4/3")
        check = _row(_scan(path), "Aspect Ratio")
        assert check[1] == "WARN" and "non-square" in check[2], check
        strict = _row(_scan(path, require_square_pixels=True), "Aspect Ratio")
        assert strict[1] == "FAIL", strict

    def test_square_pixels_pass(self, media):
        assert _row(_scan(_equirect(media), require_square_pixels=True), "Aspect Ratio")[1] == "PASS"

    def test_flat_180_rotation_warns_but_90_does_not(self, media, tmp_path):
        for angle, expected in (("180", "WARN"), ("90", None)):
            path = tmp_path / f"rot{angle}.mp4"
            ffmpeg("-display_rotation", angle, "-i", _equirect(media), "-c", "copy", str(path))
            result = _scan(path, target_width=320 if angle == "180" else 160,
                           target_height=160 if angle == "180" else 320)
            check = _row(result, "Resolution")
            if expected:
                assert check[1] == "WARN" and "180" in check[2], check
            else:
                assert check[1] == "PASS", check

    def test_mirrored_display_matrix_warns(self, media, tmp_path):
        path = tmp_path / "flip.mp4"
        ffmpeg("-display_hflip", "-i", _equirect(media), "-c", "copy", str(path))
        check = _row(_scan(path), "Resolution")
        assert check[1] == "WARN" and "mirrored" in check[2], check


def _tagged(media, name, size, primaries=None, trc=None, matrix=None, pix="yuv420p", x265=""):
    """An HEVC clip whose VUI carries exactly the given colour tags (setparams)."""
    def build(path):
        params = ":".join(f"{k}={v}" for k, v in (("color_primaries", primaries), ("color_trc", trc),
                                                  ("colorspace", matrix)) if v)
        vf = ["-vf", f"setparams={params}"] if params else []
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d=1", *vf, "-c:v", "libx265",
               "-preset", "ultrafast", "-x265-params", "log-level=error" + (":" + x265 if x265 else ""),
               "-tag:v", "hvc1", "-pix_fmt", pix, path)
        return path
    return media.get(name, build)


class TestColour:
    def test_untagged_warns_and_can_be_switched_off(self, media):
        path = _tagged(media, "untagged.mp4", "320x160")
        check = _row(_scan(path, audio_required=False), "Color / HDR")
        assert check[1] == "WARN" and "unsignalled" in check[2], check
        assert _row(_scan(path, audio_required=False, warn_untagged_color=False), "Color / HDR")[1] == "PASS"

    def test_bt709_tagged_passes(self, media):
        path = _tagged(media, "bt709.mp4", "320x160", "bt709", "bt709", "bt709")
        assert _row(_scan(path, audio_required=False), "Color / HDR")[1] == "PASS"

    def test_sd_matrix_on_hd(self, media):
        path = _tagged(media, "601hd.mp4", "1280x720", "smpte170m", "smpte170m", "smpte170m")
        check = _row(_scan(path, audio_required=False, target_width=1280, target_height=720), "Color / HDR")
        assert check[1] == "WARN" and "BT.601" in check[2], check

    def test_pq_without_mastering_metadata(self, media):
        path = _tagged(media, "pq.mp4", "320x160", "bt2020", "smpte2084", "bt2020nc", pix="yuv420p10le")
        check = _row(_scan(path, audio_required=False), "Color / HDR")
        assert check[1] == "WARN" and "ST 2086" in check[2], check
        hdr = _row(_scan(path, audio_required=False, required_transfer="pq"), "Color / HDR")
        assert hdr[1] == "FAIL", hdr

    def test_pq_with_mastering_metadata_passes(self, media):
        path = _tagged(media, "pq_md.mp4", "320x160", "bt2020", "smpte2084", "bt2020nc", pix="yuv420p10le",
                       x265="hdr10=1:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                            "WP(15635,16450)L(10000000,1):max-cll=1000,400")
        check = _row(_scan(path, audio_required=False, required_transfer="pq"), "Color / HDR")
        assert check[1] == "PASS", check

    def test_pq_on_8_bit(self, media):
        path = _tagged(media, "pq8.mp4", "320x160", "bt2020", "smpte2084", "bt2020nc")
        check = _row(_scan(path, audio_required=False), "Color / HDR")
        assert check[1] == "WARN" and "8-bit" in check[2], check


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

class TestXRPresets:
    def test_8k_presets(self):
        assert (PRESETS["8k360"]["width"], PRESETS["8k360"]["height"]) == (7680, 3840)
        assert (PRESETS["8k30"]["width"], PRESETS["8k30"]["height"]) == (7680, 4320)
        assert "required_projection" not in PRESETS["8k30"]

    @pytest.mark.parametrize("key,size", [("360tb5760", 5760), ("360tb3840", 3840)])
    def test_top_bottom_presets_are_square_quest_specs(self, key, size):
        cfg = preset_config(key)
        assert (cfg["target_width"], cfg["target_height"]) == (size, size)
        assert cfg["required_stereo_mode"] == "top_bottom"
        assert cfg["allowed_pix_fmts"] == ["yuv420p"] and cfg["require_square_pixels"]
        assert cfg["max_frame_pixels"] == 8192 * 4096 and cfg["max_pixel_rate"] == 8192 * 4096 * 60

    def test_every_builtin_preset_validates(self):
        for key in PRESETS:
            presets.validate_preset_config(preset_config(key))

    def test_bundled_vr_preset_validates(self):
        path = presets.get_bundled_presets_dir() / "vr_360_5.7k.json"
        config = presets.validate_preset_config(json.loads(path.read_text())["config"])
        assert config["require_square_pixels"] is True

    @pytest.mark.parametrize("name,expected", [
        ("TPG_VR_8K_360_C001.mp4", "8k360"), ("vr_8k_master.mov", "8k360"),
        ("show_8k_30p.mp4", "8k30"), ("cam_7680x4320.mov", "8k30"),
        ("export_7680x3840.mp4", "8k360"), ("TB_5760x5760.mp4", "360tb5760"),
        ("A_3840x3840.mp4", "360tb3840"),
        ("PROJ_CAM01_TAKE_20260319_C0360.mp4", None), ("TPG_VR180_7680x3840.mp4", None),
        ("B_5.7K_TB.mp4", None), ("clip_4k_2398.mov", None), ("ep1_1080p_29.97.mov", None),
        ("x_5.7k.mp4", "5.7k30"),
    ])
    def test_filename_tokens(self, name, expected):
        assert presets.detect_preset_from_filename(name, PRESETS) == expected


class TestPresetValidation:
    def test_new_keys_validate(self):
        config = presets.validate_preset_config({
            "allowed_pix_fmts": ["YUV420P"], "allowed_video_profiles": ["High", "Main"],
            "disabled_checks": ["edit list"], "spatial_audio": "Required", "av_offset_warn_ms": 5,
            "max_frame_pixels": 8192 * 4096, "allow_ntsc_rate_match": True})
        assert config["allowed_pix_fmts"] == ["yuv420p"]
        assert config["disabled_checks"] == ["Edit List"]
        assert config["spatial_audio"] == "required"

    @pytest.mark.parametrize("key,value", [
        ("disabled_checks", ["File Integrity"]), ("spatial_audio", "maybe"),
        ("av_offset_max_late_ms", -1), ("max_frame_pixels", 1.5), ("allowed_pix_fmts", []),
        ("max_audio_tracks", 65),
    ])
    def test_bad_values_are_rejected(self, key, value):
        with pytest.raises(presets.PresetValidationError):
            presets.validate_preset_config({key: value})

    def test_empty_disabled_list_is_allowed(self):
        assert presets.validate_preset_config({"disabled_checks": []}) == {"disabled_checks": []}
