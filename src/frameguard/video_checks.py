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
``file=``) and one raw 16x9 grid stream, written to a private temporary
directory. Keeping them out of stderr means they never compete with decoder
errors for the bounded log capture, and threaded filters cannot interleave
their lines mid-record.
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import tempfile
import weakref
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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

# BT.1702-3 is applied on a 16x9 grid: 144 blocks, so "more than 25 % of the
# screen" is more than 36 blocks.
FLASH_GRID = (16, 9)

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

# BT.1702-3 Annex 1: a flash is a pair of opposing changes of at least
# 20 cd/m2 where the darker state is below 160 cd/m2, on an SDR display
# assumed to peak at 200 cd/m2; hazardous when more than three flashes fall
# within any one second over more than 25 % of the screen. BT.1702 gives no
# numeric red definition; the WCAG 2.2 one (R/(R+G+B) >= 0.8 and a change of
# (R-G-B) x 320 of at least 20, linear RGB) is used, as the reference
# implementation in the benchmark did.
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


def _print(key: str, path: str) -> str:
    return f"metadata=mode=print:key={key}:file={filter_path(path)}"


def _rgb_mask_expressions(info: PictureInfo) -> Tuple[str, str, str, str]:
    """lut2 expressions for the two packed gamut stages.

    Stage 1 pairs planes (Y, Y, U) with (V, U, V): R depends on (Y, Cr) and
    B on (Y, Cb), so one three-plane lut2 yields the R mask, the B mask and a
    helper plane W = 128 + (b*Cb + c*Cr) in luma code units (quantisation
    error <= 0.23 % of nominal, far inside the 5 % tolerance). Stage 2 pairs
    W with Y for the G mask, G = Y - (W - 128). Masks are 255 where the
    component lies outside -5 % .. 105 %. Returns (red, blue, helper, green);
    ``green`` takes x = W, y = Y.
    """
    a, b, c, d = MATRICES[info.matrix]
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


