"""0.9.2 container / XR / preset / verdict fixes from the independent 0.9.1 review
(r091_container/REPORT.md B1-B19, REVIEW.md section 3), run against the REAL FFmpeg.

Each class is one defect; every test failed on release/0.9.2 before its fix.
"""

import os
import time

import pytest

from frameguard import container_checks as cc
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import MediaLibrary, ffmpeg, make_clip, needs_ffmpeg

pytestmark = needs_ffmpeg


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=320, target_height=240, target_fps=30.0, min_bitrate_mbps=0,
               loudness_mode="off", accepted_containers=[".mp4", ".mov", ".mkv", ".ts"],
               accepted_codecs=["h264", "hevc", "mpeg2video", "prores"])
    cfg.update(overrides)
    return cfg


def _scan(path, quick=True, strict=False, **overrides):
    result = run_qa_checks(str(path), cfg=_cfg(**overrides), quick=quick, strict=strict)
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _rows(result, name):
    return [check for check in result.checks if check[0] == name]


def _not_verified(check):
    return check[1] == "WARN" and str(check[2]).startswith("Not verified")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg092c")))


def _clean(media):
    return media.get("clean.mp4", lambda p: make_clip(p, duration=2, extra=("-movflags", "+faststart"))
                     or p)


# ---------------------------------------------------------------------------
# 1. Container Format: an unrecognised or disguised container never passes (B4)
# ---------------------------------------------------------------------------

def _prefixed(src, dst, junk: bytes):
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(junk + a.read())
    return str(dst)


def _ts_packets(packet: int, junk: int, count: int = 12) -> bytes:
    """Synthetic transport stream: ``junk`` stray bytes, then ``count`` packets
    whose sync byte sits where ISO/IEC 13818-1 (188), BDAV M2TS (4-byte
    timestamp + 188) or 204-byte (188 + 16 Reed-Solomon) framing puts it."""
    body = bytearray()
    for n in range(count):
        pkt = bytearray((n * 7 + k * 13) & 0xFF for k in range(packet))
        sync_at = 4 if packet == 192 else 0
        pkt[sync_at] = 0x47
        # Keep 0x47 out of the payload so only the real sync positions match.
        for k in range(packet):
            if k != sync_at and pkt[k] == 0x47:
                pkt[k] = 0x46
        body += pkt
    return bytes(range(1, junk + 1)) + bytes(body)


class TestContainerSniff:
    def test_junk_prefixed_ts_named_mp4_fails_in_default_and_strict(self, media, tmp_path):
        ts = tmp_path / "src.ts"
        ffmpeg("-i", _clean(media), "-c", "copy", "-f", "mpegts", str(ts))
        disguised = _prefixed(ts, tmp_path / "junk_ts.mp4", b"\x00\x01\x02\x03")
        for strict in (False, True):
            result = _scan(disguised, strict=strict)
            check = _row(result, "Container Format")
            assert check[1] == "FAIL" and "MPEG-TS" in check[2], check
            assert result.status == "MISMATCH", [c[:3] for c in result.checks]

    @pytest.mark.parametrize("packet,junk", [(188, 0), (188, 5), (188, 187), (192, 0), (192, 9),
                                             (204, 0), (204, 33)])
    def test_transport_stream_resync(self, tmp_path, packet, junk):
        path = tmp_path / f"ts_{packet}_{junk}.bin"
        path.write_bytes(_ts_packets(packet, junk))
        family, detail = cc.sniff_container(str(path))
        assert family == "MPEG-TS" and str(packet) in detail, (family, detail)
        assert ("junk" in detail) == (junk > 0), detail

    def test_flv_and_program_stream_named_mp4_fail(self, media, tmp_path):
        flv = tmp_path / "flv.mp4"
        ffmpeg("-i", _clean(media), "-c:v", "copy", "-c:a", "aac", "-f", "flv", str(flv))
        check = _row(_scan(flv), "Container Format")
        assert check[1] == "FAIL" and "FLV" in check[2], check
        ps = tmp_path / "ps.mp4"
        ffmpeg("-i", _clean(media), "-c:v", "mpeg2video", "-c:a", "mp2", "-f", "vob", str(ps))
        check = _row(_scan(ps, accepted_codecs=["mpeg2video"]), "Container Format")
        assert check[1] == "FAIL" and "MPEG-PS" in check[2], check

    def test_unrecognised_signature_is_judged_by_ffprobe_format(self, media, tmp_path):
        """A raw H.264 elementary stream has no container signature; ffprobe
        names it, and that must contradict the .mp4 name."""
        raw = tmp_path / "raw.mp4"
        ffmpeg("-i", _clean(media), "-an", "-c:v", "copy", "-f", "h264", str(raw))
        assert cc.sniff_container(str(raw))[0] == "unknown"
        check = _row(_scan(raw), "Container Format")
        assert check[1] == "FAIL" and "h264" in check[2].lower(), check

    def test_unreadable_unknown_file_is_never_a_container_pass(self, tmp_path):
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(bytes(range(256)) * 64)
        result = _scan(junk)
        rows = _rows(result, "Container Format")
        assert rows and all(r[1] != "PASS" for r in rows), rows
        assert result.status == "MISMATCH"

    def test_real_containers_still_pass(self, media, tmp_path):
        mkv = tmp_path / "real.mkv"
        ts = tmp_path / "real.ts"
        ffmpeg("-i", _clean(media), "-c", "copy", str(mkv))
        ffmpeg("-i", _clean(media), "-c", "copy", str(ts))
        for path in (_clean(media), mkv, ts):
            check = _row(_scan(path), "Container Format")
            assert check[1] == "PASS", (path, check)


# ---------------------------------------------------------------------------
# 2. A/V Sync Offset: unsignalled AAC priming is judged over its whole range (B5)
# ---------------------------------------------------------------------------

def _moov_last(media):
    return media.get("moovlast.mp4", lambda p: make_clip(p, duration=2) or p)


def _video_delayed(media, milliseconds):
    """Sound ``milliseconds`` early: an empty edit delays the picture (the
    audio track keeps its encoder-priming edit)."""
    from container_fixtures import set_edit_list

    def build(path):
        with open(_moov_last(media), "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
        structure = cc.read_structure(path)
        edit = structure.tracks_of("vide")[0].edits[0]
        lead = round(milliseconds * structure.movie_timescale / 1000)
        set_edit_list(path, b"vide", [(lead, -1, 1), (edit.segment_duration, edit.media_time, 1)])
        return path
    return media.get(f"video_late_{milliseconds}.mp4", build)


def _sync_probe(audio_start, profile="LC", padding=0, video_start=0.0):
    return {"format": {"format_name": "mpegts"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": str(video_start)},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "profile": profile,
                 "sample_rate": "48000", "channels": 2, "initial_padding": padding,
                 "start_time": str(audio_start)}]}


