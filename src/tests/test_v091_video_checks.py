"""0.9.1 picture checks (video_checks.py).

Pure-function tests pin each detector's thresholds and boundaries; the
real-FFmpeg tests build small clips with the defect at a known place and run
the full deep scan, one positive and one clean negative per detector.
"""

import os

import pytest

from frameguard import core, presets, video_checks as vc
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import ffmpeg, needs_ffmpeg

FPS = 30.0


def _times(n, fps=FPS):
    return [i / fps for i in range(n)]


# ---------------------------------------------------------------------------
# Pure detectors
# ---------------------------------------------------------------------------

class TestFreezeDetector:
    """Near-identical runs, robust to long-GOP refresh blips."""

    @staticmethod
    def _motion(n, level=2.5):
        return [level + 0.3 * ((i * 7) % 5) for i in range(n)]

    def test_freeze_with_iframe_blips_is_one_event(self):
        mafd = self._motion(300)
        for i in range(91, 210):         # frames 90..209 frozen (4.0 s)
            mafd[i] = 0.01
        for i in (120, 150, 180):        # GOP refreshes inside the freeze
            mafd[i] = 1.2
        found = vc.detect_freezes(mafd, _times(300), FPS, 1.0, 10.0)
        assert len(found.holds) == 1
        start, length = found.holds[0]
        assert start == pytest.approx(3.0) and length == pytest.approx(4.0)

    def test_quiet_dark_scene_is_not_a_freeze(self):
        """Uniformly low activity (a night scene) never collapses relative
        to its surroundings and is not bit-identical."""
        mafd = [0.08 + 0.02 * ((i * 3) % 4) for i in range(300)]
        assert vc.detect_freezes(mafd, _times(300), FPS, 1.0, 10.0).holds == []

    def test_bit_identical_whole_file_is_a_freeze(self):
        mafd = [0.0] * 300
        found = vc.detect_freezes(mafd, _times(300), FPS, 1.0, 10.0)
        assert found.holds and found.holds[0][1] == pytest.approx(10.0)

    def test_minimum_duration_boundary(self):
        def hold(frames):
            mafd = self._motion(300)
            for i in range(91, 91 + frames - 1):
                mafd[i] = 0.0
            return vc.detect_freezes(mafd, _times(300), FPS, 1.0, 10.0).holds
        assert hold(30)            # 30 identical frames = 1.0 s
        assert not hold(28)

    def test_black_hold_is_left_to_black_frames(self):
        mafd = self._motion(300)
        for i in range(91, 151):
            mafd[i] = 0.0
        found = vc.detect_freezes(mafd, _times(300), FPS, 1.0, 10.0, black=[(3.0, 2.0)])
        assert found.holds == [] and found.black_holds == 1

    def test_static_slate_at_the_head_is_not_reported(self):
        mafd = [0.0] * 60 + self._motion(240)
        found = vc.detect_freezes(mafd, _times(300), FPS, 1.0, 10.0)
        assert found.holds == [] and found.edge_holds
        status, detail, _ = vc.freeze_entry(found, 10.0, vc.VideoCheckConfig())
        assert status == "PASS" and "slate" in detail


