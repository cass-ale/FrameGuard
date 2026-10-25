"""0.9.2 picture-check fixes from the independent 0.9.1 review.

Each class pins one defect: the counter-example the review used (or the
standard's own case) must be caught, and the standard's hard negatives must
still pass. Pure-function tests come first; the real-FFmpeg tests build
small clips with the bundled FFmpeg and run the full deep scan.
"""

import math
import os

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
        # The first transition is frame 29 -> 30: the event starts at 1.000 s
        # (the streamed grid starts one frame late, FLASH_GRID_OFFSET).
        assert "at 00:00:01.000-" in row[2], row

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
# Interlace (idet documented defaults; field order; telecine; field timing)
# ---------------------------------------------------------------------------

class TestInterlaceRules:
    def test_idet_runs_with_its_documented_defaults(self):
        with vc.VideoAnalysis({"index": 0, "width": 640, "height": 360, "avg_frame_rate": "30/1"},
                              dict(DEFAULT_CONFIG)) as analysis:
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


def _r103_gamut_reference(raw, width, height, siting):
    """Per-frame largest R, G or B share (%) outside -5..105 % after R 103.

    Independent of FFmpeg's scaler: 4:2:0 chroma is interpolated linearly
    with its samples at the signalled siting (left: x = 2k, y = 2n + 0.5;
    center: x = 2k + 0.5, y = 2n + 0.5, in luma pixels), Y, Cb and Cr are
    filtered with the R 103 kernels (edges replicated) and rounded to 8 bits,
    and BT.709 limited-range R'G'B' is tested per channel.
    """
    W, H = width, height
    w2, h2 = W // 2, H // 2
    ox = 0.0 if siting in ("left", "topleft") else 0.5
    oy = 0.0 if siting == "topleft" else 0.5
    frame = W * H * 3 // 2

    def clamp(v, n):
        return min(max(v, 0), n - 1)

    def filt(p):
        hor = [[sum(k * row[clamp(x + i - 3, W)] for i, k in enumerate(_R103_H)) / 16.0
                for x in range(W)] for row in p]
        return [[(hor[clamp(y - 1, H)][x] + 2 * hor[y][x] + hor[clamp(y + 1, H)][x]) / 4.0
                 for x in range(W)] for y in range(H)]

    def up(c):
        out = []
        for y in range(H):
            fy = (y - oy) / 2.0
            y0 = math.floor(fy)
            ty = fy - y0
            r0, r1 = c[clamp(y0, h2)], c[clamp(y0 + 1, h2)]
            row = []
            for x in range(W):
                fx = (x - ox) / 2.0
                x0 = math.floor(fx)
                tx = fx - x0
                a0, a1 = clamp(x0, w2), clamp(x0 + 1, w2)
                row.append((r0[a0] * (1 - tx) + r0[a1] * tx) * (1 - ty)
                           + (r1[a0] * (1 - tx) + r1[a1] * tx) * ty)
            out.append(row)
        return out

    shares = []
    for k in range(len(raw) // frame):
        f = raw[k * frame:(k + 1) * frame]
        Y = [list(f[y * W:(y + 1) * W]) for y in range(H)]
        U = [list(f[W * H + y * w2:W * H + (y + 1) * w2]) for y in range(h2)]
        V = [list(f[W * H + w2 * h2 + y * w2:W * H + w2 * h2 + (y + 1) * w2]) for y in range(h2)]
        Yf, Uf, Vf = filt(Y), filt(up(U)), filt(up(V))
        # The pixels FrameGuard measures: whole 4 x 2 phase cells from column
        # 3 and line 1 (the kernels' full support; see _r103_exact).
        xs = range(3, 3 + ((W - 3) // 4 - 1) * 4)
        ys = range(1, 1 + ((H - 1) // 2 - 1) * 2)
        counts = [0, 0, 0]
        for y in ys:
            for x in xs:
                l = (int(Yf[y][x] + 0.5) - 16) / 219.0
                cb = (int(Uf[y][x] + 0.5) - 128) / 224.0
                cr = (int(Vf[y][x] + 0.5) - 128) / 224.0
                for c, v in enumerate((l + 1.5748 * cr, l - 0.1873 * cb - 0.4681 * cr, l + 1.8556 * cb)):
                    counts[c] += v < -0.05 or v > 1.05
        shares.append(100.0 * max(counts) / (len(xs) * len(ys)))
    return shares


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
        assert share == pytest.approx(expected * 100, rel=0.03)

    def test_8k_speculars_are_caught(self, tmp_path):
        expected = _r103_share(_SPECULAR)
        path = _tiled_clip(tmp_path / "8k.mp4", _SPECULAR, 7680, 3840, frames=3)
        row, share = _levels(path, 7680, 3840)
        assert row[1] == "WARN" and "Luma" in row[2], row
        assert share == pytest.approx(expected * 100, rel=0.03)

    def test_uhd_thin_rules_are_caught(self, tmp_path):
        expected = _r103_share(_RULES)
        path = _tiled_clip(tmp_path / "rules.mp4", _RULES, 3840, 2160)
        row, share = _levels(path, 3840, 2160)
        assert row[1] == "WARN", row
        assert share == pytest.approx(expected * 100, rel=0.03)

    def test_grid_aligned_thin_bars_are_measured_exactly(self, tmp_path):
        """Round 3 bug 5: 3 px super-white bars on a 235 background, on a
        4 px-aligned grid; only their centre column exceeds 246 after the
        filter. Sampling one phase in eight misses them all."""
        tile = _Tile(128, 96, [(k * 40 + 3, 0, 3, 96) for k in range(3)], background=235)
        expected = _r103_share(tile)
        path = _tiled_clip(tmp_path / "bars.mp4", tile, 1920, 1080)
        row, share = _levels(path, 1920, 1080)
        assert row[1] == "WARN", row
        assert share == pytest.approx(expected * 100, rel=0.03)

    def test_interlaced_signal_is_filtered_intra_field(self, tmp_path):
        """Round 3 bug 4: R 103's vertical filter is "applied intra field for
        interlace signals". A block whose top field is super-white (250) and
        bottom field black: within a field it stays at 250 (> 246); a
        frame-based 1-2-1 filter would average it to ~133."""
        path = str(tmp_path / "fields.mp4")
        lum = "if(between(X,100,299)*between(Y,100,199),if(mod(Y,2),16,250),60)"
        ffmpeg("-f", "lavfi", "-i", "color=black:s=640x360:r=30:d=0.2,format=yuv420p,"
               "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709",
               "-vf", f"geq=lum='{lum}':cb=128:cr=128,setparams=field_mode=tff",
               "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-flags", "+ildct+ilme",
               "-color_range", "tv", "-colorspace", "bt709", "-an", path)
        row, share = _levels(path, 640, 360)
        assert row[1] == "WARN" and "Luma" in row[2], row

    @pytest.mark.parametrize("level", [247, 246])
    def test_one_code_over_the_limit_is_exact(self, tmp_path, level):
        """A flat 247 is outside R 103 luma (5-246) on every pixel; 246 on
        none. The 16 -> 8-bit narrowing used to dither and mis-scale (v x 257
        rounding against swscale's v x 256), reading part of 247 as 246.
        (Grey 246 is still outside the RGB range: 105.02 %.)"""
        tile = _Tile(128, 96, [(20, 20, 60, 50)], level=level, background=128)
        expected = _r103_share(tile)
        path = _tiled_clip(tmp_path / f"flat{level}.mp4", tile, 1280, 720, frames=3)
        row, share = _levels(path, 1280, 720)
        # 2 %: the 3 edge columns and 1 edge line the kernels cannot reach
        assert (share or 0.0) == pytest.approx(expected * 100, rel=0.02, abs=1e-9), (row, expected)

    @pytest.mark.parametrize("siting", ["left", "center"])
    def test_gamut_honours_the_chroma_siting(self, tmp_path, siting):
        """Review C1: the exact path upsampled 4:2:0 chroma with swscale's
        default centre siting, reading gamut about 2x low on the left-sited
        H.264/HEVC default (reviewer's clean_hevc repro: 0.74 % PASS against
        1.6-1.8 % with > 1 % on most frames). Checked against an independent
        pure-Python R 103 reference that interpolates chroma at the signalled
        siting; the clip's siting is set in the bitstream."""
        import subprocess
        from frameguard.tools import get_ffmpeg_path, get_ffprobe_path
        clean = tmp_path / "x265.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=0.34", "-c:v", "libx265",
               "-x265-params", "log-level=error", "-pix_fmt", "yuv420p", "-color_range", "tv",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(clean))
        path = tmp_path / f"{siting}.mp4"
        loc = {"left": 0, "center": 1}[siting]
        ffmpeg("-i", str(clean), "-c", "copy", "-bsf:v", f"hevc_metadata=chroma_sample_loc_type={loc}",
               str(path))
        probed = subprocess.run([get_ffprobe_path(), "-v", "error", "-show_entries", "stream=chroma_location",
                                 "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
        assert probed.strip().splitlines()[0] == siting
        raw = subprocess.run([get_ffmpeg_path(), "-v", "error", "-i", str(path), "-f", "rawvideo",
                              "-pix_fmt", "yuv420p", "-"], capture_output=True).stdout
        expected = max(_r103_gamut_reference(raw, 320, 240, siting))
        result = _scan(str(path), target_width=320, target_height=240)
        measured = result.metadata.get("levels_max_gamut_pct")
        assert measured == pytest.approx(expected, rel=0.03), (measured, expected)
        if siting == "left":
            assert expected > 1.0 and result.metadata.get("levels_max_gamut_pct") > 1.0

    def test_hd_small_blobs_under_the_limit_pass(self, tmp_path):
        """0.9.1's 4x4 box over-reported these (1.5 %); R 103 keeps them under 1 %."""
        expected = _r103_share(_SMALL)
        path = _tiled_clip(tmp_path / "hd.mp4", _SMALL, 1920, 1080)
        row, share = _levels(path, 1920, 1080)
        assert row[1] == "PASS", row
        assert share == pytest.approx(expected * 100, rel=0.03)


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
# Black Frames: 98 % area plus a static overlay, both log forms, never PASS unmeasured
# ---------------------------------------------------------------------------

class TestBlackEvidence:
    def test_default_area_is_ninety_eight_percent(self):
        """Round 2: FFmpeg's default; Telestream VQ-101 documents 100 %."""
        assert DEFAULT_CONFIG["black_detect_picture_threshold"] == 0.98
        assert vc.VideoCheckConfig.from_cfg({}).black_pic_th == 0.98

    def test_overlay_rule(self):
        times = [i / 30 for i in range(30)]
        logo = [0.04] * 30                       # 4 % non-black, in place
        runs = vc.overlay_black_runs(logo, [0.04 if i % 9 == 0 else None for i in range(30)],
                                     times, 9, 0.98, 1 / 30)
        assert runs and runs[0][0] == 0.0
        # moving text: little stays non-black in both frames
        assert not vc.overlay_black_runs(logo, [0.005 if i % 9 == 0 else None for i in range(30)],
                                         times, 9, 0.98, 1 / 30)
        # too large to be an overlay
        assert not vc.overlay_black_runs([0.09] * 30, [0.09 if i % 9 == 0 else None for i in range(30)],
                                         times, 9, 0.98, 1 / 30)
        # too short to show it stays in place
        assert not vc.overlay_black_runs([0.04] * 8, [0.04] + [None] * 7, times[:8], 9, 0.98, 1 / 30)
        assert vc.merge_runs([(0, 1), (2, 3)], [(0.5, 2.1)]) == [(0, 3)]

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
        HEVC slice data but its accounting shows 0 decode errors.

        Encoder-independent (review round 3, bug 9): the slice NAL units are
        located by parsing the samples (4-byte length prefixes, HEVC NAL
        header type 0-31 = VCL), only slice payload bytes are damaged, and
        the test first asserts that FFmpeg itself reports a decoder error,
        so it cannot pass vacuously on another x265 build."""
        import random
        import subprocess
        from frameguard.tools import get_ffmpeg_path, get_ffprobe_path
        clean = tmp_path / "hevc.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=8", "-c:v", "libx265",
               "-x265-params", "log-level=error:keyint=60", "-tag:v", "hvc1",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               str(clean))
        packets = subprocess.run([get_ffprobe_path(), "-v", "error", "-select_streams", "v",
                                  "-show_entries", "packet=pos,size", "-of", "csv=p=0", str(clean)],
                                 capture_output=True, text=True).stdout.split()
        original = clean.read_bytes()

        def slices(pos, size):
            """(start, end) of each VCL NAL unit's payload in one sample."""
            found, offset = [], pos
            while offset + 6 <= pos + size:
                length = int.from_bytes(original[offset:offset + 4], "big")
                nal_type = (original[offset + 4] >> 1) & 0x3F
                if nal_type < 32 and length > 16:
                    found.append((offset + 4 + 2, offset + 4 + length))
                offset += 4 + length
            return found

        damaged = tmp_path / "damaged.mp4"
        rng = random.Random(1)
        for attempt in range(8):
            data = bytearray(original)
            for entry in packets[70 + 5 * attempt:75 + 5 * attempt]:
                size, pos = (int(v) for v in entry.split(","))
                for start, end in slices(pos, size):
                    # Slice data, past the slice header: the middle third.
                    third = (end - start) // 3
                    for k in range(start + third, start + 2 * third):
                        data[k] = rng.randrange(256)
            damaged.write_bytes(bytes(data))
            report = subprocess.run([get_ffmpeg_path(), "-v", "error", "-i", str(damaged),
                                     "-f", "null", "-"], capture_output=True, text=True).stderr
            if "hevc" in report.lower():
                break
        assert "hevc" in report.lower(), "FFmpeg reported no HEVC decoder error for the damage"
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

    def test_freeze_is_found_with_signal_levels_off(self, tmp_path):
        """The structural series comes from the area branch, which must run
        for the freeze check alone."""
        source = (f"testsrc2=s={SIZE}:r=30:d=6,split[a][b];"
                  "[a][b]freezeframes=first=60:last=135:replace=60,noise=alls=10:allf=t")
        path = _clip(tmp_path / "freeze_nolevels.mp4", source=source)
        row = _row(_scan(path, signal_levels_mode="off", black_frames_enabled=False), "Freeze Frames")
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

class TestBlackEdges:
    cfg = vc.VideoCheckConfig()

    def test_black_inside_the_first_second_after_picture_is_reported(self):
        """0.6 s of black starting at 0.2 s: picture, then black - not a lead-in."""
        found = vc.classify_black([(0.2, 0.8)], 10.0, FPS, self.cfg)
        assert found.segments == [(0.2, pytest.approx(0.6))]

    def test_head_and_tail_black_is_noted_not_dropped(self):
        found = vc.classify_black([(0.0, 0.6), (9.5, 10.0)], 10.0, FPS, self.cfg)
        assert found.segments == [] and found.edge_segments
        status, detail, _ = vc.black_entry(found, 10.0, self.cfg)
        assert status == "PASS" and "00:00:00.000" in detail and "00:00:09.500" in detail


# ---------------------------------------------------------------------------
# CFR: timestamps against the nominal rate
# ---------------------------------------------------------------------------

def _cfr(times, rate="30/1", time_base="1/1000"):
    from frameguard import core
    probe = {"streams": [{"index": 0, "codec_type": "video", "r_frame_rate": rate,
                          "avg_frame_rate": rate, "time_base": time_base}], "format": {}}
    result = core.QAResult("x.mkv")
    core.check_cfr(probe, result, dict(DEFAULT_CONFIG), core.FrameTiming([list(times)], False, "all"))
    return _row(result, "CFR Check"), result.metadata


class TestCfrNominalRate:
    def test_fifth_of_a_frame_jitter_is_not_constant(self):
        """Review B12: +-0.2-frame sinusoidal jitter passed as 'CFR 24.399 fps'."""
        row, meta = _cfr([(k + 0.2 * math.sin(k)) / 30 for k in range(300)])
        assert row[1] != "PASS" and "30.000 fps" in row[2], row

    def test_large_jitter_reports_the_nominal_rate(self):
        row, _ = _cfr([(k + 0.4 * math.sin(k)) / 30 for k in range(300)])
        assert row[1] == "FAIL" and "30.000 fps" in row[2] and "102" not in row[2], row

    def test_millisecond_rounding_is_constant(self):
        row, _ = _cfr([round(k / 30, 3) for k in range(300)])
        assert row[1] == "PASS", row

    def test_ntsc_rate(self):
        row, _ = _cfr([k * 1001 / 30000 for k in range(300)], "30000/1001", "1/30000")
        assert row[1] == "PASS" and "29.970" in row[2], row


class TestCfrTimestampRounding:
    """Round 4: timestamps rounded to an earlier container's grid, then
    re-expressed in a finer time base (MKV 1 ms -> MP4 1/16000), are CFR."""

    def test_millisecond_grid_in_a_finer_time_base(self):
        times = [round(k * 1001 / 30000, 3) for k in range(300)]          # MKV 1 ms
        row, _ = _cfr([round(t * 16000) / 16000 for t in times], "30000/1001", "1/16000")
        assert row[1] == "PASS", row
        times = [round(k * 1001 / 24000, 3) for k in range(300)]
        row, _ = _cfr([round(t * 24000) / 24000 for t in times], "24000/1001", "1/24000")
        assert row[1] == "PASS", row

    def test_jitter_on_a_fine_time_base_is_still_caught(self):
        row, _ = _cfr([round((k + 0.2 * math.sin(k)) / 30 * 15360) / 15360 for k in range(300)],
                      "30/1", "1/15360")
        assert row[1] != "PASS", row

    def test_alternating_jitter_is_not_a_coarse_grid(self):
        """Gaps alternating 0.8 / 1.2 frames lie on a 13 ms grid; no container
        rounds that coarsely."""
        times, t = [], 0.0
        for k in range(300):
            times.append(t)
            t += (0.8 if k % 2 else 1.2) / 30
        row, _ = _cfr([round(x * 30000) / 30000 for x in times], "30/1", "1/30000")
        assert row[1] != "PASS", row

    def test_whole_frame_gap_is_a_dropped_frame(self):
        times = [k / 30 for k in range(300) if k != 150]
        row, meta = _cfr(times)
        assert row[1] == "WARN" and "dropped" in row[2] and "Variable" not in row[2], row
        assert meta.get("dropped_frames") == 1

    def test_dropped_frames_are_not_variable_frame_rate(self):
        times = [k / 30 for k in range(300) if k not in (50, 100, 101, 200, 250)]
        row, meta = _cfr(times)
        # 4 gaps in 295 intervals (> vfr_threshold 1 %): a FAIL, named as drops
        assert row[1] == "FAIL" and "5 dropped" in row[2] and "Variable" not in row[2], row
        assert meta.get("dropped_frames") == 5

    def test_repeated_timestamp_is_a_duplicated_frame(self):
        times = [k / 30 for k in range(300)]
        times.insert(150, times[150])
        row, meta = _cfr(times)
        assert row[1] == "WARN" and "duplicated" in row[2], row


@needs_ffmpeg
class TestCfrRemux:
    """Round 4 (sealed corpus): a plain -c copy remux of a clean 29.97 /
    23.976 clip between MKV and MP4 must stay CFR."""

    @pytest.mark.parametrize("rate", ["30000/1001", "24000/1001"])
    @pytest.mark.parametrize("first,second", [("mkv", "mp4"), ("mp4", "mkv")])
    def test_remux_stays_constant(self, tmp_path, rate, first, second):
        source = tmp_path / f"src.{first}"
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=320x240:r={rate}:d=6", *X264, "-an", str(source))
        remux = tmp_path / f"remux.{second}"
        ffmpeg("-i", str(source), "-c", "copy", str(remux))
        row = _row(_scan(str(remux), target_width=320, target_height=240), "CFR Check")
        assert row[1] == "PASS", row

    def test_one_dropped_frame_is_reported_as_dropped(self, tmp_path):
        path = tmp_path / "drop.mkv"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=6", "-vf", "select='not(eq(n,90))'",
               "-fps_mode", "passthrough", *X264, "-an", str(path))
        row = _row(_scan(str(path), target_width=320, target_height=240), "CFR Check")
        assert row[1] == "WARN" and "dropped" in row[2] and "Variable" not in row[2], row


# ---------------------------------------------------------------------------
# No FFmpeg summary -> Not verified, never PASS
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestNoSummaryIsNotVerified:
    def test_missing_measurement_files_are_not_verified(self, tmp_path, monkeypatch):
        """FFmpeg decoded the frames but wrote none of a check's summaries
        (an older build, a filter that did not run): those rows must say
        Not verified."""
        path = _clip(tmp_path / "clean.mp4", seconds=3)
        real_collect = vc.VideoAnalysis.collect

        def collect_without_summaries(self):
            for name, file in self.paths.items():
                if name != "mafd" and os.path.exists(file):
                    os.remove(file)
            return real_collect(self)

        monkeypatch.setattr(vc.VideoAnalysis, "collect", collect_without_summaries)
        result = _scan(path)
        for name in (vc.FREEZE, vc.LEVELS, vc.INTERLACE):
            row = _row(result, name)
            assert row[1] == "WARN" and "not verified" in row[2].lower(), row
        # Black Frames still has blackdetect's own log record (see TestBlackEvidence
        # for the case where that is missing too).
        assert _row(result, vc.BLACK)[1] == "PASS"


# ---------------------------------------------------------------------------
# Interlace: progressive test patterns are not woven (integration regression)
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestProgressivePatternsAreNotWoven:
    """The fixtures' clean clip (progressive testsrc2, 320x240) read as
    '87 % interlaced, combing' in the merged 0.9.2 suite: idet's 4 %
    prev/next asymmetry fires on its moving graphics, and a 2 s clip has a
    single section, so the field-order steadiness rule could not help."""

    @pytest.mark.parametrize("seconds", [2, 10])
    @pytest.mark.parametrize("size", ["320x240", "640x360"])
    def test_progressive_testsrc2_is_not_flagged(self, tmp_path, seconds, size):
        from media_fixtures import make_clip
        path = make_clip(tmp_path / f"ts_{size}_{seconds}.mp4", duration=seconds, size=size,
                         audio=())
        width, height = (int(v) for v in size.split("x"))
        row = _row(_scan(path, target_width=width, target_height=height), "Interlace Detection")
        assert row[1] == "PASS", row


@needs_ffmpeg
class TestProgressiveCreditRollsAreNotWoven:
    """End credits: hard-edged white text bars scrolling up on black, 1-4
    lines per frame (the merged-candidate corpus gate flagged one as woven)."""

    @pytest.mark.parametrize("speed", [1, 2, 3, 4])
    def test_text_bar_roll(self, tmp_path, speed):
        expr = (f"if(lt(mod(Y+{speed}*N,60),12)*between(X,150,490)"
                f"*lt(mod(X+floor((Y+{speed}*N)/60)*37,90),70),235,16)")
        path = _clip(tmp_path / f"bars{speed}.mp4", f"geq=lum='{expr}':cb=128:cr=128",
                     source="color=black:s=640x360:r=30:d=4,format=yuv420p")
        assert _row(_scan(path), "Interlace Detection")[1] == "PASS"

    @pytest.mark.parametrize("pixels_per_second", [30, 45, 90])
    def test_single_bar_roll(self, tmp_path, pixels_per_second):
        graph = ("color=white:s=400x6:r=30:d=4[s];color=black:s=640x360:r=30:d=4[b];"
                 f"[b][s]overlay=x=120:y='H-mod(t*{pixels_per_second},H+20)',format=yuv420p")
        path = str(tmp_path / f"roll{pixels_per_second}.mp4")
        ffmpeg("-filter_complex", graph, *X264, "-an", path)
        assert _row(_scan(path), "Interlace Detection")[1] == "PASS"


# Camera-like test texture: smooth blobs (blurred noise), larger than the
# picture so it can be panned over.
_TEXTURE = ("color=gray:s=480x270:d=1,noise=alls=100:allf=0:all_seed=7,gblur=sigma=4,"
            "eq=contrast=3,scale=1920:1080:flags=bicubic")


def _camera_pan(seconds, px_per_s, grain, x0=200, y0=200, shake=3):
    return (f"{_TEXTURE},loop=-1:1,fps=30,trim=duration={seconds},setpts=PTS-STARTPTS,"
            f"crop=640:360:'{x0}+{px_per_s}*t+{shake}*sin(2*PI*1.3*t)+{shake / 2}*sin(2*PI*3.1*t)':"
            f"'{y0}+{shake}*sin(2*PI*0.9*t+1)',"
            f"noise=alls={grain}:allf=t,format=yuv420p")


def _graph_clip(path, graph, encoder=X264):
    ffmpeg("-filter_complex", graph + "[o]", "-map", "[o]", *encoder, "-color_range", "tv",
           "-an", str(path))
    return str(path)


VP9 = ["-c:v", "libvpx-vp9", "-b:v", "2M", "-deadline", "realtime", "-cpu-used", "8",
       "-pix_fmt", "yuv420p"]


@needs_ffmpeg
class TestFreezeRecall:
    """Round 2 / round 3 (bug 10): freezes under grain and mid-programme."""

    @pytest.mark.parametrize("grain", [8, 12, 20])
    def test_freeze_under_fresh_grain_is_found(self, tmp_path, grain):
        """One frame held 2.5 s mid-shot, fresh grain on every frame."""
        graph = ("testsrc2=s=640x360:r=30:d=8,split[a][b];"
                 "[a][b]freezeframes=first=60:last=134:replace=60,"
                 f"noise=alls={grain}:allf=t,format=yuv420p")
        row = _row(_scan(_graph_clip(tmp_path / f"g{grain}.mp4", graph)), "Freeze Frames")
        assert row[1] in ("WARN", "FAIL") and "00:00:02.0" in row[2], row

    def test_mid_programme_freeze_in_vp9_mkv_is_not_a_slate(self, tmp_path):
        """A 3 s freeze at 8 s of a grainy camera pan, VP9 in Matroska."""
        graph = (_camera_pan(14, 60, 4) + ",split[a][b];"
                 "[a][b]freezeframes=first=240:last=329:replace=240")
        row = _row(_scan(_graph_clip(tmp_path / "vp9.mkv", graph, VP9)), "Freeze Frames")
        assert row[1] in ("WARN", "FAIL") and "slate" not in row[2], row
        assert "00:00:07.9" in row[2] or "00:00:08.0" in row[2], row


@needs_ffmpeg
class TestNoFalseFreezes:
    """Round 2: a freeze is a static picture, not a slow or interrupted one."""

    def test_dips_to_black_between_shots(self, tmp_path):
        """Moving shot, 1-frame dip, locked-off shot (live grain), 2-frame
        dip, moving shot."""
        graph = (f"{_camera_pan(4, 90, 6, shake=0)}[a];"
                 f"{_camera_pan(4, 0, 6, x0=900, y0=500, shake=0)}[b];"
                 f"{_camera_pan(4, 90, 6, x0=600, shake=0)}[c];[a][b][c]concat=n=3:v=1:a=0,"
                 "drawbox=c=black:t=fill:enable='eq(n,120)+between(n,240,241)'")
        assert _row(_scan(_graph_clip(tmp_path / "dips.mp4", graph)), "Freeze Frames")[1] == "PASS"

    @pytest.mark.parametrize("dark", [False, True])
    def test_slow_pan_with_light_grain(self, tmp_path, dark):
        graph = _camera_pan(10, 4, 3) + (",eq=brightness=-0.25:contrast=0.5" if dark else "")
        assert _row(_scan(_graph_clip(tmp_path / f"pan{dark}.mp4", graph)), "Freeze Frames")[1] == "PASS"


@needs_ffmpeg
class TestBlackArea:
    """Round 2 class 3: 0.90 flagged clean dark content; a small static
    overlay (timecode, logo) is excluded instead."""

    def test_dark_grade_with_practical_lights(self, tmp_path):
        """Mean Y about 19 with grain (some 5 % of the scene above the black
        level) and two small lamps: at 0.90 this read as black."""
        graph = (f"{_camera_pan(4, 20, 0)},lutyuv=y='17+val*2/255',noise=c0s=4:c0f=t,"
                 "drawbox=x=100:y=80:w=40:h=30:c=white:t=fill,"
                 "drawbox=x=420:y=200:w=36:h=28:c=0xFFE0A0:t=fill")
        fine = [e if e != "18" else "6" for e in X264]
        row = _row(_scan(_graph_clip(tmp_path / "dark.mp4", graph, fine)), "Black Frames")
        assert row[1] == "PASS", row

    def test_end_credits_on_black(self, tmp_path):
        """About 6 % white text bars rolling up over black at 1 px/frame."""
        bars = "".join(f"drawbox=x={170 + 37 * (k % 3)}:y={60 * k}:w={220 - 30 * (k % 3)}:h=9:"
                       "c=white:t=fill," for k in range(14))
        graph = (f"color=black:s=640x840:d=1,{bars}format=yuv420p,loop=-1:1,fps=30,"
                 "trim=duration=6,setpts=PTS-STARTPTS[roll];"
                 "color=black:s=640x360:r=30:d=6[bg];[bg][roll]overlay=x=0:y='-30*t',format=yuv420p")
        row = _row(_scan(_graph_clip(tmp_path / "credits.mp4", graph)), "Black Frames")
        assert row[1] == "PASS", row

    def test_black_with_burnt_in_timecode_and_logo(self, tmp_path):
        graph = ("testsrc2=s=640x360:r=30:d=6,drawbox=c=black:t=fill:enable='between(t,2,4)',"
                 "drawbox=x=500:y=20:w=120:h=75:c=white@0.8:t=fill,"
                 "drawtext=timecode='00\\:00\\:00\\:00':rate=30:x=250:y=330:fontsize=20:fontcolor=white,"
                 "format=yuv420p")
        row = _row(_scan(_graph_clip(tmp_path / "tc.mp4", graph)), "Black Frames")
        assert row[1] in ("WARN", "FAIL") and "00:00:02" in row[2], row


_GREY_720 = "color=gray:s=1280x720:r=30:d=5,format=yuv420p"


@needs_ffmpeg
class TestFineFlashPatterns:
    """Round 3 bug 3 (SAFETY): antiphase patterns finer than a grid block
    (80 x 80 px at 720p) keep every block's mean light constant."""

    @staticmethod
    def _checker(tmp_path, size, frames_per_state):
        lum = (f"if(gte(T,1)*lt(T,4),if(eq(mod(floor(X/{size})+floor(Y/{size}),2),"
               f"lt(mod(N,{2 * frames_per_state}),{frames_per_state})),235,16),16)")
        return _clip(tmp_path / f"c{size}_{frames_per_state}.mp4",
                     f"geq=lum='{lum}':cb=128:cr=128", source=_GREY_720,
                     extra=("-crf", "12"))

    @pytest.mark.parametrize("size", [20, 40, 50])
    def test_fine_checkerboard_at_5_hz_warns(self, tmp_path, size):
        row = _row(_scan(self._checker(tmp_path, size, 3), target_width=1280, target_height=720),
                   "Flash Risk")
        assert row[1] == "WARN", row

    def test_fine_checkerboard_on_grey_warns(self, tmp_path):
        """Darker state above black (Y' 60, about 4 cd/m2): the darker-state
        test must use the real 160 cd/m2 limit."""
        lum = ("if(gte(T,1)*lt(T,4),if(eq(mod(floor(X/20)+floor(Y/20),2),"
               "lt(mod(N,6),3)),235,60),60)")
        path = _clip(tmp_path / "grey.mp4", f"geq=lum='{lum}':cb=128:cr=128", source=_GREY_720,
                     extra=("-crf", "12"))
        row = _row(_scan(path, target_width=1280, target_height=720), "Flash Risk")
        assert row[1] == "WARN", row

    def test_fine_checkerboard_between_bright_states_passes(self, tmp_path):
        """BT.1702-3: a flash needs its darker state below 160 cd/m2. Y' 225
        (~179 cd/m2) against 235 (200 cd/m2) at 5 Hz is not a flash."""
        lum = ("if(gte(T,1)*lt(T,4),if(eq(mod(floor(X/20)+floor(Y/20),2),"
               "lt(mod(N,6),3)),235,225),225)")
        path = _clip(tmp_path / "bright.mp4", f"geq=lum='{lum}':cb=128:cr=128", source=_GREY_720,
                     extra=("-crf", "12"))
        row = _row(_scan(path, target_width=1280, target_height=720), "Flash Risk")
        assert row[1] == "PASS", row

    def test_fine_checkerboard_at_2_hz_passes(self, tmp_path):
        row = _row(_scan(self._checker(tmp_path, 20, 8), target_width=1280, target_height=720),
                   "Flash Risk")
        assert row[1] == "PASS", row

    def test_fast_camera_pan_passes(self, tmp_path):
        """Motion is not flashing: a fast pan over a high-contrast texture."""
        graph = (f"{_TEXTURE},loop=-1:1,fps=30,trim=duration=4,setpts=PTS-STARTPTS,"
                 "crop=640:360:'100+300*t':'200+20*sin(2*PI*2*t)',noise=alls=4:allf=t,format=yuv420p")
        assert _row(_scan(_graph_clip(tmp_path / "pan.mp4", graph)), "Flash Risk")[1] == "PASS"


@needs_ffmpeg
def test_hdr_flash_is_not_verified(tmp_path):
    """Round 3 bug 14: switched on but unmeasurable is Not verified, not SKIP."""
    path = str(tmp_path / "pq.mp4")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2",
           "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-an", path)
    row = _row(_scan(path), "Flash Risk")
    assert row[1] not in ("SKIP", "PASS") and "not" in row[2].lower(), row


@needs_ffmpeg
def test_hdr_flash_not_verified_is_not_escalated_by_strict(tmp_path):
    """Review round 5 (N5): the Flash Risk Not verified is inherent to PQ/HLG
    content; Strict Mode must not turn every HDR file into a MISMATCH."""
    path = str(tmp_path / "pq.mp4")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2",
           "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-an", path)
    cfg = dict(DEFAULT_CONFIG, target_width=640, target_height=360, target_fps=30.0, min_bitrate_mbps=0,
               audio_required=False, loudness_mode="off", accepted_containers=[".mp4"])
    result = run_qa_checks(path, cfg=cfg, strict=True)
    result.finalize()
    row = _row(result, "Flash Risk")
    assert "not" in row[2].lower() and "Flash Risk" in result.metadata.get("not_verifiable", []), row
    exempt = set(result.metadata.get("not_verifiable", [])) | set(result.metadata.get("not_run_by_choice", []))
    escalating = [c[:3] for c in result.checks if c[1] in ("WARN", "FAIL") and c[0] not in exempt]
    assert result.status != "MISMATCH" or escalating, (result.status, escalating)


def test_new_setting_has_no_helper_text():
    """AGENTS.md: cut helper text and secondary labels; new 0.9.2 settings
    get none, and 0.9.1 settings keep their copy."""
    from frameguard.gui import SettingsPanel
    rows = {key: (title, description, choices)
            for _g, key, _k, title, description, choices in SettingsPanel._RULE_CONTROLS}
    title, description, choices = rows["freeze_hold_allowance"]
    assert description == ""
    assert all(len(label.split()) <= 2 for _v, label in choices)
    assert rows["interlace_check_enabled"][1] == (
        "Warn on combing in video declared progressive and on a wrong field order")


class TestCfrNominalRateUnits:
    """Review round 3 mutation survivors core_038 / core_044 (CFR functions)."""

    def test_rate_without_a_denominator(self):
        from frameguard import core
        assert core._nominal_interval({"r_frame_rate": "25"}) == pytest.approx(0.04)
        assert core._nominal_interval({"r_frame_rate": "30000/1001"}) == pytest.approx(1001 / 30000)
        assert core._nominal_interval({"r_frame_rate": "0/0", "avg_frame_rate": "24/1"}) == pytest.approx(1 / 24)

    def test_nominal_rate_is_used_only_when_the_frames_match_it(self):
        """Declared 30 fps, frames really every 1/15 s: the measured spacing is
        the reference (regular), not the declared one (every gap irregular)."""
        from frameguard import core
        timing = core.FrameTiming([[k / 15 for k in range(60)]], False, "test")
        reference, total, irregular, *_ = core._interval_stats(timing, nominal=1 / 30, tick=1 / 15360)
        assert reference == pytest.approx(1 / 15, rel=1e-3) and irregular == 0
        # within the tolerance, the declared interval is the reference
        reference, *_ = core._interval_stats(core.FrameTiming([[k * 0.0335 for k in range(60)]], False, "t"),
                                                   nominal=1 / 30, tick=1 / 15360)
        assert reference == pytest.approx(1 / 30)


# ---------------------------------------------------------------------------
# Mutation gate (review round 3): exact expectations for the new helpers
# ---------------------------------------------------------------------------

class TestCoverageWeightsExact:
    @staticmethod
    def _weights(amplitude):
        return vc.FlashAccumulator(FPS)._coverage_weights(amplitude)

    def test_diagonal_neighbour(self):
        assert self._weights({0: 1.0, 17: 8.0}) == {0: 0.125, 17: 1.0}

    def test_upper_left_and_lower_right_neighbours(self):
        assert self._weights({18: 1.0, 1: 4.0}) == {18: 0.25, 1: 1.0}
        assert self._weights({1: 2.0, 18: 6.0}) == pytest.approx({1: 1 / 3, 18: 1.0})

    def test_row_and_column_neighbours(self):
        assert self._weights({5: 1.0, 6: 4.0}) == {5: 0.25, 6: 1.0}
        assert self._weights({6: 1.0, 5: 4.0}) == {6: 0.25, 5: 1.0}
        assert self._weights({5: 1.0, 21: 2.0}) == {5: 0.5, 21: 1.0}
        assert self._weights({21: 1.0, 5: 2.0}) == {21: 0.5, 5: 1.0}

    def test_isolated_and_silent_blocks_count_whole(self):
        assert self._weights({40: 0.5}) == {40: 1.0}
        assert self._weights({41: 0.0}) == {41: 1.0}
        assert self._weights({143: 4.0, 0: 9.0}) == {143: 1.0, 0: 1.0}   # far corners

    def test_last_column_does_not_wrap_to_the_next_row(self):
        assert self._weights({15: 1.0, 16: 4.0}) == {15: 1.0, 16: 1.0}

    def test_first_column_and_row_neighbours(self):
        assert self._weights({1: 1.0, 0: 4.0}) == {1: 0.25, 0: 1.0}
        assert self._weights({17: 1.0, 16: 4.0}) == {17: 0.25, 16: 1.0}
        assert self._weights({16: 1.0, 0: 4.0}) == {16: 0.25, 0: 1.0}


class TestFineFlashAccumulator:
    """The fine channel's area is the smallest transition share of each block."""

    @staticmethod
    def _events(share, frames=90, period=3):
        plane = vc.FlashAccumulator.PLANE
        acc = vc.FlashAccumulator(FPS, planes=4)
        grey = bytes([60]) * plane
        data = b""
        for f in range(frames):
            alpha = share if (f % period == 0 and 15 <= f < 75) else 0
            data += grey * 3 + bytes([alpha]) * plane
        acc.feed(data)
        acc.finish()
        return acc.events(_times(frames), FPS)

    def test_half_of_every_block_in_transition_is_a_hazard(self):
        events = self._events(128)
        assert len(events) == 1 and events[0].kind == "general"
        assert events[0].area == pytest.approx(128 / 255)
        assert events[0].start == pytest.approx(15 / 30, abs=0.04)
        assert events[0].end == pytest.approx(73 / 30, abs=0.04)        # last transition 72

    def test_area_at_the_25_percent_rule(self):
        assert self._events(77)                    # 30 % of every block
        assert not self._events(51)                # 20 %

    def test_peak_is_the_largest_area_of_the_run(self):
        """Shares grow over the flashing, so the last frames carry the peak."""
        plane = vc.FlashAccumulator.PLANE
        acc = vc.FlashAccumulator(FPS, planes=4)
        data = b""
        for f in range(90):
            alpha = (80 + 2 * (f - 15)) if (f % 3 == 0 and 15 <= f < 75) else 0
            data += bytes([60]) * plane * 3 + bytes([alpha]) * plane
        acc.feed(data)
        acc.finish()
        events = acc.events(_times(90), FPS)
        assert len(events) == 1
        assert events[0].area == pytest.approx((80 + 2 * 39) / 255)    # min of the last 7 shares
        # shares falling: the first seven transitions carry the peak
        acc = vc.FlashAccumulator(FPS, planes=4)
        data = b""
        for f in range(90):
            alpha = (200 - 2 * (f - 15)) if (f % 3 == 0 and 15 <= f < 75) else 0
            data += bytes([60]) * plane * 3 + bytes([alpha]) * plane
        acc.feed(data)
        acc.finish()
        events = acc.events(_times(90), FPS)
        assert len(events) == 1 and events[0].area == pytest.approx((200 - 2 * 18) / 255)

    def test_buffers_stay_bounded_while_streaming(self):
        plane = vc.FlashAccumulator.PLANE
        acc = vc.FlashAccumulator(FPS, planes=4)
        frame = bytes([60]) * plane * 3 + bytes(plane)
        for _ in range(20):
            acc.feed(frame * 30)
        assert len(acc.recent_rows) <= 4
        acc.finish()

    def test_three_hertz_pattern_is_not_flagged(self):
        assert not self._events(255, period=5)     # 6 transitions a second

    def test_offset_maps_grid_frames_to_source_times(self):
        plane = vc.FlashAccumulator.PLANE
        acc = vc.FlashAccumulator(FPS, planes=4, offset=1)
        data = b"".join(bytes([60]) * plane * 3 + bytes([255 if (f % 3 == 0 and f >= 15) else 0]) * plane
                        for f in range(60))
        acc.feed(data)
        acc.finish()
        events = acc.events(_times(61), FPS)
        assert events and events[0].start == pytest.approx(16 / 30, abs=1e-6)


class TestFreezeHelpersExact:
    def test_structural_limit(self):
        assert vc.structural_limit(1.0, 16.0) == pytest.approx(0.02 + 2.0 / 16.0)
        assert vc.structural_limit(1.0, 0.5) == pytest.approx(0.02 + 2.0)

    def test_step_frames(self):
        assert vc.freeze_step_frames(30.0, 1.0) == 15
        assert vc.freeze_step_frames(29.97, 1.0) == 14
        assert vc.freeze_step_frames(0.0, 1.0) == 12
        assert vc.freeze_step_frames(30.0, 0.01) == 1

    def test_static_segments(self):
        assert vc._static_segments(0, 30, []) == [(0, 30)]
        assert vc._static_segments(0, 30, [(5, 20)]) == [(0, 19), (6, 30)]
        assert vc._static_segments(0, 30, [(5, 20), (10, 25)]) == [(0, 19), (6, 24), (11, 30)]
        assert vc._static_segments(0, 30, [(5, 20), (6, 22)]) == [(0, 19), (6, 21), (7, 30)]
        assert vc._static_segments(0, 30, [(5, 20), (5, 21)]) == [(0, 19), (6, 30)]
        assert vc._static_segments(10, 30, [(5, 20)]) == [(10, 30)]          # pair outside

    def test_still_runs_do_not_bridge_motion(self):
        still = [False] + [True] * 10 + [False] + [True] * 9
        mafd = [5.0] + [0.1] * 10 + [20.0] + [0.1] * 9
        assert vc._still_runs(still, mafd) == [(1, 10), (12, 20)]
        mafd[11] = 1.0                                                     # a refresh blip
        assert vc._still_runs(still, mafd) == [(1, 20)]

    def test_cut_threshold_is_scdets_default(self):
        assert vc._cut([0.0, 12.0], 1)               # scdet threshold 10
        assert not vc._cut([0.0, 9.0], 1)
        assert not vc._cut([12.0, 12.0], 1)           # continuing motion is no cut
        assert not vc._cut([12.0], 0)

    def test_default_side_factor_is_sixteen(self):
        """A structural change between the 16x and 24x grain limits."""
        mafd = [2.6] * 30 + [1.0] * 60 + [2.6] * 30
        structural = [2.0] * 30 + [0.12] * 60 + [2.0] * 30
        found = vc.detect_freezes(mafd, _times(120), FPS, 1.0, 4.0, structural=structural)
        assert found.holds

    def test_three_lead_frames_must_move(self):
        busy, still = (2.6, 2.0), (0.15, 0.003)
        rows = [busy] * 60 + [still] + [busy] * 2 + [still] * 60 + [busy] * 30
        found = vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=[r[1] for r in rows])
        assert found.holds == []

    def test_stop_test_measures_from_the_frozen_frames_noise(self):
        """Lead motion just above the grain band after larger motion: a
        freeze whose copy shows no change at all (noise 0)."""
        rows = [(2.6, 2.0)] * 60 + [(2.6, 1.03)] * 3 + [(0.01, 0.0)] * 60 + [(2.6, 2.0)] * 30
        found = vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=[r[1] for r in rows])
        assert found.holds

    def test_static_run_keeps_its_first_frame(self):
        """A held picture of exactly the minimum duration, with the
        accumulated-change series present."""
        rows = [(2.6, 2.0)] * 60 + [(0.15, 0.003)] * 29 + [(2.6, 2.0)] * 30
        step = [None] * len(rows)
        for j in range(15, len(rows), 15):
            step[j] = 0.003 if 60 <= j - 15 and j <= 88 else 2.0
        found = vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=[r[1] for r in rows], structural_step=step, step=15)
        assert found.holds and found.holds[0][1] == pytest.approx(1.0)

    def test_a_short_file_frozen_from_the_start_is_not_a_slate(self):
        rows = [(0.0, 0.0)] * 24 + [(2.6, 2.0)] * 3
        found = vc.detect_freezes([r[0] for r in rows], _times(27), FPS, 0.5, 0.9,
                                  structural=[r[1] for r in rows])
        assert found.holds and not found.edge_holds

    def test_hold_allowance_boundary_is_half_a_frame(self):
        rows = [(2.6, 2.0)] * 60 + [(40.0, 40.0)] + [(0.0, 0.0)] * 90 + [(40.0, 40.0)] + [(2.6, 2.0)] * 200
        mafd, structural = [r[0] for r in rows], [r[1] for r in rows]
        found = vc.detect_freezes(mafd, _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=structural, hold_allowance=3.01)
        assert found.holds                     # 91 frames = 3.033 s > 3.01 + half a frame

    def test_head_refinement_must_reach_the_frozen_frame(self):
        """Frames 1-2 are refinement blips, frame 3 real motion: the held
        picture after it is not at the head."""
        rows = [(0.0, 0.0), (0.5, 0.01), (0.5, 0.01), (20.0, 5.0)] + [(0.0, 0.0)] * 60 + [(2.6, 2.0)] * 120
        found = vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=[r[1] for r in rows])
        assert found.holds and not found.edge_holds

    def test_steady_motion_under_heavy_grain_stops_dead(self):
        """Lead motion between one and two grain margins, as steady as the
        motion half a second earlier: a freeze."""
        rows = [(5.4, 0.9)] * 57 + [(5.4, 0.8)] * 3 + [(4.4, 0.285)] * 60 + [(5.4, 0.9)] * 30
        found = vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=[r[1] for r in rows])
        assert found.holds
        # earlier motion is the median of three frames
        # frozen from frame 59; the earlier frames are 42-44 (half a second back)
        rows[42], rows[43], rows[44] = (5.4, 0.5), (5.4, 3.0), (5.4, 0.5)
        found = vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                  structural=[r[1] for r in rows])
        assert found.holds

    def test_dip_through_black_is_an_edit(self):
        """Black frame, then an encoder blip, then a held shot: the frame
        after the black is next to it, so the shot enters on an edit."""
        busy, still = (2.6, 2.0), (0.15, 0.003)
        rows = [busy] * 90 + [(40.0, 40.0), (40.0, 40.0), (1.0, 0.5)] + [still] * 90
        rows += [(40.0, 40.0), (40.0, 40.0)] + [busy] * 60
        mafd = [r[0] for r in rows]
        structural = [r[1] for r in rows]
        times = _times(len(rows))
        black = [(90 / 30, 1 / 30), (183 / 30, 1 / 30)]
        found = vc.detect_freezes(mafd, times, FPS, 1.0, len(rows) / 30, black,
                                  structural=structural, hold_allowance=3.0)
        assert found.holds == [] and found.composed
        blind = vc.detect_freezes(mafd, times, FPS, 1.0, len(rows) / 30, (),
                                  structural=structural, hold_allowance=3.0)
        assert blind.holds


