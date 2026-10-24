"""Natural-looking stereo (and mono) equirect frames for the layout content check.

Not the review's synthetic stitches (one picture stacked on itself, a single
global shift): each scene has depth. A sky with clouds above a horizon, a
textured ground plane whose disparity grows towards the bottom of the frame
(nearer ground), and a handful of textured objects at their own depths, drawn
far to near so nearer objects occlude farther ones. The right eye sees every
layer shifted by its own (fractional) disparity, as an omnidirectional stereo
camera does; longitude wraps, as equirect does.

Pure Python and deterministic (seeded) so the tests need no image library.
"""

import math
import random
import subprocess
from typing import List, Optional, Tuple

from media_fixtures import FFMPEG


def _value_noise(width: int, height: int, cell: int, rng: random.Random) -> List[List[float]]:
    """Bilinear value noise in 0..1, wrapping horizontally (longitude)."""
    cols = max(2, width // cell)
    rows = max(2, height // cell + 2)
    grid = [[rng.random() for _ in range(cols)] for _ in range(rows)]
    out = []
    for y in range(height):
        gy = y / cell
        y0 = int(gy)
        fy = gy - y0
        r0, r1 = grid[min(y0, rows - 1)], grid[min(y0 + 1, rows - 1)]
        row = []
        for x in range(width):
            gx = x * cols / width
            x0 = int(gx) % cols
            x1 = (x0 + 1) % cols
            fx = gx - int(gx)
            top = r0[x0] + (r0[x1] - r0[x0]) * fx
            bottom = r1[x0] + (r1[x1] - r1[x0]) * fx
            row.append(top + (bottom - top) * fy)
        out.append(row)
    return out


def _texture(width: int, height: int, rng: random.Random, cells=(48, 16, 5), weights=(0.5, 0.3, 0.2)):
    layers = [_value_noise(width, height, c, rng) for c in cells]
    return [[sum(w * layer[y][x] for w, layer in zip(weights, layers)) for x in range(width)]
            for y in range(height)]


def _sample(row: List[float], xf: float) -> float:
    """Linear interpolation with horizontal wrap (fractional disparity)."""
    width = len(row)
    x0 = math.floor(xf)
    f = xf - x0
    a = row[x0 % width]
    b = row[(x0 + 1) % width]
    return a + (b - a) * f


class Scene:
    """One equirect scene; ``render(disparity_scale)`` draws an eye."""

    def __init__(self, width: int, height: int, seed: int, max_disparity: float, objects: int = 7):
        rng = random.Random(seed)
        self.width, self.height = width, height
        self.max_disparity = max_disparity
        self.horizon = int(height * rng.uniform(0.42, 0.58))
        self.sky = _texture(width, height, rng, cells=(64, 24, 8), weights=(0.6, 0.3, 0.1))
        self.ground = _texture(width, height, rng)
        self.sky_tone = rng.uniform(150, 210)
        self.ground_tone = rng.uniform(60, 120)
        self.objects = []
        for _ in range(objects):
            w = rng.randint(width // 14, width // 5)
            h = rng.randint(height // 10, height // 3)
            cx = rng.uniform(0, width)
            cy = rng.uniform(self.horizon - h * 0.8, min(height - h / 2, self.horizon + height * 0.3))
            depth = rng.uniform(0.15, 1.0)          # fraction of the maximum disparity
            tex = _texture(w + 4, h + 4, rng, cells=(max(3, w // 4), 4, 2), weights=(0.5, 0.3, 0.2))
            tone = rng.uniform(30, 230)
            ellipse = rng.random() < 0.5
            self.objects.append((depth, cx, cy, w, h, tex, tone, ellipse))
        self.objects.sort(key=lambda o: o[0])        # far first, near last

    def render(self, eye: float, out_height: Optional[int] = None, noise: float = 0.0,
               gain: float = 1.0, offset: float = 0.0, seed: int = 0) -> List[float]:
        """Grey pixels of one eye (row-major). ``eye`` scales every disparity:
        0 = left eye, 1 = right eye, -1 = eyes swapped. ``noise`` (grey-level
        standard deviation), ``gain`` and ``offset`` model two cameras that
        do not match exactly."""
        width, height = self.width, self.height
        rows = []
        for y in range(height):
            if y < self.horizon:
                # Sky and distant clouds: effectively at infinity (no disparity).
                row = [self.sky_tone + 70 * (v - 0.5) for v in self.sky[y]]
            else:
                depth = (y - self.horizon) / max(1, height - self.horizon)
                shift = eye * self.max_disparity * depth * 0.8
                src = self.ground[y]
                row = [self.ground_tone + 110 * (_sample(src, x + shift) - 0.5) for x in range(width)]
            rows.append(row)
        for depth, cx, cy, w, h, tex, tone, ellipse in self.objects:
            left = cx - w / 2 - eye * self.max_disparity * depth   # fractional position in this eye
            top = int(cy - h / 2)
            for j in range(h):
                y = top + j
                if not 0 <= y < height:
                    continue
                ty = j / h * 2 - 1
                trow = tex[j + 2]
                for x in range(math.floor(left), math.ceil(left + w)):
                    u = x - left
                    if not 0 <= u < w:
                        continue
                    tx = u / w * 2 - 1
                    if ellipse and tx * tx + ty * ty > 1:
                        continue
                    rows[y][x % width] = tone + 120 * (_sample(trow, u + 2) - 0.5)
        if out_height and out_height != height:
            rows = [rows[int(k * height / out_height)] for k in range(out_height)]
        rng = random.Random(seed)
        return [min(255.0, max(0.0, v * gain + offset + (rng.gauss(0, noise) if noise else 0.0)))
                for row in rows for v in row]


def top_bottom(scene: Scene, swapped: bool = False, eye_height: Optional[int] = None,
               noise: float = 3.0) -> List[float]:
    """Left eye on top, right eye below (Spherical Video V2 stereo_mode 1),
    with independent sensor noise and a 5 % gain / +4 level mismatch."""
    return (scene.render(0.0, eye_height, noise=noise, seed=1)
            + scene.render(-1.0 if swapped else 1.0, eye_height, noise=noise, gain=0.95, offset=4.0, seed=2))


def mono(scene: Scene, out_height: Optional[int] = None, noise: float = 3.0) -> List[float]:
    return scene.render(0.0, out_height, noise=noise, seed=1)


def reflection(scene: Scene) -> List[float]:
    """A lake: the lower half mirrors the upper half (a mono picture whose
    halves are related, but vertically flipped, not stacked)."""
    half = scene.height // 2
    pixels = scene.render(0.0)
    width = scene.width
    rows = [pixels[y * width:(y + 1) * width] for y in range(half)]
    rng = random.Random(7)
    lower = [[min(255.0, max(0.0, v * 0.8 + rng.uniform(-6, 6))) for v in rows[half - 1 - y]] for y in range(half)]
    return [v for row in rows + lower for v in row]


def periodic(width: int, height: int, seed: int) -> List[float]:
    """Mono picture that repeats every half width (left and right halves alike)."""
    rng = random.Random(seed)
    half = _texture(width // 2, height, rng)
    return [40 + 170 * half[y][x % (width // 2)] for y in range(height) for x in range(width)]


def encode(pixels: List[float], width: int, height: int, path: str, seconds: int = 3,
           rate: int = 5) -> str:
    """H.264 MP4 (moov last, keyframe every frame) holding a still of ``pixels``."""
    frame = bytes(int(round(v)) for v in pixels)
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
           "-s", f"{width}x{height}", "-r", str(rate), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-g", "1", "-pix_fmt", "yuv420p", path]
    proc = subprocess.run(cmd, input=frame * (seconds * rate), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-2000:])
    return path


def grab_size_frame(pixels: List[float], width: int, height: int,
                    size: Tuple[int, int]) -> List[float]:
    """Area-average ``pixels`` down to ``size`` (what FFmpeg's scale=flags=area grabs)."""
    out_w, out_h = size
    out = []
    for oy in range(out_h):
        y0, y1 = oy * height / out_h, (oy + 1) * height / out_h
        for ox in range(out_w):
            x0, x1 = ox * width / out_w, (ox + 1) * width / out_w
            total = count = 0.0
            for y in range(int(y0), max(int(y0) + 1, int(math.ceil(y1)))):
                for x in range(int(x0), max(int(x0) + 1, int(math.ceil(x1)))):
                    total += pixels[y * width + x]
                    count += 1
            out.append(total / count)
    return out