class VideoAnalysis:
    """The per-scan picture analysis: graph fragment, outputs, parsing.

    Use as a context manager so the temporary measurement files are always
    removed, whatever happens to the scan.
    """

    def __init__(self, video: dict, cfg: dict):
        self.info = PictureInfo.from_stream(video)
        self.config = VideoCheckConfig.from_cfg(cfg)
        self.index = int(video["index"])
        self.directory: Optional[str] = None
        self.paths: Dict[str, str] = {}

    # -- lifetime -----------------------------------------------------------
    def __enter__(self) -> "VideoAnalysis":
        return self.open()

    def open(self) -> "VideoAnalysis":
        """Create the private directory FFmpeg writes the measurements to.

        A finalizer removes it even when the scan is cancelled or raises
        before close() is reached.
        """
        self.directory = tempfile.mkdtemp(prefix="frameguard-video-")
        self._cleanup = weakref.finalize(self, shutil.rmtree, self.directory, True)
        names = ("mafd", "idet", "luma", "band1", "band2", "gam_r", "gam_g", "gam_b", "yavg")
        self.paths = {name: os.path.join(self.directory, f"{name}.txt") for name in names}
        self.paths["flash"] = os.path.join(self.directory, "flash.rgb")
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
            graphs.append((
                f"[0:{self.index}]scale=w='min({INTERLACE_MAX_WIDTH},iw)':h=ih:flags=neighbor,"
                f"idet@il={IDET_OPTIONS},"
                f"{_print('lavfi.idet.multiple.current_frame', self.paths['idet'])}[vs_il]",
                "[vs_il]"))
        if self.needs_area_branch:
            label = None if self.flash_enabled else "[vs_area]"
            graphs.append((";".join(self._area_chains()), label))
        return graphs

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
        if c.levels:
            uses += ["luma", "gamut1"]
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
            red, blue, helper, green = _rgb_mask_expressions(info)
            chains += [
                f"[ar_luma]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=0:map2s=0:map2p=0:format=yuv444p,lutyuv=y='{m1}':u='{m2}':v='{m3}',"
                f"{reduce4},signalstats,{_print('lavfi.signalstats.YAVG', self.paths['luma'])},"
                f"{_print('lavfi.signalstats.UAVG', self.paths['band1'])},"
                f"{_print('lavfi.signalstats.VAVG', self.paths['band2'])},nullsink",
                "[ar_gamut1]split=3[g_src][g_alt][g_luma]",
                "[g_src]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=0:map2s=0:map2p=1:format=yuv444p[g_a]",
                "[g_alt]mergeplanes=map0s=0:map0p=2:map1s=0:map1p=1:map2s=0:map2p=2:format=yuv444p[g_b]",
                f"[g_a][g_b]lut2=c0='{red}':c1='{blue}':c2='{helper}',"
                "mergeplanes=map0s=0:map0p=0:map1s=0:map1p=2:map2s=0:map2p=1:format=yuv444p[g_rwb]",
                "[g_luma]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=0:map2s=0:map2p=0:format=yuv444p[g_yyy]",
                f"[g_rwb][g_yyy]lut2=c0='x':c1='{green}':c2='x',"
                f"{reduce4},signalstats,{_print('lavfi.signalstats.YAVG', self.paths['gam_r'])},"
                f"{_print('lavfi.signalstats.UAVG', self.paths['gam_g'])},"
                f"{_print('lavfi.signalstats.VAVG', self.paths['gam_b'])},nullsink",
            ]
        if c.black or c.levels:
            # Without the flash output this branch is the graph's output.
            tail = "nullsink" if self.flash_enabled else "null[vs_area]"
            chains.append(
                f"[ar_mean]scale=w='max(1,trunc(iw/8))':h='max(1,trunc(ih/8))':flags=area,"
                f"signalstats,{_print('lavfi.signalstats.YAVG', self.paths['yavg'])},{tail}")
        if self.flash_enabled:
            linear = _flash_linear_expression(info)
            cols, rows = FLASH_GRID
            # Linear light is block-averaged on a 64x36 copy: the colour
            # conversion then costs nothing, and a block of the 16x9 grid
            # still averages 16 linear samples.
            chains.append(
                f"[ar_flash]scale=w={cols * 4}:h={rows * 4}:flags=area,"
                f"scale=in_color_matrix={info.matrix}:in_range={rng}:out_range=pc,"
                f"format=gbrp,lutrgb=r='{linear}':g='{linear}':b='{linear}',"
                f"scale={cols}:{rows}:flags=area[vs_flash]")
        return chains

    def extra_outputs(self) -> List[str]:
        """Arguments for the raw flash-grid output (after the main output)."""
        if not self.flash_enabled:
            return []
        # -y: the file lives in this scan's private directory; never prompt.
        return ["-y", "-map", "[vs_flash]", "-f", "rawvideo", "-pix_fmt", "gbrp",
                self.paths["flash"]]

    # -- results --------------------------------------------------------------
    def collect(self) -> "VideoSeries":
        """Read every per-frame series FFmpeg wrote (missing files -> empty)."""
        times, mafd = read_metadata_series(self.paths.get("mafd", ""))
        series = VideoSeries(times=times, mafd=[_to_float(v) for v in mafd])
        if self.needs_interlace_branch:
            series.idet = read_metadata_series(self.paths["idet"])[1]
        if self.needs_area_branch and self.config.levels:
            fraction = {name: [_to_float(v) / 255.0 for v in read_metadata_series(self.paths[name])[1]]
                        for name in ("luma", "band1", "band2", "gam_r", "gam_g", "gam_b")}
            series.luma, series.band1, series.band2 = fraction["luma"], fraction["band1"], fraction["band2"]
            # The largest single-component share: never above the union of
            # R, G and B violations, so it can only under-report.
            series.gam = [max(r, g, b) for r, g, b in
                          zip(fraction["gam_r"], fraction["gam_g"], fraction["gam_b"])]
        if self.needs_area_branch and (self.config.black or self.config.levels):
            series.yavg = [_to_float(v) for v in read_metadata_series(self.paths["yavg"])[1]]
        if self.flash_enabled:
            series.flash = read_flash_grid(self.paths["flash"])
        series.align()
        return series


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_FRAME_LINE = re.compile(r"^frame:\s*(\d+)\s+pts:\s*\S+\s+pts_time:\s*(\S+)")