class TestBlackHelpersExact:
    def test_pixel_level(self):
        assert vc.black_pixel_level(0.02, False) == 20
        assert vc.black_pixel_level(0.02, True) == 5
        assert vc.black_pixel_level(0.1, False) == 37
        assert vc.black_pixel_level(1.0, False) == 235 and vc.black_pixel_level(1.0, True) == 255

    def test_overlay_sample_gaps_split_runs(self):
        times = _times(61)
        nonblack = [0.04] * 61
        nonblack[30] = None
        static = [0.04 if i % 9 == 0 and i else None for i in range(61)]
        assert len(vc.overlay_black_runs(nonblack, static, times, 9, 0.98, 1 / 30)) == 2
        assert len(vc.overlay_black_runs(nonblack, static, times, 9, 0.98, 1 / 30, sample=2)) == 1
        assert vc.overlay_black_runs(nonblack, static, times, 0, 0.98, 1 / 30) == []
        every = [0.04] * 30
        assert vc.overlay_black_runs(every, [None] + [0.04] * 29, times[:30], 1, 0.98, 1 / 30)

    def test_every_pair_up_to_the_last_frame_counts(self):
        times = _times(19)
        static = [None] * 19
        static[9], static[18] = 0.04, 0.5
        assert vc.overlay_black_runs([0.04] * 19, static, times, 9, 0.98, 1 / 30) == []

    def test_overlay_pairs_stay_inside_the_run(self):
        times = _times(40)
        nonblack = [0.04] * 27 + [0.5] * 13            # black ends after frame 26
        static = [None] * 40
        static[9], static[18], static[27] = 0.04, 0.04, 0.5
        assert vc.overlay_black_runs(nonblack, static, times, 9, 0.98, 1 / 30) == \
            [(0.0, pytest.approx(27 / 30))]

    def test_overlay_allowance_boundary(self):
        times = _times(30)
        static = [0.05 if i % 9 == 0 and i else None for i in range(30)]
        fits = 0.05 + 0.02 * 0.95 - 1e-9          # the rest within 2 % of the rest
        assert vc.overlay_black_runs([fits] * 30, static, times, 9, 0.98, 1 / 30)
        assert not vc.overlay_black_runs([fits + 2e-4] * 30, static, times, 9, 0.98, 1 / 30)
        assert not vc.overlay_black_runs([0.0701] * 30, [0.0701 if v else None for v in static],
                                         times, 9, 0.98, 1 / 30)             # over the candidate share
        loose = [0.0501 if v is not None else None for v in static]
        assert not vc.overlay_black_runs([0.0505] * 30, loose, times, 9, 0.98, 1 / 30)

    def test_sparse_samples(self):
        times = _times(61)
        nonblack = [0.04 if i % 3 == 0 else None for i in range(61)]
        static = [0.04 if i % 15 == 0 and i else None for i in range(61)]
        runs = vc.overlay_black_runs(nonblack, static, times, 15, 0.98, 1 / 30, sample=3)
        assert runs == [(0.0, pytest.approx(61 / 30))]

    def test_merge_runs(self):
        assert vc.merge_runs([(0, 1)], [(1 + 5e-7, 2)]) == [(0, 2)]
        assert vc.merge_runs([(0, 1)], [(1 + 1e-5, 2)]) == [(0, 1), (1 + 1e-5, 2)]

    def test_hard_side(self):
        assert vc._hard_side(16, 40, 41)
        assert vc._hard_side(16, 40, 28)            # far within half the step
        assert not vc._hard_side(16, 40, 25)
        assert not vc._hard_side(16, 17, None)       # under BLACK_STEP_CODES
        assert vc._hard_side(16, 40, None)

    def test_black_runs_from_marks(self, tmp_path):
        start, end = tmp_path / "s.txt", tmp_path / "e.txt"
        start.write_text("frame:30   pts:15360 pts_time:1\nlavfi.black_start=1\n"
                         "frame:150  pts:76800 pts_time:5\nlavfi.black_start=5\n")
        end.write_text("frame:60   pts:30720 pts_time:2\nlavfi.black_end=2\n")
        assert vc.black_runs(str(start), str(end)) == [(1.0, 2.0), (5.0, None)]
        assert vc.black_runs(str(start), str(tmp_path / "missing.txt")) is None

    def test_lead_in_tolerance_is_half_a_frame(self):
        cfg = vc.VideoCheckConfig.from_cfg({})
        times = _times(300)
        opening = vc.classify_black([(0.4 / 30, 2.0)], 10.0, FPS, cfg, times)      # (start, end)
        late = vc.classify_black([(0.6 / 30, 2.0)], 10.0, FPS, cfg, times)
        assert opening.edge_segments and not late.edge_segments
        closing = vc.classify_black([(8.0, 10.0 - 0.4 / 30)], 10.0, FPS, cfg, times)
        early = vc.classify_black([(8.0, 10.0 - 0.6 / 30)], 10.0, FPS, cfg, times)
        assert closing.edge_segments == [(9.0, pytest.approx(1.0 - 0.4 / 30))]
        assert not early.edge_segments

    @staticmethod
    def _burst(yavg_before, yavg_after):
        """A 5-frame black burst at frame 100 of 200 (2 s isolation)."""
        cfg = vc.VideoCheckConfig.from_cfg({})
        times = _times(200)
        yavg = list(yavg_before) + [16.0] * 5 + list(yavg_after)
        return vc.classify_black([(100 / 30, 105 / 30)], 200 / 30, FPS, cfg, times, yavg)

    def test_burst_needs_steady_neighbours_on_both_sides(self):
        steady = self._burst([90.0] * 100, [90.0] * 95)
        assert steady.bursts == [(pytest.approx(100 / 30), 5)]
        # two frames before the burst still fading: a dip, not a cut
        fading = self._burst([90.0] * 98 + [40.0, 90.0], [90.0] * 95)
        assert fading.bursts == []
        # the frame three before may differ (only two are looked at)
        early = self._burst([90.0] * 97 + [40.0, 90.0, 90.0], [90.0] * 95)
        assert early.bursts
        # after the burst: the second frame is compared with the first
        after = self._burst([90.0] * 100, [90.0, 50.0] + [90.0] * 93)
        assert after.bursts == []


