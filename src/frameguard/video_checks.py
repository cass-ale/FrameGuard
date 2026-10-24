"""Frame-level video signal checks run inside the single integrity decode.

core.run_deep_scan decodes every file exactly once. This module adds the
picture measurements to that one pass and turns them into verdict rows:

* **Black Frames** - ``blackdetect`` at full resolution; a long tier (a
  black segment of at least ``black_detect_duration``) and a short-burst
  tier (3 frames up to that duration) that only reports hard-cut bursts,
  i.e. likely edit gaps, never dips to black.
* **Freeze Frames** - a near-identical-run detector on the full-resolution
  frame difference (``scdet`` MAFD), robust to long-GOP I-frame refreshes.
* **Signal Levels** - EBU R 103 v3.0 luma and RGB gamut tolerance, plus a
  full/limited range plausibility test.
* **Flash Risk** - an ITU-R BT.1702-3 general/red flash counter (advisory
  only; certification needs a Harding FPA class analyser).
* **Interlace Detection** - ``idet`` combing in progressive-flagged video and
  field-order disagreement with the stream's metadata.

Measurements leave FFmpeg as small per-frame text files (``metadata``
``file=``) and one raw linear-light grid stream, written to a private temporary
directory. Keeping them out of stderr means they never compete with decoder
errors for the bounded log capture, and threaded filters cannot interleave
their lines mid-record.
"""

from __future__ import annotations

import bisect
import math
import os
import re
import shutil
import statistics
import tempfile
import threading
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Check names (rows) and modes
# ---------------------------------------------------------------------------

BLACK = "Black Frames"
FREEZE = "Freeze Frames"
LEVELS = "Signal Levels"
FLASH = "Flash Risk"
INTERLACE = "Interlace Detection"

VIDEO_CHECK_NAMES = (BLACK, FREEZE, LEVELS, FLASH, INTERLACE)

# Same vocabulary as loudness_mode: camera originals routinely sit outside
# broadcast levels, so "report" measures without judging; "advisory" warns;
# "essential" (broadcast delivery presets) fails a confident violation.
LEVELS_MODES = ("off", "report", "advisory", "essential")


class ScanCancelled(Exception):
    """The operator cancelled while the measurements were being evaluated."""


def check_cancel(cancel: Optional[threading.Event]) -> None:
    """Raise ScanCancelled once ``cancel`` is set (cheap; call in long loops)."""
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()


# Long loops look at the cancel flag every this many items.
CANCEL_CHECK_EVERY = 8192

# ---------------------------------------------------------------------------
# Analysis geometry
# ---------------------------------------------------------------------------

# Levels, gamut and flash are measured on an area-averaged copy at most this
# wide and at least 4x smaller than the source. Averaging 4x4 source pixels
# (sigma 1.15 px) is comparable to the EBU R 103 v3.0 low-pass horizontally
# (1-2-3-4-3-2-1 / 16, sigma 1.41 px) and stronger vertically (1-2-1 / 4,
# sigma 0.71 px), so chroma-subsampling ringing on saturated edges cannot
# trip the 1 % area rule, while regions of the size that rule is about
# (1 % of the picture) survive averaging intact. A 2x copy flagged ringing
# on clean graphics (testsrc-style colour bars) as gamut errors.
ANALYSIS_MAX_WIDTH = 480
ANALYSIS_MIN_FACTOR = 4
# Interlace analysis keeps every line (fields must stay separate) but
# point-samples columns: combing is a vertical-frequency artefact, and 320
# columns classify the benchmark's woven clips exactly as full width does.
INTERLACE_MAX_WIDTH = 320
# Pictures smaller than this are not analysed for levels/flash.
MIN_ANALYSIS_DIMENSION = 32

# BT.1702-3 flashes are judged per region on a 16x9 grid of linear light
# (144 blocks). A block's value is the mean light its area emits (light is
# linearised before it is block-averaged), and partly covered blocks are
# weighted by their covered share (see FlashAccumulator.events), so the 25 %
# area rule is not block-quantised.
FLASH_GRID = (16, 9)
# Linear light is computed on a copy with this many samples per block side
# before block averaging: only those samples mix gamma-coded values, so an
# edge mis-weights at most one 1/8-block strip (see _area_chains).
FLASH_SUBSAMPLES = 8

# ---------------------------------------------------------------------------
# Thresholds (each cites where it comes from)
# ---------------------------------------------------------------------------

# EBU R 103 v3.0 Table 1: preferred range 5-246 (8-bit) / 20-984 (10-bit)
# for luma and for each RGB component, i.e. -5 % .. 105 % of nominal; flag
# when the error exceeds 1 % of the image (Annex 1). The analysis copy is
# 8-bit, so 10-bit limits map onto the same codes (20/4 = 5, 984/4 = 246).
R103_LUMA_MIN = 5
R103_LUMA_MAX = 246
R103_RGB_MIN = -0.05
R103_RGB_MAX = 1.05
R103_AREA = 0.01
R103_MIN_FRAMES = 3

# Range plausibility (codes on the 8-bit analysis copy, after averaging).
# Limited-range content has black at 16 and white at 235; full-range black
# is 0 and white 255.
RANGE_DEEP_BLACK_MAX = 10      # tv: well below limited black
RANGE_SUPER_WHITE_MIN = 240    # tv: well above limited white
RANGE_PC_FLOOR_MAX = 13        # pc: anything here means full-range blacks exist
RANGE_PC_CEIL_MIN = 242        # pc: anything here means full-range whites exist
RANGE_PC_BAND_LO = (14, 19)    # pc: a pile-up here is limited-range black clipping
RANGE_PC_BAND_HI = (231, 237)  # pc: a pile-up here is limited-range white clipping
RANGE_PC_EXTREMES_SHARE = 0.0001  # pc: above this share, full-range codes are in use

# ITU-R BT.1702-3 Annex 1, Guideline 1: a flash is a pair of opposing
# changes in luminance of 20 cd/m2 or more where the darker image is below
# 160 cd/m2 (Notes 2-3: SDR peak white displayed at 200 cd/m2). A sequence is
# not permitted when "the combined area of flashes occurring concurrently
# occupies more than 25% of the displayed screen area" and there are "more
# than three flashes (i.e. six changes in luminance) within any one-second
# period". "Irrespective of luminance, a transition to or from a saturated
# red is also potentially harmful"; BT.1702-3 gives no numeric red
# definition (Annex 3 leaves it to further study), so the WCAG 2.2 one is
# used: R/(R+G+B) >= 0.8 and a change of (R-G-B) x 320 of at least 20, on
# linear RGB 0..1 with negative values set to 0.
FLASH_PEAK_CD = 200.0
FLASH_DELTA_CD = 20.0
FLASH_DARK_CD = 160.0
FLASH_MAX_TRANSITIONS = 6       # > 3 flashes = > 6 transitions in 1 s
FLASH_AREA = 0.25
FLASH_RED_RATIO = 0.8
FLASH_RED_DELTA = 20.0

# Freeze (see detect_freezes()). scdet MAFD is the mean absolute difference
# to the previous frame in % of full scale over all planes. Frozen frames
# measured <= 0.03 (median) on ProRes, CRF and CBR long-GOP H.264/HEVC;
# 48,000 frames of real 4K camera footage never went below 0.026 and only
# 0.05 % of them below 0.42, never for a run; moving synthetic content sits
# at 1-25.
FREEZE_STILL_MAFD = 0.20
FREEZE_BLIP_MAFD = 1.5
FREEZE_MAX_BLIP_FRAMES = 8
FREEZE_RELATIVE_DROP = 0.15
FREEZE_IDENTICAL_MAFD = 0.004
FREEZE_CONTEXT_SECONDS = 2.0

# The flash grid is written in segments of this length and consumed while
# FFmpeg decodes: at most about three segments (~0.8 MB at 60 fps) exist at
# once, whatever the programme length.
FLASH_SEGMENT_SECONDS = 5

# Mean luma (8-bit analysis copy) read back for frames not written: the
# series only records frames dark enough to matter (see mean_luma_limit).
YAVG_ABSENT = 255.0

# Black: the short tier reports a burst only when it is isolated (not part
# of a strobe; the Flash check covers those) and cut hard into black on
# both sides (a dip to black fades through dark frames instead).
BLACK_ISOLATION_SECONDS = 1.0
BLACK_HARD_CUT_CODES = 24.0
BLACK_EDGE_ALLOWANCE = 1.0

# Interlace: share of frames idet's multi-frame detector calls interlaced in
# a one-second window, on a stream that declares itself progressive. idet's
# interlace threshold is raised from 1.04 to 1.5: at the default, clean
# graphics with thin moving horizontal lines (testsrc2, hard-cut mixes) read
# as 17-83 % interlaced; at 1.5 they read 0 % while woven 60p content still
# reads 98-100 % (and a 2 s woven section 25 % of a 10 s clip).
IDET_OPTIONS = "intl_thres=1.5"
# A vertical scroll (end credits) moving an integer number of lines per
# frame is, field by field, a valid interlaced sequence: idet reads it as
# 99 % TFF/BFF although no frame is combed. Such motion leaves every
# column's mean unchanged while the row profile slides, so a combed window
# is discounted when column-profile change is under this share of the total
# profile change (measured: credit rolls 0.001-0.034; woven interlaced
# clips 0.175-0.74 - natural, graphics and vertically tilting content).
INTERLACE_VERTICAL_SHARE = 0.08
# Profile change (mean |delta| in 8-bit codes) below which motion is too
# small to judge; such frames are not counted either way.
INTERLACE_MOTION_FLOOR = 0.02
INTERLACE_WINDOW_SHARE = 0.5
INTERLACE_MIN_SECONDS = 1.0
FIELD_ORDER_MAJORITY = 0.8
FIELD_ORDER_MIN_FRAMES = 30

MATRICES = {
    # Kr-derived Y'CbCr -> R'G'B' coefficients: R = Y + a*Cr,
    # G = Y - b*Cb - c*Cr, B = Y + d*Cb (ITU-R BT.709 / BT.601 / BT.2020).
    "bt709": (1.5748, 0.1873, 0.4681, 1.8556),
    "bt601": (1.402, 0.344136, 0.714136, 1.772),
    "bt2020": (1.4746, 0.16455, 0.57135, 1.8814),
}

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


# ---------------------------------------------------------------------------
# Picture description
# ---------------------------------------------------------------------------

def _matrix_for(video: dict) -> str:
    """The Y'CbCr matrix to invert: the tag, else the HD/SD convention."""
    space = str(video.get("color_space") or "").lower()
    if space.startswith("bt2020"):
        return "bt2020"
    if space in ("smpte170m", "bt470bg", "bt601", "fcc"):
        return "bt601"
    if space == "bt709":
        return "bt709"
    height = int(video.get("height") or 0)
    return "bt709" if height > 576 else "bt601"


def is_full_range(video: dict) -> bool:
    """True for full-range (pc / JPEG) coding: the tag, else a yuvj format."""
    tag = str(video.get("color_range") or "").lower()
    if tag in ("pc", "jpeg", "full"):
        return True
    if tag in ("tv", "mpeg", "limited"):
        return False
    return str(video.get("pix_fmt") or "").lower().startswith("yuvj")