class _Rows:
    def __init__(self):
        self.checks, self.metadata = [], {}

    def add_pass(self, name, detail="", confidence=100):
        self.checks.append((name, "PASS", detail, confidence))

    def add_warn(self, name, detail="", confidence=100):
        self.checks.append((name, "WARN", detail, confidence))

    def add_fail(self, name, detail="", confidence=100):
        self.checks.append((name, "FAIL", detail, confidence))

    def add_skip(self, name, detail="", confidence=100):
        self.checks.append((name, "SKIP", detail, confidence))

    def add_not_verified(self, name, reason, by_choice=False, inherent=False):
        self.checks.append((name, "WARN", "Not verified: " + reason, 100))
        if by_choice or inherent:
            self.metadata.setdefault("strict_exempt", []).append(name)

    def set_metadata(self, key, value):
        self.metadata[key] = value


def _sync_row(probe, structure=None, **cfg):
    rows = _Rows()
    cc.check_av_sync_offset(structure, probe, rows, _cfg(**cfg))
    return rows.checks[0], rows.metadata


class TestAacPrimingRange:
    def test_transport_stream_sound_early_beyond_its_priming_is_not_a_pass(self, media, tmp_path):
        """FFmpeg's TS audio starts one AAC frame (21 ms) early; 20 ms more
        lead used to be absorbed by the 44 ms 'most favourable' window."""
        early = tmp_path / "early.ts"
        ffmpeg("-i", _clean(media), "-itsoffset", "-0.020", "-i", _clean(media), "-map", "0:v",
               "-map", "1:a", "-c", "copy", "-f", "mpegts", str(early))
        check = _row(_scan(early, accepted_containers=[".ts"]), "A/V Sync Offset")
        # A -41 ms lead is no priming in use, and compliant sync is still possible
        # inside the unknown range: Not verified (round 2, bug 6), never PASS.
        assert _not_verified(check) and "not signalled" in check[2], check
        known = _row(_scan(early, accepted_containers=[".ts"], aac_priming=1024), "A/V Sync Offset")
        assert known[1] == "WARN" and "20 ms before" in known[2], known

    def test_known_encoder_priming_makes_the_offset_exact(self):
        assert _sync_row(_sync_probe(-1024 / 48000), aac_priming=1024)[0][1] == "PASS"
        assert _sync_row(_sync_probe(-1024 / 48000), aac_priming=2112)[0][1] == "WARN"
        he, _ = _sync_row(_sync_probe(-2 * 2112 / 48000, profile="HE-AAC"), aac_priming=2112)
        assert he[1] == "PASS", he

    def test_priming_setting_follows_the_preset_pattern(self):
        from frameguard import presets
        from frameguard.report import spec_rows
        assert DEFAULT_CONFIG["aac_priming"] == "unknown"
        assert presets.validate_preset_config({"aac_priming": "1024"}) == {"aac_priming": 1024}
        with pytest.raises(presets.PresetValidationError):
            presets.validate_preset_config({"aac_priming": 999})
        assert presets.normalize_preset_config({"av_offset_warn_ms": 5}) == {"av_offset_warn_ms": 5}
        rows = dict(spec_rows(dict(DEFAULT_CONFIG)))
        assert "0-2624 samples" in rows["A/V start offset"], rows

    def test_he_aac_window_no_longer_hides_88_ms(self):
        # 90 ms is no HE priming in use (88 ms = 2 x 2112 is, and is inferred:
        # TestAacPrimingDefault): it is never "start together".
        check, meta = _sync_row(_sync_probe(-0.090, profile="HE-AAC"))
        assert _not_verified(check) and "90" in check[2], check
        assert meta["av_start_offset_range_ms"][0] == -90.0, meta

    def test_whole_range_beyond_r37_fails_and_whole_range_late_fails(self):
        assert _sync_row(_sync_probe(-0.110))[0][1] == "FAIL"     # -110 .. -55 ms: all beyond 40 early
        assert _sync_row(_sync_probe(0.070))[0][1] == "FAIL"      # priming only makes late later
        assert _sync_row(_sync_probe(-0.060))[0][1] == "WARN"     # -60 .. -5.3 ms: no compliant value

    def test_priming_is_read_from_the_audio_edit_list(self, media, monkeypatch):
        """ffprobe 6/7 report initial_padding=0 for an edit-listed AAC MP4;
        the audio track's own edit (media_time = priming) signals it."""
        from frameguard import core
        real = core.run_ffprobe

        def no_padding(*args, **kwargs):
            data = real(*args, **kwargs)
            for stream in data["streams"]:
                stream.pop("initial_padding", None)
            return data
        monkeypatch.setattr(core, "run_ffprobe", no_padding)
        check = _row(_scan(_video_delayed(media, 35)), "A/V Sync Offset")
        assert check[1] == "WARN" and "35 ms before" in check[2], check
        assert _row(_scan(_clean(media)), "A/V Sync Offset")[1] == "PASS"

    def test_a_trims_audio_edit_also_signals_the_priming(self, media, monkeypatch):
        """FFmpeg 8 reports initial_padding=0 for a stream-copy trim; the audio
        edit that hides the trimmed lead-in hides the priming with it."""
        from frameguard import core
        from media_fixtures import stream_copy_trim
        real = core.run_ffprobe

        def no_padding(*args, **kwargs):
            data = real(*args, **kwargs)
            for stream in data["streams"]:
                stream.pop("initial_padding", None)
            return data
        source = media.get("trim_src.mp4", lambda p: make_clip(p, duration=6) or p)
        trimmed = media.get("trimmed.mp4", lambda p: stream_copy_trim(source, p, 1.37, 3))
        monkeypatch.setattr(core, "run_ffprobe", no_padding)
        check = _row(_scan(trimmed), "A/V Sync Offset")
        assert "not signalled" not in check[2], check

    def test_signalled_priming_is_exact(self):
        check, _ = _sync_row(_sync_probe(0.0, padding=1024))
        assert check[1] == "PASS", check


# ---------------------------------------------------------------------------
# 3. Stereo layout from the picture: disparity-independent, multi-frame (B6)
# ---------------------------------------------------------------------------

def _copy(src, dst):
    with open(src, "rb") as a, open(dst, "wb") as b:
        b.write(a.read())
    return str(dst)


def _tagged(path, mode):
    from container_fixtures import add_to_sample_entry, st3d, sv3d
    return add_to_sample_entry(path, b"vide", st3d(mode), sv3d())