class TestInterlaceAssessmentBoundaries:
    def test_half_progressive_section_stays_combed(self):
        labels = ["tff"] * 60
        verdicts = ["progressive"] * 30 + [""] * 30           # exactly half of the moving frames
        assert vc.assess_interlace(labels, _times(60), FPS, "progressive", labels,
                                   displacement=verdicts).combed

    def test_field_order_majority_boundary(self):
        """12 of 15 decided single-frame calls agree: exactly 0.8 is steady."""
        labels = ["tff"] * 60
        singles = (["tff"] * 4 + ["bff"]) * 3 + [""] * 45
        assert vc.assess_interlace(labels, _times(60), FPS, "progressive", singles).combed

    def test_last_frames_of_a_section_vote(self):
        labels = ["tff"] * 60
        verdicts = [""] * 29 + ["progressive"] * 31
        assert vc.assess_interlace(labels, _times(60), FPS, "progressive", labels,
                                   displacement=verdicts).combed == []

    def test_field_displacements_are_symmetric(self):
        """Both field orders: offsets -2..2 lines, each once."""
        assert sorted(vc.INTERLACE_DISPLACEMENTS) == [-2, -1, 0, 1, 2]


class TestInterlaceHelpersExact:
    def test_vertical_displacement(self):
        assert vc.vertical_displacement({-1: 1.0, 0: 3.0, 1: 2.0}) == "woven"
        assert vc.vertical_displacement({-1: 2.0, 0: 1.0, 1: 1.5}) == "progressive"
        assert vc.vertical_displacement({0: 1.0, 1: 1.4}) == ""
        assert vc.vertical_displacement({1: 1.0, -1: 2.0}) == ""
        assert vc.vertical_displacement({0: 1.0, 1: None}) == ""

    def test_displacement_kernels(self):
        assert vc._displacement_kernel(0) == "0 0 -1 2 -1 0 0"
        assert vc._displacement_kernel(1) == "0 0 0 2 -2 0 0"
        assert vc._displacement_kernel(-1) == "0 0 -2 2 0 0 0"
        assert vc._displacement_kernel(2) == "0 0 0 2 -1 0 -1"
        assert vc._displacement_kernel(-2) == "-1 0 -1 2 0 0 0"

    def test_field_timings_pairing(self):
        same = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0}
        top = {1: 3.0, 2: 3.0, 3: 1.0}
        bottom = {1: 1.0, 2: 3.0, 3: 3.0}
        move = {1: 2.0, 2: 2.0, 3: 2.0}
        assert vc.field_timings(4, [0, 1, 2, 3], same, top, bottom, move) == \
            ["woven", "progressive", "woven", ""]
        # a repeated frame (no motion to the next) decides nothing
        assert vc.field_timings(2, [0, 1], {0: 1.0}, {1: 1.0}, {1: 3.0}, {1: 0.5}) == ["", ""]
        assert vc.field_timings(8, [5, 7], {5: 1.0}, {7: 3.0}, {7: 1.0}, {7: 2.0}) == [""] * 8

    def test_dense_sections(self):
        times = _times(120)
        long = vc._dense_sections([True] * 45 + [False] * 75, times, 1 / 30, 30, 0.5)
        assert len(long) == 1 and long[0][1] == pytest.approx(1.5)
        assert vc._dense_sections([True] * 25 + [False] * 95, times, 1 / 30, 30, 0.5) == []
        gap = [True] * 40 + [False] * 10 + [True] * 40 + [False] * 30
        merged = vc._dense_sections(gap, times, 1 / 30, 30, 0.5)
        assert len(merged) == 1 and merged[0][1] == pytest.approx(3.0)
        assert vc._dense_sections([True] * 10, times[:10], 1 / 30, 30, 0.5) == []
        mixed = [c == "1" for c in "110111101001000111010101110111111110111000111101101101111111111101"
                                   "100101011100111010000001111010100001111000001100110101"]
        found = vc._dense_sections(mixed, times, 1 / 30, 30, 0.5)
        assert found == [(0.0, pytest.approx(95 / 30))]

    def test_telecine_flags_at_the_edges(self):
        assert vc._telecine_flags(["top", "", "", "", "", "top"]) == [True, False, False, False, False, True]
        assert vc._telecine_flags(["", "top", "", "", "", ""]) == [False] * 6
        assert vc._telecine_flags(["top", "", "", "", "", "bottom"]) == [False] * 6

    def test_frame_range(self):
        times = [k / 10 for k in range(10)]
        assert vc._frame_range(times, (0.2, 0.3)) == (2, 4)
        assert vc._frame_range(times, (0.2, 0.01)) == (2, 2)