def _rate(text) -> float:
    try:
        num, _, den = str(text or "0/0").partition("/")
        num, den = float(num), float(den or 1)
        return num / den if den else 0.0
    except ValueError:
        return 0.0


@dataclass
class PictureInfo:
    """What the analysis needs to know about the primary video stream."""
    width: int
    height: int
    fps: float
    full_range: bool
    matrix: str
    field_order: str
    hdr: bool
    matrix_assumed: bool = False    # no colour-matrix tag: guessed from the size

    @classmethod
    def from_stream(cls, video: dict) -> "PictureInfo":
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        return cls(width=int(video.get("width") or 0), height=int(video.get("height") or 0),
                   fps=fps if 0 < fps < 1000 else 0.0,
                   full_range=is_full_range(video), matrix=_matrix_for(video),
                   field_order=str(video.get("field_order") or "unknown").lower(),
                   hdr=str(video.get("color_transfer") or "").lower() in _HDR_TRANSFERS,
                   matrix_assumed=str(video.get("color_space") or "").lower()
                   in ("", "unknown", "unspecified", "reserved"))

    @property
    def gamut_matrices(self) -> Tuple[str, ...]:
        """Matrices the RGB gamut is judged under: the signalled one, or, when
        none is signalled, the size-based guess first and then the other
        common SDR matrix.

        Untagged files are often HD encoded with BT.601 coefficients (FFmpeg's
        swscale default when nothing is signalled), so a gamut verdict under
        the guessed BT.709 alone flagged 63-97 % of ordinary pictures. BT.2020
        is not a candidate: it is always signalled in practice (HDR and wide
        gamut deliveries require the tags).
        """
        if not self.matrix_assumed:
            return (self.matrix,)
        return (self.matrix,) + tuple(m for m in ("bt709", "bt601") if m != self.matrix)

    @property
    def range_name(self) -> str:
        return "pc" if self.full_range else "tv"

    @property
    def analysable(self) -> bool:
        return self.width >= MIN_ANALYSIS_DIMENSION and self.height >= MIN_ANALYSIS_DIMENSION


# ---------------------------------------------------------------------------
# Configuration view
# ---------------------------------------------------------------------------

@dataclass
class VideoCheckConfig:
    black: bool = True
    black_duration: float = 0.3
    black_pix_th: float = 0.02
    black_pic_th: float = 0.98
    black_short_frames: int = 3
    freeze: bool = True
    freeze_duration: float = 1.0
    levels_mode: str = "advisory"
    flash: bool = True
    interlace: bool = True

    @classmethod
    def from_cfg(cls, cfg: dict) -> "VideoCheckConfig":
        mode = str(cfg.get("signal_levels_mode", "advisory")).lower()
        return cls(
            black=bool(cfg.get("black_frames_enabled", True)),
            black_duration=float(cfg.get("black_detect_duration", 0.3)),
            black_pix_th=float(cfg.get("black_detect_pix_threshold", 0.02)),
            black_pic_th=float(cfg.get("black_detect_picture_threshold", 0.98)),
            black_short_frames=int(cfg.get("black_short_min_frames", 3)),
            freeze=bool(cfg.get("freeze_frames_enabled", True)),
            freeze_duration=float(cfg.get("freeze_detect_duration", 1.0)),
            levels_mode=mode if mode in LEVELS_MODES else "advisory",
            flash=bool(cfg.get("flash_check_enabled", True)),
            interlace=bool(cfg.get("interlace_check_enabled", True)),
        )

    @property
    def levels(self) -> bool:
        return self.levels_mode != "off"

    def enabled_names(self) -> List[str]:
        flags = ((BLACK, self.black), (FREEZE, self.freeze), (LEVELS, self.levels),
                 (FLASH, self.flash), (INTERLACE, self.interlace))
        return [name for name, on in flags if on]


# ---------------------------------------------------------------------------
# FFmpeg filter-graph construction
# ---------------------------------------------------------------------------

def _escape(text: str, specials: str) -> str:
    return "".join("\\" + ch if ch in specials else ch for ch in text)


def filter_path(path: str) -> str:
    """A file path quoted for a filter option inside -filter_complex.

    FFmpeg parses a graph description twice: the option value (``\\``, ``'``
    and ``:`` are special), then the graph (``\\ ' [ ] , ;``). Windows paths
    carry a drive colon and backslashes, so both levels are escaped
    (FFmpeg filters documentation, "Notes on filtergraph escaping").
    """
    return _escape(_escape(path, "\\':"), "\\'[],;")


def _print(key: str, path: str, value: Optional[str] = None,
           function: Optional[str] = None) -> str:
    """metadata print of ``key``; with ``value``/``function`` only matching frames.

    A conditional print still numbers frames by their position in the
    stream ("frame:N"), so sparse output stays aligned.
    """
    condition = f":value={value}:function={function}" if value is not None else ""
    return f"metadata=mode=print:key={key}{condition}:file={filter_path(path)}"


def _select(key: str, value: str, function: str) -> str:
    """Pass only frames whose ``key`` matches (they keep their own pts)."""
    return f"metadata=mode=select:key={key}:value={value}:function={function}"


def _rgb_mask_expressions(info: PictureInfo,
                          matrix: Optional[str] = None) -> Tuple[str, str, str, str]:
    """lut2 expressions for the two packed gamut stages.

    Stage 1 pairs planes (Y, Y, U) with (V, U, V): R depends on (Y, Cr) and
    B on (Y, Cb), so one three-plane lut2 yields the R mask, the B mask and a
    helper plane W = 128 + (b*Cb + c*Cr) in luma code units (quantisation
    error <= 0.23 % of nominal, far inside the 5 % tolerance). Stage 2 pairs
    W with Y for the G mask, G = Y - (W - 128). Masks are 255 where the
    component lies outside -5 % .. 105 %. Returns (red, blue, helper, green);
    ``green`` takes x = W, y = Y. ``matrix`` overrides ``info.matrix``.
    """
    a, b, c, d = MATRICES[matrix or info.matrix]
    if info.full_range:
        y_off, y_scale, c_scale = 0, 255.0, 255.0
    else:
        y_off, y_scale, c_scale = 16, 219.0, 224.0

    def outside(expr: str) -> str:
        return f"if(lt({expr},{R103_RGB_MIN})+gt({expr},{R103_RGB_MAX}),255,0)"

    red = outside(f"((x-{y_off})/{y_scale}+{a}*(y-128)/{c_scale})")
    blue = outside(f"((x-{y_off})/{y_scale}+{d}*(y-128)/{c_scale})")
    helper = f"clip(128+({b}*(x-128)/{c_scale}+{c}*(y-128)/{c_scale})*{y_scale},0,255)"
    green = outside(f"((y-{y_off})/{y_scale}-(x-128)/{y_scale})")
    return red, blue, helper, green


def _luma_mask_expressions(info: PictureInfo) -> Tuple[str, str, str]:
    """Three luma masks (255 = pixel in class) of the averaged picture.

    Limited range: outside the R 103 luma limits, deep sub-black, and
    super-white. Full range: the R 103 luma limits cannot be exceeded, so
    the planes test whether full-range codes are used at all (black or
    white extremes) and whether limited-range clip points pile up.
    """
    def band(*ranges: Tuple[int, int]) -> str:
        terms = "+".join(f"between(val,{low},{high})" for low, high in ranges)
        return f"if({terms},255,0)"

    if info.full_range:
        return (band((0, RANGE_PC_FLOOR_MAX), (RANGE_PC_CEIL_MIN, 255)),
                band(RANGE_PC_BAND_LO), band(RANGE_PC_BAND_HI))
    return (band((0, R103_LUMA_MIN - 1), (R103_LUMA_MAX + 1, 255)),
            band((0, RANGE_DEEP_BLACK_MAX)), band((RANGE_SUPER_WHITE_MIN, 255)))


def _flash_linear_expression(info: PictureInfo) -> str:
    """lutrgb expression: R'G'B' code -> linear light scaled to 0..255.

    The BT.1886 reference EOTF (gamma 2.4) models the SDR display BT.1702-3
    assumes; 255 maps to its 200 cd/m2 peak.
    """
    return "clip(255*pow(val/255,2.4),0,255)"


def black_filter(config: VideoCheckConfig) -> str:
    """blackdetect reporting every black run (d=0); the tiers are applied later."""
    if not config.black:
        return "null"
    return f"blackdetect@v=d=0:pix_th={config.black_pix_th}:pic_th={config.black_pic_th}"


def mean_luma_limit(config: VideoCheckConfig, full_range: bool) -> float:
    """Mean luma at or above which a frame's exact value cannot matter.

    The short-burst test (classify_black) compares a black frame's mean with
    its neighbours': a neighbour at least BLACK_HARD_CUT_CODES brighter is a
    hard cut. A black frame's mean is at most ``pic_th`` x black level +
    (1 - ``pic_th``) x 255, so any neighbour above that plus the cut margin
    passes the test whatever its exact value. Frames at or above this limit
    are not written and read back as YAVG_ABSENT (255), which decides
    identically; 8 codes cover the analysis copy's rounding.
    """
    level = config.black_pix_th * 255.0 if full_range else 16.0 + config.black_pix_th * 219.0
    darkest_black = config.black_pic_th * level + (1.0 - config.black_pic_th) * 255.0
    return darkest_black + BLACK_HARD_CUT_CODES + 8.0


def _release(directory: str, reader: Optional["_FlashSegmentReader"]) -> None:
    """Stop the flash reader and remove the measurement directory."""
    if reader is not None:
        reader.stop()
    shutil.rmtree(directory, ignore_errors=True)


class FlashUnavailable(Exception):
    """The streamed flash grid could not be read or evaluated."""


class _FlashSegmentReader(threading.Thread):
    """Folds FFmpeg's flash-grid segments into a FlashAccumulator as they close.

    Segment k is complete once segment k+1 exists (the segment muxer closes
    one file before opening the next) or once FFmpeg has exited; each one is
    read, fed and deleted. The thread stops on close() or when the scan is
    cancelled.
    """

    def __init__(self, directory: str, fps: float, cancel: Optional[threading.Event]):
        super().__init__(name="frameguard-flash-reader", daemon=True)
        self.directory = directory
        self.template = os.path.join(directory, "flash_").replace("%", "%%") + "%06d.rgb"
        self.accumulator = FlashAccumulator(fps)
        self.cancel = cancel
        self.error: Optional[str] = None
        self.segments_read = 0
        self._stop_event = threading.Event()
        self._finish_event = threading.Event()

    def _segment(self, number: int) -> str:
        return os.path.join(self.directory, f"flash_{number:06d}.rgb")

    def _halted(self) -> bool:
        return self._stop_event.is_set() or bool(self.cancel is not None and self.cancel.is_set())

    def run(self) -> None:
        try:
            while not self._halted():
                finishing = self._finish_event.is_set()
                progressed = False
                while (not self._halted() and os.path.exists(self._segment(self.segments_read))
                       and (finishing or os.path.exists(self._segment(self.segments_read + 1)))):
                    self._consume(self._segment(self.segments_read))
                    self.segments_read += 1
                    progressed = True
                if finishing:
                    if not self._halted():
                        self.accumulator.finish()
                    return
                if not progressed:
                    self._stop_event.wait(0.2)
        except Exception as exc:  # noqa: BLE001 - thread boundary: finish() reports it
            self.error = f"{type(exc).__name__}: {exc}"

    def _consume(self, path: str) -> None:
        with open(path, "rb") as handle:
            data = handle.read()
        self.accumulator.feed(data)
        os.remove(path)

    def finish(self, cancel: Optional[threading.Event]) -> "FlashAccumulator":
        """FFmpeg has exited: read the remaining segments and return the result."""
        self._finish_event.set()
        while self.is_alive():
            self.join(0.2)
            check_cancel(cancel)
        check_cancel(cancel)
        if self.error:
            raise FlashUnavailable(self.error)
        if not self.accumulator.frames and not self.segments_read:
            raise FlashUnavailable("FFmpeg wrote no flash measurements")
        return self.accumulator

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive() and self is not threading.current_thread():
            self.join(5)