class TestBlackClassification:
    cfg = vc.VideoCheckConfig()

    @staticmethod
    def _yavg(n, dark=()):
        values = [120.0] * n
        for i in dark:
            values[i] = 16.0
        return values

    def test_segment_and_edge_fades(self):
        found = vc.classify_black([(0.0, 0.5), (4.0, 5.0), (9.6, 10.0)], 10.0, FPS, self.cfg)
        assert found.segments == [(4.0, 1.0)]

    def test_short_hard_cut_burst_is_reported(self):
        times = _times(300)
        found = vc.classify_black([(4.0, 4.0 + 5 / FPS)], 10.0, FPS, self.cfg, times,
                                  self._yavg(300, range(120, 125)))
        assert found.bursts == [(4.0, 5)]
        status, detail, confidence = vc.black_entry(found, 10.0, self.cfg)
        assert status == "WARN" and confidence < 70 and "edit gap" in detail

    def test_dip_to_black_burst_is_ignored(self):
        times = _times(300)
        yavg = self._yavg(300, range(120, 125))
        yavg[119], yavg[125] = 24.0, 26.0            # fading neighbours
        found = vc.classify_black([(4.0, 4.0 + 5 / FPS)], 10.0, FPS, self.cfg, times, yavg)
        assert found.bursts == [] and found.ignored_bursts == 1

    def test_strobe_bursts_are_left_to_flash_risk(self):
        events = [(3.0 + k * 0.5, 3.0 + k * 0.5 + 5 / FPS) for k in range(4)]
        dark = [i for s, _ in events for i in range(int(s * FPS), int(s * FPS) + 5)]
        found = vc.classify_black(events, 10.0, FPS, self.cfg, _times(300), self._yavg(300, dark))
        assert found.bursts == [] and found.ignored_bursts == 4

    def test_two_frames_is_below_the_short_tier(self):
        found = vc.classify_black([(4.0, 4.0 + 2 / FPS)], 10.0, FPS, self.cfg, _times(300),
                                  self._yavg(300, (120, 121)))
        assert found.bursts == [] and found.ignored_bursts == 0


class TestLevels:
    @staticmethod
    def _series(luma=None, gam=None, n=60):
        return vc.VideoSeries(times=_times(n), mafd=[1.0] * n,
                              luma=luma or [0.0] * n, gam=gam or [0.0] * n,
                              band1=[0.0] * n, band2=[0.0] * n, yavg=[100.0] * n)

    def test_one_percent_area_boundary(self):
        over = [0.0] * 20 + [0.0101] * 10 + [0.0] * 30
        under = [0.0] * 20 + [0.0099] * 10 + [0.0] * 30
        assert vc.assess_levels(self._series(luma=over), FPS, False).luma
        assert not vc.assess_levels(self._series(luma=under), FPS, False).luma

    def test_three_frame_boundary(self):
        three = [0.0] * 20 + [0.05] * 3 + [0.0] * 37
        two = [0.0] * 20 + [0.05] * 2 + [0.0] * 38
        assert vc.assess_levels(self._series(gam=three), FPS, False).gamut
        assert not vc.assess_levels(self._series(gam=two), FPS, False).gamut

    def test_full_range_luma_cannot_violate(self):
        findings = vc.assess_levels(self._series(luma=[0.5] * 60), FPS, True)
        assert findings.luma == [] and findings.max_luma == 0.0

    def test_modes(self):
        findings = vc.assess_levels(self._series(gam=[0.05] * 60), FPS, False)
        assert vc.levels_entry(findings, "report", False)[0] == "PASS"
        assert vc.levels_entry(findings, "advisory", False)[0] == "WARN"
        status, _, confidence = vc.levels_entry(findings, "essential", False)
        assert status == "FAIL" and confidence >= 70
        near = vc.assess_levels(self._series(gam=[0.012] * 60), FPS, False)
        assert vc.levels_entry(near, "essential", False)[2] < 70

    def test_unsignalled_matrix_needs_every_candidate_to_agree(self):
        """Untagged HD: judged under BT.709 and BT.601; a share counts only
        when it is out of gamut under both (FFmpeg encodes untagged RGB with
        BT.601, so BT.709 alone flagged ordinary pictures)."""
        n = 60
        series = self._series(n=n)
        series.gam_by_matrix = {"bt709": [0.6] * n, "bt601": [0.0] * n}
        series.gam = [min(v) for v in zip(*series.gam_by_matrix.values())]
        findings = vc.assess_levels(series, FPS, False, ("bt709", "bt601"))
        status, detail, _ = vc.levels_entry(findings, "advisory", False)
        assert status == "PASS" and "within R 103 under at least one" in detail
        series.gam_by_matrix = {"bt709": [0.6] * n, "bt601": [0.5] * n}
        series.gam = [min(v) for v in zip(*series.gam_by_matrix.values())]
        findings = vc.assess_levels(series, FPS, False, ("bt709", "bt601"))
        status, detail, confidence = vc.levels_entry(findings, "advisory", False)
        assert status == "WARN" and confidence >= 70 and "holds under BT.709 and BT.601" in detail

    def test_candidate_matrices(self):
        untagged_hd = vc.PictureInfo.from_stream({"width": 1920, "height": 1080, "color_space": "unknown"})
        untagged_sd = vc.PictureInfo.from_stream({"width": 720, "height": 576})
        tagged = vc.PictureInfo.from_stream({"width": 1920, "height": 1080, "color_space": "bt709"})
        assert untagged_hd.gamut_matrices == ("bt709", "bt601")
        assert untagged_sd.gamut_matrices == ("bt601", "bt709")
        assert tagged.gamut_matrices == ("bt709",)

    def test_full_range_content_tagged_limited(self):
        series = self._series()
        series.band1 = [0.2] * 60                    # deep sub-black everywhere
        assert "full range" in vc.range_plausibility(series, False)

    def test_limited_content_tagged_full(self):
        series = self._series()
        series.band1 = [0.15] * 60                   # pile-up at code 16
        assert "limited range" in vc.range_plausibility(series, True)
        series.luma = [0.001] * 60                   # real full-range blacks exist
        assert vc.range_plausibility(series, True) is None