def read_metadata_series(path: str) -> Tuple[List[float], List[str]]:
    """(pts_time per frame, value per frame) from a ``metadata=print`` file.

    A frame without the key (possible on the first frame of scdet) gets the
    value "" so positions stay aligned with the frame order.
    """
    times: List[float] = []
    values: List[str] = []
    if not path or not os.path.exists(path):
        return times, values
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _FRAME_LINE.match(line)
            if match:
                try:
                    times.append(float(match.group(2)))
                except ValueError:
                    times.append(times[-1] if times else 0.0)
                values.append("")
            elif "=" in line and values:
                values[-1] = line.split("=", 1)[1].strip()
    return times, values


def read_flash_grid(path: str) -> List[Tuple[bytes, bytes, bytes]]:
    """Per-frame (G, B, R) planes of the 16x9 linear-light grid (gbrp)."""
    cols, rows = FLASH_GRID
    plane = cols * rows
    frames = []
    if not os.path.exists(path):
        return frames
    with open(path, "rb") as handle:
        data = handle.read()
    for offset in range(0, len(data) - 3 * plane + 1, 3 * plane):
        chunk = data[offset:offset + 3 * plane]
        frames.append((chunk[:plane], chunk[plane:2 * plane], chunk[2 * plane:]))
    return frames


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
    luma: List[float] = field(default_factory=list)   # tv: outside R 103; pc: extremes used
    gam: List[float] = field(default_factory=list)
    band1: List[float] = field(default_factory=list)
    band2: List[float] = field(default_factory=list)
    yavg: List[float] = field(default_factory=list)
    flash: List[tuple] = field(default_factory=list)

    def align(self) -> None:
        """Truncate every non-empty series to the shortest one.

        All branches see the same decoded frames in the same order, so a
        length difference only arises when FFmpeg stopped mid-flush; the
        common prefix is the part every branch measured.
        """
        names = ("times", "mafd", "idet", "luma", "gam", "band1", "band2", "yavg", "flash")
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
    uses_extremes = max(series.luma or [0.0]) > 0.0001
    if uses_extremes:
        return None
    piled = [b1 > R103_AREA or b2 > R103_AREA for b1, b2 in zip(series.band1, series.band2)]
    if _share(piled) >= 0.10 and sum(piled) >= R103_MIN_FRAMES:
        return (f"no code below {RANGE_PC_FLOOR_MAX + 1} or above {RANGE_PC_CEIL_MIN - 1} is ever "
                f"used while {_share(piled) * 100:.0f}% of frames pile up at the limited-range "
                f"black/white clip points: the content looks limited range (16-235) but is "
                f"tagged full (pc)")
    return None


def assess_levels(series: VideoSeries, fps: float, full_range: bool) -> LevelsFindings:
    """EBU R 103 v3.0 luma and RGB-gamut tolerance on the averaged copy.

    A frame violates when more than 1 % of its picture lies outside the
    preferred range (luma 5-246 / 20-984; R, G and B -5 % .. 105 %); an event
    needs at least R103_MIN_FRAMES consecutive violating frames, so a single
    corrupt or transitional frame does not count. Full-range pictures cannot
    leave the luma range (0-255 is the nominal range), so only gamut applies.
    """
    period = _frame_period(fps, series.times)
    count = series.frames
    luma_fraction = (list(series.luma) if not full_range and series.luma else [0.0] * count)
    gamut_fraction = list(series.gam) if series.gam else [0.0] * count
    return LevelsFindings(
        luma=_area_events(luma_fraction, series.times, period),
        gamut=_area_events(gamut_fraction, series.times, period),
        max_luma=max(luma_fraction or [0.0]),
        max_gamut=max(gamut_fraction or [0.0]),
        range_hint=range_plausibility(series, full_range),
    )