class VideoAnalysis:
    """The per-scan picture analysis: graph fragment, outputs, parsing.

    Use as a context manager so the temporary measurement files are always
    removed, whatever happens to the scan.
    """

    def __init__(self, video: dict, cfg: dict,
                 cancel: Optional[threading.Event] = None):
        self.info = PictureInfo.from_stream(video)
        self.config = VideoCheckConfig.from_cfg(cfg)
        self.index = int(video["index"])
        self.cancel = cancel
        self.directory: Optional[str] = None
        self.paths: Dict[str, str] = {}
        self.flash_reader: Optional[_FlashSegmentReader] = None

    # -- lifetime -----------------------------------------------------------
    def __enter__(self) -> "VideoAnalysis":
        return self.open()

    def open(self) -> "VideoAnalysis":
        """Create the private directory FFmpeg writes the measurements to.

        A finalizer removes it even when the scan is cancelled or raises
        before close() is reached.
        """
        self.directory = tempfile.mkdtemp(prefix="frameguard-video-")
        names = ("mafd", "idet", "colmo", "rowmo", "luma", "band1", "band2", "yavg",
                 "gam_r", "gam_g", "gam_b")
        self.paths = {name: os.path.join(self.directory, f"{name}.txt") for name in names}
        if self.flash_enabled:
            self.flash_reader = _FlashSegmentReader(self.directory, self.info.fps, self.cancel)
            self.flash_reader.start()
        # The finalizer holds the directory and the reader, never self, so it
        # still runs when a cancelled scan drops the analysis without close().
        self._cleanup = weakref.finalize(self, _release, self.directory, self.flash_reader)
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.directory:
            self._cleanup()
            self.directory = None

    # -- what runs ------------------------------------------------------------
    @property
    def needs_area_branch(self) -> bool:
        """Levels, flash and the black short-tier mean luma share one copy."""
        c = self.config
        return self.info.analysable and (c.levels or c.flash or c.black)

    @property
    def needs_interlace_branch(self) -> bool:
        return self.config.interlace and self.info.height >= 2 * MIN_ANALYSIS_DIMENSION

    @property
    def flash_enabled(self) -> bool:
        return self.config.flash and self.needs_area_branch and not self.info.hdr

    def filter_chains(self, out_label: str = "v") -> List[str]:
        """Chains for the main graph; its black-detect chain ends in ``[out_label]``.

        The frame difference always runs here: it carries every frame's
        presentation time, which the other per-frame series are aligned to.
        """
        return [f"[0:{self.index}]split=2[vs_main][vs_fz]",
                f"[vs_main]{black_filter(self.config)}[{out_label}]",
                f"[vs_fz]scdet@fz=threshold=100,"
                f"{_print('lavfi.scd.mafd', self.paths['mafd'])},nullsink"]

    def extra_graphs(self) -> List[Tuple[str, Optional[str]]]:
        """Separate filter graphs: (description, label to map to the null output).

        FFmpeg runs each -filter_complex graph in its own thread, while the
        filters inside one graph run one after another for every frame.
        Interlace and the area analysis therefore get graphs of their own:
        on an idle 16-core machine this took the 4K overhead from +24 % to
        +7-9 % for a little more CPU. A graph without an output label (the
        area graph when it feeds the raw flash output) returns None.
        """
        graphs: List[Tuple[str, Optional[str]]] = []
        if self.needs_interlace_branch:
            graphs.append((";".join(self._interlace_chains()), "[vs_il]"))
        if self.needs_area_branch:
            label = None if self.flash_enabled else "[vs_area]"
            graphs.append((";".join(self._area_chains()), label))
        return graphs

    def _interlace_chains(self) -> List[str]:
        """idet plus the column and row luma profiles' frame-to-frame change.

        The profiles tell a vertical scroll (rows slide, columns unchanged)
        from motion that can comb. Column means come from area reductions
        of at most 16x per step (a single large reduction is not an exact
        mean); tblend difference + signalstats gives mean |delta| per frame.

        Only frames idet calls interlaced (TFF/BFF) are written: they are
        the only ones the decision reads, and progressive footage then
        writes nothing. The profiles are kept in step with idet's verdict
        (tblend keeps the current frame's metadata) and matched by pts.
        """
        label = "lavfi.idet.multiple.current_frame"
        interlaced = _select(label, "ff", "ends_with")
        reduce16 = "scale=w={w}:h='max(1,trunc(ih/16))':flags=area"
        return [
            f"[0:{self.index}]scale=w='min({INTERLACE_MAX_WIDTH},iw)':h=ih:flags=neighbor,"
            f"idet@il={IDET_OPTIONS},split=3[il_det][il_col][il_row]",
            f"[il_det]{_print(label, self.paths['idet'], 'ff', 'ends_with')}[vs_il]",
            f"[il_col]format=gray,{reduce16.format(w=64)},{reduce16.format(w=64)},"
            f"scale=w=64:h=1:flags=area,tblend=all_mode=difference,signalstats,{interlaced},"
            f"{_print('lavfi.signalstats.YAVG', self.paths['colmo'])},nullsink",
            "[il_row]format=gray,scale=w='max(1,trunc(iw/16))':h=ih:flags=area,"
            f"scale=w=1:h=ih:flags=area,tblend=all_mode=difference,signalstats,{interlaced},"
            f"{_print('lavfi.signalstats.YAVG', self.paths['rowmo'])},nullsink",
        ]

    def _area_chains(self) -> List[str]:
        """Levels, gamut, mean luma and flash from one area-averaged copy.

        Few, cheap filters: the graph runs serially per frame, so filter
        count matters more than pixel count on a 480-pixel copy. Planes are
        reordered with single-input mergeplanes (shuffleplanes would force a
        yuva444p conversion) and processed three at a time by lutyuv/lut2,
        and each packed mask frame gives three area fractions from one
        signalstats call (plane averages, on a 4x smaller copy - area
        averaging preserves the mean).
        """
        info, c = self.info, self.config
        rng = info.range_name
        uses = []
        matrices = info.gamut_matrices
        if c.levels:
            uses += ["luma"] + [f"gamut{k}" for k in range(len(matrices))]
        if c.black or c.levels:
            uses.append("mean")
        if self.flash_enabled:
            uses.append("flash")
        labels = "".join(f"[ar_{u}]" for u in uses)
        reduce4 = "scale=w='max(1,trunc(iw/4))':h='max(1,trunc(ih/4))':flags=area"
        chains = [
            f"[0:{self.index}]scale=w='max(2,min({ANALYSIS_MAX_WIDTH},trunc(iw/{2 * ANALYSIS_MIN_FACTOR})*2))'"
            f":h=-2:flags=area:in_range={rng}:out_range={rng},format=yuv444p,"
            f"split={len(uses)}{labels}"]
        if c.levels:
            m1, m2, m3 = _luma_mask_expressions(info)
            for k, matrix in enumerate(matrices):
                chains += self._gamut_chains(k, matrix)
            chains += self._gamut_combined(len(matrices), reduce4)
            chains += [
                f"[ar_luma]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=0:map2s=0:map2p=0:format=yuv444p,lutyuv=y='{m1}':u='{m2}':v='{m3}',"
                f"{reduce4},signalstats,{','.join(self._luma_prints())},nullsink",
            ]
        if c.black or c.levels:
            # Without the flash output this branch is the graph's output.
            tail = "nullsink" if self.flash_enabled else "null[vs_area]"
            limit = f"{mean_luma_limit(self.config, info.full_range):.3f}"
            chains.append(
                f"[ar_mean]scale=w='max(1,trunc(iw/8))':h='max(1,trunc(ih/8))':flags=area,"
                f"signalstats,{_print('lavfi.signalstats.YAVG', self.paths['yavg'], limit, 'less')},"
                f"{tail}")
        if self.flash_enabled:
            linear = _flash_linear_expression(info)
            cols, rows = FLASH_GRID
            # Area-preserving: light is linearised on a copy with
            # FLASH_SUBSAMPLES samples per block side *before* it is
            # block-averaged, so a block's value is the mean light its area
            # emits (averaging gamma-coded values would under-read a partly
            # covered block: half white reads 19 %, not 50 %). Gamma values
            # mix only within one of those samples, i.e. in a strip of at
            # most 1/8 of a block along an edge.
            sub = FLASH_SUBSAMPLES
            chains.append(
                f"[ar_flash]scale=w={cols * sub}:h={rows * sub}:flags=area,"
                f"scale=in_color_matrix={info.matrix}:in_range={rng}:out_range=pc,"
                f"format=gbrp,lutrgb=r='{linear}':g='{linear}':b='{linear}',"
                f"scale={cols}:{rows}:flags=area[vs_flash]")
        return chains

    def _share_print(self, plane: str, name: str, above: float = 0.0) -> str:
        """Print a mask plane's share only for frames where it can matter.

        Frames not written read back as 0. ``above`` is the share at or below
        which the decision cannot use the value (0: any non-zero share, as
        the displayed maxima need). Zero is what nearly every frame of clean
        footage measures, so most series are written for few frames.
        """
        # FFmpeg's "greater" also passes a value equal to the reference, so
        # the reference sits just under the threshold (and just above zero:
        # the smallest real share on the reduced mask prints as about 0.002).
        reference = max(above * 255.0 * 0.999, 0.000001)
        return _print(f"lavfi.signalstats.{plane}AVG", self.paths[name],
                      f"{reference:.6g}", "greater")

    def _luma_prints(self) -> List[str]:
        """Prints of the packed luma masks (Y: R 103 luma / extremes, U, V: bands).

        Limited range: the luma share is displayed, so every non-zero value
        is kept; only "deep black above 1 %" is read from the first band and
        the second band is not used. Full range: only "extremes above 0.01 %"
        and "band above 1 %" are read (range_plausibility).
        """
        if self.info.full_range:
            return [self._share_print("Y", "luma", RANGE_PC_EXTREMES_SHARE),
                    self._share_print("U", "band1", R103_AREA),
                    self._share_print("V", "band2", R103_AREA)]
        return [self._share_print("Y", "luma"), self._share_print("U", "band1", R103_AREA)]

    def _gamut_chains(self, k: int, matrix: str) -> List[str]:
        """Packed (R, G, B) gamut masks of ``[ar_gamut<k>]`` under ``matrix`` -> ``[g<k>_rgb]``."""
        red, blue, helper, green = _rgb_mask_expressions(self.info, matrix)
        g = f"g{k}"
        return [
            f"[ar_gamut{k}]split=3[{g}_src][{g}_alt][{g}_luma]",
            f"[{g}_src]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=0:map2s=0:map2p=1"
            f":format=yuv444p[{g}_a]",
            f"[{g}_alt]mergeplanes=map0s=0:map0p=2:map1s=0:map1p=1:map2s=0:map2p=2"
            f":format=yuv444p[{g}_b]",
            f"[{g}_a][{g}_b]lut2=c0='{red}':c1='{blue}':c2='{helper}',"
            f"mergeplanes=map0s=0:map0p=0:map1s=0:map1p=2:map2s=0:map2p=1:format=yuv444p[{g}_rwb]",
            f"[{g}_luma]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=0:map2s=0:map2p=0"
            f":format=yuv444p[{g}_yyy]",
            f"[{g}_rwb][{g}_yyy]lut2=c0='x':c1='{green}':c2='x'[{g}_rgb]",
        ]

    def _gamut_combined(self, count: int, reduce4: str) -> List[str]:
        """R, G and B shares, a pixel counting only if outside under every matrix.

        With one (signalled) matrix this is that matrix's masks. Without a
        tag, the candidate matrices' masks are intersected per pixel
        (minimum of 0/255 masks), so the shares are already "outside R 103
        under every candidate" - and an untagged file that is in gamut under
        one standard matrix writes nothing.
        """
        chains, label = [], "[g0_rgb]"
        for k in range(1, count):
            chains.append(f"{label}[g{k}_rgb]lut2=c0='min(x,y)':c1='min(x,y)':c2='min(x,y)'[gand{k}]")
            label = f"[gand{k}]"
        chains.append(f"{label}{reduce4},signalstats,{self._share_print('Y', 'gam_r')},"
                      f"{self._share_print('U', 'gam_g')},{self._share_print('V', 'gam_b')},nullsink")
        return chains

    def extra_outputs(self) -> List[str]:
        """Arguments for the raw flash-grid output (after the main output).

        The grid (FlashAccumulator.FRAME_BYTES a frame) is written in short segments that the
        reader thread folds into a FlashAccumulator and deletes while FFmpeg
        is still decoding, so it never accumulates on disk. Timestamps are
        passed through untouched: one grid per decoded frame, in order.
        """
        if self.flash_reader is None:
            return []
        # -y: the files live in this scan's private directory; never prompt.
        return ["-y", "-map", "[vs_flash]", "-fps_mode", "passthrough",
                "-c:v", "rawvideo", "-pix_fmt", "gbrp", "-f", "segment",
                "-segment_format", "rawvideo", "-segment_time", str(FLASH_SEGMENT_SECONDS),
                self.flash_reader.template]

    # -- results --------------------------------------------------------------
    def collect(self) -> "VideoSeries":
        """Read every per-frame series FFmpeg wrote (missing files -> empty).

        The frame difference is written for every frame and is the master
        timeline; sparse series are placed by frame number (or by pts after
        a frame-selecting filter) and the rest read back as their default.
        Raises ScanCancelled when the scan is cancelled meanwhile.
        """
        cancel = self.cancel
        master = read_print_entries(self.paths.get("mafd", ""), cancel)
        series = VideoSeries(times=[e[2] for e in master], mafd=[_to_float(e[3]) for e in master])
        by_frame = {e[0]: k for k, e in enumerate(master)}
        by_pts = {e[1]: k for k, e in enumerate(master)}

        def sparse(name: str, default, convert, table=by_frame, key: int = 0) -> list:
            values = [default] * len(master)
            for entry in read_print_entries(self.paths[name], cancel):
                position = table.get(entry[key])
                if position is not None and entry[3] != "":
                    values[position] = convert(entry[3])
            return values

        if self.needs_interlace_branch:
            series.idet = sparse("idet", "", str)
            series.col_motion = sparse("colmo", None, lambda v: _to_float(v, None), by_pts, 1)
            series.row_motion = sparse("rowmo", None, lambda v: _to_float(v, None), by_pts, 1)
        if self.needs_area_branch and self.config.levels:
            def fraction(name: str) -> List[float]:
                return sparse(name, 0.0, lambda v: _to_float(v) / 255.0)
            series.luma, series.band1, series.band2 = fraction("luma"), fraction("band1"), fraction("band2")
            # The largest single-component share: never above the union of
            # R, G and B violations, so it can only under-report.
            series.gam = [max(r, g, b) for r, g, b in
                          zip(fraction("gam_r"), fraction("gam_g"), fraction("gam_b"))]
        if self.needs_area_branch and (self.config.black or self.config.levels):
            series.yavg = sparse("yavg", YAVG_ABSENT, _to_float)
        if self.flash_reader is not None:
            try:
                series.flash_stream = self.flash_reader.finish(cancel)
            except FlashUnavailable as exc:
                series.flash_error = str(exc)
        series.align()
        return series


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_FRAME_LINE = re.compile(r"^frame:\s*(\d+)\s+pts:\s*(\S+)\s+pts_time:\s*(\S+)")