def _flash_series(hz, frames=150, area_blocks=144, level=255):
    cols, rows = vc.FLASH_GRID
    blocks = cols * rows
    grid = []
    period = FPS / hz
    for i in range(frames):
        on = int((i % period) < period / 2) and 30 <= i < 120
        plane = bytes(level if (on and b < area_blocks) else 0 for b in range(blocks))
        grid.append((plane, plane, plane))
    return vc.VideoSeries(times=_times(frames), mafd=[1.0] * frames, flash=grid)


class TestFlashCounter:
    def test_five_hertz_full_screen_is_a_hazard(self):
        events = vc.assess_flash(_flash_series(5), FPS)
        assert events and events[0].kind == "general"
        assert vc.flash_entry(events)[0] == "WARN"

    def test_three_hertz_is_the_limit_not_a_hazard(self):
        assert vc.assess_flash(_flash_series(3), FPS) == []

    def test_quarter_screen_is_not_enough(self):
        assert vc.assess_flash(_flash_series(10, area_blocks=36), FPS) == []
        assert vc.assess_flash(_flash_series(10, area_blocks=40), FPS)

    def test_small_luminance_change_is_not_a_flash(self):
        # 255 -> 20 cd/m2 swing needs ~ code 25 linear at 200 cd/m2 peak.
        assert vc.assess_flash(_flash_series(10, level=20), FPS) == []

    def test_entry_never_fails_and_states_the_caveat(self):
        status, detail, _ = vc.flash_entry([vc.FlashEvent(1.0, 2.0, 1.0, "general")])
        assert status == "WARN" and "Harding" in detail


class TestInterlaceAssessment:
    def test_combed_section_in_progressive_stream(self):
        labels = ["progressive"] * 90 + ["tff"] * 60 + ["progressive"] * 90
        found = vc.assess_interlace(labels, _times(240), FPS, "progressive")
        assert found.combed and found.combed[0][0] == pytest.approx(3.0, abs=0.1)
        assert vc.interlace_entry(found)[0] == "WARN"

    def test_scattered_frames_are_not_combing(self):
        labels = ["tff" if i % 4 == 0 else "progressive" for i in range(240)]
        assert vc.assess_interlace(labels, _times(240), FPS, "progressive").combed == []

    def test_field_order_mismatch(self):
        found = vc.assess_interlace(["tff"] * 240, _times(240), FPS, "bb")
        status, detail, _ = vc.interlace_entry(found)
        assert status == "WARN" and "mismatch" in detail

    def test_vertical_scroll_is_not_combing(self):
        labels = ["bff"] * 240
        col = [None] + [0.02] * 239          # columns unchanged
        row = [None] + [6.0] * 239           # row profile slides
        found = vc.assess_interlace(labels, _times(240), FPS, "progressive", col, row)
        assert found.combed == [] and found.scrolls
        status, detail, _ = vc.interlace_entry(found)
        assert status == "PASS" and "vertical scroll" in detail

    def test_mixed_motion_keeps_combing(self):
        labels = ["tff"] * 240
        found = vc.assess_interlace(labels, _times(240), FPS, "progressive",
                                    [None] + [0.4] * 239, [None] + [1.5] * 239)
        assert found.combed and not found.scrolls

    def test_vertical_share_boundary(self):
        share = vc.INTERLACE_VERTICAL_SHARE
        def combed(col):
            return vc.assess_interlace(["tff"] * 60, _times(60), FPS, "progressive",
                                       [col] * 60, [1.0 - col] * 60).combed
        assert combed(share * 1.05) and not combed(share * 0.95)

    def test_no_measurable_motion_keeps_idet_verdict(self):
        found = vc.assess_interlace(["tff"] * 60, _times(60), FPS, "progressive",
                                    [0.0] * 60, [0.01] * 60)
        assert found.combed

    def test_declared_interlaced_matching_order_passes(self):
        found = vc.assess_interlace(["tff"] * 240, _times(240), FPS, "tt")
        assert vc.interlace_entry(found)[0] == "PASS"