class TestAnalysisGeometryExact:
    @staticmethod
    def _analysis(width, height, fps="30/1", **cfg):
        config = dict(DEFAULT_CONFIG)
        config.update(cfg)
        return vc.VideoAnalysis({"index": 0, "width": width, "height": height,
                                 "avg_frame_rate": fps, "pix_fmt": "yuv420p"}, config)

    def test_structural_side_factor(self):
        assert self._analysis(1920, 1080).structural_side_factor == 16.0
        assert self._analysis(640, 360).structural_side_factor == 16.0
        assert self._analysis(100, 60).structural_side_factor == pytest.approx(100 / 6)

    def test_overlay_frames(self):
        a = self._analysis(1920, 1080)
        assert (a.overlay_sample_frames, a.overlay_step_frames) == (3, 15)
        b = self._analysis(1920, 1080, fps="30000/1001")
        assert (b.overlay_sample_frames, b.overlay_step_frames) == (2, 14)
        c = self._analysis(1920, 1080, fps="0/0")
        assert (c.overlay_sample_frames, c.overlay_step_frames) == (2, 12)
        assert self._analysis(1920, 1080, fps="10/1").overlay_sample_frames == 1

    def test_tiny_picture_has_no_levels_branch(self):
        assert not self._analysis(16, 16).needs_levels_branch
        assert self._analysis(640, 360).needs_levels_branch
        assert not self._analysis(640, 360, signal_levels_mode="off").needs_levels_branch

    def test_chroma_location_defaults(self):
        """H.273: signalled siting wins; unsignalled 4:2:0 is left except for
        MPEG-1 / JPEG-family codecs (centre)."""
        assert vc.chroma_location({"chroma_location": "topleft", "codec_name": "hevc"}) == "topleft"
        assert vc.chroma_location({"chroma_location": "unspecified", "codec_name": "h264"}) == "left"
        assert vc.chroma_location({"codec_name": "hevc"}) == "left"
        assert vc.chroma_location({"codec_name": "mjpeg"}) == "center"
        assert vc.chroma_location({"codec_name": "mpeg1video", "chroma_location": "center"}) == "center"
        assert vc.chroma_location({}) == "left"

    def test_black_switched_off_runs_no_detector(self):
        with self._analysis(640, 360, black_frames_enabled=False) as off:
            assert off._black_chain() == "null"
        with self._analysis(640, 360) as on:
            assert "blackdetect" in on._black_chain()

    def test_mean_tree_pairs_inputs(self):
        assert vc._mean_tree(["a", "b", "c", "d"], "m", "[o]")[-1].startswith("[m0_0][m0_1]")

    def test_hold_allowance_default(self):
        assert vc.VideoCheckConfig.from_cfg({}).freeze_hold == 3.0

    def test_partial_tags_only_when_some_are_set(self):
        assert any("partly" in s for s in vc.colour_tag_advisories("bt709", "unknown", "unknown"))
        assert not any("partly" in s for s in vc.colour_tag_advisories("unknown", "unknown", "unknown"))