def read_print_entries(path: str, cancel: Optional[threading.Event] = None) -> List[list]:
    """[frame number, pts, pts_time, value] per frame of a ``metadata=print`` file.

    A frame without the key (scdet's first frame) has the value "".
    """
    entries: List[list] = []
    if not path or not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            if number % CANCEL_CHECK_EVERY == 0:
                check_cancel(cancel)
            match = _FRAME_LINE.match(line)
            if match:
                try:
                    when = float(match.group(3))
                except ValueError:
                    when = entries[-1][2] if entries else 0.0
                entries.append([int(match.group(1)), match.group(2), when, ""])
            elif "=" in line and entries:
                entries[-1][3] = line.split("=", 1)[1].strip()
    return entries


def read_metadata_series(path: str) -> Tuple[List[float], List[str]]:
    """(pts_time per frame, value per frame) from a ``metadata=print`` file."""
    entries = read_print_entries(path)
    return [e[2] for e in entries], [e[3] for e in entries]


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class VideoSeries:
    """Per-frame measurements, index-aligned to ``times``."""
    times: List[float] = field(default_factory=list)
    mafd: List[float] = field(default_factory=list)
    idet: List[str] = field(default_factory=list)
    col_motion: List[Optional[float]] = field(default_factory=list)
    row_motion: List[Optional[float]] = field(default_factory=list)
    luma: List[float] = field(default_factory=list)   # tv: outside R 103; pc: extremes used
    gam: List[float] = field(default_factory=list)       # outside under every candidate matrix
    band1: List[float] = field(default_factory=list)
    band2: List[float] = field(default_factory=list)
    yavg: List[float] = field(default_factory=list)
    flash: List[tuple] = field(default_factory=list)   # in-memory grid (tests, batch use)
    flash_stream: Optional["FlashAccumulator"] = None   # streamed grid (scans)
    flash_error: Optional[str] = None

    def align(self) -> None:
        """Truncate every non-empty series to the shortest one.

        All branches see the same decoded frames in the same order, so a
        length difference only arises when FFmpeg stopped mid-flush; the
        common prefix is the part every branch measured.
        """
        names = ("times", "mafd", "idet", "col_motion", "row_motion", "luma", "gam",
                 "band1", "band2", "yavg", "flash")
        lengths = [len(getattr(self, n)) for n in names if getattr(self, n)]
        if not lengths:
            return
        common = min(lengths)
        for name in names:
            values = getattr(self, name)
            if values:
                setattr(self, name, values[:common])

    @property
    def frames(self) -> int:
        return len(self.times)

    @property
    def flash_frames(self) -> int:
        return self.flash_stream.frames if self.flash_stream is not None else len(self.flash)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

Interval = Tuple[float, float]          # (start seconds, duration seconds)


def timecode(seconds: float) -> str:
    """HH:MM:SS.mmm, the format every FrameGuard event list uses."""
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def event_list(events: Sequence[Interval], limit: int = 6) -> str:
    shown = [f"{timecode(start)} ({duration:.1f}s)" for start, duration in events[:limit]]
    if len(events) > limit:
        shown.append(f"+{len(events) - limit} more")
    return ", ".join(shown)


def _frame_period(fps: float, times: Sequence[float]) -> float:
    """Nominal frame duration: from the stream rate, else from timestamps."""
    if fps > 0:
        return 1.0 / fps
    if len(times) > 1:
        steps = [b - a for a, b in zip(times, times[1:]) if b > a]
        if steps:
            return statistics.median(steps)
    return 1.0 / 30.0


def frame_runs(flags: Sequence[bool], min_frames: int = 1) -> List[Tuple[int, int]]:
    """(first, last) index of each run of True at least ``min_frames`` long."""
    runs, start = [], None
    for i, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start >= min_frames:
                runs.append((start, i - 1))
            start = None
    return runs


def _interval_of(times: Sequence[float], first: int, last: int, period: float) -> Interval:
    return times[first], times[last] - times[first] + period


# ---------------------------------------------------------------------------
# Black frames
# ---------------------------------------------------------------------------

@dataclass
class BlackFindings:
    segments: List[Interval]        # >= black_detect_duration, edge-trimmed
    bursts: List[Tuple[float, int]]  # (start, frames) hard-cut short bursts
    ignored_bursts: int              # short runs judged fades or strobes


def _mean_luma_at(times: Sequence[float], yavg: Sequence[float], t: float) -> Optional[float]:
    """Mean luma of the frame shown at time ``t`` (nearest earlier frame)."""
    if not yavg or not times:
        return None
    lo, hi = 0, min(len(times), len(yavg)) - 1
    if t < times[0] or hi < 0:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if times[mid] <= t + 1e-6:
            lo = mid
        else:
            hi = mid - 1
    return yavg[lo]