class TestConfiguration:
    def test_defaults_and_classification(self):
        for key in ("black_frames_enabled", "black_detect_picture_threshold",
                    "black_short_min_frames", "freeze_frames_enabled",
                    "signal_levels_mode", "flash_check_enabled", "interlace_check_enabled"):
            assert key in DEFAULT_CONFIG
        assert "Signal Levels" in core.ESSENTIAL_CHECKS
        assert {"Flash Risk", "Interlace Detection"} <= core.ADVISORY_CHECKS
        assert "freeze_detect_noise" in core.RETIRED_CONFIG_KEYS

    def test_presets_validate_new_keys(self):
        good = presets.validate_preset_config({"signal_levels_mode": "essential",
                                               "black_detect_picture_threshold": 0.95,
                                               "black_short_min_frames": 0,
                                               "flash_check_enabled": False})
        assert good["signal_levels_mode"] == "essential"
        for bad in ({"signal_levels_mode": "strict"}, {"black_detect_pix_threshold": 0.9},
                    {"black_short_min_frames": 2.5}, {"freeze_detect_duration": 0}):
            with pytest.raises(presets.PresetValidationError):
                presets.validate_preset_config(bad)

    def test_retired_freeze_noise_still_loads(self):
        assert presets.validate_preset_config({"freeze_detect_noise": 0.003}) == {}

    def test_filter_path_escaping(self):
        # Option level escapes \ ' : ; graph level then escapes \ ' [ ] , ;
        assert vc.filter_path("a:b") == r"a\\:b"
        assert vc.filter_path("x'y") == r"x\\\'y"
        assert vc.filter_path("p,q[r];s") == r"p\,q\[r\]\;s"
        assert vc.filter_path("c\\d") == r"c\\\\d"


# ---------------------------------------------------------------------------
# Real FFmpeg
# ---------------------------------------------------------------------------

SIZE = "640x360"
GRAIN = "noise=alls=8:allf=t"
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-g", "30",
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709"]


def _clip(path, vf="null", seconds=6, extra=(), encoder=X264, source=None):
    # Explicit limited-range BT.709 YUV, so pixel values written by geq are
    # exactly what the encoder stores (no implicit range conversion).
    source = source or (f"testsrc2=s={SIZE}:r=30:d={seconds},{GRAIN},"
                        "scale=out_range=tv:out_color_matrix=bt709,format=yuv420p")
    ffmpeg("-f", "lavfi", "-i", source, "-vf", vf, *encoder, *extra, "-an", str(path))
    return str(path)


def _scan(path, **overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=640, target_height=360, target_fps=30.0, min_bitrate_mbps=0,
               audio_required=False, loudness_mode="off",
               accepted_containers=[".mp4", ".mov", ".mkv"])
    cfg.update(overrides)
    result = run_qa_checks(path, cfg=cfg)
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


@pytest.fixture(scope="module")
def clean_clip(tmp_path_factory):
    return _clip(tmp_path_factory.mktemp("v091") / "clean.mp4")