XR_SPEC = {"required_projection": "equirectangular", "audio_required": False}


class TestStereoContentCheck:
    """Validated on depth-varying stereo scenes (stereo_fixtures), not on a
    picture stacked on itself: sky at infinity, a ground plane whose
    disparity grows towards the viewer, occluding objects at their own
    depths, sensor noise and a gain mismatch between the eyes."""

    @pytest.mark.parametrize("disparity", [0, 1.5, 3, 4.5, 6, 7.5, 9, 12, 18])
    def test_top_bottom_found_at_any_disparity(self, disparity):
        import stereo_fixtures as sf
        hits = 0
        for seed in (11, 12):
            frame = sf.top_bottom(sf.Scene(384, 192, seed, disparity), swapped=seed % 2 == 0, eye_height=96)
            evidence = cc.stereo_layout_evidence([frame])
            hits += bool(evidence and evidence[0])
        assert hits == 2, disparity

    @pytest.mark.parametrize("kind", ["mono", "reflection", "periodic"])
    def test_mono_pictures_are_not_called_stereo(self, kind):
        import stereo_fixtures as sf
        for seed in (21, 22, 23):
            scene = sf.Scene(384, 192, seed, 10)
            frame = {"mono": lambda: sf.mono(scene), "reflection": lambda: sf.reflection(scene),
                     "periodic": lambda: sf.periodic(384, 192, seed)}[kind]()
            evidence = cc.stereo_layout_evidence([frame])
            assert not (evidence and evidence[0]), (kind, seed, evidence)

    def _encoded(self, media, name, pixels, width, height):
        import stereo_fixtures as sf
        return media.get(name, lambda p: sf.encode(pixels(), width, height, p))

    @pytest.mark.parametrize("disparity", [9, 24])
    def test_half_height_top_bottom_tagged_mono_warns(self, media, tmp_path, disparity):
        """768x384 half-height TB (the Quest-critical mislabel geometry cannot
        see): 9 source px = 4.5 grab px, where 0.9.1 never fired."""
        import stereo_fixtures as sf
        scene = sf.Scene(768, 384, 30 + disparity, disparity)
        clip = self._encoded(media, f"tb_half_{disparity}.mp4", lambda: sf.top_bottom(scene, eye_height=192),
                             768, 384)
        path = _tagged(_copy(clip, tmp_path / "tb_mono.mp4"), 0)
        check = _row(_scan(path, target_width=768, target_height=384, **XR_SPEC), "XR Metadata")
        assert check[1] == "WARN" and "looks top-bottom" in check[2] and check[3] < 70, check

    def test_natural_mono_equirect_is_quiet(self, media, tmp_path):
        import stereo_fixtures as sf
        scene = sf.Scene(768, 384, 41, 12)
        clip = self._encoded(media, "mono_eq.mp4", lambda: sf.mono(scene), 768, 384)
        path = _tagged(_copy(clip, tmp_path / "mono.mp4"), 0)
        check = _row(_scan(path, target_width=768, target_height=384, **XR_SPEC), "XR Metadata")
        assert check[1] == "PASS" and "looks top-bottom" not in check[2], check

    def test_several_frames_are_compared(self, media, tmp_path, monkeypatch):
        import stereo_fixtures as sf
        scene = sf.Scene(768, 384, 51, 12)
        clip = self._encoded(media, "mono_eq_51.mp4", lambda: sf.mono(scene), 768, 384)
        path = _tagged(_copy(clip, tmp_path / "frames.mp4"), 0)
        seen = []
        real = cc.FrameGrabber.gray

        def spy(self, index, at):
            seen.append(at)
            return real(self, index, at)
        monkeypatch.setattr(cc.FrameGrabber, "gray", spy)
        _scan(path, target_width=768, target_height=384, **XR_SPEC)
        assert len(seen) == 3 and len(set(seen)) == 3, seen


# ---------------------------------------------------------------------------
# 3b. 180 deg per-eye geometry (B9)
# ---------------------------------------------------------------------------

QUARTER = 1 << 30   # 0.25 in the 0.32 fixed point of equi bounds


class TestHalfSphereGeometry:
    def _vr180(self, media, tmp_path, name, size, mode):
        from container_fixtures import add_to_sample_entry, st3d, sv3d
        clip = media.get(f"vr180_{size}.mp4", lambda p: make_clip(p, duration=2, size=size) or p)
        path = _copy(clip, tmp_path / name)
        return add_to_sample_entry(path, b"vide", st3d(mode), sv3d(bounds=(0, 0, QUARTER, QUARTER)))

    def test_180_with_2to1_eyes_is_flagged(self, media, tmp_path):
        path = self._vr180(media, tmp_path, "vr180_bad.mp4", "320x160", 0)
        check = _row(_scan(path, target_width=320, target_height=160, audio_required=False), "XR Metadata")
        assert check[1] == "WARN" and "180°" in check[2] and "1:1" in check[2], check

    def test_180_side_by_side_square_eyes_passes(self, media, tmp_path):
        path = self._vr180(media, tmp_path, "vr180_ok.mp4", "640x320", 2)
        check = _row(_scan(path, target_width=640, target_height=320, audio_required=False), "XR Metadata")
        assert check[1] == "PASS" and "tiled" in check[2], check

    def test_half_equirect_projection_is_judged_as_180(self):
        hard, soft, _ = cc.xr_metadata_findings(
            {"width": 4096, "height": 2048}, "half_equirectangular", "mono", None, {})
        assert any("180°" in text for text in soft), soft


# ---------------------------------------------------------------------------
# 4. An unread moov is Not verified everywhere it matters (B7, B8)
# ---------------------------------------------------------------------------

def _foa_v2(media, tmp_path, name):
    """Spherical V2 mono equirect with a 4-channel AAC track and a valid SA3D box."""
    from container_fixtures import add_to_sample_entry, sa3d, st3d, sv3d

    def build(path):
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x160:r=30:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2",
               "-filter_complex", "[1:a]pan=4.0|c0=c0|c1=0.5*c0|c2=0*c0|c3=0.8*c0[a]", "-map", "0:v",
               "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", path)
        return path
    path = _copy(media.get("foa.mp4", build), tmp_path / name)
    add_to_sample_entry(path, b"vide", st3d(0), sv3d())
    return add_to_sample_entry(path, b"soun", sa3d(4))