def classify_black(events: Sequence[Tuple[float, float]], duration: float, fps: float,
                   cfg: VideoCheckConfig, times: Sequence[float] = (),
                   yavg: Sequence[float] = ()) -> BlackFindings:
    """Split blackdetect runs into black segments and short hard-cut bursts.

    ``events`` are (start, end) runs of pictures with at least
    ``black_detect_picture_threshold`` of their pixels at or below the
    ``black_detect_pix_threshold`` level (Y <= 20 at 8-bit limited range by
    default; FFmpeg scales the level for full range and higher bit depths).

    * The first and last second are excluded: fades from and to black at the
      head and tail are normal.
    * A run of at least ``black_detect_duration`` is a black segment.
    * A run of ``black_short_min_frames`` frames up to that duration is a
      short burst - the classic edit gap - and is reported only when it is
      isolated (a strobe is the Flash check's business) and both neighbouring
      frames are clearly brighter than black (a dip to black fades through
      dark frames, so its neighbours are dark too). EBU QC item 0016B uses a
      3-frame minimum in its example.
    """
    period = _frame_period(fps, times)
    runs = sorted((float(s), max(0.0, float(e) - float(s))) for s, e in events)
    segments: List[Interval] = []
    bursts: List[Tuple[float, int]] = []
    ignored = 0
    edge = BLACK_EDGE_ALLOWANCE if duration > 3 * BLACK_EDGE_ALLOWANCE else 0.0
    for position, (start, length) in enumerate(runs):
        begin = max(start, edge)
        finish = min(start + length, duration - edge) if edge else start + length
        if finish - begin <= period * 0.5:
            continue
        if finish - begin >= cfg.black_duration - 1e-6:
            segments.append((begin, finish - begin))
            continue
        frames = int(round(length / period))
        if cfg.black_short_frames <= 0 or frames < cfg.black_short_frames:
            continue
        gaps = []
        if position > 0:
            gaps.append(start - sum(runs[position - 1]))
        if position + 1 < len(runs):
            gaps.append(runs[position + 1][0] - (start + length))
        isolated = all(gap > BLACK_ISOLATION_SECONDS for gap in gaps)
        before = _mean_luma_at(times, yavg, start - period * 0.5)
        after = _mean_luma_at(times, yavg, start + length + period * 0.25)
        inside = _mean_luma_at(times, yavg, start + period * 0.25)
        hard_cut = (before is not None and after is not None and inside is not None
                    and before - inside >= BLACK_HARD_CUT_CODES
                    and after - inside >= BLACK_HARD_CUT_CODES)
        if isolated and hard_cut:
            bursts.append((start, frames))
        else:
            ignored += 1
    return BlackFindings(segments, bursts, ignored)


def black_entry(findings: BlackFindings, duration: float,
                cfg: VideoCheckConfig) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Black Frames row."""
    level = f"≥{cfg.black_pic_th * 100:g}% of the picture at black level"
    if not findings.segments and not findings.bursts:
        note = (f" ({findings.ignored_bursts} brief dip(s)/flash(es) to black ignored)"
                if findings.ignored_bursts else "")
        return "PASS", f"No black segments detected{note}.", 100
    parts = []
    status, confidence = "WARN", 55
    if findings.segments:
        total = sum(d for _, d in findings.segments)
        parts.append(f"{len(findings.segments)} black segment(s) ≥{cfg.black_duration:g}s, "
                     f"total {total:.1f}s ({level}). At: {event_list(findings.segments)}")
        if duration > 0 and total > duration * 0.1 and total >= 4.0:
            status, confidence = "FAIL", 90
        else:
            confidence = 65
    if findings.bursts:
        shown = ", ".join(f"{timecode(s)} ({n} frames)" for s, n in findings.bursts[:6])
        more = f", +{len(findings.bursts) - 6} more" if len(findings.bursts) > 6 else ""
        parts.append(f"{len(findings.bursts)} short black burst(s) cut in hard "
                     f"(possible edit gap): {shown}{more}")
    return status, ". ".join(parts) + ".", confidence


# ---------------------------------------------------------------------------
# Freeze frames
# ---------------------------------------------------------------------------

@dataclass
class FreezeFindings:
    holds: List[Interval]           # mid-programme freezes to report
    edge_holds: List[Interval]      # static pictures touching the head or tail
    black_holds: int = 0            # candidates that were black (Black Frames' job)
    edge_dominant: bool = False     # a head/tail hold covers half the file


def _still_runs(mafd: Sequence[float]) -> List[Tuple[int, int]]:
    """Runs of near-identical frames, bridging long-GOP refresh blips.

    ``mafd[i]`` is the mean absolute difference between frame i and i-1. A
    digitally frozen picture re-encoded by a long-GOP encoder is not bit
    identical: every I-frame re-codes it, which shows as one to three frames
    of small change (measured up to 1.0 % MAFD on 10 Mbit/s CBR H.264 and
    HEVC), and a starved CBR encoder spends the first few frames refining
    the picture (six frames of 0.2-0.4 % at 3 Mbit/s). FFmpeg's
    freezedetect compares against the first frozen frame, so each refresh
    restarted its timer and freezes shorter than about two GOPs were never
    reported. Here up to FREEZE_MAX_BLIP_FRAMES consecutive moderate-change
    frames (below FREEZE_BLIP_MAFD, far under real motion) do not end a run.
    Returns (first, last) indices into ``mafd``; the frozen picture itself
    starts one frame earlier.
    """
    runs: List[Tuple[int, int]] = []
    start = last = None
    blips = 0
    for i in range(1, len(mafd)):
        value = mafd[i]
        if value < FREEZE_STILL_MAFD:
            if start is None:
                start = i
            last, blips = i, 0
        elif start is not None and value < FREEZE_BLIP_MAFD and blips < FREEZE_MAX_BLIP_FRAMES:
            blips += 1
        else:
            if start is not None:
                runs.append((start, last))
            start, blips = None, 0
    if start is not None:
        runs.append((start, last))
    return runs


def _overlap(interval: Interval, others: Sequence[Interval]) -> float:
    start, length = interval
    end = start + length
    covered = 0.0
    for o_start, o_length in others:
        covered += max(0.0, min(end, o_start + o_length) - max(start, o_start))
    return covered


def detect_freezes(mafd: Sequence[float], times: Sequence[float], fps: float,
                   min_duration: float, duration: float,
                   black: Sequence[Interval] = ()) -> FreezeFindings:
    """Frozen-picture runs that real low-motion footage does not produce.

    A candidate run of still frames (see _still_runs) at least
    ``min_duration`` long is a freeze when its median frame difference is
    either essentially zero (bit-identical frames: <= FREEZE_IDENTICAL_MAFD)
    or below FREEZE_RELATIVE_DROP of the activity in the two seconds around
    it. Absolute thresholds alone cannot work: a dark, quiet scene changes
    less per frame (0.1-0.4 code mean difference) than a CBR encoder's
    refinement noise on a frozen picture, and FFmpeg's freezedetect fired
    on such scenes (0.9.0 flagged a night scene as a 4 s freeze). A freeze
    inserted into a programme is a sudden collapse of activity; a quiet
    scene is quiet throughout.

    The frame difference is measured at full resolution because sensor
    grain is what keeps real static shots above the still threshold: 52
    real 4K camera clips never produced a candidate run, while a 480-pixel
    copy averaged the grain away and produced a false 2.2 s freeze.
    Runs mostly covered by black are dropped (EBU QC 0044B / Telestream
    VQ-105 "ignore black"): Black Frames reports those.
    """
    period = _frame_period(fps, times)
    context = max(1, int(round(FREEZE_CONTEXT_SECONDS / period)))
    holds: List[Interval] = []
    edges: List[Interval] = []
    black_holds = 0
    edge_dominant = False
    count = min(len(mafd), len(times))
    for first, last in _still_runs(mafd[:count]):
        frozen_from = first - 1
        span = _interval_of(times, frozen_from, last, period)
        if span[1] < min_duration - period * 0.5:
            continue
        run = list(mafd[first:last + 1])
        around = list(mafd[max(1, frozen_from - context):frozen_from]) + \
            list(mafd[last + 2:last + 2 + context])
        level = statistics.median(run)
        reference = statistics.median(around) if len(around) >= context // 4 + 1 else None
        if not (level <= FREEZE_IDENTICAL_MAFD
                or (reference is not None and level <= FREEZE_RELATIVE_DROP * reference)):
            continue
        if black and _overlap(span, black) >= 0.5 * span[1]:
            black_holds += 1
            continue
        touches_edge = frozen_from <= 1 or last >= count - 2
        if touches_edge and not (duration > 0 and span[1] >= 0.5 * duration):
            edges.append(span)
        else:
            holds.append(span)
            if touches_edge:
                edge_dominant = True
    return FreezeFindings(holds, edges, black_holds, edge_dominant)


def freeze_entry(findings: FreezeFindings, duration: float,
                 cfg: VideoCheckConfig) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Freeze Frames row."""
    edge_note = ""
    if findings.edge_holds:
        edge_note = (f" Static picture at the head/tail treated as slate or end card: "
                     f"{event_list(findings.edge_holds)}.")
    if not findings.holds:
        return "PASS", f"No freezes ≥{cfg.freeze_duration:g}s detected.{edge_note}", 100
    total = sum(d for _, d in findings.holds)
    detail = (f"{len(findings.holds)} frozen picture(s) ≥{cfg.freeze_duration:g}s, "
              f"total {total:.1f}s. At: {event_list(findings.holds)}.{edge_note}")
    if (duration > 0 and total > duration * 0.1 and total >= 4.0
            and not findings.edge_dominant):
        return "FAIL", detail, 90
    longest = max(d for _, d in findings.holds)
    # A 1-2 s hold may be an intentional title or graphic: below 70.
    return "WARN", detail, 75 if longest >= 2.0 else 60


# ---------------------------------------------------------------------------
# Signal levels (EBU R 103) and range plausibility
# ---------------------------------------------------------------------------

@dataclass
class LevelEvent:
    start: float
    duration: float
    peak: float                      # largest area fraction in the event


@dataclass
class LevelsFindings:
    luma: List[LevelEvent]
    gamut: List[LevelEvent]
    max_luma: float
    max_gamut: float
    range_hint: Optional[str] = None
    # Unsignalled matrix: the candidate matrices gamut was judged under.
    gamut_matrices: Tuple[str, ...] = ()


def _area_events(fractions: Sequence[float], times: Sequence[float],
                 period: float) -> List[LevelEvent]:
    events = []
    flags = [value > R103_AREA for value in fractions]
    for first, last in frame_runs(flags, R103_MIN_FRAMES):
        start, length = _interval_of(times, first, last, period)
        events.append(LevelEvent(start, length, max(fractions[first:last + 1])))
    return events


def _share(flags: Sequence[bool]) -> float:
    return sum(1 for f in flags if f) / len(flags) if flags else 0.0


def range_plausibility(series: VideoSeries, full_range: bool) -> Optional[str]:
    """A sentence when the picture's levels contradict its range tag, else None.

    Limited ("tv") range puts black at code 16 and white at 235; full ("pc")
    range at 0 and 255. Mis-tagging shows as crushed or washed-out pictures
    on every correctly behaving player.

    * Tagged limited: deep sub-black (<= code 10 after averaging) over more
      than 1 % of the picture in at least a quarter of the frames is not
      overshoot or noise - it is full-range black.
    * Tagged full: no frame ever uses codes below 12 or above 241, yet in at
      least a tenth of the frames over 1 % of the picture piles up exactly at
      the limited-range clip points (14-19 or 231-237). Low-contrast and log
      footage avoids the extremes too, but has no such pile-up.
    """
    frames = series.frames
    if frames < R103_MIN_FRAMES or not series.band1:
        return None
    if not full_range:
        deep = [value > R103_AREA for value in series.band1]
        if _share(deep) >= 0.25 and sum(deep) >= R103_MIN_FRAMES:
            return (f"black sits at or below code {RANGE_DEEP_BLACK_MAX} on >1% of the picture in "
                    f"{_share(deep) * 100:.0f}% of frames: the content looks full range (0-255) "
                    f"but is tagged limited (tv)")
        return None
    uses_extremes = max(series.luma or [0.0]) > RANGE_PC_EXTREMES_SHARE
    if uses_extremes:
        return None
    piled = [b1 > R103_AREA or b2 > R103_AREA for b1, b2 in zip(series.band1, series.band2)]
    if _share(piled) >= 0.10 and sum(piled) >= R103_MIN_FRAMES:
        return (f"no code below {RANGE_PC_FLOOR_MAX + 1} or above {RANGE_PC_CEIL_MIN - 1} is ever "
                f"used while {_share(piled) * 100:.0f}% of frames pile up at the limited-range "
                f"black/white clip points: the content looks limited range (16-235) but is "
                f"tagged full (pc)")
    return None


