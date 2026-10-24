"""0.9.2 picture-check fixes from the independent 0.9.1 review.

Each class pins one defect: the counter-example the review used (or the
standard's own case) must be caught, and the standard's hard negatives must
still pass. Pure-function tests come first; the real-FFmpeg tests build
small clips with the bundled FFmpeg and run the full deep scan.
"""

import math

import pytest

from frameguard import video_checks as vc
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import ffmpeg, needs_ffmpeg

FPS = 30.0


def _times(n, fps=FPS):
    return [i / fps for i in range(n)]


# ---------------------------------------------------------------------------
# Flash (ITU-R BT.1702-3 Annex 1, Guideline 1)
# ---------------------------------------------------------------------------

def _grid_series(picture, frames=150, fps=FPS):
    """A flash-grid series from ``picture(frame, x, y) -> (r, g, b)``.

    x, y are the block centre in 0..1 screen coordinates; r, g, b are linear
    light 0..1 (the grid FFmpeg writes is linear light scaled to 0..255).
    """
    cols, rows = vc.FLASH_GRID
    grid = []
    for i in range(frames):
        planes = ([], [], [])
        for b in range(cols * rows):
            x, y = (b % cols + 0.5) / cols, (b // cols + 0.5) / rows
            r, g, bl = picture(i, x, y)
            planes[0].append(round(255 * g))
            planes[1].append(round(255 * bl))
            planes[2].append(round(255 * r))
        grid.append(tuple(bytes(p) for p in planes))
    return vc.VideoSeries(times=_times(frames, fps), mafd=[1.0] * frames, flash=grid)


def _square(i, hz, fps=FPS):
    """1 in the first half of each period of a ``hz`` square wave, else 0."""
    period = fps / hz
    return 1 if (i % period) < period / 2 else 0


WHITE, BLACK = (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)


def _active(i):
    return 30 <= i < 120


class TestFlashRegions:
    """BT.1702-3: 'the combined area of flashes occurring concurrently
    occupies more than 25% of the displayed screen area'. Flashes are judged
    per region: antiphase and patterned flashing is concurrent flashing, even
    though the screen's mean luminance stays constant."""

    def test_antiphase_halves(self):
        def picture(i, x, y):
            on = _square(i, 5) if _active(i) else 0
            left = on if x < 0.5 else (1 - on if _active(i) else 0)
            return WHITE if left else BLACK
        events = vc.assess_flash(_grid_series(picture), FPS)
        assert events and events[0].kind == "general" and events[0].area > 0.9

    def test_antiphase_saturated_red_halves(self):
        def picture(i, x, y):
            on = _square(i, 7.5) if _active(i) else 0
            red = on if x < 0.5 else (1 - on if _active(i) else 0)
            return (1.0, 0.0, 0.0) if red else BLACK
        events = vc.assess_flash(_grid_series(picture), FPS)
        assert {e.kind for e in events} >= {"red"}

    def test_inverting_checkerboard(self):
        def picture(i, x, y):
            cell = (int(x * 4) + int(y * 4)) % 2
            phase = _square(i, 5) if _active(i) else 0
            return WHITE if cell ^ phase else BLACK
        events = vc.assess_flash(_grid_series(picture), FPS)
        assert events and events[0].area > 0.9

    def test_rotating_windmill(self):
        """Eight radial vanes turning 1.25 rev/s: every region sees 5 flashes
        a second, each at its own phase (BT.1702 names radial stripes)."""
        def picture(i, x, y):
            if not _active(i):
                return BLACK
            angle = math.atan2((y - 0.5) * 9 / 16, x - 0.5) / (2 * math.pi)
            vane = int(((angle + 1.25 * i / FPS) % 1.0) * 8) % 2
            return WHITE if vane else BLACK
        events = vc.assess_flash(_grid_series(picture), FPS)
        assert events and events[0].area > 0.5

    def test_dark_saturated_red_flashes_irrespective_of_luminance(self):
        """'Irrespective of luminance, a transition to or from a saturated red
        is also potentially harmful': a red at 0.19 linear swings only 8 cd/m2."""
        def picture(i, x, y):
            return (0.19, 0.0, 0.0) if _active(i) and _square(i, 5) else BLACK
        events = vc.assess_flash(_grid_series(picture), FPS)
        assert [e.kind for e in events] == ["red"]

    def test_red_to_green_at_equal_luminance(self):
        green = 0.2126 / 0.7152          # same luminance as full red
        def picture(i, x, y):
            if not _active(i):
                return BLACK
            return (1.0, 0.0, 0.0) if _square(i, 5) else (0.0, green, 0.0)
        events = vc.assess_flash(_grid_series(picture), FPS)
        assert [e.kind for e in events] == ["red"]

    # Hard negatives ------------------------------------------------------
    def test_two_hertz_full_screen_passes(self):
        def picture(i, x, y):
            return WHITE if _active(i) and _square(i, 2) else BLACK
        assert vc.assess_flash(_grid_series(picture), FPS) == []

    def test_three_hertz_borderline_passes(self):
        """Exactly three flashes a second is the limit, not over it."""
        def picture(i, x, y):
            return WHITE if _active(i) and _square(i, 3) else BLACK
        assert vc.assess_flash(_grid_series(picture), FPS) == []

    def test_antiphase_quarter_screens_at_two_hertz_pass(self):
        def picture(i, x, y):
            on = _square(i, 2) if _active(i) else 0
            return WHITE if (on if x < 0.5 else 1 - on) else BLACK
        assert vc.assess_flash(_grid_series(picture), FPS) == []

    def test_area_is_not_block_quantised(self):
        """A flashing rectangle off the grid: 22 % of the screen is under the
        limit (0.9.1 counted partly covered blocks whole: '28 %'); 28 % is over."""
        def rectangle(share):
            width = share / 0.6                 # 0.6 of the height, off-grid
            x0, y0 = 0.137, 0.213

            def picture(i, x, y):
                cols, rows = vc.FLASH_GRID
                # Linear coverage of this block by the rectangle.
                bx0, by0 = x - 0.5 / cols, y - 0.5 / rows
                cover_x = max(0.0, min(bx0 + 1 / cols, x0 + width) - max(bx0, x0)) * cols
                cover_y = max(0.0, min(by0 + 1 / rows, y0 + 0.6) - max(by0, y0)) * rows
                level = cover_x * cover_y if _active(i) and _square(i, 10) else 0.0
                return (level, level, level)
            return picture
        assert vc.assess_flash(_grid_series(rectangle(0.22)), FPS) == []
        events = vc.assess_flash(_grid_series(rectangle(0.28)), FPS)
        assert events and 0.25 < events[0].area < 0.31

    def test_entry_keeps_the_screening_caveat(self):
        status, detail, _ = vc.flash_entry([vc.FlashEvent(1.0, 2.0, 1.0, "general")])
        assert status == "WARN" and "Harding" in detail and "screening" in detail


# ---------------------------------------------------------------------------
# Real FFmpeg
# ---------------------------------------------------------------------------

SIZE = "640x360"
GRAIN = "noise=alls=8:allf=t"
X264 = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-g", "30",
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709"]


def _clip(path, vf="null", seconds=6, extra=(), encoder=X264, source=None, size=SIZE):
    source = source or (f"testsrc2=s={size}:r=30:d={seconds},{GRAIN},"
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


_GREY = "color=gray:s=640x360:r=30:d=5,format=yuv420p"


@needs_ffmpeg
class TestFlashScan:
    @staticmethod
    def _geq(tmp_path, name, lum):
        return _clip(tmp_path / name, f"geq=lum='{lum}':cb=128:cr=128", source=_GREY)

    def test_antiphase_halves(self, tmp_path):
        # 5 Hz at 30 fps: three frames per state; left and right opposite.
        lum = "if(gte(T,1)*lt(T,4),if(eq(lt(X,W/2),lt(mod(N,6),3)),235,16),16)"
        row = _row(_scan(self._geq(tmp_path, "anti.mp4", lum)), "Flash Risk")
        assert row[1] == "WARN", row

    def test_inverting_checkerboard(self, tmp_path):
        lum = ("if(gte(T,1)*lt(T,4),if(eq(mod(floor(X/80)+floor(Y/90),2),lt(mod(N,6),3)),235,16),16)")
        row = _row(_scan(self._geq(tmp_path, "checker.mp4", lum)), "Flash Risk")
        assert row[1] == "WARN", row

    def test_rotating_windmill(self, tmp_path):
        lum = ("if(gte(T,1)*lt(T,4),if(mod(floor((atan2(Y-H/2,X-W/2)/(2*PI)+1.25*T+1)*8),2),235,16),16)")
        row = _row(_scan(self._geq(tmp_path, "windmill.mp4", lum)), "Flash Risk")
        assert row[1] == "WARN", row

    def test_antiphase_red_halves(self, tmp_path):
        path = str(tmp_path / "police.mp4")
        on = "gte(t,1)*lt(t,4)*lt(mod(n,4),2)"
        off = "gte(t,1)*lt(t,4)*gte(mod(n,4),2)"
        ffmpeg("-f", "lavfi", "-i", "color=black:s=640x360:r=30:d=5",
               "-vf", f"drawbox=x=0:y=0:w=320:h=360:c=red:t=fill:enable='{on}',"
                      f"drawbox=x=320:y=0:w=320:h=360:c=red:t=fill:enable='{off}',format=yuv420p",
               *X264, "-an", path)
        row = _row(_scan(path), "Flash Risk")
        assert row[1] == "WARN" and "red" in row[2], row

    def test_two_hertz_passes(self, tmp_path):
        lum = "if(gte(T,1)*lt(T,4),if(lt(mod(N,15),8),235,16),16)"
        assert _row(_scan(self._geq(tmp_path, "two.mp4", lum)), "Flash Risk")[1] == "PASS"

    def test_three_hertz_borderline_passes(self, tmp_path):
        lum = "if(gte(T,1)*lt(T,4),if(lt(mod(N,10),5),235,16),16)"
        assert _row(_scan(self._geq(tmp_path, "three.mp4", lum)), "Flash Risk")[1] == "PASS"


# ---------------------------------------------------------------------------
# Interlace (idet documented defaults; field order; telecine; displacement)
# ---------------------------------------------------------------------------

class TestInterlaceRules:
    def test_idet_runs_with_its_documented_defaults(self):
        analysis = vc.VideoAnalysis({"index": 0, "width": 640, "height": 360,
                                     "avg_frame_rate": "30/1"}, dict(DEFAULT_CONFIG))
        analysis.paths = {name: f"/tmp/{name}.txt" for name in
                          ("idet", "idet1", "rep_top", "rep_bottom")
                          + tuple(vc._disp_name(d) for d in vc.INTERLACE_DISPLACEMENTS)}
        graph = ";".join(analysis._interlace_chains())
        assert "idet@il," in graph and "intl_thres" not in graph

    def test_field_order_that_flips_is_not_woven(self):
        """Progressive graphics give idet a slight, content-driven asymmetry
        whose sign flips (testsrc2: TFF and BFF sections); one woven source has
        one field order."""
        labels = ["tff"] * 60 + ["progressive"] * 30 + ["bff"] * 60 + ["progressive"] * 30
        found = vc.assess_interlace(labels, _times(180), FPS, "progressive", labels)
        assert found.combed == [] and len(found.unsteady) == 2
        assert vc.interlace_entry(found)[0] == "PASS"

    def test_steady_field_order_is_woven(self):
        labels = ["tff"] * 150
        singles = ["tff"] * 20 + ["undetermined"] * 130     # smooth picture: few single calls
        found = vc.assess_interlace(labels, _times(150), FPS, "progressive", singles)
        assert found.combed and vc.interlace_entry(found)[0] == "WARN"

    def test_three_two_cadence_is_telecine(self):
        cycle = ["", "", "top", "", "bottom"]
        repeated = cycle * 36
        found = vc.assess_interlace(["progressive"] * 180, _times(180), FPS, "progressive",
                                    repeated=repeated)
        status, detail, _ = vc.interlace_entry(found)
        assert found.telecine and status == "WARN" and "3:2 pulldown" in detail

    def test_scattered_repeats_are_not_a_cadence(self):
        repeated = ["top" if k in (7, 40, 77, 120) else "" for k in range(180)]
        found = vc.assess_interlace(["progressive"] * 180, _times(180), FPS, "progressive",
                                    repeated=repeated)
        assert found.telecine == []


def _texture(path):
    """A natural-looking still: five octaves of noise, bicubic-upscaled and
    blended (1/f-like spectrum), with no test-pattern structure."""
    octaves = [f"color=gray:s={s}x{s * 9 // 16}:d=1,noise=alls=100:allf=0+u:all_seed={11 + k},"
               f"scale=1920:1080:flags=bicubic,format=gray[o{k}]"
               for k, s in enumerate((24, 64, 180, 480, 1440))]
    mix = ("[o0][o1]blend=all_expr='A*0.45+B*0.55'[m1];[m1][o2]blend=all_expr='A*0.6+B*0.4'[m2];"
           "[m2][o3]blend=all_expr='A*0.7+B*0.3'[m3];[m3][o4]blend=all_expr='A*0.8+B*0.2',"
           "eq=contrast=1.6,format=gray")
    ffmpeg("-filter_complex", ";".join(octaves) + ";" + mix, "-frames:v", "1", str(path))
    return str(path).replace("\\", "/").replace(":", "\\\\:")


def _pan(still, rate, seconds, px_per_s, grain=6):
    """Camera-like pan: drift plus two-frequency handheld shake, temporal grain."""
    x = f"'(iw-640)/2-{px_per_s * seconds / 2}+{px_per_s}*t+3*sin(2*PI*1.3*t)+1.5*sin(2*PI*3.1*t)'"
    y = "'(ih-360)/2+3*sin(2*PI*0.9*t+1)'"
    return (f"movie='{still}',loop=-1:1,fps={rate},trim=duration={seconds},setpts=PTS-STARTPTS,"
            f"crop=640:360:{x}:{y},noise=alls={grain}:allf=t,format=yuv420p")


@pytest.fixture(scope="module")
def still(tmp_path_factory):
    return _texture(tmp_path_factory.mktemp("tex") / "texture.png")


@needs_ffmpeg
class TestInterlaceScan:
    def _encode(self, path, graph, extra=()):
        ffmpeg("-filter_complex", graph, *X264, *extra, "-an", str(path))
        return str(path)

    @pytest.mark.parametrize("px_per_field", [0.75, 2, 6])
    def test_natural_woven_pan_is_combed(self, tmp_path, still, px_per_field):
        graph = _pan(still, 60, 4, px_per_field * 60) + ",tinterlace=mode=interleave_top,setfield=prog"
        path = self._encode(tmp_path / "woven.mp4", graph, ["-r", "30"])
        row = _row(_scan(path), "Interlace Detection")
        assert row[1] == "WARN" and "combing" in row[2], row

    def test_natural_telecine_is_flagged(self, tmp_path, still):
        graph = (_pan(still, "24000/1001", 4, 120, grain=0)
                 + ",telecine=pattern=23,noise=alls=6:allf=t,setfield=prog")
        path = self._encode(tmp_path / "telecine.mp4", graph)
        row = _row(_scan(path, target_fps=29.97, fps_tolerance=0.1), "Interlace Detection")
        assert row[1] == "WARN" and "3:2 pulldown" in row[2], row

    @pytest.mark.parametrize("px_per_s", [60, 300])
    def test_natural_progressive_pan_passes(self, tmp_path, still, px_per_s):
        path = self._encode(tmp_path / "prog.mp4", _pan(still, 30, 4, px_per_s))
        assert _row(_scan(path), "Interlace Detection")[1] == "PASS"

    def test_natural_field_order_mismatch(self, tmp_path, still):
        graph = _pan(still, 60, 4, 240) + ",tinterlace=mode=interleave_top,setfield=bff"
        path = self._encode(tmp_path / "bff.mp4", graph,
                            ["-flags", "+ildct+ilme", "-x264-params", "bff=1", "-r", "30"])
        row = _row(_scan(path, require_progressive=False), "Interlace Detection")
        assert row[1] == "WARN" and "mismatch" in row[2], row


# ---------------------------------------------------------------------------
# EBU R 103 v3.0 low-pass filter at native resolution
# ---------------------------------------------------------------------------

_R103_H = (1, 2, 3, 4, 3, 2, 1)
_R103_V = (1, 2, 1)


def _r103_share(tile, high=246, low=5):
    """Exact share of a periodic tile outside R 103 after its filter.

    ``tile`` is a list of rows of 8-bit luma; the picture is the tile
    repeated, so the filter wraps around. A filtered value counts when it
    rounds outside 5..246 (as an 8-bit pipeline stores it).
    """
    tile = tile.rows()
    th, tw = len(tile), len(tile[0])
    horiz = [[sum(w * row[(x + k - 3) % tw] for k, w in enumerate(_R103_H)) / 16.0
              for x in range(tw)] for row in tile]
    outside = 0
    for y in range(th):
        for x in range(tw):
            v = sum(w * horiz[(y + k - 1) % th][x] for k, w in enumerate(_R103_V)) / 4.0
            outside += v >= high + 0.5 or v < low - 0.5
    return outside / (tw * th)


class _Tile:
    """Rectangles of super-white on black repeated over the picture."""

    def __init__(self, tw, th, rects, level=254, background=16):
        self.tw, self.th, self.rects = tw, th, rects
        self.level, self.background = level, background

    def rows(self):
        tile = [[self.background] * self.tw for _ in range(self.th)]
        for x0, y0, w, h in self.rects:
            for y in range(y0, y0 + h):
                for x in range(x0, x0 + w):
                    tile[y % self.th][x % self.tw] = self.level
        return tile

    def expression(self):
        x, y = f"mod(X,{self.tw})", f"mod(Y,{self.th})"
        inside = "+".join(f"between({x},{x0},{x0 + w - 1})*between({y},{y0},{y0 + h - 1})"
                          for x0, y0, w, h in self.rects)
        return f"if(gt({inside},0),{self.level},{self.background})"


def _tiled_clip(path, tile, width, height, frames=4):
    """A lossless clip of ``tile`` repeated over width x height (codes kept:
    geq writes limited-range YUV directly, as the 0.9.1 luma test does)."""
    source = f"color=black:s={width}x{height}:r=30:d={frames / 30},format=yuv420p,setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
    ffmpeg("-f", "lavfi", "-i", source, "-vf", f"geq=lum='{tile.expression()}':cb=128:cr=128",
           "-frames:v", str(frames), "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
           "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", "-an", str(path))
    return str(path)


def _levels(path, width, height):
    result = _scan(path, target_width=width, target_height=height)
    return _row(result, "Signal Levels"), result.metadata.get("levels_max_luma_pct")


# Tiles whose sizes are not multiples of the 4 x 2 analysis lattice, so the
# squares fall at every lattice phase across the picture.
_SPECULAR = _Tile(127, 95, [(5, 7, 16, 16), (61, 52, 16, 16)])               # 16 px speculars
_SMALL = _Tile(127, 95, [(9, 11, 10, 10), (70, 50, 10, 10), (40, 80, 10, 10)])  # 10 px blobs
_RULES = _Tile(127, 95, [(0, 20, 127, 4)])                                      # 4-line rules


@needs_ffmpeg
class TestR103Filter:
    def test_reference_shares(self):
        """The cases straddle the 1 % rule under the R 103 filter itself."""
        assert 0.012 < _r103_share(_SPECULAR) < 0.03
        assert 0.006 < _r103_share(_SMALL) < 0.0095
        assert 0.012 < _r103_share(_RULES) < 0.03

    def test_uhd_speculars_are_caught(self, tmp_path):
        expected = _r103_share(_SPECULAR)
        path = _tiled_clip(tmp_path / "uhd.mp4", _SPECULAR, 3840, 2160)
        row, share = _levels(path, 3840, 2160)
        assert row[1] == "WARN" and "Luma" in row[2], row
        assert share == pytest.approx(expected * 100, rel=0.1)

    def test_8k_speculars_are_caught(self, tmp_path):
        expected = _r103_share(_SPECULAR)
        path = _tiled_clip(tmp_path / "8k.mp4", _SPECULAR, 7680, 3840, frames=3)
        row, share = _levels(path, 7680, 3840)
        assert row[1] == "WARN" and "Luma" in row[2], row
        assert share == pytest.approx(expected * 100, rel=0.1)

    def test_uhd_thin_rules_are_caught(self, tmp_path):
        expected = _r103_share(_RULES)
        path = _tiled_clip(tmp_path / "rules.mp4", _RULES, 3840, 2160)
        row, share = _levels(path, 3840, 2160)
        assert row[1] == "WARN", row
        assert share == pytest.approx(expected * 100, rel=0.1)

    def test_hd_small_blobs_under_the_limit_pass(self, tmp_path):
        """0.9.1's 4x4 box over-reported these (1.5 %); R 103 keeps them under 1 %."""
        expected = _r103_share(_SMALL)
        path = _tiled_clip(tmp_path / "hd.mp4", _SMALL, 1920, 1080)
        row, share = _levels(path, 1920, 1080)
        assert row[1] == "PASS", row
        assert share == pytest.approx(expected * 100, rel=0.1)


# ---------------------------------------------------------------------------
# File Integrity: absolute truncation tolerance
# ---------------------------------------------------------------------------

def _integrity(expected_frames, decoded_frames, fps=30.0, audio=None):
    from frameguard import core
    probe = {"streams": [{"index": 0, "codec_type": "video", "avg_frame_rate": f"{int(fps)}/1",
                          "nb_frames": str(expected_frames),
                          "duration": f"{expected_frames / fps:.6f}"}],
             "format": {"duration": f"{expected_frames / fps:.6f}"}}
    if audio:
        probe["streams"].append({"index": 1, "codec_type": "audio", "sample_rate": "48000",
                                 "channels": 2, "duration": f"{audio[0]:.6f}"})
    plan = core._ScanPlan(probe, dict(DEFAULT_CONFIG))
    accounting = core._DecodeAccounting()
    accounting.add(f"Input stream #0:0 (video): {decoded_frames} packets read (1 bytes); "
                   f"{decoded_frames} frames decoded; 0 decode errors")
    if audio:
        samples = int(audio[1] * 48000)
        accounting.add(f"Input stream #0:1 (audio): 10 packets read (1 bytes); "
                       f"10 frames decoded; 0 decode errors ({samples} samples)")
    return core._coverage(plan, accounting, None)[1]


class TestTruncationTolerance:
    def test_two_hour_master_missing_ten_seconds_is_truncated(self):
        """0.9.1 accepted up to 2 % missing: 144 s of a 2 h master."""
        frames = 2 * 3600 * 30
        assert _integrity(frames, frames - 300)          # 10 s missing
        assert _integrity(frames, frames - 30)           # 1 s missing

    def test_review_mkv_case_is_truncated(self):
        """45 s MKV cut at 98.5 % of its bytes: 1328 of 1350 frames (0.73 s)."""
        assert _integrity(1350, 1328)

    def test_rounding_of_the_last_frame_is_tolerated(self):
        assert not _integrity(216000, 215998)            # 2 frames
        assert not _integrity(300, 298)

    def test_audio_tolerance_is_absolute(self):
        assert _integrity(300, 300, audio=(3600.0, 3590.0))       # 10 s of audio missing
        assert not _integrity(300, 300, audio=(10.0, 9.95))       # AAC priming/padding


# ---------------------------------------------------------------------------
# Black Frames: 90 % area, both FFmpeg log forms, never PASS unmeasured
# ---------------------------------------------------------------------------

class TestBlackEvidence:
    def test_default_area_is_ninety_percent(self):
        assert DEFAULT_CONFIG["black_detect_picture_threshold"] == 0.90
        assert vc.VideoCheckConfig.from_cfg({}).black_pic_th == 0.90

    @pytest.mark.parametrize("context", ["blackdetect@v", "blackdetect"])
    def test_both_log_forms_are_parsed(self, context):
        """FFmpeg <= 8.0 logs from the filter's private context
        ([blackdetect @ ...]); 8.1+ from the filter instance."""
        from frameguard import core
        log = (f"[{context} @ 0x6000] [verbose] black_min_duration:0 pixel_black_th:0.020000 "
               f"picture_black_ratio_th:0.900000 alpha:0\n"
               f"[{context} @ 0x6000] [info] black_start:2 black_end:2.6 black_duration:0.6\n")
        assert core._black_events(log, 10.0) == [(2.0, 2.6)]

    def test_no_detector_output_is_not_verified(self):
        from frameguard import core
        assert core._black_events("[h264 @ 0x1] [verbose] nothing here\n", 10.0) is None
        result = core.QAResult("x.mp4")
        config = vc.VideoCheckConfig.from_cfg(DEFAULT_CONFIG)
        vc._report_black(result, config, None, 10.0, 30.0, vc.VideoSeries())
        row = [c for c in result.checks if c[0] == vc.BLACK][0]
        assert row[1] == "WARN" and "not verified" in row[2].lower(), row

    def test_measurement_file_runs_win_over_the_log(self):
        """Per-frame black runs from FFmpeg's metadata file are complete even
        when the log lost them."""
        from frameguard import core
        result = core.QAResult("x.mp4")
        config = vc.VideoCheckConfig.from_cfg(DEFAULT_CONFIG)
        series = vc.VideoSeries(times=_times(300), black_runs=[(2.0, 2.6)])
        vc._report_black(result, config, [], 10.0, 30.0, series)
        row = [c for c in result.checks if c[0] == vc.BLACK][0]
        assert row[1] == "WARN" and "00:00:02.000" in row[2], row


@needs_ffmpeg
class TestBlackScan:
    def test_black_with_burnt_in_timecode_and_bug(self, tmp_path):
        """1 s of black carrying a burnt-in timecode box and a channel bug
        (~3 % of the picture not black): 0.9.1's 98 % rule missed it."""
        vf = ("drawbox=c=black:t=fill:enable='between(t,2,3)',"
              "drawbox=x=250:y=320:w=140:h=24:c=white@1:t=fill:enable='between(t,2,3)',"
              "drawbox=x=560:y=20:w=60:h=40:c=0xC0C0C0:t=fill:enable='between(t,2,3)'")
        path = _clip(tmp_path / "tc.mp4", vf)
        row = _row(_scan(path), "Black Frames")
        assert row[1] == "WARN" and "00:00:02.000" in row[2], row


# ---------------------------------------------------------------------------
# File Integrity: decoder error messages count unless known-benign
# ---------------------------------------------------------------------------

class TestDecoderMessages:
    def test_concealed_decoder_error_counts(self):
        from frameguard import core
        accounting = core._DecodeAccounting()
        accounting.add("[hevc @ 0x1] [error] The cu_qp_delta -70 is outside the valid range [-26, 25].")
        accounting.add("Input stream #0:0 (video): 240 packets read (1 bytes); 240 frames decoded; "
                       "0 decode errors")
        assert accounting.total_errors == 1

    def test_benign_messages_are_listed_and_justified(self):
        assert vc.BENIGN_DECODER_ERRORS
        for rule in vc.BENIGN_DECODER_ERRORS:
            assert rule.pattern and len(rule.reason) > 30

    def test_null_muxer_timestamp_complaint_is_benign(self):
        """R7a: jittered timestamps make FFmpeg's null muxer complain about
        FrameGuard's own output; the CFR check judges timing."""
        from frameguard import core
        accounting = core._DecodeAccounting()
        accounting.add("[null @ 0x2] [error] Application provided invalid, non monotonically "
                       "increasing dts to muxer in stream 0: 3 >= 3")
        assert accounting.total_errors == 0

    def test_failed_frame_is_not_counted_twice(self):
        from frameguard import core
        accounting = core._DecodeAccounting()
        accounting.add("[vist#0:0/h264 @ 0x3] [error] Error while decoding stream #0:0: "
                       "Invalid data found when processing input")
        accounting.add("[h264 @ 0x4] [error] Invalid NAL unit size (1000 > 20).")
        accounting.add("Input stream #0:0 (video): 10 packets read (1 bytes); 9 frames decoded; "
                       "1 decode errors")
        assert accounting.total_errors == 1


@needs_ffmpeg
class TestDecoderMessagesScan:
    def test_hevc_slice_corruption_fails(self, tmp_path):
        """Held-out review case: FFmpeg prints a decoder error for damaged
        HEVC slice data but its accounting shows 0 decode errors."""
        import random
        import subprocess
        from frameguard.tools import get_ffprobe_path
        clean = tmp_path / "hevc.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8", "-c:v", "libx265",
               "-x265-params", "log-level=error:keyint=60", "-tag:v", "hvc1",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               str(clean))
        packets = subprocess.run([get_ffprobe_path(), "-v", "error", "-select_streams", "v",
                                  "-show_entries", "packet=pos,size", "-of", "csv=p=0", str(clean)],
                                 capture_output=True, text=True).stdout.split()
        data = bytearray(clean.read_bytes())
        rng = random.Random(1)
        for entry in packets[70:75]:
            size, pos = (int(v) for v in entry.split(","))
            for k in range(pos + 40, min(pos + 60, pos + size)):
                data[k] = rng.randrange(256)
        damaged = tmp_path / "damaged.mp4"
        damaged.write_bytes(bytes(data))
        result = _scan(str(damaged), audio_required=False)
        row = _row(result, "File Integrity")
        assert row[1] == "FAIL" and "decoder error" in row[2], row

    def test_jittered_timestamps_stay_clean(self, tmp_path):
        path = str(tmp_path / "jitter.mkv")
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4",
               "-vf", "setpts='(N+0.4*sin(N))/30/TB'", "-fps_mode", "passthrough",
               "-c:v", "libx264", "-bf", "0", path)
        row = _row(_scan(path, require_cfr=False), "File Integrity")
        assert row[1] == "PASS", row