class TestUnreadMoov:
    def test_moov_over_the_cap_is_not_a_structure_pass(self, media, monkeypatch):
        monkeypatch.setattr(cc, "MAX_MOOV_BYTES", 64)
        check = _row(_scan(_clean(media)), "Container Structure")
        assert _not_verified(check) and "moov" in check[2], check
        assert "every box and sample" not in check[2], check

    def test_box_level_xr_and_spatial_audio_are_not_verified(self, media, tmp_path, monkeypatch):
        path = _foa_v2(media, tmp_path, "foa_v2.mp4")
        spec = dict(target_width=320, target_height=160, required_projection="equirectangular",
                    spatial_audio="required", accepted_audio_codecs=["aac"])
        clean = _scan(path, **spec)
        assert _row(clean, "Spatial Audio")[1] == "PASS", _row(clean, "Spatial Audio")
        monkeypatch.setattr(cc, "MAX_MOOV_BYTES", 64)
        result = _scan(path, **spec)
        spatial = _row(result, "Spatial Audio")
        assert _not_verified(spatial), spatial          # was a false FAIL: "no track carries a valid SA3D"
        xr = _row(result, "XR Metadata")
        assert xr[1] != "PASS" and "not verified" in xr[2].lower(), xr

    def test_timecode_track_is_not_verified(self, media, tmp_path, monkeypatch):
        mov = tmp_path / "tc.mov"
        ffmpeg("-i", _clean(media), "-c", "copy", "-timecode", "01:00:00:00", str(mov))
        assert _row(_scan(mov), "Timecode")[1] == "PASS"
        monkeypatch.setattr(cc, "MAX_MOOV_BYTES", 64)
        check = _row(_scan(mov), "Timecode")
        assert _not_verified(check), check


# ---------------------------------------------------------------------------
# 5. Presets: cross-field validation (B15); switched-off essentials (B16)
# ---------------------------------------------------------------------------

class TestPresetCrossFields:
    @pytest.mark.parametrize("config,key,value", [
        ({"av_offset_warn_ms": 500, "av_offset_max_early_ms": 40}, "av_offset_warn_ms", 40),   # review repro
        ({"av_offset_warn_ms": 70, "av_offset_max_late_ms": 60}, "av_offset_warn_ms", 60),
        ({"av_offset_max_late_ms": 5000}, "av_offset_max_late_ms", 1000),                       # above the cap
        ({"max_frame_pixels": 1000, "target_width": 3840, "target_height": 2160}, "max_frame_pixels", 1000),
        ({"max_pixel_rate": 1_000_000, "target_width": 1920, "target_height": 1080, "target_fps": 30},
         "max_pixel_rate", 1_000_000),
    ])
    def test_contradictory_presets_load_adjusted_with_a_recorded_notice(self, config, key, value):
        """Round 2, bug 7: 0.9.1 loaded these; refusing them broke 'old presets
        still load'. The loosening value is clamped (a contradiction that
        loosens nothing is kept) and every change is recorded."""
        from frameguard import presets
        out = presets.validate_preset_config(config)
        assert out[key] == value, out
        assert out["preset_adjustments"] and key in out["preset_adjustments"][0], out

    def test_values_0_9_1_also_refused_are_still_refused(self):
        from frameguard import presets
        for bad in ({"av_offset_max_late_ms": 20000}, {"av_offset_warn_ms": "loud"}, {"no_such_key": 1}):
            with pytest.raises(presets.PresetValidationError):
                presets.validate_preset_config(bad)

    def test_consistent_presets_still_load(self):
        from frameguard import presets
        from frameguard.core import PRESETS, preset_config
        out = presets.validate_preset_config({"av_offset_warn_ms": 5, "av_offset_max_early_ms": 40,
                                              "av_offset_max_late_ms": 60})
        assert "preset_adjustments" not in out, out
        for key in PRESETS:
            assert "preset_adjustments" not in presets.validate_preset_config(preset_config(key)), key

    def test_saved_settings_adjust_a_contradiction_instead_of_loosening(self):
        from frameguard import presets
        out = presets.normalize_preset_config({"av_offset_warn_ms": 500, "av_offset_max_early_ms": 40,
                                               "target_fps": 25})
        assert out["av_offset_warn_ms"] == 40 and out["av_offset_max_early_ms"] == 40, out

    def test_engine_never_lifts_the_fail_threshold_to_the_warn_threshold(self):
        check, _ = _sync_row(_sync_probe(-0.045, padding=1024), av_offset_warn_ms=500,
                             av_offset_max_early_ms=40)
        assert check[1] == "FAIL", check


class TestSwitchedOffEssentials:
    ESSENTIAL_OFF = ["A/V Sync Offset", "Codec Profile", "Spatial Audio"]

    def test_switched_off_essentials_are_not_verified_not_skipped(self, media):
        result = _scan(_clean(media), quick=False, disabled_checks=self.ESSENTIAL_OFF + ["Edit List"])
        for name in ("A/V Sync Offset", "Codec Profile"):
            check = _row(result, name)
            assert _not_verified(check) and "switched off" in check[2], check
        assert _row(result, "Edit List")[1] == "SKIP"            # advisory: still a plain skip
        assert result.status == "PASS_WITH_WARNINGS", [c[:3] for c in result.checks if c[1] != "PASS"]

    def test_the_spec_choice_does_not_escalate_in_strict_mode(self, media):
        result = _scan(_clean(media), quick=False, strict=True, disabled_checks=["Codec Profile"])
        assert result.status != "MISMATCH", [c[:3] for c in result.checks if c[1] != "PASS"]

    def test_pdf_spec_row_names_switched_off_essentials(self):
        from frameguard.report import spec_rows
        rows = dict(spec_rows(dict(DEFAULT_CONFIG, disabled_checks=["Codec Profile", "Edit List"])))
        assert "Codec Profile (essential: not verified)" in rows["Checks switched off"], rows


# ---------------------------------------------------------------------------
# 6. Quest presets: codecs Meta names for 180/360 (B17); Media Studio's mono max (B18)
# ---------------------------------------------------------------------------

QUEST_360 = ("8k360", "360tb5760", "360tb3840")


def _video_probe(codec, profile, width, height, fps="30/1", level=-99):
    return {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": codec, "profile": profile,
                         "pix_fmt": "yuv420p", "width": width, "height": height, "avg_frame_rate": fps,
                         "r_frame_rate": fps, "level": level}]}