def assess_levels(series: VideoSeries, fps: float, full_range: bool,
                  gamut_matrices: Sequence[str] = ()) -> LevelsFindings:
    """EBU R 103 v3.0 luma and RGB-gamut tolerance on the averaged copy.

    A frame violates when more than 1 % of its picture lies outside the
    preferred range (luma 5-246 / 20-984; R, G and B -5 % .. 105 %); an event
    needs at least R103_MIN_FRAMES consecutive violating frames, so a single
    corrupt or transitional frame does not count. Full-range pictures cannot
    leave the luma range (0-255 is the nominal range), so only gamut applies.
    With more than one candidate matrix (unsignalled), ``series.gam`` only
    counts pixels outside R 103 under every candidate (see _gamut_combined).
    """
    period = _frame_period(fps, series.times)
    count = series.frames
    luma_fraction = (list(series.luma) if not full_range and series.luma else [0.0] * count)
    gamut_fraction = list(series.gam) if series.gam else [0.0] * count
    candidates = tuple(gamut_matrices) if len(gamut_matrices) > 1 else ()
    return LevelsFindings(
        luma=_area_events(luma_fraction, series.times, period),
        gamut=_area_events(gamut_fraction, series.times, period),
        max_luma=max(luma_fraction or [0.0]),
        max_gamut=max(gamut_fraction or [0.0]),
        range_hint=range_plausibility(series, full_range),
        gamut_matrices=candidates,
    )


def _level_event_text(kind: str, events: Sequence[LevelEvent]) -> str:
    shown = ", ".join(f"{timecode(e.start)} ({e.duration:.1f}s) up to {e.peak * 100:.1f}%"
                      for e in events[:4])
    more = f", +{len(events) - 4} more" if len(events) > 4 else ""
    return f"{kind} outside the R 103 range on >1% of the picture: {shown}{more}"


def _matrix_label(matrix: str) -> str:
    return matrix.upper().replace("BT", "BT.")


def _matrix_note(findings: LevelsFindings) -> str:
    """One sentence on an unsignalled matrix ('' when it is signalled)."""
    if not findings.gamut_matrices:
        return ""
    names = " and ".join(_matrix_label(m) for m in findings.gamut_matrices)
    if findings.gamut:
        return (f" The colour matrix is not signalled; the gamut excursion holds under "
                f"{names}.")
    return (f" The colour matrix is not signalled; gamut was judged under {names}, "
            f"counting only pixels outside R 103 under every one.")