# ---------------------------------------------------------------------------
# Mutation gate, round 5: the reviewer's seed-92 survivors
# ---------------------------------------------------------------------------

class TestReviewerSurvivors:
    def test_flash_luminance_uses_all_three_primaries(self):
        """BT.709 luminance of linear light: Y = 0.2126 R + 0.7152 G + 0.0722 B."""
        assert vc._rule_value("general", 0, 0, 255) == pytest.approx(200 * 0.0722)
        assert vc._rule_value("general", 255, 255, 255) == pytest.approx(200.0)
        assert vc._rule_value("red", 255, 0, 0) == pytest.approx(320.0)

    def test_hard_side_needs_a_neighbour_and_a_two_code_step(self):
        assert vc._hard_side(16, None, None) is False
        assert vc._hard_side(16, 18, None) is True          # exactly BLACK_STEP_CODES
        assert vc._hard_side(16, 17.99, None) is False

    def test_coverage_weights_below_one_candela(self):
        weights = vc.FlashAccumulator(FPS)._coverage_weights({0: 0.25, 1: 0.5})
        assert weights == {0: 0.5, 1: 1.0}

    def test_vertical_scroll_needs_both_profiles(self):
        assert vc.is_vertical_scroll(None, 1.0) is False
        assert vc.is_vertical_scroll(1.0, None) is False

    def test_interlaced_signal_orders(self):
        info = vc.PictureInfo.from_stream({"width": 720, "height": 576, "field_order": "tt"})
        assert info.interlaced is True
        assert vc.PictureInfo.from_stream({"width": 720, "height": 576, "field_order": "bt"}).interlaced
        assert not vc.PictureInfo.from_stream({"width": 720, "height": 576,
                                               "field_order": "progressive"}).interlaced

    def test_short_closing_black_is_listed(self):
        """Black covering the last 0.6 frame of a 10 s file is a run-out."""
        cfg = vc.VideoCheckConfig.from_cfg({})
        found = vc.classify_black([(10.0 - 0.6 / 30, 10.0)], 10.0, FPS, cfg, _times(300))
        assert found.edge_segments == [(pytest.approx(10.0 - 0.6 / 30), pytest.approx(0.6 / 30))]

    def test_sections_half_a_second_apart_merge(self):
        times = _times(150)
        flags = [True] * 45 + [False] * 15 + [True] * 45 + [False] * 45
        sections = vc._dense_sections(flags, times, 1 / 30, 30, 0.5)
        assert len(sections) == 1 and sections[0] == (0.0, pytest.approx(3.5))

    def test_pq_with_unsignalled_primaries_is_not_a_contradiction(self):
        assert not any("BT.2100 defines" in s for s in
                       vc.colour_tag_advisories("unknown", "smpte2084", "bt2020nc", report_partial=False))
        assert any("BT.2100 defines" in s for s in
                   vc.colour_tag_advisories("bt709", "smpte2084", "bt2020nc", report_partial=False))

    def test_freeze_area_branch_runs_for_each_check_alone(self):
        for only in ("black_frames_enabled", "freeze_frames_enabled"):
            cfg = dict(DEFAULT_CONFIG, black_frames_enabled=False, freeze_frames_enabled=False,
                       signal_levels_mode="off", flash_check_enabled=False)
            cfg[only] = True
            with vc.VideoAnalysis({"index": 0, "width": 640, "height": 360, "avg_frame_rate": "30/1"},
                                  cfg) as analysis:
                chains = analysis._area_chains()
            assert chains[0].endswith("split=1[ar_mean]"), (only, chains[0])


