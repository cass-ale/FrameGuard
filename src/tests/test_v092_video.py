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