class TestQuestCodecs:
    @pytest.mark.parametrize("key", QUEST_360)
    def test_h264_is_refused_at_quest_360_sizes(self, key):
        from frameguard import core
        cfg = dict(DEFAULT_CONFIG, **core.preset_config(key))
        rows = _Rows()
        core.check_video_codec(_video_probe("h264", "High", cfg["target_width"], cfg["target_height"]), rows, cfg)
        assert rows.checks[0][1] == "FAIL", rows.checks

    @pytest.mark.parametrize("key", QUEST_360)
    def test_hevc_main_and_av1_main_pass_and_444_profiles_fail(self, key):
        from frameguard import core
        cfg = dict(DEFAULT_CONFIG, **core.preset_config(key))
        size = (cfg["target_width"], cfg["target_height"])
        for codec, profile in (("hevc", "Main"), ("av1", "Main")):
            rows = _Rows()
            core.check_video_codec(_video_probe(codec, profile, *size), rows, cfg)
            cc.check_codec_profile(_video_probe(codec, profile, *size), rows, cfg)
            assert [c[1] for c in rows.checks] == ["PASS", "PASS"], rows.checks
        rows = _Rows()
        cc.check_codec_profile(_video_probe("hevc", "Rext", *size), rows, cfg)
        assert rows.checks[0][1] == "FAIL", rows.checks


class TestQuestMonoPresets:
    def test_8k360_is_the_horizon_streaming_target(self):
        from frameguard.core import PRESETS
        preset = PRESETS["8k360"]
        assert (preset["width"], preset["height"]) == (7680, 3840)
        assert "Horizon" in preset["label"] and preset["min_bitrate_mbps"] == 50, preset

    def test_media_studio_mono_maximum_has_its_own_preset(self):
        from frameguard import presets
        from frameguard.core import PRESETS, preset_config
        preset = PRESETS["360mono7200"]
        assert (preset["width"], preset["height"]) == (7200, 3600)
        assert "Media Studio" in preset["label"] and preset["min_bitrate_mbps"] == 25, preset
        cfg = preset_config("360mono7200")
        assert cfg["required_stereo_mode"] == "mono" and cfg["required_projection"] == "equirectangular"
        presets.validate_preset_config(cfg)
        assert presets.detect_preset_from_filename("TPG_360_7200x3600.mp4", PRESETS) == "360mono7200"


# ---------------------------------------------------------------------------
# 7. FFmpeg version gate
# ---------------------------------------------------------------------------

def _fake_ffmpeg(directory, version: str, libavfilter: str = "") -> str:
    """An ffmpeg that reports ``version`` (and a libavfilter line) for
    -version and runs the real one otherwise, so the gate is tested without
    installing old builds."""
    from media_fixtures import FFMPEG
    path = os.path.join(str(directory), "ffmpeg")
    lib = f'; echo "libavfilter    {libavfilter} / {libavfilter}"' if libavfilter else ""
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n"
                     f'if [ "$1" = "-version" ]; then echo "ffmpeg version {version} Copyright (c) the FFmpeg '
                     f'developers"{lib}; exit 0; fi\nexec "{FFMPEG}" "$@"\n')
    os.chmod(path, 0o755)
    return path


needs_posix = pytest.mark.skipif(os.name == "nt", reason="shell-script FFmpeg stand-in")


class TestFFmpegVersionGate:
    def test_minimum_is_the_oldest_build_the_suite_passes_on(self):
        from frameguard import tools
        assert tools.MIN_VERSIONS["ffmpeg"] == tools.MIN_VERSIONS["ffprobe"] == (9, 0, 2)

    @pytest.mark.parametrize("banner,ok", [
        ("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright", False),
        ("ffmpeg version 7.0.2 Copyright (c) 2000-2024", False),
        ("ffmpeg version 7.1.1 Copyright (c) 2000-2025", False),
        ("ffmpeg version 8.0.1 Copyright (c) 2000-2025", False),
        ("ffmpeg version 9.0.2-https://www.martin-riedl.de Copyright", True),
        ("ffmpeg version n9.1 Copyright", True),
    ])
    def test_banner_against_the_minimum(self, banner, ok):
        from frameguard import tools
        assert tools.ffmpeg_banner_problem(banner) is None if ok else tools.ffmpeg_banner_problem(banner)

    @needs_posix
    def test_old_ffmpeg_refuses_frame_level_checks_as_not_verified(self, media, tmp_path, monkeypatch):
        from frameguard import core
        fake = _fake_ffmpeg(tmp_path, "7.1.1")
        monkeypatch.setattr(core, "get_ffmpeg_path", lambda: fake)
        result = _scan(_clean(media), quick=False)
        integrity = _row(result, "File Integrity")
        assert _not_verified(integrity) and "7.1.1" in integrity[2] and "9.0.2" in integrity[2], integrity
        for name in ("Black Frames", "Audio Silence", "Audio Hum"):
            assert _not_verified(_row(result, name)), _row(result, name)
        assert result.status == "PASS_WITH_WARNINGS"
        assert result.metadata["ffmpeg_below_minimum"] == "7.1.1"

    @needs_posix
    def test_version_is_detected_per_scan_when_the_binary_changes(self, tmp_path):
        from frameguard import tools
        fake = _fake_ffmpeg(tmp_path, "8.0.1")
        assert "8.0.1" in tools.ffmpeg_version_problem(fake)
        time.sleep(0.01)
        _fake_ffmpeg(tmp_path, "9.0.2.1")         # replaced in place (new size and mtime)
        assert tools.ffmpeg_version_problem(fake) is None

    def test_pdf_tools_line_shows_the_version_and_the_gate(self):
        from frameguard.core import QAResult
        from frameguard.report import _tools_line
        result = QAResult("a.mp4")
        result.tool_versions = {"ffmpeg": {"version": "ffmpeg version 7.1.1 Copyright (c) 2000-2025"},
                                "ffprobe": {"version": "ffprobe version 7.1.1 Copyright (c) 2000-2025"}}
        result.set_metadata("ffmpeg_below_minimum", "7.1.1")
        line = _tools_line([result])
        assert "ffmpeg version 7.1.1" in line and "below the 9.0.2 minimum" in line, line


# ---------------------------------------------------------------------------
# 9. Remaining low-severity container items (B10, B11, B12, B14, B19, R1.1 brand)
# ---------------------------------------------------------------------------

class TestMissingMediaText:
    def test_no_negative_seconds_when_mdhd_is_shorter_than_the_samples(self):
        """B12: '... -1.00 s ...' when the declared duration ends before the missing sample."""
        track = cc.TrackInfo(track_id=1, handler="vide", timescale=1000, duration=500, sample_count=100,
                             first_missing_sample=60)
        track.stts = [(100, 33)]
        text = cc._missing_media_text(cc.Structure(family="ISO-BMFF", tracks=[track]))[0]
        assert "-" not in text.split("(")[1], text


def _matrix_stream(a, b, c, d, rotation):
    text = (f"\n00000000: {a:12d} {b:12d}           0\n00000001: {c:12d} {d:12d}           0\n"
            f"00000002:            0            0  1073741824\n")
    return {"side_data_list": [{"side_data_type": "Display Matrix", "displaymatrix": text, "rotation": rotation}]}