# ---------------------------------------------------------------------------
# Colour metadata: consistent primaries / transfer / matrix, complete tagging
# ---------------------------------------------------------------------------

def _colour(primaries="bt709", transfer="bt709", matrix="bt709", width=1920, **extra):
    from frameguard import core
    video = {"index": 0, "codec_type": "video", "width": width, "height": 1080,
             "pix_fmt": "yuv420p10le", "color_primaries": primaries, "color_transfer": transfer,
             "color_space": matrix, **extra}
    result = core.QAResult("x.mp4")
    core.check_color_hdr({"streams": [video], "format": {}}, result, dict(DEFAULT_CONFIG))
    return _row(result, "Color / HDR")


class TestColourTags:
    def test_consistent_sets_pass(self):
        assert _colour()[1] == "PASS"
        assert _colour("bt2020", "smpte2084", "bt2020nc",
                       side_data_list=[])[1] in ("PASS", "WARN")   # HDR metadata gaps are separate
        assert _colour("bt2020", "bt2020-10", "bt2020nc")[1] == "PASS"
        assert _colour("bt2020", "bt709", "bt2020nc")[1] == "PASS"   # same OETF (H.273 note)
        assert _colour("smpte170m", "smpte170m", "smpte170m", width=720)[1] == "PASS"

    def test_bt2020_primaries_on_bt709_matrix(self):
        """Held-out review case: BT.2020 primaries with BT.709 transfer and matrix."""
        row = _colour("bt2020", "bt709", "bt709")
        assert row[1] == "WARN" and "BT.2020 primaries" in row[2] and "matrix" in row[2], row

    def test_bt709_primaries_with_bt2020_matrix(self):
        row = _colour("bt709", "bt709", "bt2020nc")
        assert row[1] == "WARN" and "matrix" in row[2], row

    def test_pq_needs_bt2020_primaries(self):
        row = _colour("bt709", "smpte2084", "bt709")
        assert row[1] == "WARN" and "BT.2100" in row[2], row

    def test_hlg_with_bt709_matrix(self):
        row = _colour("bt2020", "arib-std-b67", "bt709")
        assert row[1] == "WARN" and "BT.2100" in row[2], row

    def test_partial_tagging_is_flagged(self):
        """x264's common case: only the matrix is signalled."""
        row = _colour("unknown", "unknown", "bt709")
        assert row[1] == "WARN" and "partly" in row[2], row
        row = _colour("bt709", "unknown", "bt709")
        assert row[1] == "WARN" and "transfer" in row[2], row

    def test_partial_tagging_follows_the_untagged_setting(self):
        from frameguard import core
        video = {"index": 0, "codec_type": "video", "width": 1920, "height": 1080,
                 "pix_fmt": "yuv420p", "color_space": "bt709"}
        result = core.QAResult("x.mp4")
        core.check_color_hdr({"streams": [video], "format": {}}, result,
                             dict(DEFAULT_CONFIG, warn_untagged_color=False))
        assert _row(result, "Color / HDR")[1] == "PASS"