@needs_ffmpeg
class TestRealScan:
    def test_clean_clip_passes_every_picture_check(self, clean_clip):
        result = _scan(clean_clip)
        for name in vc.VIDEO_CHECK_NAMES:
            assert _row(result, name)[1] == "PASS", _row(result, name)

    def test_black_segment(self, tmp_path):
        path = _clip(tmp_path / "black.mp4", "drawbox=c=black:t=fill:enable='between(t,2,2.6)'")
        row = _row(_scan(path), "Black Frames")
        assert row[1] == "WARN" and "00:00:02.000" in row[2], row

    def test_short_black_burst(self, tmp_path):
        path = _clip(tmp_path / "burst.mp4",
                     "drawbox=c=black:t=fill:enable='between(n,90,94)'")
        row = _row(_scan(path), "Black Frames")
        assert row[1] == "WARN" and "5 frames" in row[2], row

    def test_freeze_in_cbr_long_gop(self, tmp_path):
        """The red team's case: 10 Mbit/s CBR, 1 s GOP - freezedetect found
        nothing; the frozen picture is re-coded at every I-frame."""
        source = (f"testsrc2=s={SIZE}:r=30:d=8,{GRAIN},split[a][b];"
                  "[a][b]freezeframes=first=60:last=150:replace=60")
        cbr = ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", "3M", "-minrate", "3M",
               "-maxrate", "3M", "-bufsize", "3M", "-g", "30", "-pix_fmt", "yuv420p",
               "-x264-params", "nal-hrd=cbr"]
        path = _clip(tmp_path / "freeze.mp4", encoder=cbr, source=source)
        row = _row(_scan(path), "Freeze Frames")
        assert row[1] in ("WARN", "FAIL") and "00:00:02.000" in row[2], row

    def test_luma_outside_r103(self, tmp_path):
        # drawbox clips to legal white, so the excursion is written with geq:
        # 80x60 of 640x360 = 2.1 % of the picture at code 252 for 1 s.
        path = _clip(tmp_path / "luma.mp4",
                     "geq=lum='if(between(T,2,3)*between(X,280,359)*between(Y,150,209),252,lum(X,Y))'"
                     ":cb='cb(X,Y)':cr='cr(X,Y)'")
        result = _scan(path)
        row = _row(result, "Signal Levels")
        assert row[1] == "WARN" and "Luma" in row[2], row
        assert _row(_scan(path, signal_levels_mode="essential"), "Signal Levels")[1] == "FAIL"
        assert _row(_scan(path, signal_levels_mode="report"), "Signal Levels")[1] == "PASS"

    @staticmethod
    def _untagged(path, source, vf="null"):
        """FFmpeg's default path: RGB source -> yuv420p (swscale BT.601), no
        colour tags at all."""
        ffmpeg("-f", "lavfi", "-i", source, "-vf", vf, "-c:v", "libx264", "-preset",
               "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(path))
        return str(path)

    def test_untagged_saturated_hd_is_not_a_gamut_warning(self, tmp_path):
        path = self._untagged(tmp_path / "untagged.mp4", "testsrc2=s=1280x720:r=30:d=2")
        row = _row(_scan(path, target_width=1280, target_height=720), "Signal Levels")
        assert row[1] == "PASS" and "not signalled" in row[2], row

    def test_untagged_genuine_excursion_still_warns(self, tmp_path):
        # Cb 16 / Cr 240 at mid grey is outside -5..105 % under BT.709 and BT.601.
        bad = ("geq=lum='lum(X,Y)':cb='if(between(X,320,960)*between(Y,180,540),16,cb(X,Y))'"
               ":cr='if(between(X,320,960)*between(Y,180,540),240,cr(X,Y))'")
        path = self._untagged(tmp_path / "bad.mp4", "testsrc2=s=1280x720:r=30:d=2,format=yuv420p", bad)
        row = _row(_scan(path, target_width=1280, target_height=720), "Signal Levels")
        assert row[1] == "WARN" and "holds under BT.709 and BT.601" in row[2], row

    def test_tagged_file_uses_only_its_matrix(self, tmp_path):
        """BT.601-coded picture tagged BT.709: the tag is trusted, as before,
        so the wrong tag shows up as a gamut excursion."""
        # Tags set on the encoder make swscale convert with them, so the
        # BT.601 picture is retagged in the bitstream instead.
        source = self._untagged(tmp_path / "plain.mp4", "testsrc2=s=1280x720:r=30:d=2")
        path = str(tmp_path / "mistagged.mp4")
        ffmpeg("-i", source, "-c", "copy", "-bsf:v",
               "h264_metadata=matrix_coefficients=1:colour_primaries=1:"
               "transfer_characteristics=1:video_full_range_flag=0", path)
        row = _row(_scan(path, target_width=1280, target_height=720), "Signal Levels")
        assert row[1] == "WARN" and "not signalled" not in row[2], row
        video = core._primary_video_stream(core.run_ffprobe(path))
        with vc.VideoAnalysis(video, dict(DEFAULT_CONFIG)) as analysis:
            graph = ";".join(analysis._area_chains())
        assert "[ar_gamut1]" not in graph and "[ar_gamut0]" in graph

    def test_luma_patch_inside_the_area_tolerance(self, tmp_path):
        path = _clip(tmp_path / "small.mp4",
                     "geq=lum='if(between(T,2,3)*between(X,300,320)*between(Y,170,180),252,lum(X,Y))'"
                     ":cb='cb(X,Y)':cr='cr(X,Y)'")
        assert _row(_scan(path), "Signal Levels")[1] == "PASS"

    def test_flash_five_hertz(self, tmp_path):
        path = _clip(tmp_path / "flash5.mp4",
                     "drawbox=c=white:t=fill:enable='gte(t,1)*lt(t,4)*lt(mod(n,6),3)'")
        assert _row(_scan(path), "Flash Risk")[1] == "WARN"

    def test_flash_three_hertz_limit(self, tmp_path):
        path = _clip(tmp_path / "flash3.mp4",
                     "drawbox=c=white:t=fill:enable='gte(t,1)*lt(t,4)*lt(mod(n,10),5)'")
        assert _row(_scan(path), "Flash Risk")[1] == "PASS"

    def test_combing_in_progressive_video(self, tmp_path):
        source = f"testsrc2=s={SIZE}:r=60:d=6,{GRAIN},tinterlace=mode=interleave_top,setfield=prog"
        path = _clip(tmp_path / "combed.mp4", "setfield=prog", source=source,
                     extra=["-r", "30"])
        row = _row(_scan(path), "Interlace Detection")
        assert row[1] == "WARN" and "combing" in row[2], row

    @pytest.mark.parametrize("speed", [1, 2, 3, 4])
    def test_credit_roll_is_not_interlaced(self, tmp_path, speed):
        """Rolling credits: thin white strokes scrolling up on black. At an
        integer number of lines per field idet reads them as 99 % interlaced
        (the corpus hard negative); no frame is combed."""
        row = f"mod(Y+{speed}*N,48)"
        glyph = f"lt({row},14)*between(X,120,520)*lt(mod(X,9),6)"
        stroke = (f"gt(lt({row},2)+between({row},6,7)+between({row},12,13)"
                  f"+eq(mod(X,9),0)+eq(mod(X,9),5),0)")
        source = f"color=black:s={SIZE}:r=30:d=3,format=yuv420p"
        path = _clip(tmp_path / f"credits{speed}.mp4", source=source,
                     vf=f"geq=lum='16+219*{glyph}*{stroke}':cb=128:cr=128")
        row = _row(_scan(path), "Interlace Detection")
        assert row[1] == "PASS", row
        if speed == 2:   # one line per field: idet calls it interlaced
            assert "vertical scroll" in row[2], row

    @pytest.mark.parametrize("pixels_per_second", [60, 110])
    def test_overlay_credit_bar_roll_is_set_aside(self, tmp_path, pixels_per_second):
        """The corpus recipe: a bar overlaid at y = H - t*speed (2 and 3.67
        lines per frame). idet reads ~99 % interlaced; the row says why not."""
        graph = ("color=white:s=400x6:r=30:d=4[s];color=black:s=640x360:r=30:d=4[b];"
                 f"[b][s]overlay=x=120:y='H-mod(t*{pixels_per_second},H+20)',format=yuv420p")
        path = str(tmp_path / f"roll{pixels_per_second}.mp4")
        ffmpeg("-filter_complex", graph, *X264, "-an", path)
        row = _row(_scan(path), "Interlace Detection")
        assert row[1] == "PASS" and "vertical scroll" in row[2], row

    def test_woven_clip_with_vertical_tilt_is_still_combed(self, tmp_path):
        source = ("testsrc2=s=640x720:r=60:d=4,crop=640:360:0:'mod(n*3,360)',"
                  "tinterlace=mode=interleave_top,setfield=prog")
        path = _clip(tmp_path / "tilt.mp4", "setfield=prog", source=source, extra=["-r", "30"])
        row = _row(_scan(path), "Interlace Detection")
        assert row[1] == "WARN" and "combing" in row[2], row

    def test_field_order_mismatch(self, tmp_path):
        source = f"testsrc2=s={SIZE}:r=60:d=6,{GRAIN},tinterlace=mode=interleave_top"
        path = _clip(tmp_path / "bff.mp4", "setfield=bff", source=source,
                     extra=["-flags", "+ildct+ilme", "-x264-params", "bff=1", "-r", "30"])
        result = _scan(path, require_progressive=False)
        row = _row(result, "Interlace Detection")
        assert row[1] == "WARN" and "mismatch" in row[2], row

    def test_checks_can_be_switched_off(self, clean_clip):
        result = _scan(clean_clip, black_frames_enabled=False, freeze_frames_enabled=False,
                       signal_levels_mode="off", flash_check_enabled=False,
                       interlace_check_enabled=False)
        for name in vc.VIDEO_CHECK_NAMES:
            assert _row(result, name)[1] == "SKIP"

    def test_measurement_files_are_removed(self, clean_clip, tmp_path, monkeypatch):
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        _scan(clean_clip)
        assert not [p for p in os.listdir(tmp_path) if p.startswith("frameguard-video-")]

    def test_awkward_temporary_directory(self, clean_clip, tmp_path, monkeypatch):
        """Filter-graph escaping: quotes, colons, commas, brackets, spaces."""
        odd = tmp_path / ("it's a [test], ok; 1" + ("" if os.name == "nt" else ":2"))
        odd.mkdir()
        monkeypatch.setattr("tempfile.tempdir", str(odd))
        result = _scan(clean_clip)
        assert _row(result, "Freeze Frames")[1] == "PASS"
        assert _row(result, "Signal Levels")[1] == "PASS"

    def test_full_range_black_uses_the_full_range_black_level(self, tmp_path):
        """Y 0-255 coding: black is code 0, so code 12 (4.7 %) is dark grey,
        not black; code 3 is black. A format conversion slipping in before
        blackdetect once judged full-range codes against limited levels."""
        full = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-g", "30",
                "-pix_fmt", "yuvj420p", "-color_range", "pc"]
        src = (f"testsrc2=s={SIZE}:r=30:d=6,{GRAIN},"
               "scale=out_range=pc:out_color_matrix=bt709,format=yuvj420p")
        grey = _clip(tmp_path / "grey.mp4", source=src, encoder=full,
                     vf="geq=lum='if(between(T,2,3),12,lum(X,Y))':cb=128:cr=128")
        black = _clip(tmp_path / "black.mp4", source=src, encoder=full,
                      vf="geq=lum='if(between(T,2,3),3,lum(X,Y))':cb=128:cr=128")
        assert _row(_scan(grey), "Black Frames")[1] == "PASS"
        assert _row(_scan(black), "Black Frames")[1] == "WARN"

    def test_no_temporary_space_reports_not_verified(self, clean_clip, monkeypatch):
        def refuse(*args, **kwargs):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(vc.tempfile, "mkdtemp", refuse)
        result = _scan(clean_clip)
        assert _row(result, "Black Frames")[1] == "PASS"
        for name in ("Freeze Frames", "Signal Levels", "Flash Risk", "Interlace Detection"):
            row = _row(result, name)
            assert row[1] == "WARN" and "temporary" in row[2], row

    def test_cancelled_scan_leaves_no_measurement_files(self, clean_clip, tmp_path, monkeypatch):
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        monkeypatch.setattr(core, "_run_scan_process", lambda *args, **kwargs: None)
        result = core.QAResult(clean_clip)
        core.run_deep_scan(clean_clip, result, dict(DEFAULT_CONFIG))
        assert not [p for p in os.listdir(tmp_path) if p.startswith("frameguard-video-")]