def _level_event_text(kind: str, events: Sequence[LevelEvent]) -> str:
    shown = ", ".join(f"{timecode(e.start)} ({e.duration:.1f}s) up to {e.peak * 100:.1f}%"
                      for e in events[:4])
    more = f", +{len(events) - 4} more" if len(events) > 4 else ""
    return f"{kind} outside the R 103 range on >1% of the picture: {shown}{more}"


def levels_entry(findings: LevelsFindings, mode: str, full_range: bool,
                 assumed_matrix: Optional[str] = None) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Signal Levels row.

    ``assumed_matrix`` names the Y'CbCr matrix guessed for an untagged file:
    the gamut result depends on it, so confidence stays below 70.
    """
    luma_part = ("luma n/a (full range)" if full_range
                 else f"luma max {findings.max_luma * 100:.2f}%")
    measured = (f"Largest share outside EBU R 103 (limit 1% of the picture): {luma_part}, "
                f"RGB gamut max {findings.max_gamut * 100:.2f}%")
    problems = []
    if findings.luma:
        problems.append(_level_event_text("Luma", findings.luma))
    if findings.gamut:
        problems.append(_level_event_text("RGB gamut", findings.gamut))
    if findings.range_hint:
        problems.append("Range: " + findings.range_hint)
    if not problems:
        return "PASS", measured + ".", 100
    detail = ". ".join(problems) + f". ({measured}.)"
    if assumed_matrix and findings.gamut:
        detail += (f" The colour matrix is not signalled; {assumed_matrix.upper().replace('BT', 'BT.')}"
                   f" was assumed from the picture size.")
    if mode == "report":
        return "PASS", "Not enforced: " + detail, 100
    peak = max([e.peak for e in findings.luma + findings.gamut] or [0.0])
    # Near the 1 % limit the result depends on the averaging approximation
    # of the R 103 filter: keep confidence below 70 there.
    confident = (peak >= 2 * R103_AREA or bool(findings.range_hint and peak >= R103_AREA)) \
        and not (assumed_matrix and findings.gamut and not findings.luma)
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


def _transitions(values: Sequence[float], delta: float,
                 dark_limit: Optional[float] = None,
                 qualifies: Optional[Callable[[int, int], bool]] = None) -> List[Tuple[int, int]]:
    """(frame index, +1 rising / -1 falling) where a swing of ``delta`` completes.

    A hysteresis zig-zag over the series: a transition is a move of at least
    ``delta`` away from the last turning point, timed at the frame where the
    swing first reaches ``delta``. The direction only reverses after a move
    of ``delta`` back from the extreme, so a slow fade counts once and
    frame-to-frame jitter never counts. ``dark_limit`` is BT.1702-3's
    "darker state below 160 cd/m2" condition; ``qualifies(i, j)`` can veto a
    swing between frame indices i and j (the saturated-red rule).
    """
    out: List[Tuple[int, int]] = []
    if len(values) < 2:
        return out

    def accept(pivot: int, extreme: int) -> bool:
        if abs(values[extreme] - values[pivot]) < delta:
            return False
        if dark_limit is not None and min(values[extreme], values[pivot]) >= dark_limit:
            return False
        return qualifies is None or qualifies(pivot, extreme)

    low = high = 0
    trend = 0
    pivot = extreme = 0
    counted = False
    for i in range(1, len(values)):
        value = values[i]
        if trend == 0:
            if value > values[high]:
                high = i
            if value < values[low]:
                low = i
            if values[high] - values[low] < delta:
                continue
            trend = 1 if high > low else -1
            pivot, extreme = (low, high) if trend == 1 else (high, low)
            counted = False
        elif (value - values[extreme]) * trend >= 0:
            extreme = i
        elif abs(values[extreme] - value) >= delta:
            pivot, extreme, trend, counted = extreme, i, -trend, False
        if not counted and accept(pivot, extreme):
            out.append((i, 1 if values[extreme] > values[pivot] else -1))
            counted = True
    return out


def _hazard_spans(marks: Sequence[Tuple[int, int]], times: Sequence[float],
                  window: float) -> List[Tuple[int, int]]:
    """(first, last) frame spans with more than three flashes within ``window``.

    More than three flashes is more than six transitions: seven
    transitions spanning less than a second. At exactly 3 Hz seven
    transitions span exactly one second, so the BT.1702 limit itself is not
    flagged.
    """
    need = FLASH_MAX_TRANSITIONS + 1
    spans = []
    for k in range(len(marks) - need + 1):
        first, last = marks[k][0], marks[k + need - 1][0]
        if times[last] - times[first] < window:
            spans.append((first, last))
    return spans


def _flash_transitions(kind: str, grid, frames: Sequence[int],
                       blocks: Sequence[int]) -> List[Tuple[int, int]]:
    """Transitions of one flash rule for the mean of ``blocks`` over ``frames``.

    ``kind`` "general": luminance Y = 0.2126 R + 0.7152 G + 0.0722 B of the
    linear grid scaled to the 200 cd/m2 peak, swings of 20 cd/m2 with the
    darker state below 160 cd/m2. "red": (R - G - B) x 320 on linear values
    0..1, swings of 20 where one end is saturated red (R/(R+G+B) >= 0.8).
    Returned indices are positions within ``frames``.
    """
    n = float(len(blocks))
    values, saturated = [], []
    for i in frames:
        g = sum(grid[i][0][b] for b in blocks) / n
        bl = sum(grid[i][1][b] for b in blocks) / n
        r = sum(grid[i][2][b] for b in blocks) / n
        if kind == "general":
            values.append(FLASH_PEAK_CD / 255.0 * (0.2126 * r + 0.7152 * g + 0.0722 * bl))
        else:
            values.append(max(0.0, (r - g - bl) / 255.0) * 320.0)
            saturated.append(r > 0 and r >= FLASH_RED_RATIO * (r + g + bl))
    if kind == "general":
        return _transitions(values, FLASH_DELTA_CD, FLASH_DARK_CD)
    return _transitions(values, FLASH_RED_DELTA, None,
                        lambda i, j: saturated[i] or saturated[j])


def assess_flash(series: VideoSeries, fps: float) -> List[FlashEvent]:
    """BT.1702-3 general and red flash counting on a 16x9 grid of linear light.

    1. Per block, the rule's quantity (luminance Y = 0.2126 R + 0.7152 G +
       0.0722 B of BT.1886 display light at a 200 cd/m2 peak, or the WCAG
       saturated-red measure) is turned into transitions; a block is
       hazardous while seven transitions (more than three flashes) fall
       within one second.
    2. Where more than 25 % of the blocks are hazardous at once, the flash
       must also be *concurrent*: the mean of those blocks has to flash by
       itself. Blocks flashing in phase reinforce; a camera panning past
       lights or a shaking frame makes blocks brighten and darken at
       different moments, which cancels in the mean (real 4K camera footage
       otherwise produced false hazards).

    Advisory only: BT.1702 compliance is certified with a Harding FPA class
    analyser.
    """
    grid = series.flash
    frames = min(len(grid), len(series.times))
    if frames < FLASH_MAX_TRANSITIONS + 1:
        return []
    times = series.times[:frames]
    period = _frame_period(fps, times)
    window = 1.0 - 0.5 * period
    blocks = len(grid[0][0])
    events: List[FlashEvent] = []
    for kind in ("general", "red"):
        hazard_frames: List[List[int]] = [[] for _ in range(frames)]
        for b in range(blocks):
            marks = _flash_transitions(kind, grid, range(frames), [b])
            for first, last in _hazard_spans(marks, times, window):
                for f in range(first, last + 1):
                    if not hazard_frames[f] or hazard_frames[f][-1] != b:
                        hazard_frames[f].append(b)
        flagged = [len(h) > FLASH_AREA * blocks for h in hazard_frames]
        lead = max(1, int(round(1.0 / period)))
        for first, last in frame_runs(flagged):
            members = sorted({b for f in range(first, last + 1) for b in hazard_frames[f]})
            lo = max(0, first - lead)
            local_times = times[lo:last + 1]
            marks = _flash_transitions(kind, grid, range(lo, last + 1), members)
            spans = _hazard_spans(marks, local_times, window)
            if not spans:
                continue
            start = local_times[spans[0][0]]
            end = local_times[spans[-1][1]] + period
            area = max(len(hazard_frames[f]) for f in range(first, last + 1)) / blocks
            events.append(FlashEvent(start, end, area, kind))
    return sorted(events, key=lambda e: e.start)


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


def assess_interlace(labels: Sequence[str], times: Sequence[float], fps: float,
                     field_order: str) -> InterlaceFindings:
    """Combing in progressive-flagged video; field order vs metadata.

    Uses idet's multiple-frame classification (each frame judged with its
    neighbours, steadier than single-frame). A section is combed when more
    than half the frames in a one-second window are called TFF or BFF; at
    least a second of such windows is required. Field order compares idet's
    TFF/BFF majority with the declared order when the stream is interlaced.
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
        dense = [False] * count
        running = sum(interlaced[:window])
        for k in range(count - window + 1):
            if k:
                running += interlaced[k + window - 1] - interlaced[k - 1]
            if running > INTERLACE_WINDOW_SHARE * window:
                for j in range(k, k + window):
                    dense[j] = dense[j] or interlaced[j]
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
    tff, bff = labels.count("tff"), labels.count("bff")
    content = None
    if tff + bff >= FIELD_ORDER_MIN_FRAMES:
        if tff >= FIELD_ORDER_MAJORITY * (tff + bff):
            content = "tff"
        elif bff >= FIELD_ORDER_MAJORITY * (tff + bff):
            content = "bff"
    return InterlaceFindings(combed, content, flagged, share)


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
        return "PASS", f"No interlace combing found ({share}).", 100
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
                 black_events: Sequence[Tuple[float, float]],
                 analysis: Optional[VideoAnalysis],
                 analysis_error: Optional[str] = None) -> None:
    """Add every picture-check row to ``result`` (a core.QAResult).

    A check switched off in the settings is SKIP; one whose per-frame
    measurements are missing although frames were decoded is "Not
    verified" - never a silent PASS.
    """
    series = analysis.collect() if analysis is not None else VideoSeries()
    reason = analysis_error or _NO_MEASUREMENTS
    if series.frames:
        result.set_metadata("video_frames_analysed", series.frames)
    black = _report_black(result, config, black_events, video_end, fps, series)
    _report_freeze(result, config, series, video_end, fps, black, decoded_frames, reason)
    _report_levels(result, config, series, analysis, fps, decoded_frames, reason)
    _report_flash(result, config, series, analysis, fps, decoded_frames, reason)
    _report_interlace(result, config, series, analysis, video, fps, decoded_frames, reason)


def _report_black(result, config, events, duration, fps, series) -> List[Interval]:
    """Black Frames row; returns the raw black runs for the freeze check."""
    if not config.black:
        result.add_skip(BLACK, "Switched off in the settings.")
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
    findings = assess_levels(series, fps, full)
    result.set_metadata("levels_max_luma_pct", round(findings.max_luma * 100, 3))
    result.set_metadata("levels_max_gamut_pct", round(findings.max_gamut * 100, 3))
    if findings.range_hint:
        result.set_metadata("range_mismatch_suspected", True)
    assumed = analysis.info.matrix if analysis is not None and analysis.info.matrix_assumed else None
    _add(result, LEVELS, *levels_entry(findings, config.levels_mode, full, assumed))


def _report_flash(result, config, series, analysis, fps, decoded, reason) -> None:
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
    if decoded and not series.flash:
        result.add_not_verified(FLASH, reason)
        return
    events = assess_flash(series, fps)
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
    findings = assess_interlace(series.idet, series.times, fps, field_order)
    result.set_metadata("interlaced_frame_share", round(findings.interlaced_share, 3))
    _add(result, INTERLACE, *interlace_entry(findings))