class TestDisplayTransform:
    def test_a_horizontal_flip_is_a_mirror_not_a_rotation(self):
        """B11: ffprobe reports rotation -180 for an hflip matrix; that is a mirror only."""
        assert cc.display_transform(_matrix_stream(-65536, 0, 0, 65536, -180)) == (0, True)

    def test_a_vertical_flip_is_an_upside_down_mirror(self):
        """ffprobe reports rotation 0 for a vflip matrix: it is rotated 180 degrees and mirrored."""
        assert cc.display_transform(_matrix_stream(65536, 0, 0, -65536, 0)) == (180, True)

    def test_unflipped_rotation_is_unchanged(self):
        assert cc.display_transform(_matrix_stream(0, 65536, -65536, 0, -90)) == (-90, False)


class TestRightLeftStereo:
    def test_st3d_mode_4_is_side_by_side_with_the_eyes_reversed(self, media, tmp_path):
        """B10: FFmpeg ignores stereo_mode 4 (right-left), so the row read
        'no stereo metadata (mono)'; the box reader names the layout and the
        eye order."""
        from container_fixtures import add_to_sample_entry, st3d, sv3d
        clip = media.get("vr180_640x320.mp4", lambda p: make_clip(p, duration=2, size="640x320") or p)
        path = add_to_sample_entry(_copy(clip, tmp_path / "rl.mp4"), b"vide", st3d(4),
                                   sv3d(bounds=(0, 0, QUARTER, QUARTER)))
        check = _row(_scan(path, target_width=640, target_height=320, audio_required=False), "XR Metadata")
        assert "side_by_side" in check[2] and "right eye" in check[2], check
        assert "does not fit" not in check[2], check


def _iso_audio_v1_file(path, brand: bytes, quicktime_layout: bool):
    """A minimal moov whose audio sample entry is version 1 with an SA3D child."""
    import struct as _s
    from container_fixtures import box, full_box, sa3d
    head = bytes(6) + _s.pack(">H", 1) + _s.pack(">H", 1) + bytes(6) + _s.pack(">HH", 4, 16) + bytes(4) \
        + _s.pack(">I", 48000 << 16)
    if quicktime_layout:
        head += bytes(16)     # QuickTime SoundDescription v1: four extra 32-bit fields
    entry = box(b"mp4a", head + sa3d(4))
    stsd = full_box(b"stsd", 0, 0, _s.pack(">I", 1) + entry)
    hdlr = full_box(b"hdlr", 0, 0, bytes(4) + b"soun" + bytes(13))
    mdhd = full_box(b"mdhd", 0, 0, _s.pack(">IIII", 0, 0, 48000, 48000) + bytes(4))
    tkhd = full_box(b"tkhd", 0, 0, _s.pack(">III", 0, 0, 1) + bytes(68))
    trak = box(b"trak", tkhd + box(b"mdia", mdhd + hdlr + box(b"minf", box(b"stbl", stsd))))
    mvhd = full_box(b"mvhd", 0, 0, _s.pack(">IIII", 0, 0, 1000, 1000) + bytes(80))
    data = box(b"ftyp", brand + bytes(4) + brand) + box(b"moov", mvhd + trak)
    with open(path, "wb") as handle:
        handle.write(data)
    return str(path)


class TestAudioSampleEntryV1:
    def test_sa3d_in_an_iso_v1_audio_entry_is_found(self, tmp_path):
        """B14: ISO AudioSampleEntryV1 keeps the 28-byte layout; the QuickTime
        v1 44-byte skip missed SA3D in it."""
        structure = cc.read_structure(_iso_audio_v1_file(tmp_path / "iso.mp4", b"isom", False))
        assert structure.tracks[0].entries[0].sa3d is not None, structure.tracks[0].entries

    def test_quicktime_v1_audio_entry_still_reads(self, tmp_path):
        structure = cc.read_structure(_iso_audio_v1_file(tmp_path / "qt.mov", b"qt  ", True))
        assert structure.tracks[0].entries[0].sa3d is not None, structure.tracks[0].entries


class TestFilenameSuggestions:
    @pytest.mark.parametrize("name,expected", [
        ("promo_360_7680x4320.mp4", None),      # an explicit 16:9 size contradicts 360
        ("promo_4K_360.mp4", None),             # a 4K 360 is not the flat 4K UHD spec
        ("tour_vr_1080p.mp4", None),
        ("TPG_VR_8K_360_C001.mp4", "8k360"),    # bare 8K next to 360 is still 8K 360
        ("show_8k_30p.mp4", "8k30"),
        ("clip_4k_30p.mov", "4k30"),
    ])
    def test_spherical_tokens_never_suggest_a_flat_preset(self, name, expected):
        """B19."""
        from frameguard import presets
        from frameguard.core import PRESETS
        assert presets.detect_preset_from_filename(name, PRESETS) == expected


class TestQuickTimeBrandNamedMp4:
    def test_quicktime_movie_named_mp4_is_flagged(self, media, tmp_path):
        """R1.1 (review section 1, NOT DONE): a 'qt  ' brand with no ISO brand
        in a file named .mp4 passed silently."""
        mov = tmp_path / "movie.mov"
        ffmpeg("-i", _clean(media), "-c", "copy", str(mov))
        renamed = tmp_path / "movie.mp4"
        os.rename(mov, renamed)
        check = _row(_scan(renamed), "Container Structure")
        assert check[1] == "WARN" and "QuickTime" in check[2], check

    def test_real_mp4_and_mov_are_quiet(self, media, tmp_path):
        mov = tmp_path / "movie.mov"
        ffmpeg("-i", _clean(media), "-c", "copy", str(mov))
        for path in (_clean(media), mov):
            assert _row(_scan(path), "Container Structure")[1] == "PASS", path


# ---------------------------------------------------------------------------
# Round 2, bug 6: the unknown-priming default
# ---------------------------------------------------------------------------