def test_overlay_samples_on_a_square_grid():
    """The black-overlay mask samples every BLACK_OVERLAY_BLOCK pixels in
    both directions (an overlay is judged by area)."""
    with vc.VideoAnalysis({"index": 0, "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
                          dict(DEFAULT_CONFIG)) as analysis:
        graph = ";".join(analysis._area_chains())
    block = 8 * vc.BLACK_OVERLAY_BLOCK
    assert f"w='max(8,trunc(iw/{block})*8)':h='max(8,trunc(ih/{block})*8)'" in graph


class TestFlashEventPeaks:
    def test_two_events_in_one_candidate_run_keep_their_own_peaks(self):
        """Every block flashes at 10 transitions a second throughout; the
        flashing share of each block is 30 %, then 20 % (below the 25 % rule),
        then 50 %: two events, 0.3 and 0.5."""
        plane = vc.FlashAccumulator.PLANE
        acc = vc.FlashAccumulator(FPS, planes=4)
        shares = [77] * 10 + [51] * 14 + [128] * 10
        data, mark = b"", 0
        for f in range(240):
            alpha = 0
            if f % 3 == 0 and f >= 90 and mark < len(shares):
                alpha, mark = shares[mark], mark + 1
            data += bytes([60]) * plane * 3 + bytes([alpha]) * plane
        acc.feed(data)
        acc.finish()
        events = acc.events(_times(240), FPS)
        assert [round(e.area, 3) for e in events] == [round(77 / 255, 3), round(128 / 255, 3)]


class TestFreezeEdges:
    @staticmethod
    def _found(rows, **kw):
        return vc.detect_freezes([r[0] for r in rows], _times(len(rows)), FPS, 1.0, len(rows) / 30,
                                 structural=[r[1] for r in rows], **kw)

    def test_eight_refinement_frames_are_still_the_head(self):
        """Frames 1-8 change below a refresh blip but above grain: still at the head."""
        rows = [(0.0, 0.0)] + [(0.5, 0.2)] * 8 + [(0.0, 0.0)] * 60 + [(2.6, 2.0)] * 120
        found = self._found(rows)
        assert found.edge_holds and not found.holds

    def test_motion_on_frame_one_is_not_a_head_slate(self):
        rows = [(0.0, 0.0), (20.0, 5.0)] + [(0.0, 0.0)] * 60 + [(2.6, 2.0)] * 120
        found = self._found(rows)
        assert found.holds and not found.edge_holds

    def test_exactly_three_moving_lead_frames_stop_dead(self):
        rows = [(2.6, 2.0)] * 60 + [(0.15, 0.003)] + [(2.6, 2.0)] * 3 + [(0.15, 0.003)] * 60
        rows += [(2.6, 2.0)] * 30
        assert self._found(rows).holds

    def test_steady_motion_boundary_is_inclusive(self):
        """Lead motion exactly half the motion half a second earlier, between
        one and two grain margins above the frozen frames' noise (0)."""
        rows = [(0.05, 0.06)] * 60 + [(0.05, 0.03)] * 3 + [(0.01, 0.0)] * 60 + [(2.6, 2.0)] * 30
        assert self._found(rows).holds

    def test_accumulated_change_equal_to_the_refresh_limit_is_static(self):
        rows = [(2.6, 2.0)] * 60 + [(0.15, 0.003)] * 60 + [(2.6, 2.0)] * 30
        step = [None] * len(rows)
        refresh = vc.structural_limit(vc.FREEZE_BLIP_MAFD, 16.0)
        for j in range(15, len(rows), 15):
            step[j] = refresh if 60 <= j - 15 and j <= 119 else 2.0
        found = self._found(rows, structural_step=step, step=15)
        assert found.holds and found.holds[0][1] == pytest.approx(61 / 30)