# ---------------------------------------------------------------------------
# Freeze: structural stillness, abrupt stop, composed holds, tail freezes
# ---------------------------------------------------------------------------

def _freeze_case(segments, fps=FPS):
    """mafd / structural series from (frames, mafd, structural) segments; a
    segment with mafd 'cut' is a single scene-change frame."""
    mafd, structural = [], []
    for frames, m, s in segments:
        if m == "cut":
            mafd.append(40.0)
            structural.append(30.0)
            continue
        for k in range(frames):
            wobble = 1.0 + 0.1 * ((k * 7) % 5 - 2)
            mafd.append(m * wobble)
            structural.append(s * wobble)
    mafd[0] = 0.0
    return mafd, structural, _times(len(mafd), fps)


def _freezes(segments, hold=3.0, minimum=1.0):
    mafd, structural, times = _freeze_case(segments)
    duration = len(times) / FPS
    found = vc.detect_freezes(mafd, times, FPS, minimum, duration, structural=structural,
                              hold_allowance=hold)
    return found, vc.freeze_entry(found, duration, vc.VideoCheckConfig(freeze_hold=hold))


BUSY = (2.6, 2.0)            # moving picture: full-res and structural change
GRAINY_STILL = (0.15, 0.003)  # nothing moves; temporal grain only
IDENTICAL = (0.0, 0.0)