def levels_entry(findings: LevelsFindings, mode: str, full_range: bool) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Signal Levels row."""
    luma_part = ("luma n/a (full range)" if full_range
                 else f"luma max {findings.max_luma * 100:.2f}%")
    measured = (f"Largest share outside EBU R 103 (limit 1% of the picture): {luma_part}, "
                f"RGB gamut max {findings.max_gamut * 100:.2f}%")
    note = _matrix_note(findings)
    problems = []
    if findings.luma:
        problems.append(_level_event_text("Luma", findings.luma))
    if findings.gamut:
        problems.append(_level_event_text("RGB gamut", findings.gamut))
    if findings.range_hint:
        problems.append("Range: " + findings.range_hint)
    if not problems:
        return "PASS", measured + "." + note, 100
    detail = ". ".join(problems) + f". ({measured}.)" + note
    if mode == "report":
        return "PASS", "Not enforced: " + detail, 100
    peak = max([e.peak for e in findings.luma + findings.gamut] or [0.0])
    # Near the 1 % limit the result depends on the averaging approximation
    # of the R 103 filter: keep confidence below 70 there.
    confident = peak >= 2 * R103_AREA or bool(findings.range_hint and peak >= R103_AREA)
    if mode == "essential":
        return "FAIL", detail, 85 if confident else 60
    return "WARN", detail, 80 if confident else 60


# ---------------------------------------------------------------------------
# Flash risk (ITU-R BT.1702-3)
# ---------------------------------------------------------------------------

@dataclass
class FlashEvent:
    start: float
    end: float
    area: float                      # peak share of the screen flashing
    kind: str                        # "general" or "red"


class _ZigZag:
    """Incremental hysteresis zig-zag: where a swing of ``delta`` completes.

    A transition is a move of at least ``delta`` away from the last turning
    point, timed at the frame where the swing first reaches ``delta``. The
    direction only reverses after a move of ``delta`` back from the extreme,
    so a slow fade counts once and frame-to-frame jitter never counts.
    ``dark_limit`` is BT.1702-3's "darker state below 160 cd/m2" condition;
    with ``red`` set, one end of the swing must be saturated red.

    Turning points are kept as (index, value, saturated) so the detector can
    run over a stream without holding the series; batch and streaming use
    this one implementation.
    """

    __slots__ = ("delta", "dark_limit", "red", "index", "low", "high", "pivot",
                 "extreme", "trend", "counted")

    def __init__(self, delta: float, dark_limit: Optional[float] = None, red: bool = False):
        self.delta, self.dark_limit, self.red = delta, dark_limit, red
        self.index = -1
        self.low = self.high = self.pivot = self.extreme = None
        self.trend = 0
        self.counted = False

    def _accept(self) -> bool:
        pivot, extreme = self.pivot, self.extreme
        if abs(extreme[1] - pivot[1]) < self.delta:
            return False
        if self.dark_limit is not None and min(extreme[1], pivot[1]) >= self.dark_limit:
            return False
        return not self.red or pivot[2] or extreme[2]

    def push(self, value: float, saturated: bool = False) -> int:
        """Feed the next value; +1 / -1 when a rising / falling swing completes, else 0."""
        self.index += 1
        point = (self.index, value, saturated)
        if self.index == 0:
            self.low = self.high = self.pivot = self.extreme = point
            return 0
        if self.trend == 0:
            if value > self.high[1]:
                self.high = point
            if value < self.low[1]:
                self.low = point
            if self.high[1] - self.low[1] < self.delta:
                return 0
            self.trend = 1 if self.high[0] > self.low[0] else -1
            self.pivot, self.extreme = ((self.low, self.high) if self.trend == 1
                                        else (self.high, self.low))
            self.counted = False
        elif (value - self.extreme[1]) * self.trend >= 0:
            self.extreme = point
        elif abs(self.extreme[1] - value) >= self.delta:
            self.pivot, self.extreme = self.extreme, point
            self.trend, self.counted = -self.trend, False
        if not self.counted and self._accept():
            self.counted = True
            return 1 if self.extreme[1] > self.pivot[1] else -1
        return 0


_FLASH_KINDS = ("general", "red")
# Per-byte terms of a block's rule quantity, computed once: luminance in
# cd/m2 (Y = 0.2126 R + 0.7152 G + 0.0722 B of linear light, scaled to the
# 200 cd/m2 SDR peak) and the WCAG saturated-red measure (R - G - B) x 320.
_LUM_R = [0.2126 * v for v in range(256)]
_LUM_G = [0.7152 * v for v in range(256)]
_LUM_B = [0.0722 * v for v in range(256)]
_LUM_SCALE = FLASH_PEAK_CD / 255.0
_RED = [max(0.0, v / 255.0) * 320.0 for v in range(-510, 256)]


def _rule_value(kind: str, r: int, g: int, b: int) -> float:
    """The quantity ``kind``'s rule swings on, for one block's linear bytes."""
    if kind == "general":
        return _LUM_SCALE * (_LUM_R[r] + _LUM_G[g] + _LUM_B[b])
    return _RED[r - g - b + 510]


class FlashAccumulator:
    """BT.1702-3 flash counting over a stream of linear-light grids.

    The grid (FLASH_GRID blocks, one gbrp frame per video frame) arrives in
    chunks while FFmpeg decodes. Every block's swings are detected on the
    fly; the raw grid is kept only where the final decision can need it, so
    memory and disk stay small for any length of programme.

    What is kept, and why it is enough for an exact result (identical to
    judging the whole series at once):

    * A block flashes hazardously over 7 consecutive swings spanning less
      than one second. In frames that is at most ``span_frames`` (two
      seconds' worth at the stream rate, leaving room for timing jitter),
      so swings are committed only when seven of them fall within
      ``span_frames`` - a superset of every hazardous run.
    * The area decision (events) needs each flashing block's swing size to
      weight partly covered blocks, i.e. the raw grid on frames where the
      flashing blocks could cover more than 25 % of the screen. Weights never
      exceed 1, so those are frames where more than 25 % of the blocks have
      a committed run over them; only they are retained.
    """

    PLANE = FLASH_GRID[0] * FLASH_GRID[1]
    FRAME_BYTES = 3 * PLANE

    def __init__(self, fps: float):
        rate = fps if 0 < fps < 1000 else 0.0
        self.span_frames = int(math.ceil(2.0 * rate)) + 2 if rate else 482
        blocks = self.PLANE
        self.detectors = {
            "general": [_ZigZag(FLASH_DELTA_CD, FLASH_DARK_CD) for _ in range(blocks)],
            "red": [_ZigZag(FLASH_RED_DELTA, None, red=True) for _ in range(blocks)]}
        self.recent = {kind: [deque(maxlen=FLASH_MAX_TRANSITIONS) for _ in range(blocks)]
                       for kind in _FLASH_KINDS}
        self.swing_count = {kind: [0] * blocks for kind in _FLASH_KINDS}
        self.committed: Dict[str, List[Dict[int, int]]] = {
            kind: [{} for _ in range(blocks)] for kind in _FLASH_KINDS}
        self.covered_until = {kind: [-1] * blocks for kind in _FLASH_KINDS}
        self.coverage_delta: Dict[str, Dict[int, int]] = {kind: {} for kind in _FLASH_KINDS}
        self.coverage_running = {kind: 0 for kind in _FLASH_KINDS}
        self.final_through = -1          # coverage is final for frames <= this
        self.frames = 0
        self.pending = b""
        self.recent_rows: deque = deque()   # (first frame, bytes) of unfinalised chunks
        self.retained: Dict[int, bytes] = {}

    # -- streaming -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        """Add raw gbrp grid bytes (any length; partial frames are carried)."""
        data = self.pending + data
        whole = len(data) - len(data) % self.FRAME_BYTES
        self.pending = data[whole:]
        if whole:
            self._feed_frames(data[:whole])

    def _feed_frames(self, data: bytes) -> None:
        start, count, stride, plane = self.frames, len(data) // self.FRAME_BYTES, self.FRAME_BYTES, self.PLANE
        for b in range(plane):
            g_series, b_series, r_series = data[b::stride], data[plane + b::stride], data[2 * plane + b::stride]
            general = self.detectors["general"][b].push
            for k, (r, g, x) in enumerate(zip(r_series, g_series, b_series)):
                if general(_LUM_SCALE * (_LUM_R[r] + _LUM_G[g] + _LUM_B[x])):
                    self._swing("general", b, start + k)
            red = self.detectors["red"][b].push
            for k, (r, g, x) in enumerate(zip(r_series, g_series, b_series)):
                if red(_RED[r - g - x + 510], r > 0 and r >= FLASH_RED_RATIO * (r + g + x)):
                    self._swing("red", b, start + k)
        self.frames += count
        self.recent_rows.append((start, data))
        self._finalise(self.frames - 1 - self.span_frames)

    def _swing(self, kind: str, block: int, frame: int) -> None:
        """Record a completed swing; commit runs that may form a hazard."""
        seq = self.swing_count[kind][block]
        self.swing_count[kind][block] = seq + 1
        recent = self.recent[kind][block]
        if len(recent) == recent.maxlen and frame - recent[0][1] <= self.span_frames:
            committed = self.committed[kind][block]
            for mark_seq, mark_frame in recent:
                committed[mark_seq] = mark_frame
            committed[seq] = frame
            first = max(recent[0][1], self.covered_until[kind][block] + 1)
            if first <= frame:
                delta = self.coverage_delta[kind]
                delta[first] = delta.get(first, 0) + 1
                delta[frame + 1] = delta.get(frame + 1, 0) - 1
                self.covered_until[kind][block] = frame
        recent.append((seq, frame))

    def _finalise(self, through: int) -> None:
        """Decide retention for frames whose coverage can no longer grow."""
        threshold = FLASH_AREA * self.PLANE
        for frame in range(self.final_through + 1, through + 1):
            flagged = False
            for kind in _FLASH_KINDS:
                running = self.coverage_running[kind] + self.coverage_delta[kind].pop(frame, 0)
                self.coverage_running[kind] = running
                flagged = flagged or running > threshold
            if flagged and frame not in self.retained:
                self.retained[frame] = self._row(frame)
        self.final_through = max(self.final_through, through)
        while self.recent_rows and (self.recent_rows[0][0]
                                    + len(self.recent_rows[0][1]) // self.FRAME_BYTES) <= self.final_through + 1:
            self.recent_rows.popleft()

    def _row(self, frame: int) -> bytes:
        for start, data in self.recent_rows:
            offset = (frame - start) * self.FRAME_BYTES
            if 0 <= offset < len(data):
                return data[offset:offset + self.FRAME_BYTES]
        raise LookupError(f"flash grid frame {frame} is no longer buffered")

    def finish(self) -> None:
        """All frames fed: finalise the tail."""
        self._finalise(self.frames - 1)
        self.recent_rows.clear()

    # -- decision --------------------------------------------------------------
    def _amplitudes(self, kind: str, frames: range, blocks: Sequence[int]) -> Dict[int, float]:
        """Swing size (max - min of the rule quantity) of each block over ``frames``."""
        plane = self.PLANE
        low = {b: math.inf for b in blocks}
        high = {b: -math.inf for b in blocks}
        for f in frames:
            row = self.retained[f]
            for b in blocks:
                value = _rule_value(kind, row[2 * plane + b], row[b], row[plane + b])
                if value < low[b]:
                    low[b] = value
                if value > high[b]:
                    high[b] = value
        return {b: high[b] - low[b] for b in blocks}

    def _coverage_weights(self, amplitude: Dict[int, float]) -> Dict[int, float]:
        """Share of each flashing block covered by the flash (area-preserving).

        A block partly covered by a flashing area of uniform swing S swings
        by (covered share) x S, because the grid holds mean linear light. Its
        coverage is therefore its own swing over the swing of its fully
        covered neighbours - taken as the largest swing among itself and its
        eight neighbours that also flash. Inside a flashing area that is 1;
        along its edge it is the covered share. A block with no larger
        neighbour counts whole (a flash smaller than a block is overstated,
        never missed).
        """
        cols, rows = FLASH_GRID
        weights = {}
        for b, amp in amplitude.items():
            x, y = b % cols, b // cols
            reference = amp
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < cols and 0 <= ny < rows:
                        reference = max(reference, amplitude.get(ny * cols + nx, 0.0))
            weights[b] = min(1.0, amp / reference) if reference > 0 else 1.0
        return weights

    def events(self, times: Sequence[float], fps: float,
               cancel: Optional["threading.Event"] = None) -> List[FlashEvent]:
        """Flash events with the real frame times.

        Per rule (general, red): a block flashes at frame f while f lies
        within a run of seven of its swings spanning less than one second
        (more than three flashes). "Flashes occurring concurrently" are the
        blocks flashing at the same frame, whatever their phase - antiphase
        halves, an inverting checkerboard or a turning windmill flash
        concurrently over the whole screen although the screen's mean light
        never changes. A frame is hazardous when the coverage-weighted area
        of its flashing blocks exceeds 25 % of the screen.
        """
        frames = min(self.frames, len(times))
        if frames < FLASH_MAX_TRANSITIONS + 1:
            return []
        times = times[:frames]
        period = _frame_period(fps, times)
        window = 1.0 - 0.5 * period
        blocks = self.PLANE
        events: List[FlashEvent] = []
        for kind in _FLASH_KINDS:
            hazard: Dict[int, List[int]] = {}
            for b in range(blocks):
                check_cancel(cancel)
                for first, last in self._exact_spans(kind, b, times, frames, window):
                    for f in range(first, last + 1):
                        members = hazard.setdefault(f, [])
                        if not members or members[-1] != b:
                            members.append(b)
            candidates = sorted(f for f, members in hazard.items()
                                if len(members) > FLASH_AREA * blocks)
            for first, last in _consecutive_runs(candidates):
                check_cancel(cancel)
                members = sorted({b for f in range(first, last + 1) for b in hazard[f]})
                weights = self._coverage_weights(
                    self._amplitudes(kind, range(first, last + 1), members))
                areas = [sum(weights[b] for b in hazard[f]) / blocks
                         for f in range(first, last + 1)]
                flagged = [first + k for k, area in enumerate(areas) if area > FLASH_AREA]
                for run_first, run_last in _consecutive_runs(flagged):
                    peak = max(areas[run_first - first:run_last - first + 1])
                    events.append(FlashEvent(times[run_first], times[run_last] + period, peak, kind))
        return sorted(events, key=lambda e: e.start)

    def _exact_spans(self, kind: str, block: int, times: Sequence[float], frames: int,
                     window: float) -> List[Tuple[int, int]]:
        """(first, last) frames of each run of seven consecutive swings < 1 s.

        More than three flashes is more than six transitions: seven
        transitions spanning less than a second. At exactly 3 Hz seven
        transitions span exactly one second, so the BT.1702 limit itself is
        not flagged. Every such run was committed whole, so only runs whose
        sequence numbers are consecutive are judged.
        """
        committed = self.committed[kind][block]
        if len(committed) < FLASH_MAX_TRANSITIONS + 1:
            return []
        seqs = sorted(committed)
        need = FLASH_MAX_TRANSITIONS + 1
        spans = []
        for k in range(len(seqs) - need + 1):
            if seqs[k + need - 1] - seqs[k] != need - 1:
                continue
            first, last = committed[seqs[k]], committed[seqs[k + need - 1]]
            if last < frames and times[last] - times[first] < window:
                spans.append((first, last))
        return spans


def _consecutive_runs(frames: Sequence[int]) -> List[Tuple[int, int]]:
    """(first, last) runs of consecutive integers in a sorted list."""
    runs: List[Tuple[int, int]] = []
    for f in frames:
        if runs and f == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], f)
        else:
            runs.append((f, f))
    return runs


def assess_flash(series: VideoSeries, fps: float,
                 cancel: Optional["threading.Event"] = None) -> List[FlashEvent]:
    """ITU-R BT.1702-3 general and red flash screening, per screen region.

    Each block of the linear-light grid is a region. Its rule quantity
    (luminance of BT.1886 display light at a 200 cd/m2 peak, or the WCAG
    saturated-red measure) is turned into transitions (see _ZigZag), and a
    region flashes hazardously while seven transitions (more than three
    flashes) fall within one second. The flashing regions' coverage-weighted
    area must exceed 25 % of the screen at the same moment (see
    FlashAccumulator.events). Phase is deliberately not considered: the
    Recommendation's area rule is about flashes occurring concurrently, and
    regions flashing in antiphase do.

    The scan streams the grid into ``series.flash_stream`` while FFmpeg
    runs; a series built in memory (``series.flash``) is fed here.
    Screening only: BT.1702 compliance is certified with a Harding FPA class
    analyser.
    """
    accumulator = series.flash_stream
    if accumulator is None:
        accumulator = FlashAccumulator(fps)
        accumulator.feed(b"".join(g + b + r for g, b, r in series.flash))
        accumulator.finish()
    return accumulator.events(series.times, fps, cancel)


def flash_entry(events: Sequence[FlashEvent]) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Flash Risk row (never FAIL)."""
    caveat = ("ITU-R BT.1702-3 screening only; PSE certification requires a "
              "Harding FPA class analyser")
    if not events:
        return "PASS", f"No flashing above BT.1702-3 limits detected ({caveat}).", 100
    shown = ", ".join(f"{timecode(e.start)}-{timecode(e.end)} ({e.kind}, up to "
                      f"{e.area * 100:.0f}% of the screen)" for e in events[:4])
    more = f", +{len(events) - 4} more" if len(events) > 4 else ""
    return ("WARN", f"Possible photosensitive-seizure risk: more than 3 flashes per second "
            f"over more than 25% of the screen at {shown}{more} ({caveat}).", 65)


# ---------------------------------------------------------------------------
# Interlace detection
# ---------------------------------------------------------------------------

_FIELD_ORDER_NAMES = {"tt": "tff", "tb": "tff", "bb": "bff", "bt": "bff"}


@dataclass
class InterlaceFindings:
    combed: List[Interval]           # sections idet calls interlaced
    content_order: Optional[str]     # "tff"/"bff" when idet has a majority
    flagged_order: Optional[str]     # from the stream's field_order
    interlaced_share: float
    scrolls: List[Interval] = field(default_factory=list)  # idet hits that are vertical scrolls


def _vertical_scroll(first: int, last: int, interlaced: Sequence[bool],
                     col_motion: Sequence[Optional[float]],
                     row_motion: Sequence[Optional[float]]) -> bool:
    """True when the motion in frames first..last is a vertical translation.

    Judged on the frames idet calls interlaced (the only ones whose profile
    change is recorded): measurable motion on at least half of them, with
    the column-profile change under INTERLACE_VERTICAL_SHARE of the total.
    """
    shares = []
    frames = 0
    for k in range(first, min(last + 1, len(col_motion), len(row_motion), len(interlaced))):
        if not interlaced[k]:
            continue
        frames += 1
        col, row = col_motion[k], row_motion[k]
        if col is None or row is None or col + row <= INTERLACE_MOTION_FLOOR:
            continue
        shares.append(col / (col + row))
    if len(shares) < max(1, frames // 2):
        return False
    return statistics.median(shares) < INTERLACE_VERTICAL_SHARE


def assess_interlace(labels: Sequence[str], times: Sequence[float], fps: float,
                     field_order: str, col_motion: Sequence[Optional[float]] = (),
                     row_motion: Sequence[Optional[float]] = ()) -> InterlaceFindings:
    """Combing in progressive-flagged video; field order vs metadata.

    Uses idet's multiple-frame classification (each frame judged with its
    neighbours, steadier than single-frame). A section is combed when more
    than half the frames in a one-second window are called TFF or BFF; at
    least a second of such windows is required. A section whose motion is a
    vertical translation (see INTERLACE_VERTICAL_SHARE) is set aside as a
    scroll: whichever way its fields are paired, an integer-line vertical
    move shows no feathering. Field order compares idet's TFF/BFF majority
    with the declared order when the stream is interlaced.
    """
    count = min(len(labels), len(times))
    labels = [str(v).lower() for v in labels[:count]]
    period = _frame_period(fps, times)
    window = max(1, int(round(1.0 / period)))
    interlaced = [v in ("tff", "bff") for v in labels]
    share = _share(interlaced)
    flagged = _FIELD_ORDER_NAMES.get(field_order)
    combed: List[Interval] = []
    if flagged is None and count >= window:
        # An interlaced frame is "dense" when some one-second window holding
        # it is more than half interlaced; windows are unioned with a
        # difference array (linear in the frame count).
        cover = [0] * (count + 1)
        running = sum(interlaced[:window])
        for k in range(count - window + 1):
            if k:
                running += interlaced[k + window - 1] - interlaced[k - 1]
            if running > INTERLACE_WINDOW_SHARE * window:
                cover[k] += 1
                cover[k + window] -= 1
        dense, depth = [False] * count, 0
        for j in range(count):
            depth += cover[j]
            dense[j] = depth > 0 and interlaced[j]
        min_frames = max(1, int(round(INTERLACE_MIN_SECONDS / period)))
        for first, last in frame_runs(dense, 1):
            span = _interval_of(times, first, last, period)
            combed.append(span)
        merged: List[Interval] = []
        for start, length in combed:
            if merged and start - sum(merged[-1]) <= 0.5:
                merged[-1] = (merged[-1][0], start + length - merged[-1][0])
            else:
                merged.append((start, length))
        combed = [m for m in merged if m[1] >= min_frames * period - 1e-6]
    scrolls: List[Interval] = []
    if combed and col_motion and row_motion:
        kept = []
        frame_times = list(times[:count])
        for start, length in combed:
            first = bisect.bisect_left(frame_times, start - 1e-6)
            last = max(first, bisect.bisect_left(frame_times, start + length - 1e-6) - 1)
            vertical = _vertical_scroll(first, last, interlaced, col_motion, row_motion)
            (scrolls if vertical else kept).append((start, length))
        combed = kept
    tff, bff = labels.count("tff"), labels.count("bff")
    content = None
    if tff + bff >= FIELD_ORDER_MIN_FRAMES:
        if tff >= FIELD_ORDER_MAJORITY * (tff + bff):
            content = "tff"
        elif bff >= FIELD_ORDER_MAJORITY * (tff + bff):
            content = "bff"
    return InterlaceFindings(combed, content, flagged, share, scrolls)


def interlace_entry(findings: InterlaceFindings) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Interlace Detection row.

    The metadata Scan Type check already judges a stream declared
    interlaced, so this row never repeats that verdict: on interlaced
    streams it only checks the field order; on progressive ones it reports
    combing the metadata does not admit to.
    """
    names = {"tff": "top field first", "bff": "bottom field first"}
    share = f"{findings.interlaced_share * 100:.0f}% of frames look interlaced"
    if findings.flagged_order:
        if findings.content_order and findings.content_order != findings.flagged_order:
            return ("WARN", f"Field order mismatch: the stream declares "
                    f"{names[findings.flagged_order]} but the picture moves "
                    f"{names[findings.content_order]} ({share}); playback will judder.", 75)
        seen = (f"; content agrees ({names[findings.content_order]})"
                if findings.content_order else "")
        return "PASS", f"Declared {names[findings.flagged_order]}{seen} ({share}).", 100
    if not findings.combed:
        scroll = ""
        if findings.scrolls:
            scroll = (f"; frames that read as interlaced at {event_list(findings.scrolls)} are a "
                      f"vertical scroll (e.g. rolling credits) with no combing")
        return "PASS", f"No interlace combing found ({share}{scroll}).", 100
    total = sum(d for _, d in findings.combed)
    return ("WARN", f"Interlace combing in video declared progressive: {total:.1f}s at "
            f"{event_list(findings.combed)} ({share}). Fine horizontal detail in "
            f"graphics can mimic combing; check these sections.", 60)


# ---------------------------------------------------------------------------
# Verdict rows
# ---------------------------------------------------------------------------

_NO_MEASUREMENTS = "FFmpeg produced no per-frame measurements."


def _add(result, name: str, status: str, detail: str, confidence: int = 100) -> None:
    {"PASS": result.add_pass, "WARN": result.add_warn, "FAIL": result.add_fail,
     "SKIP": result.add_skip}[status](name, detail, confidence=confidence)


def report_video(result, config: VideoCheckConfig, video: dict, fps: float, *,
                 decoded_frames: int, video_end: float,
                 black_events: Optional[Sequence[Tuple[float, float]]],
                 analysis: Optional[VideoAnalysis],
                 analysis_error: Optional[str] = None) -> None:
    """Add every picture-check row to ``result`` (a core.QAResult).

    A check switched off in the settings is SKIP; one whose per-frame
    measurements are missing although frames were decoded is "Not
    verified" - never a silent PASS. Evaluation looks at the scan's cancel
    flag throughout; when it is set, every row not yet added is reported
    not verified and the work stops.
    """
    try:
        series = analysis.collect() if analysis is not None else VideoSeries()
        reason = analysis_error or _NO_MEASUREMENTS
        if series.frames:
            result.set_metadata("video_frames_analysed", series.frames)
        cancel = analysis.cancel if analysis is not None else None
        black = _report_black(result, config, black_events, video_end, fps, series)
        check_cancel(cancel)
        _report_freeze(result, config, series, video_end, fps, black, decoded_frames, reason)
        check_cancel(cancel)
        _report_levels(result, config, series, analysis, fps, decoded_frames, reason)
        _report_flash(result, config, series, analysis, fps, decoded_frames, reason, cancel)
        check_cancel(cancel)
        _report_interlace(result, config, series, analysis, video, fps, decoded_frames, reason)
    except ScanCancelled:
        reported = {check[0] for check in result.checks}
        for name in config.enabled_names():
            if name not in reported:
                result.add_not_verified(name, "the scan was cancelled during the picture analysis.")


def _report_black(result, config, events, duration, fps, series) -> List[Interval]:
    """Black Frames row; returns the raw black runs for the freeze check."""
    if not config.black:
        result.add_skip(BLACK, "Switched off in the settings.")
        return []
    if events is None:
        # blackdetect reports through FFmpeg's log; when the bounded capture
        # dropped evidence, a partial list could invent or hide black runs.
        result.add_not_verified(BLACK, "FFmpeg's log overflowed, so the black-frame events are incomplete.")
        return []
    findings = classify_black(events, duration, fps, config, series.times, series.yavg)
    if findings.segments:
        result.set_metadata("black_frame_sequences", len(findings.segments))
        result.set_metadata("black_frame_total_seconds",
                            round(sum(d for _, d in findings.segments), 2))
        result.set_metadata("black_frame_timestamps", event_list(findings.segments))
    if findings.bursts:
        result.set_metadata("black_short_bursts", len(findings.bursts))
    _add(result, BLACK, *black_entry(findings, duration, config))
    return [(start, end - start) for start, end in events]


def _report_freeze(result, config, series, duration, fps, black, decoded, reason) -> None:
    if not config.freeze:
        result.add_skip(FREEZE, "Switched off in the settings.")
        return
    if decoded and not series.mafd:
        result.add_not_verified(FREEZE, reason)
        return
    findings = detect_freezes(series.mafd, series.times, fps, config.freeze_duration,
                              duration, black)
    if findings.holds:
        result.set_metadata("freeze_frame_count", len(findings.holds))
        result.set_metadata("freeze_frame_total_seconds",
                            round(sum(d for _, d in findings.holds), 2))
        result.set_metadata("freeze_frame_timestamps", event_list(findings.holds))
    _add(result, FREEZE, *freeze_entry(findings, duration, config))


def _report_levels(result, config, series, analysis, fps, decoded, reason) -> None:
    if not config.levels:
        result.add_skip(LEVELS, "Not measured (switched off in the settings).")
        return
    if analysis is not None and not analysis.info.analysable:
        result.add_skip(LEVELS, "Picture too small to analyse.")
        return
    if decoded and not series.gam:
        result.add_not_verified(LEVELS, reason)
        return
    full = analysis.info.full_range if analysis is not None else False
    matrices = analysis.info.gamut_matrices if analysis is not None else ()
    findings = assess_levels(series, fps, full, matrices)
    result.set_metadata("levels_max_luma_pct", round(findings.max_luma * 100, 3))
    result.set_metadata("levels_max_gamut_pct", round(findings.max_gamut * 100, 3))
    if findings.range_hint:
        result.set_metadata("range_mismatch_suspected", True)
    _add(result, LEVELS, *levels_entry(findings, config.levels_mode, full))


def _report_flash(result, config, series, analysis, fps, decoded, reason,
                  cancel: Optional[threading.Event] = None) -> None:
    if not config.flash:
        result.add_skip(FLASH, "Switched off in the settings.")
        return
    if analysis is not None and analysis.info.hdr:
        result.add_skip(FLASH, "Not assessed for PQ/HLG video: the BT.1702-3 thresholds "
                               "used here assume an SDR display.")
        return
    if analysis is not None and not analysis.info.analysable:
        result.add_skip(FLASH, "Picture too small to analyse.")
        return
    if series.flash_error:
        result.add_not_verified(FLASH, f"the flash measurements could not be read ({series.flash_error}).")
        return
    if decoded and not series.flash_frames:
        result.add_not_verified(FLASH, reason)
        return
    try:
        events = assess_flash(series, fps, cancel)
    except LookupError as exc:
        result.add_not_verified(FLASH, f"the flash measurements are incomplete ({exc}).")
        return
    if events:
        result.set_metadata("flash_risk_events", len(events))
    _add(result, FLASH, *flash_entry(events))


def _report_interlace(result, config, series, analysis, video, fps, decoded, reason) -> None:
    if not config.interlace:
        result.add_skip(INTERLACE, "Switched off in the settings.")
        return
    if analysis is not None and not analysis.needs_interlace_branch:
        result.add_skip(INTERLACE, "Picture too small to analyse.")
        return
    if decoded and not series.idet:
        result.add_not_verified(INTERLACE, reason)
        return
    field_order = str((video or {}).get("field_order") or "unknown").lower()
    findings = assess_interlace(series.idet, series.times, fps, field_order,
                                series.col_motion, series.row_motion)
    result.set_metadata("interlaced_frame_share", round(findings.interlaced_share, 3))
    _add(result, INTERLACE, *interlace_entry(findings))