class TestAacPrimingDefault:
    """WARN/FAIL only when no priming value in use can explain the offset;
    a range that allows compliant sync is Not verified and Strict does not
    escalate it; a PTS lead that equals a priming in use is that priming."""

    def test_ffmpeg_transport_stream_passes_by_its_pts_lead(self, media, tmp_path):
        ts = tmp_path / "cam.ts"
        ffmpeg("-i", _clean(media), "-c:v", "copy", "-c:a", "aac", "-f", "mpegts", str(ts))
        result = _scan(ts, strict=True, accepted_containers=[".ts"])
        check = _row(result, "A/V Sync Offset")
        assert check[1] == "PASS" and "inferred" in check[2] and "1024" in check[2], check

    @pytest.mark.parametrize("samples", [1024, 2048, 2112, 2624])
    def test_every_priming_in_use_is_inferred_from_an_exact_lead(self, samples):
        check, meta = _sync_row(_sync_probe(-samples / 48000))
        assert check[1] == "PASS" and str(samples) in check[2], check
        assert meta["av_start_offset_range_ms"] == [0.0, 0.0], meta

    def test_he_aac_lead_is_inferred_at_twice_the_core_priming(self):
        check, _ = _sync_row(_sync_probe(-2 * 2112 / 48000, profile="HE-AAC"))
        assert check[1] == "PASS", check

    def test_a_lead_that_is_no_priming_in_use_is_not_inferred(self):
        check, _ = _sync_row(_sync_probe(-1500 / 48000))
        assert _not_verified(check) and "not signalled" in check[2], check
        off_by_two = _sync_row(_sync_probe(-1026 / 48000))[0]
        assert _not_verified(off_by_two), off_by_two

    def test_compliant_range_is_not_verified_and_strict_exempt(self):
        rows = _Rows()
        cc.check_av_sync_offset(None, _sync_probe(-0.050), rows, _cfg())   # -50 .. +4.7 ms
        assert _not_verified(rows.checks[0]), rows.checks
        assert rows.metadata["strict_exempt"] == ["A/V Sync Offset"], rows.metadata

    def test_strict_mode_does_not_escalate_unsignalled_priming(self, media, tmp_path):
        early = tmp_path / "early30.ts"
        ffmpeg("-i", _clean(media), "-itsoffset", "-0.030", "-i", _clean(media), "-map", "0:v",
               "-map", "1:a", "-c", "copy", "-f", "mpegts", str(early))
        result = _scan(early, strict=True, accepted_containers=[".ts"])
        assert _not_verified(_row(result, "A/V Sync Offset"))
        assert "A/V Sync Offset" in result.metadata.get("not_verifiable", []), result.metadata
        escalating = [c[:3] for c in result.checks if c[1] in ("WARN", "FAIL") and c[0] not in
                      set(result.metadata.get("not_verifiable", [])) | set(result.metadata.get("not_run_by_choice", []))]
        assert result.status != "MISMATCH" or escalating, escalating

    def test_no_compliant_value_warns_and_whole_range_beyond_r37_fails(self):
        assert _sync_row(_sync_probe(0.020))[0][1] == "WARN"      # +20 .. +75 ms: never within 5 ms
        assert _sync_row(_sync_probe(-0.070))[0][1] == "WARN"     # -70 .. -15 ms
        assert _sync_row(_sync_probe(-0.120))[0][1] == "FAIL"     # -120 .. -65 ms
        assert _sync_row(_sync_probe(0.061))[0][1] == "FAIL"      # later than R37's 60 ms whatever the priming

    def test_signalled_priming_stays_exact(self):
        assert _sync_row(_sync_probe(-0.030, padding=1024))[0][1] == "WARN"
        assert _sync_row(_sync_probe(0.003, padding=1024))[0][1] == "PASS"


# ---------------------------------------------------------------------------
# Round 2, bug 7: 0.9.1 presets load leniently, with every change recorded
# ---------------------------------------------------------------------------

def _write_091_preset(path, config):
    """A preset as FrameGuard 0.9.1 wrote it (format_version 2)."""
    import json
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"format_version": 2, "type": "frameguard_preset", "name": os.path.basename(str(path)),
                   "description": "", "config": config}, handle)
    return str(path)


class TestOldPresetsLoad:
    @pytest.mark.parametrize("config,expected", [
        ({"av_offset_max_early_ms": 2000.0}, {"av_offset_max_early_ms": 1000}),
        ({"av_offset_warn_ms": 50.0, "av_offset_max_early_ms": 40.0}, {"av_offset_warn_ms": 40}),
        ({"target_width": 7680, "target_height": 3840, "max_frame_pixels": 8192 * 2160},
         {"max_frame_pixels": 8192 * 2160}),
    ], ids=["early2000", "warn_gt_fail", "decoder_ceiling"])
    def test_the_three_kinds_0_9_2_refused_now_load(self, tmp_path, config, expected):
        from frameguard import presets
        preset = presets.import_preset(_write_091_preset(tmp_path / "old.fg", config), raise_errors=True)
        for key, value in expected.items():
            assert preset["config"][key] == value, preset
        assert preset["notices"] and preset["notices"] == preset["config"]["preset_adjustments"], preset

    def test_adjustments_reach_the_pdf_spec_table(self, tmp_path):
        from frameguard import presets
        from frameguard.report import spec_rows
        preset = presets.import_preset(_write_091_preset(tmp_path / "old.fg", {"av_offset_max_early_ms": 2000}))
        rows = dict(spec_rows(dict(DEFAULT_CONFIG, **preset["config"])))
        assert "av_offset_max_early_ms" in rows["Preset adjusted on import"], rows

    def test_the_stored_copy_keeps_the_record(self, tmp_path):
        from frameguard import presets
        stored = presets.save_imported_preset(_write_091_preset(tmp_path / "old.fg",
                                                                {"av_offset_max_early_ms": 2000}))
        again = presets.import_preset(stored["path"], raise_errors=True)
        assert again["config"]["av_offset_max_early_ms"] == 1000
        assert any("av_offset_max_early_ms" in n for n in again["config"]["preset_adjustments"]), again