class TestFreezeRules:
    def test_title_card_hold_within_the_allowance(self):
        found, (status, detail, _) = _freezes([(90, *BUSY), (1, "cut", 0), (45, *IDENTICAL),
                                               (1, "cut", 0), (90, *BUSY)])
        assert found.holds == [] and status == "PASS" and "hold" in detail.lower()

    def test_hold_allowance_is_a_setting(self):
        found, (status, _, _) = _freezes([(90, *BUSY), (1, "cut", 0), (45, *IDENTICAL),
                                          (1, "cut", 0), (90, *BUSY)], hold=1.0)
        assert found.holds and status == "WARN"

    def test_freeze_under_grain_mid_shot(self):
        """Finishing grain over a frozen picture: full-res change stays at
        grain level, picture structure stops dead mid-motion."""
        found, (status, _, _) = _freezes([(90, *BUSY), (60, *GRAINY_STILL), (90, *BUSY)])
        assert len(found.holds) == 1 and status == "WARN"
        assert found.holds[0][1] == pytest.approx(60 / FPS, abs=0.1)

    def test_quiet_night_shot_between_busy_shots_is_not_a_freeze(self):
        found, (status, _, _) = _freezes([(90, *BUSY), (1, "cut", 0), (90, 0.2, 0.01),
                                          (1, "cut", 0), (90, *BUSY)])
        assert found.holds == [] and status == "PASS"

    def test_gradual_stop_is_not_a_freeze(self):
        # Grain stays; motion (and so the structural change) eases out over
        # a second (15 % less each frame).
        segments = [(90, *BUSY)] + [(1, 0.15 + 2.45 * 0.85 ** k, 0.003 + 2.0 * 0.85 ** k)
                                    for k in range(1, 31)]
        segments += [(60, *GRAINY_STILL), (60, *BUSY)]
        found, _ = _freezes(segments)
        assert found.holds == []

    def test_freeze_running_to_the_end_is_reported(self):
        """The render ran out of media: the last 2.5 s are frozen mid-shot."""
        found, (status, detail, _) = _freezes([(150, *BUSY), (75, *IDENTICAL)])
        assert found.holds and status == "WARN" and "end card" not in detail

    def test_end_card_after_a_cut_is_a_hold(self):
        found, (status, _, _) = _freezes([(150, *BUSY), (1, "cut", 0), (60, *IDENTICAL)])
        assert found.holds == [] and status == "PASS"


