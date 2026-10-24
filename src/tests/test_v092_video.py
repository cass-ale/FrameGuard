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