class TestRetiredDefaultsMigrate:
    """Round 2, bug 7b: 0.9.1's export writes every key, so a 0.9.1 preset
    carries silence_noise_db=-50 (the room-tone MISMATCH, 0.9.1 bug 3) and
    black_detect_picture_threshold=0.98 although its author chose neither."""

    def test_0_9_1_defaults_become_0_9_2_defaults(self, tmp_path):
        from frameguard import presets
        old = _write_091_preset(tmp_path / "full.fg", {"silence_noise_db": -50, "black_detect_picture_threshold": 0.98,
                                                       "target_fps": 25})
        preset = presets.import_preset(old, raise_errors=True)
        assert preset["config"]["silence_noise_db"] == DEFAULT_CONFIG["silence_noise_db"] == -70
        assert preset["config"]["black_detect_picture_threshold"] == DEFAULT_CONFIG["black_detect_picture_threshold"]
        assert preset["config"]["target_fps"] == 25
        notes = " ".join(preset["notices"])
        assert "silence_noise_db -50 -> -70" in notes and "black_detect_picture_threshold" in notes, notes

    def test_a_value_the_author_chose_is_kept(self, tmp_path):
        from frameguard import presets
        old = _write_091_preset(tmp_path / "chosen.fg", {"silence_noise_db": -60})
        preset = presets.import_preset(old, raise_errors=True)
        assert preset["config"]["silence_noise_db"] == -60 and not preset["notices"], preset

    def test_a_0_9_2_preset_is_not_migrated(self, tmp_path):
        from frameguard import presets
        import json
        written = presets.export_preset("new", {"silence_noise_db": -50}, str(tmp_path / "new.fg"))
        assert json.load(open(written))["format_version"] == presets.PRESET_FORMAT_VERSION == 3
        preset = presets.import_preset(written, raise_errors=True)
        assert preset["config"]["silence_noise_db"] == -50 and not preset["notices"], preset

    def test_migration_is_listed_in_the_pdf(self, tmp_path):
        from frameguard import presets
        from frameguard.report import spec_rows
        preset = presets.import_preset(_write_091_preset(tmp_path / "f.fg", {"silence_noise_db": -50}))
        rows = dict(spec_rows(dict(DEFAULT_CONFIG, **preset["config"])))
        assert "silence_noise_db -50 -> -70" in rows["Preset adjusted on import"], rows

    def test_settings_saved_before_0_9_2_migrate_too(self, qapp):
        from PySide6.QtCore import QSettings
        from frameguard import gui
        import json
        store = QSettings(gui._SETTINGS_ORG, gui._SETTINGS_APP)
        store.setValue("scan/config", json.dumps({"silence_noise_db": -50, "target_fps": 25.0}))
        store.remove("scan/config_format")
        store.sync()
        panel = gui.SettingsPanel()
        panel.restore_settings()
        config = panel.get_config_overrides()
        assert config["silence_noise_db"] == -70, config
        assert any("silence_noise_db" in n for n in config["preset_adjustments"]), config
        panel.save_settings()
        assert int(QSettings(gui._SETTINGS_ORG, gui._SETTINGS_APP).value("scan/config_format")) == 3


# ---------------------------------------------------------------------------
# Round 2, bug 11: version gate for git builds, the tested floor, the cache
# ---------------------------------------------------------------------------

class TestVersionGateRound2:
    @pytest.mark.parametrize("banner,ok", [
        ("ffmpeg version N-118000-g1234abcd Copyright\nlibavfilter    10.  4.100 / 10.  4.100", False),
        ("ffmpeg version N-121000-g9abcdef Copyright\nlibavfilter    12.  3.100 / 12.  3.100", True),
        ("ffmpeg version 2024-11-03-git-df00705e00-full_build-www.gyan.dev Copyright\n"
         "libavfilter    10.  6.101 / 10.  6.101", False),
        ("ffmpeg version 2026-09-30-git-0123456789-full_build-www.gyan.dev Copyright\n"
         "libavfilter    12.  1.100 / 12.  1.100", True),
        ("ffmpeg version N-118000-g1234abcd Copyright", False),     # no version to compare: not passed
        ("Usage: wrapper", True),                                   # not an FFmpeg banner: nothing to judge
    ])
    def test_git_builds_are_judged_by_their_libavfilter_base(self, banner, ok):
        from frameguard import tools
        problem = tools.ffmpeg_banner_problem(banner)
        assert (problem is None) == ok, problem

    def test_a_two_part_release_number_is_padded(self, monkeypatch):
        """9.0.0 reports itself as 'ffmpeg version 9.0'; (9, 0) < (9, 0, 0) as tuples."""
        from frameguard import tools
        monkeypatch.setattr(tools, "FFMPEG_MIN_VERSION", (9, 0, 0))
        assert tools.ffmpeg_banner_problem("ffmpeg version 9.0 Copyright") is None
        assert tools.ffmpeg_banner_problem("ffmpeg version 8.1 Copyright")

    def test_below_the_floor_says_it_is_the_tested_floor(self):
        from frameguard import tools
        problem = tools.ffmpeg_banner_problem("ffmpeg version 9.0.1 Copyright")
        assert "9.0.1" in problem and "tested" in problem, problem

    def test_the_cached_gate_answers_without_reprobing(self, monkeypatch, tmp_path):
        from frameguard import tools
        fake = tmp_path / "ffmpeg"
        fake.write_text("x")
        calls = []

        def probe(path):
            calls.append(path)
            return "ffmpeg version 8.0.1 Copyright\nlibavfilter    11.  4.100 / 11.  4.100"
        monkeypatch.setattr(tools, "_ffmpeg_version_text", probe)
        monkeypatch.setattr(tools, "_gate_cache", {})
        first = tools.ffmpeg_version_problem(str(fake))
        second = tools.ffmpeg_version_problem(str(fake))
        assert first and second == first and len(calls) == 1, (first, second, calls)

    @needs_posix
    def test_an_old_git_build_is_refused_in_a_scan(self, media, tmp_path, monkeypatch):
        from frameguard import core
        fake = _fake_ffmpeg(tmp_path, "N-110000-gabcdef0", libavfilter=" 9. 12.100")
        monkeypatch.setattr(core, "get_ffmpeg_path", lambda: fake)
        result = _scan(_clean(media), quick=False)
        integrity = _row(result, "File Integrity")
        assert _not_verified(integrity) and "libavfilter" in integrity[2], integrity


# ---------------------------------------------------------------------------
# Round 2: AGENTS.md UI copy for this fixer's new setting and dialog
# ---------------------------------------------------------------------------

class TestUiCopy:
    def test_aac_priming_has_no_helper_line_and_plain_option_labels(self, qapp):
        from frameguard import gui
        panel = gui.SettingsPanel()
        combo = panel._rule_controls["aac_priming"]
        assert [combo.itemText(i) for i in range(combo.count())] == ["Unknown", "1024 samples", "2112 samples"]
        row = next(r for r in panel._setting_rows if r[1].text() == "AAC Encoder Priming")
        assert row[4].text() == "" and row[4].isHidden(), row[4].text()

    def test_ffmpeg_too_old_dialog_is_one_plain_sentence(self, qapp, monkeypatch):
        from frameguard import gui, tools
        shown = []
        monkeypatch.setattr(gui, "check_ffmpeg_available", lambda: (True, ""))
        monkeypatch.setattr(tools, "ffmpeg_version_problem", lambda path=None: "FFmpeg 8.0.1 is too old")
        monkeypatch.setattr(gui.QMessageBox, "warning", lambda parent, title, text: shown.append((title, text)))
        gui.MainWindow._check_ffmpeg(type("W", (), {})())
        assert shown == [("FFmpeg Too Old", "FFmpeg 8.0.1 is too old.")], shown