def _hold_allowance_setting():
    from frameguard import presets
    assert DEFAULT_CONFIG["freeze_hold_allowance"] == 3.0
    assert presets.validate_preset_config({"freeze_hold_allowance": 0})["freeze_hold_allowance"] == 0
    with pytest.raises(presets.PresetValidationError):
        presets.validate_preset_config({"freeze_hold_allowance": -1})


def test_hold_allowance_setting_follows_the_preset_pattern():
    _hold_allowance_setting()


@needs_ffmpeg
class TestFreezeScan:
    def test_title_card_hold_passes(self, tmp_path):
        """A 1.5 s static title card cut into moving footage (review: 0.9.1
        tripped its 1 s freeze threshold)."""
        graph = (f"testsrc2=s={SIZE}:r=30:d=6,{GRAIN},format=yuv420p[v];"
                 "color=c=0x202060:s=640x360:r=30:d=6,format=yuv420p,"
                 "drawbox=x=170:y=150:w=300:h=60:c=white:t=fill[card];"
                 "[v][card]overlay=enable='between(t,2,3.5)'")
        path = str(tmp_path / "card.mp4")
        ffmpeg("-filter_complex", graph, *X264, "-an", path)
        row = _row(_scan(path), "Freeze Frames")
        assert row[1] == "PASS", row

    def test_freeze_under_finishing_grain(self, tmp_path):
        source = (f"testsrc2=s={SIZE}:r=30:d=8,split[a][b];"
                  "[a][b]freezeframes=first=60:last=135:replace=60,noise=alls=10:allf=t")
        path = _clip(tmp_path / "grainfreeze.mp4", source=source)
        row = _row(_scan(path), "Freeze Frames")
        assert row[1] == "WARN" and "00:00:02.000" in row[2], row

    def test_freeze_at_the_very_end(self, tmp_path):
        source = (f"testsrc2=s={SIZE}:r=30:d=8,{GRAIN},split[a][b];"
                  "[a][b]freezeframes=first=165:last=239:replace=165")
        path = _clip(tmp_path / "tail.mp4", source=source)
        row = _row(_scan(path), "Freeze Frames")
        assert row[1] == "WARN" and "end card" not in row[2], row

    def test_still_grainy_shot_between_busy_shots_passes(self, tmp_path, still):
        """A locked-off, dark, grainy shot cut between two moving shots."""
        busy = f"testsrc2=s={SIZE}:r=30:d=3,{GRAIN},format=yuv420p"
        quiet = (f"movie='{still}',loop=-1:1,fps=30,trim=duration=3,setpts=PTS-STARTPTS,"
                 "crop=640:360:600:300,eq=brightness=-0.3:contrast=0.4,noise=alls=4:allf=t,"
                 "format=yuv420p")
        graph = f"{busy}[a];{quiet}[b];{busy}[c];[a][b][c]concat=n=3:v=1:a=0"
        path = str(tmp_path / "night.mp4")
        ffmpeg("-filter_complex", graph, *X264, "-an", path)
        row = _row(_scan(path), "Freeze Frames")
        assert row[1] == "PASS", row


def test_hold_allowance_has_a_settings_control(qapp):
    from frameguard.gui import SettingsPanel
    panel = SettingsPanel()
    assert panel.get_config_overrides()["freeze_hold_allowance"] == 3.0
    panel._apply_preset_config({**panel.get_config_overrides(), "freeze_hold_allowance": 5.0})
    assert panel.get_config_overrides()["freeze_hold_allowance"] == 5.0


def test_hold_allowance_is_listed_in_the_pdf_spec():
    from frameguard import report
    assert "hold ≤3.0 s" in report._black_freeze_spec(dict(DEFAULT_CONFIG))


# ---------------------------------------------------------------------------
# Black: edit gaps in dark scenes; black in the first / last second
# ---------------------------------------------------------------------------

class TestBlackEditGaps:
    cfg = vc.VideoCheckConfig()

    def test_edit_gap_in_a_night_scene(self):
        """5 black frames cut hard into a night grade (mean luma 25 -> 16 -> 25)."""
        yavg = [25.0 + 0.2 * ((k * 3) % 4) for k in range(300)]
        for i in range(120, 125):
            yavg[i] = 16.0
        found = vc.classify_black([(4.0, 4.0 + 5 / FPS)], 10.0, FPS, self.cfg, _times(300), yavg)
        assert found.bursts == [(4.0, 5)]

    def test_fade_in_a_night_scene_is_not_an_edit_gap(self):
        yavg = [25.0] * 300
        for i, v in zip(range(114, 120), (24, 23, 21, 19, 18, 17)):
            yavg[i] = float(v)
        for i in range(120, 125):
            yavg[i] = 16.0
        for i, v in zip(range(125, 131), (17, 18, 19, 21, 23, 24)):
            yavg[i] = float(v)
        found = vc.classify_black([(4.0, 4.0 + 5 / FPS)], 10.0, FPS, self.cfg, _times(300), yavg)
        assert found.bursts == [] and found.ignored_bursts == 1
