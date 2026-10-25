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
* **Interlace Detection** - woven fields (``idet`` at its documented
  defaults, confirmed by field-order steadiness and a field
  timing test) and 3:2 pulldown in progressive-flagged video, and
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


class BenignDecoderError:
    """An error-level FFmpeg record that is not damage in the file, and why."""

    __slots__ = ("pattern", "reason")

    def __init__(self, pattern: str, reason: str):
        self.pattern = re.compile(pattern)
        self.reason = reason


# File Integrity counts every error-level record FFmpeg prints during the
# decode (decoders conceal damage - an HEVC slice with an impossible
# cu_qp_delta decodes "successfully" and leaves FFmpeg's own decode-error
# count at 0), except these. Each is listed with the reason it is not damage;
# nothing else is excused.
BENIGN_DECODER_ERRORS: Tuple[BenignDecoderError, ...] = (
    BenignDecoderError(
        r"non monotonically increasing dts to muxer",
        "the null muxer complaining about FrameGuard's own output timestamps "
        "(passthrough of variable-rate input); timing is judged by the CFR check "
        "(Oct 21 recommendation R7a)"),
    BenignDecoderError(
        r"^mmco: unref short failure",
        "H.264 asked to release a short-term reference picture the decoder never "
        "received - a stream that starts on an open-GOP / recovery-point picture, as a "
        "lossless trim does; releasing a picture that is not there changes no decoded "
        "picture (H.264 8.2.5.4.1)"),
    BenignDecoderError(
        r"^Error while decoding stream #|^Decoding error:|^Error submitting packet to decoder",
        "FFmpeg's per-packet summary of a decode failure, which its stream "
        "accounting already counts as a decode error (counting it again would "
        "double the number)"),
)


def is_benign_decoder_error(message: str) -> bool:
    """True when an error-level record is on BENIGN_DECODER_ERRORS."""
    text = message.strip()
    return any(rule.pattern.search(text) for rule in BENIGN_DECODER_ERRORS)


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

# Flash and the black short-tier mean luma are measured on an area-averaged
# copy at most this wide and at least 4x smaller than the source (both are
# area measurements, which averaging preserves).
ANALYSIS_MAX_WIDTH = 480
ANALYSIS_MIN_FACTOR = 4
# EBU R 103 v3.0, "Video Signal Filtering": "A quarter band filter applied
# horizontally and a half band filter applied vertically is recommended.
# Horizontal Filter Coefficients: 1/16, 2/16, 3/16, 4/16, 3/16, 2/16, 1/16
# Vertical Filter Coefficients: 1/4, 1/2, 1/4", for interlaced and
# progressive signals, in pixel taps at the signal's own resolution - so the
# levels are judged on the native picture filtered exactly so, at every
# resolution (0.9.1 approximated it with box averaging that grew with the
# resolution and missed UHD/8K violations).
# The filter is evaluated at every native pixel (see _r103_exact), in 4 x 2
# phases: R103_PHASES columns and lines.
R103_PHASES = (4, 2)
# Subsampled chroma is brought to the luma grid by linear interpolation at
# the stream's signalled chroma sample location (ITU-T H.273
# chroma_sample_loc_type; FFmpeg's chroma_location). When it is not
# signalled, the codec's default applies: H.273 / H.264 / HEVC / VVC and
# MPEG-2 site 4:2:0 chroma "left" (co-sited horizontally with even luma
# columns, midway vertically); MPEG-1 and JPEG-family codecs site it in the
# centre (_CENTRE_SITED_CODECS).
_CHROMA_LOCATIONS = ("left", "center", "topleft", "top", "bottomleft", "bottom")
_CENTRE_SITED_CODECS = {"mpeg1video", "mjpeg", "jpeg2000", "jpegls", "mjpegb", "ljpeg"}
# The R 103 graph runs as up to LEVELS_PARTS_MAX parallel copies (see
# VideoAnalysis.levels_parts); LEVELS_SERIES are the series it writes.
LEVELS_PARTS_MAX = 4
LEVELS_SERIES = ("luma", "band1", "band2", "gam_r", "gam_g", "gam_b")
# Interlace analysis keeps every line (fields must stay separate) but
# point-samples columns: idet compares each line with the lines above and
# below it, a purely vertical operation, so dropping columns only thins the
# sample (320 columns still give idet >= 100,000 comparisons a frame at
# 720 lines, so its counts are not noise-limited).
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
# The streamed grid is gbrap: alpha holds the fine-transition share (see
# _area_chains); its first frame is the source's second.
FLASH_GRID_OFFSET = 1

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

# Range plausibility (8-bit codes of the R 103-filtered picture).
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

# Freeze (see detect_freezes()). A frozen picture is consecutive frames that
# are identical in content and not creative intent (MovieLabs QC vocabulary,
# DUPLICATE-FRAMES "also known as Freeze Frame"; EBU QC freeze item: the
# duration is a spec choice, freeze_detect_duration). Measurements:
# * scdet MAFD: mean |difference| to the previous frame over all planes at
#   full resolution, in % of full scale;
# * structural change: the same on a copy area-averaged at least 16x per
#   side (the analysis copy reduced FREEZE_COPY_FACTOR more), where
#   independent temporal grain shrinks by the side factor while moving
#   picture structure, down to objects of ~1/30 of the picture, survives.
FREEZE_COPY_FACTOR = 4
# Without the structural series, still is a MAFD under 0.2 % of full scale:
# half an 8-bit code on average, below any visible change.
FREEZE_STILL_MAFD = 0.20
# A long-GOP encoder re-codes a held picture at every I-frame; the change is
# requantisation noise, at most about one quantiser step of the residual
# (1-1.5 % of full scale at broadcast QPs), far under real motion (5-50 %).
# It lasts at most the encoder's lookahead/refinement, 8 frames.
FREEZE_BLIP_MAFD = 1.5
FREEZE_MAX_BLIP_FRAMES = 8
# Identical: a mean change of 0.004 % of full scale - one code on one pixel
# in a hundred - i.e. bit-identical up to encoder rounding.
FREEZE_IDENTICAL_MAFD = 0.004
# Structurally still: the change on the averaged copy is no more than grain
# explains. White grain shrinks by the averaging side factor k; film grain
# at HD is correlated over about 2 px (35 mm grain clumps of 5-15 um against
# ~11 um per 1080p pixel on a Super 35 frame), so 2/k of the full-resolution
# change is allowed, plus a floor of 1/20 code (0.02 %) for the copy's
# rounding. Motion is not grain: it survives the averaging, and also
# accumulates - a static picture shows no more than grain between frames
# FREEZE_STEP_FRACTION of the minimum freeze duration apart, where a slow pan
# has moved by several pixels.
FREEZE_GRAIN_CORRELATION = 2.0
FREEZE_STRUCTURAL_FLOOR = 0.02
FREEZE_STEP_FRACTION = 0.5
# A scene change, as FFmpeg's scdet decides it with its documented default
# threshold (10): score = min(MAFD, |MAFD - previous MAFD|) >= 10.
FREEZE_CUT_SCORE = 10.0
# A freeze stops the picture dead: motion runs right up to the frozen frame.
# Real motion decelerates over several frames (inertia), so each of the
# FREEZE_LEAD_FRAMES frames before the run must still be moving (above the
# grain limit), and their median change above the frozen frames' own
# (noise-only) change must be at least FREEZE_MOVING_FACTOR times the margin
# the grain limit leaves above that noise: clearly outside the noise band
# right up to the stop. A shot easing to a stop crosses the band gradually,
# its last moving frames just above the limit. Under heavy grain the band is
# wide, so motion at least one margin above the noise also counts when it
# had not declined: at least 1/FREEZE_MOVING_FACTOR of the motion half the
# minimum freeze duration earlier (a shot easing out loses most of its
# motion over that time; a freeze cuts off motion at its running level).
FREEZE_LEAD_FRAMES = 3
FREEZE_MOVING_FACTOR = 2.0

# The flash grid is written in segments of this length and consumed while
# FFmpeg decodes: at most about three segments (~0.8 MB at 60 fps) exist at
# once, whatever the programme length.
FLASH_SEGMENT_SECONDS = 5

# Mean luma (8-bit analysis copy) read back for a frame FFmpeg did not write.
YAVG_ABSENT = 255.0

# Black: the short tier reports a burst only when it is isolated (not part
# of a strobe; the Flash check covers those) and cut hard into black on
# both sides. A cut changes the picture in one frame; a dip to black fades
# through several. So on each side the neighbouring frame must differ from
# the black by at least BLACK_STEP_CODES (frame means move by well under a
# code with grain or coding noise; a cut from a night grade at Y 25 to black
# steps 5+), and the frame beyond it must be within half that step of it
# (no ramp). Judged relative to the scene, so dark scenes count too.
BLACK_ISOLATION_SECONDS = 1.0
BLACK_STEP_CODES = 2.0
BLACK_EDGE_ALLOWANCE = 1.0
# A burnt-in timecode or a channel logo keeps a black picture under the
# picture threshold (Telestream VQ-101 documents 100 % of the frame; FFmpeg's
# blackdetect default is 98 %). Such an overlay is small and does not move:
# frames whose non-black pixels (above the black pixel level, on the
# analysis copy) are at most BLACK_OVERLAY_MAX of the picture and stay in
# place - non-black in both frames BLACK_OVERLAY_STEP apart - while the rest
# meets the picture threshold, are black too. Credits or a dark scene move
# or exceed the share, and stay picture. The mask is sampled every
# BLACK_OVERLAY_BLOCK px and compared BLACK_OVERLAY_STEP seconds apart:
# credits roll at one or more pixels per frame (>= 25 px/s), so in 0.5 s
# their text has moved off the sampled points it covered.
BLACK_OVERLAY_MAX = 0.05
BLACK_OVERLAY_BLOCK = 8
BLACK_OVERLAY_STEP = 0.5
BLACK_OVERLAY_SAMPLE = 0.1
# Only a graphic overlay is excluded (round 6: soft practical lights in a
# locked-off dark grade were swallowed). A burnt-in timecode or a logo is a
# near-uniform fill bounded by a sharp edge, so at least BLACK_GRAPHIC_SHARE
# (a majority) of its non-black samples sit at its fill, at or above
# BLACK_GRAPHIC_LEVEL (half of nominal white); a soft light is mostly
# fall-off (a Gaussian blob of peak P above black has only (r_half/r_black)^2,
# under a fifth at P >= 100 codes, of its non-black area above half white).
# And the rest must be true black: its mean code within BLACK_TRUE_MARGIN
# (one 8-bit code, about 0.5 % of nominal) of the R 103 / BT.709 black level,
# not merely dark (a lifted dark grade sits a few codes above it; grain on
# black averages to it).
BLACK_GRAPHIC_LEVEL = 0.5
BLACK_GRAPHIC_SHARE = 0.5
BLACK_TRUE_MARGIN = 1.0

# Interlace (see assess_interlace). idet runs with FFmpeg's documented
# defaults (intl_thres 1.04, prog_thres 1.5, rep_thres 3.0; FFmpeg filters
# documentation, "idet"): no threshold here is tuned to test media.
# A section of a progressive-flagged stream is combed when more than half the
# frames of a one-second window are called TFF/BFF by idet's multiple-frame
# detector (Oct 21 recommendation R5b), for at least a second.
INTERLACE_WINDOW_SHARE = 0.5
INTERLACE_MIN_SECONDS = 1.0
# An interlaced source has one temporal field order (fixed by the camera or
# the encoder; it only changes at an edit), so idet's single-frame calls in
# a woven section agree. Progressive pictures can still give idet a slight
# asymmetry between its two cross-field comparisons (it needs only 4 %), but
# its sign then follows the content and flips; such a section is not combed.
# The same majority decides the field order of declared-interlaced streams.
# 0.8 is a chance bound, not a fitted value: a call whose sign is a coin
# toss reaches 80 % agreement over 15 decided frames (half a second at
# 30 frame/s) with probability 1.8 % (binomial, P[X >= 12 of 15]), while a
# real field order agrees on every frame whose motion idet can see.
FIELD_ORDER_MAJORITY = 0.8
FIELD_ORDER_MIN_FRAMES = 30
# Field timing (see field_timing()). In a progressive frame both fields
# show the same instant; in a woven frame the second field was sampled half
# a frame later. So the bottom field of frame n is compared with the top
# field (interpolated to the bottom field's lines, so the half-line offset
# cancels) of frames n-1, n and n+1: a progressive bottom field matches its
# own frame's top field far better than either neighbour's; a woven one
# matches its own frame's top field no better than the neighbour on its
# temporal side. idet's own progressive ratio (prog_thres 1.5) is the margin.
# Before comparing, the idet copy (at most 320 point-sampled columns) is
# area-averaged over FIELD_TIMING_COLUMNS columns, a span of 36 source
# columns at 1080p: temporal grain is independent sample to sample and
# shrinks by sqrt(6) ~ 2.4x, while picture motion of a pixel or more per
# field moves structure larger than that. Lines are not averaged: the
# fields' vertical alignment is what the comparison is about.
INTERLACE_DECISIVE_RATIO = 1.5
FIELD_TIMING_COLUMNS = 6
# Vertical scrolls (credits) are judged on the row profile (the mean of each
# line) instead: hard-edged graphics moving vertically defeat the field
# interpolation the timing comparison relies on. A frame is a vertical
# scroll when its column profile (each column's mean) changes by less than
# INTERLACE_VERTICAL_SHARE of the total profile change: a pure vertical
# translation changes column means only through the few lines entering and
# leaving the picture (d of H lines: under 1 % for d <= 4, H >= 480), while
# horizontal or 2-D motion moves both profiles alike (about half each); the
# threshold is the geometric mean of those two orders, 7 %. Profile change
# below INTERLACE_MOTION_FLOOR (a fiftieth of a code) is no motion.
# Vertical field displacement on the row profile: In a progressive frame both
# fields are sampled at the same instant: the bottom field is best predicted
# from the top field interpolated at its own lines (displacement 0). In a
# woven frame whose content moves vertically, the bottom field was sampled
# half a frame later and is best predicted at an offset. A vertical scroll by
# whole lines (end credits) can make idet call a progressive picture TFF/BFF,
# while a woven scroll is combed; the displacement tells the two apart. The
# winner must be better by idet's own progressive ratio (prog_thres 1.5);
# otherwise the frame is undecided.
INTERLACE_DISPLACEMENTS = (-2, -1, 0, 1, 2)
INTERLACE_VERTICAL_SHARE = 0.07
INTERLACE_MOTION_FLOOR = 0.02
# A woven vertical scroll misregisters its fields by half the frame-to-frame
# speed. At up to INTERLACE_SCROLL_SLOW lines per frame that is at most one
# line, the line pitch of the picture itself: a woven and a progressive
# scroll then look alike and show no feathering, so such frames are not
# counted as combed. The speed is the shift k (|k| <= INTERLACE_SCROLL_RANGE
# lines) that best maps one frame's row profile onto the next.
INTERLACE_SCROLL_SLOW = 2
INTERLACE_SCROLL_RANGE = 4
# 3:2 pulldown, the film-to-30 frame/s transfer cadence: four film frames
# become ten fields (3 + 2 + 3 + 2), so two of every five video frames repeat
# a field of the frame before, top and bottom alternately, and idet's
# repeated-field call recurs with a period of five frames.
# A one-second window holding at least half of the expected repeats (two in
# five) is a cadence window: idet can only call a repeat where the picture
# moved between the film frames, so on slow film motion it sees one of the
# two repeats per cycle, and the period-five recurrence is still
# unambiguous at one in five.
TELECINE_PERIOD = 5
TELECINE_REPEAT_SHARE = 0.4

MATRICES = {
    # Kr-derived Y'CbCr -> R'G'B' coefficients: R = Y + a*Cr,
    # G = Y - b*Cb - c*Cr, B = Y + d*Cb (ITU-R BT.709 / BT.601 / BT.2020).
    "bt709": (1.5748, 0.1873, 0.4681, 1.8556),
    "bt601": (1.402, 0.344136, 0.714136, 1.772),
    "bt2020": (1.4746, 0.16455, 0.57135, 1.8814),
}

_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}

# Colour signalling families (ITU-T H.273 code points as FFmpeg names them).
# A set of primaries implies its own Y'CbCr matrix: BT.709 (Part 2, signal
# format) derives its luma and colour-difference coefficients from the
# BT.709 primaries, BT.2020 (Table 4) from the BT.2020 ones (non-constant or
# constant luminance), BT.601 from the 525/625 ones; BT.2100 adds ICtCp. PQ and HLG (BT.2100 Tables 4/5) are
# defined only with BT.2020 primaries (BT.2100 Table 2, colorimetry). The BT.709, BT.601
# and BT.2020 OETFs are the same curve (H.273: transfer 1, 6, 14 and 15 are
# functionally identical), so an SDR transfer is consistent with any SDR
# primaries.
_UNSIGNALLED_TAGS = ("", "unknown", "unspecified", "reserved", "none")
_PRIMARY_MATRICES = {
    "bt709": ("bt709",),
    "bt2020": ("bt2020nc", "bt2020c", "ictcp"),
    "smpte170m": ("smpte170m", "bt470bg"),
    "bt470bg": ("bt470bg", "smpte170m"),
}
_MATRIX_LABELS = {"bt709": "BT.709", "bt2020nc": "BT.2020", "bt2020c": "BT.2020 (constant luminance)",
                  "ictcp": "ICtCp", "smpte170m": "BT.601", "bt470bg": "BT.601"}
_PRIMARY_LABELS = {"bt709": "BT.709", "bt2020": "BT.2020", "smpte170m": "BT.601 (525)",
                   "bt470bg": "BT.601 (625)", "smpte432": "P3-D65", "smpte431": "DCI-P3"}


def colour_tag_advisories(primaries: str, transfer: str, matrix: str,
                          report_partial: bool = True) -> List[str]:
    """Sentences on inconsistent or partial colour tags (advisory).

    ``primaries``/``transfer``/``matrix`` are FFmpeg's color_primaries,
    color_transfer and color_space names. A fully untagged stream is
    reported elsewhere; here: some tags present and others missing (players
    then assume the missing ones, often differently), primaries with another
    system's matrix, and PQ/HLG outside BT.2100.
    """
    p, t, m = (str(v or "").lower() for v in (primaries, transfer, matrix))
    missing = [name for name, v in (("primaries", p), ("transfer", t), ("matrix", m))
               if v in _UNSIGNALLED_TAGS]
    out = []
    if report_partial and missing and len(missing) < 3:
        present = [name for name in ("primaries", "transfer", "matrix") if name not in missing]
        out.append(f"colour tags are only partly signalled ({' and '.join(present)} set, "
                   f"{' and '.join(missing)} not), so players assume the rest and can disagree")
    expected = _PRIMARY_MATRICES.get(p)
    if expected and m not in _UNSIGNALLED_TAGS and m not in expected:
        out.append(f"{_PRIMARY_LABELS.get(p, p)} primaries are signalled with a "
                   f"{_MATRIX_LABELS.get(m, m)} matrix; each system's Y'CbCr matrix is derived "
                   f"from its own primaries (BT.709 / BT.2020 / BT.601), so the tags contradict "
                   f"each other and colours decode wrong in one of the two")
    if t in _HDR_TRANSFERS:
        name = "PQ" if t == "smpte2084" else "HLG"
        if p not in _UNSIGNALLED_TAGS and p != "bt2020":
            out.append(f"{name} is signalled with {_PRIMARY_LABELS.get(p, p)} primaries; ITU-R "
                       f"BT.2100 defines {name} only with BT.2020 primaries")
        if m not in _UNSIGNALLED_TAGS and m not in ("bt2020nc", "bt2020c", "ictcp"):
            out.append(f"{name} is signalled with a {_MATRIX_LABELS.get(m, m)} matrix; ITU-R "
                       f"BT.2100 uses the BT.2020 matrix or ICtCp")
    elif t in ("bt2020-10", "bt2020-12") and p not in _UNSIGNALLED_TAGS and p != "bt2020":
        out.append(f"the BT.2020 transfer is signalled with {_PRIMARY_LABELS.get(p, p)} primaries; "
                   f"it belongs to the BT.2020 system")
    return out


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


def chroma_location(video: dict) -> str:
    """The stream's chroma sample location for the scaler (see _CHROMA_LOCATIONS)."""
    tag = str(video.get("chroma_location") or "").lower()
    if tag in _CHROMA_LOCATIONS:
        return tag
    codec = str(video.get("codec_name") or "").lower()
    return "center" if codec in _CENTRE_SITED_CODECS else "left"


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
    chroma_loc: str = "left"        # chroma sample location (see chroma_location)

    @classmethod
    def from_stream(cls, video: dict) -> "PictureInfo":
        fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
        return cls(width=int(video.get("width") or 0), height=int(video.get("height") or 0),
                   fps=fps if 0 < fps < 1000 else 0.0,
                   full_range=is_full_range(video), matrix=_matrix_for(video),
                   field_order=str(video.get("field_order") or "unknown").lower(),
                   hdr=str(video.get("color_transfer") or "").lower() in _HDR_TRANSFERS,
                   matrix_assumed=str(video.get("color_space") or "").lower()
                   in ("", "unknown", "unspecified", "reserved"),
                   chroma_loc=chroma_location(video))

    @property
    def interlaced(self) -> bool:
        """Signalled as interlaced (field_order tt/bb/tb/bt)."""
        return self.field_order in ("tt", "bb", "tb", "bt")

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
    freeze_hold: float = 3.0
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
            freeze_hold=float(cfg.get("freeze_hold_allowance", 3.0)),
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


# The planes are widened and narrowed with swscale treating them as limited
# range (in = out, so no level conversion happens): every plane then scales
# by a power of two (8-bit v -> v x 256, 10-bit v -> v x 64; measured on the
# bundled build - full-range luma would scale by 257 and convert back
# inexactly). A 16-bit filter output is rounded to the nearest v x 256 and
# narrowed without dithering (swscale dithers 16 -> 8-bit by default, which
# moved about 8 % of a uniform 247 to 246): the 8-bit result is exactly the
# rounded filter output, as an 8-bit filter would store.
_ROUND_TO_8BIT = "lutyuv=y='round(val/256)*256':u='round(val/256)*256':v='round(val/256)*256'"
_TO_8BIT = "scale=in_range=tv:out_range=tv:sws_dither=none:flags=accurate_rnd"
_WIDEN = "setparams=range=tv,scale=in_range=tv:out_range=tv"


def _r103_exact(source: str, out: str, work: str, final: str) -> List[str]:
    """``[source]`` R 103-filtered at every native pixel -> ``[out]``.

    The R 103 kernels factor into boxes - 1-2-3-4-3-2-1/16 is a 4-pixel box
    applied twice, 1-2-1/4 a 2-line box applied twice - and an area
    reduction by an integer factor is an exact box mean. With h_p the 4:1
    column area reductions starting at column p, the horizontally filtered
    value at column 4m + 3 + s is
    the mean of h_s..h_3 at m and h_0..h_(s-1) at m + 1. The four offsets s
    are laid side by side (hstack), then the same is done for the two line
    offsets of the 2:1 line reductions (vstack): the tiles hold every native
    pixel's filtered value once (columns and lines regrouped by phase, which
    a share does not see), except the outermost 3 columns and 1 line the
    kernels cannot reach.
    """
    columns, lines = R103_PHASES
    width = f"trunc((iw-{columns - 1})/{columns})*{columns}"
    height = f"trunc((ih-{lines - 1})/{lines})*{lines}"
    chains = [f"[{source}]split={columns}" + "".join(f"[{out}_c{p}]" for p in range(columns))]
    for p in range(columns):
        chains.append(f"[{out}_c{p}]crop=w='{width}':h=ih:x={p}:y=0,"
                      f"scale=w=iw/{columns}:h=ih:flags=area,format={work},"
                      f"split={columns}" + "".join(f"[{out}_h{p}s{t}]" for t in range(columns)))
    for t in range(columns):            # offset t uses h_p shifted by one for p < t
        for p in range(columns):
            chains.append(f"[{out}_h{p}s{t}]crop=w=iw-1:h=ih:x={1 if p < t else 0}:y=0"
                          f"[{out}_o{t}p{p}]")
        chains += _mean_tree([f"{out}_o{t}p{p}" for p in range(columns)], f"{out}_ht{t}",
                             f"[{out}_hs{t}]")
    chains.append("".join(f"[{out}_hs{t}]" for t in range(columns))
                  + f"hstack=inputs={columns},split={lines}"
                  + "".join(f"[{out}_r{q}]" for q in range(lines)))
    for q in range(lines):
        chains.append(f"[{out}_r{q}]crop=w=iw:h='{height}':x=0:y={q},"
                      f"scale=w=iw:h=ih/{lines}:flags=area,split={lines}"
                      + "".join(f"[{out}_v{q}s{t}]" for t in range(lines)))
    for t in range(lines):
        for q in range(lines):
            chains.append(f"[{out}_v{q}s{t}]crop=w=iw:h=ih-1:x=0:y={1 if q < t else 0}"
                          f"[{out}_w{t}q{q}]")
        chains += _mean_tree([f"{out}_w{t}q{q}" for q in range(lines)], f"{out}_vt{t}",
                             f"[{out}_vs{t}]")
    chains.append("".join(f"[{out}_vs{t}]" for t in range(lines))
                  + f"vstack=inputs={lines},format={work},{_ROUND_TO_8BIT},{_TO_8BIT},format={final}[{out}]")
    return chains


def _mean_tree(labels: List[str], prefix: str, tail: str) -> List[str]:
    """Chains averaging ``labels`` (a power of two of them) pairwise.

    ``blend=all_mode=average`` on 16-bit frames is exact to 1/65536 of full
    scale; the last average is followed by ``tail``.
    """
    chains, level = [], list(labels)
    depth = 0
    while len(level) > 1:
        nxt = []
        for k in range(0, len(level), 2):
            out = f"{prefix}{depth}_{k // 2}"
            last = len(level) == 2
            chains.append(f"[{level[k]}][{level[k + 1]}]blend=all_mode=average"
                          + (tail if last else f"[{out}]"))
            nxt.append(out)
        level, depth = nxt, depth + 1
    return chains


def _disp_name(d: int) -> str:
    return f"disp_{'m' if d < 0 else 'p'}{abs(d)}"


def _shift_name(k: int) -> str:
    return f"shift_{'m' if k < 0 else 'p'}{abs(k)}"


def _displacement_kernel(d: int) -> str:
    """7-tap column kernel: 2 x (line y - top field interpolated at y + d).

    Taps are lines y-3..y+3; y is a bottom-field (odd) line. At an odd offset
    the prediction is a top-field line itself; at an even offset it is the
    mean of the two top-field lines around y + d. Doubling keeps every tap an
    integer, so no rounding hides sub-code differences.
    """
    taps = [0] * 7
    taps[3] += 2
    if d % 2:
        taps[3 + d] -= 2
    else:
        taps[3 + d - 1] -= 1
        taps[3 + d + 1] -= 1
    return " ".join(str(t) for t in taps)


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
    """Three luma masks (255 = pixel in class) of the R 103-filtered picture.

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
        self.accumulator = FlashAccumulator(fps, planes=4, offset=FLASH_GRID_OFFSET)
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
        # The exact R 103 graph is the costliest per frame; frames are dealt
        # round-robin to this many copies of it, each its own filtergraph
        # thread (FFmpeg runs filtergraphs in parallel, filters within one
        # in series). Same work per frame, so the same results.
        threads = int(cfg.get("filter_threads", 4) or 4)
        self.levels_parts = max(1, min(LEVELS_PARTS_MAX, threads // 2))
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
        names = ("mafd", "struct", "struct_step", "blk_n", "blk_s", "blk_b", "blk_k", "black_start", "black_end", "idet", "idet1", "rep_top", "rep_bottom", "luma", "band1", "band2",
                 "yavg", "gam_r", "gam_g", "gam_b", "ft_same", "ft_top", "ft_bot", "ft_move", "colmo", "rowmo") + tuple(
                     _disp_name(d) for d in INTERLACE_DISPLACEMENTS) + tuple(
                     _shift_name(k) for k in range(-INTERLACE_SCROLL_RANGE, INTERLACE_SCROLL_RANGE + 1))
        names += tuple(f"{name}@{part}" for name in LEVELS_SERIES for part in range(self.levels_parts))
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
        """Flash and the black short-tier mean luma share one reduced copy."""
        c = self.config
        return self.info.analysable and (c.levels or c.flash or c.black or c.freeze)

    @property
    def structural_side_factor(self) -> float:
        """Source pixels per side of one sample of the structural-change copy."""
        width = self.info.width
        copy = max(2, min(ANALYSIS_MAX_WIDTH, (width // (2 * ANALYSIS_MIN_FACTOR)) * 2))
        return width / max(1, copy // FREEZE_COPY_FACTOR)

    @property
    def overlay_sample_frames(self) -> int:
        """Frames between non-black measurements (BLACK_OVERLAY_SAMPLE)."""
        fps = self.info.fps if self.info.fps > 0 else 25.0
        return max(1, int(fps * BLACK_OVERLAY_SAMPLE + 1e-9))

    @property
    def overlay_step_frames(self) -> int:
        """Frames between the static-overlay comparisons (BLACK_OVERLAY_STEP),
        a multiple of overlay_sample_frames."""
        fps = self.info.fps if self.info.fps > 0 else 25.0
        sample = self.overlay_sample_frames
        return sample * max(1, int(fps * BLACK_OVERLAY_STEP / sample + 1e-9))

    @property
    def freeze_step_frames(self) -> int:
        """Frames between the accumulated-change samples (FREEZE_STEP_FRACTION)."""
        return freeze_step_frames(self.info.fps, self.config.freeze_duration)

    @property
    def needs_levels_branch(self) -> bool:
        return self.info.analysable and self.config.levels

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
                f"[vs_main]{self._black_chain()}[{out_label}]",
                f"[vs_fz]scdet@fz=threshold=100,"
                f"{_print('lavfi.scd.mafd', self.paths['mafd'])},nullsink"]

    def _black_chain(self) -> str:
        """blackdetect plus prints of its per-frame black_start / black_end marks.

        The marks are the complete black-run record (the log can drop or
        rename it), and the files existing proves the detector ran.
        """
        if not self.config.black:
            return "null"
        return ",".join([black_filter(self.config),
                         _print("lavfi.black_start", self.paths["black_start"]),
                         _print("lavfi.black_end", self.paths["black_end"])])

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
        if self.needs_levels_branch:
            for part in range(self.levels_parts):
                graphs.append((";".join(self._levels_part(part)), f"[vs_lv{part}]"))
        if self.needs_area_branch:
            label = None if self.flash_enabled else "[vs_area]"
            graphs.append((";".join(self._area_chains()), label))
        return graphs

    def _interlace_chains(self) -> List[str]:
        """idet labels plus the field-timing comparison (see field_timing()).

        idet's multiple-frame, single-frame and repeated-field calls are
        printed only where they say something (TFF/BFF, a repeated field):
        progressive footage writes nothing. For idet's TFF/BFF frames the
        column and row profiles' change, the row profile's gradient and its
        field displacement are written (vertical scrolls, field_verdict).
        The field-timing branch averages the idet copy over
        FIELD_TIMING_COLUMNS columns and compares successive fields (see
        field_timings).
        """
        multi, single = "lavfi.idet.multiple.current_frame", "lavfi.idet.single.current_frame"
        repeated = "lavfi.idet.repeated.current_frame"
        prints = [_print(multi, self.paths["idet"], "ff", "ends_with"),
                  _print(single, self.paths["idet1"], "ff", "ends_with"),
                  _print(repeated, self.paths["rep_top"], "top", "same_str"),
                  _print(repeated, self.paths["rep_bottom"], "bottom", "same_str")]
        interlaced = _select(multi, "ff", "ends_with")
        reduce16 = "scale=w={w}:h='max(1,trunc(ih/16))':flags=area"
        avg = "YAVG"

        def stat(name: str) -> str:
            return f"signalstats,{_print(f'lavfi.signalstats.{avg}', self.paths[name])}"

        span = INTERLACE_SCROLL_RANGE
        shifts = range(-span, span + 1)
        chains = [
            f"[0:{self.index}]scale=w='min({INTERLACE_MAX_WIDTH},iw)':h=ih:flags=neighbor,idet@il,"
            "split=4[il_det][il_col][il_row][il_ft]",
            f"[il_det]{','.join(prints)}[vs_il]",
            # Column and row profiles (vertical scrolls, field_verdict).
            f"[il_col]format=gray,{reduce16.format(w=64)},{reduce16.format(w=64)},"
            f"scale=w=64:h=1:flags=area,tblend=all_mode=difference,{interlaced},{stat('colmo')},nullsink",
            "[il_row]format=gray,scale=w='max(1,trunc(iw/16))':h=ih:flags=area,"
            "scale=w=1:h=ih:flags=area,split=3[il_rmo][il_prof][il_shf]",
            f"[il_rmo]format=gray,tblend=all_mode=difference,{interlaced},{stat('rowmo')},nullsink",
            f"[il_prof]format=gray,{interlaced},crop=w=1:h='trunc(ih/2)*2':x=0:y=0,"
            f"split={len(INTERLACE_DISPLACEMENTS)}"
            + "".join(f"[il_d{k}]" for k in range(len(INTERLACE_DISPLACEMENTS))),
        ]
        chains += [
            f"[il_d{k}]convolution=0m='{_displacement_kernel(d)}':0rdiv=1:0bias=128:"
            "0mode=column,lutyuv=y='abs(val-128)',il=l=d,crop=w=1:h='ih/2':x=0:y='ih/2',"
            f"{stat(_disp_name(d))},nullsink"
            for k, d in enumerate(INTERLACE_DISPLACEMENTS)
        ]
        # Scroll speed. The row profile is widened to one column per shift k;
        # on odd frames column k holds the profile moved by k lines (geq), on
        # even frames the profile itself. tblend then pairs, column by column,
        # one frame's profile with its neighbour's moved by k:
        # |P_n(y) - P_n-1(y + k)| for even n, |P_n(y + k) - P_n-1(y)| for odd
        # n (see scroll_speed). Only idet's TFF/BFF frames are analysed (the
        # select comes first, so progressive footage costs nothing here);
        # frame parity is their position in that selection.
        width = len(shifts)
        move = f"if(mod(N,2),X-{span},0)"
        chains += [
            f"[il_shf]{interlaced},format=gray16,scale=w={width}:h=ih:flags=neighbor,"
            f"geq=lum='p(0,clip(Y+{move},0,H-1))',crop=w=iw:h=ih-{2 * span}:x=0:y={span},"
            "tblend=all_mode=difference,"
            f"split={width}" + "".join(f"[sc_d{i}]" for i in range(width)),
        ]
        chains += [f"[sc_d{i}]crop=w=1:h=ih:x={i}:y=0,{stat(_shift_name(k))},nullsink"
                   for i, k in enumerate(shifts)]
        # Field timing (see field_timings), on the idet copy, for idet's
        # TFF/BFF frames only (selected first, as above). Per frame Z = [Ti; B] (Ti: the top
        # field interpolated to the bottom field's lines). geq gives
        # |Ti_n - B_n| in its top half; with the halves swapped on odd frames
        # tblend pairs each frame's B with its neighbour's Ti (see
        # field_timings); tblend on the unswapped Z gives the top-field
        # motion. 16 bits throughout: an 8-bit gray -> gray16 conversion
        # after splitting fields mixed them (measured on the bundled build).
        chains += [
            f"[il_ft]{interlaced},extractplanes=y,format=gray16,"
            f"scale=w='max(1,trunc(iw/{FIELD_TIMING_COLUMNS}))':h=ih:flags=area,"
            "crop=w=iw:h='trunc(ih/4)*4':x=0:y=0,il=l=d,split=2[ft_top][ft_bot]",
            "[ft_top]crop=w=iw:h=ih/2:x=0:y=0,split=2[ft_t1][ft_t2]",
            "[ft_t1]crop=w=iw:h=ih-2:x=0:y=0[ft_tu]",
            "[ft_t2]crop=w=iw:h=ih-2:x=0:y=1[ft_td]",
            "[ft_tu][ft_td]blend=all_mode=average[ft_ti]",
            "[ft_bot]crop=w=iw:h=ih/2-2:x=0:y=ih/2[ft_b]",
            "[ft_ti][ft_b]vstack,split=3[ft_z1][ft_z2][ft_z3]",
            "[ft_z1]geq=lum='abs(p(X,Y)-p(X,Y+H/2))',crop=w=iw:h=ih/2:x=0:y=0,"
            f"{stat('ft_same')},nullsink",
            "[ft_z2]geq=lum='p(X,mod(Y+if(mod(N,2),H/2,0),H))',tblend=all_mode=difference,"
            "split=2[ft_h1][ft_h2]",
            f"[ft_h1]crop=w=iw:h=ih/2:x=0:y=0,{stat('ft_top')},nullsink",
            f"[ft_h2]crop=w=iw:h=ih/2:x=0:y=ih/2,{stat('ft_bot')},nullsink",
            "[ft_z3]tblend=all_mode=difference,crop=w=iw:h=ih/2:x=0:y=0,"
            f"{stat('ft_move')},nullsink",
        ]
        return chains

    def _levels_part(self, part: int) -> List[str]:
        """The levels graph for every levels_parts-th frame from ``part``.

        Labels get the part as a suffix, prints go to the part's files
        (``name@part``), and the output is ``[vs_lv<part>]``.
        """
        # The prints are built with the part's files (and FFmpeg's escaping
        # of their paths) by pointing the series names at them meanwhile.
        saved = {name: self.paths[name] for name in LEVELS_SERIES}
        try:
            for name in LEVELS_SERIES:
                self.paths[name] = self.paths[f"{name}@{part}"]
            chains = self._levels_chains()
        finally:
            self.paths.update(saved)
        if self.levels_parts == 1 and part == 0:
            text = ";".join(chains)
        else:
            text = ";".join(chains).replace(
                f"[0:{self.index}]",
                f"[0:{self.index}]select='eq(mod(n\\,{self.levels_parts})\\,{part})',", 1)
        text = re.sub(r"\[([A-Za-z_][A-Za-z0-9_]*)\]",
                      lambda m: f"[{m.group(1)}{part}]" if m.group(1) == "vs_lv" else f"[{m.group(1)}_p{part}]",
                      text)
        return text.split(";")

    def _levels_chains(self) -> List[str]:
        """EBU R 103 luma and RGB gamut masks of the R 103-filtered picture.

        Every native pixel is filtered exactly (_r103_exact), in 16 bits and
        rounded once. Subsampled chroma is first interpolated to the luma
        grid, linearly and at the stream's chroma sample location (swscale
        bilinear with full chroma interpolation and accurate rounding, see
        _CHROMA_LOCATIONS): R 103 filters the 4:4:4 picture the display
        reconstructs, and mis-sited chroma would misplace every colour edge
        against its luma edge (reading gamut about 2x low on 4:2:0 edges).

        The masks are computed plane by plane with lut/lut2, reduced, then
        packed three to a frame for one signalstats call each. The graph
        ends in ``[vs_lv]``.
        """
        info = self.info
        matrices = info.gamut_matrices
        # R 103: the filter is "applied intra field for interlace signals":
        # an interlaced picture is filtered with its fields stacked (il), so
        # no tap reaches into the other field.
        fields = "il=l=d:c=d:a=d," if info.interlaced else ""
        chains = [f"[0:{self.index}]{fields}{_WIDEN}:in_chroma_loc={info.chroma_loc}:"
                  "flags=bilinear+full_chroma_int+accurate_rnd,format=yuv444p16[lv_src]"]
        chains += _r103_exact("lv_src", "lv_all", "yuv444p16", "yuv444p")
        # Masks are computed per plane (no plane re-packing at full size)
        # and area-reduced 4:1 before they are packed for one signalstats
        # call: a 0/255 mask's mean (the violating share) survives.
        reduce4 = "scale=w='max(1,trunc(iw/4))':h='max(1,trunc(ih/4))':flags=area"
        count = len(matrices)
        chains.append("[lv_all]extractplanes=y+u+v[lv_y][lv_u][lv_v]")
        chains.append(f"[lv_y]split={3 * count + 3}" + "".join(f"[y{i}]" for i in range(3 * count + 3)))
        chains.append(f"[lv_u]split={2 * count}" + "".join(f"[u{i}]" for i in range(2 * count)))
        chains.append(f"[lv_v]split={2 * count}" + "".join(f"[v{i}]" for i in range(2 * count)))
        for k, matrix in enumerate(matrices):
            red, blue, helper, green = _rgb_mask_expressions(info, matrix)
            chains += [
                f"[y{3 * k}][v{2 * k}]lut2=c0='{red}'[g{k}_r]",
                f"[y{3 * k + 1}][u{2 * k}]lut2=c0='{blue}'[g{k}_b]",
                f"[u{2 * k + 1}][v{2 * k + 1}]lut2=c0='{helper}'[g{k}_w]",
                f"[g{k}_w][y{3 * k + 2}]lut2=c0='{green}'[g{k}_g]",
            ]
        planes = []
        for c in "rgb":
            # A pixel counts only if outside under every candidate matrix.
            label = f"[g0_{c}]"
            for k in range(1, count):
                chains.append(f"{label}[g{k}_{c}]lut2=c0='min(x,y)'[gand{k}_{c}]")
                label = f"[gand{k}_{c}]"
            chains.append(f"{label}{reduce4}[gr_{c}]")
            planes.append(f"[gr_{c}]")
        pack = "mergeplanes=map0s=0:map0p=0:map1s=1:map1p=0:map2s=2:map2p=0:format=yuv444p"
        chains.append("".join(planes) + f"{pack},signalstats,{self._share_print('Y', 'gam_r')},"
                      f"{self._share_print('U', 'gam_g')},{self._share_print('V', 'gam_b')},nullsink")
        m1, m2, m3 = _luma_mask_expressions(info)
        for i, mask in enumerate((m1, m2, m3)):
            chains.append(f"[y{3 * count + i}]lut=c0='{mask}',{reduce4}[lm{i}]")
        chains.append(f"[lm0][lm1][lm2]{pack},signalstats,{','.join(self._luma_prints())},null[vs_lv]")
        return chains

    def _area_chains(self) -> List[str]:
        """Mean luma and the flash grid from one area-averaged copy.

        Few, cheap filters on a copy at most ANALYSIS_MAX_WIDTH wide: the
        graph runs serially per frame, so filter count matters more than
        pixel count.
        """
        info, c = self.info, self.config
        rng = info.range_name
        uses = []
        if c.black or c.levels or c.freeze:
            uses.append("mean")
        if self.flash_enabled:
            uses.append("flash")
        labels = "".join(f"[ar_{u}]" for u in uses)
        chains = [
            f"[0:{self.index}]scale=w='max(2,min({ANALYSIS_MAX_WIDTH},trunc(iw/{2 * ANALYSIS_MIN_FACTOR})*2))'"
            f":h=-2:flags=area:in_range={rng}:out_range={rng},format=yuv444p,"
            f"split={len(uses)}{labels}"]
        if c.black or c.levels or c.freeze:
            # Without the flash output this branch is the graph's output.
            tail = "nullsink" if self.flash_enabled else "null[vs_area]"
            # The freeze check's structural change: |difference| to the
            # previous frame on this copy (at least 32x averaged per side).
            chains.append(
                f"[ar_mean]scale=w='max(1,trunc(iw/{FREEZE_COPY_FACTOR}))':"
                f"h='max(1,trunc(ih/{FREEZE_COPY_FACTOR}))':flags=area,split=2[ar_mean_y][ar_mean_d]")
            if c.black:
                # Non-black share per frame, and the share non-black in both
                # this frame and the one overlay_step_frames earlier (every
                # that many frames): a static overlay (BLACK_OVERLAY_MAX).
                # The mask is taken on native pixels, as blackdetect sees
                # them, point-sampled every BLACK_OVERLAY_BLOCK pixels: an
                # unbiased sample of the shares (averaging would hide the
                # grain that keeps a dark scene above black), at 1/64 of
                # the work.
                level = black_pixel_level(c.black_pix_th, rng == "pc")
                chains += [
                    # The sampled mask is then area-reduced by an exact 8:1
                    # per side before signalstats (a mask's mean survives),
                    # which keeps signalstats' per-frame cost negligible.
                    # Only every overlay_sample_frames-th frame is measured
                    # (a tenth of a second): the rule needs a run's extent,
                    # not every frame, and filters cost per frame.
                    f"[0:{self.index}]select='not(mod(n,{self.overlay_sample_frames}))',extractplanes=y,"
                    f"scale=w='max(8,trunc(iw/{8 * BLACK_OVERLAY_BLOCK})*8)':"
                    f"h='max(8,trunc(ih/{8 * BLACK_OVERLAY_BLOCK})*8)':flags=neighbor,"
                    "split=3[bk_y][bk_b][bk_k]",
                    f"[bk_y]lut=c0='if(gt(val,{level}),255,0)',split=2[bk_n][bk_s]",
                    # Graphic-like test: the share of samples at or above a
                    # graphic's fill (BLACK_GRAPHIC_LEVEL); true-black test:
                    # the mean code of the black samples (sum over all
                    # samples of the black ones' codes).
                    f"[bk_b]lut=c0='if(gt(val,{black_pixel_level(BLACK_GRAPHIC_LEVEL, rng == 'pc')}),255,0)',"
                    f"scale=w=iw/8:h=ih/8:flags=area,signalstats,"
                    f"{_print('lavfi.signalstats.YAVG', self.paths['blk_b'])},nullsink",
                    f"[bk_k]lut=c0='if(gt(val,{level}),0,val)',scale=w=iw/8:h=ih/8:flags=area,signalstats,"
                    f"{_print('lavfi.signalstats.YAVG', self.paths['blk_k'])},nullsink",
                    f"[bk_n]scale=w=iw/8:h=ih/8:flags=area,signalstats,"
                    f"{_print('lavfi.signalstats.YAVG', self.paths['blk_n'])},nullsink",
                    f"[bk_s]select='not(mod(n,{self.overlay_step_frames // self.overlay_sample_frames}))',"
                    "tblend=all_expr='min(A,B)',"
                    f"scale=w=iw/8:h=ih/8:flags=area,signalstats,"
                    f"{_print('lavfi.signalstats.YAVG', self.paths['blk_s'])},nullsink",
                ]
            # And to the frame freeze_step_frames earlier (every that many
            # frames): accumulated change, which a slow pan shows.
            step = self.freeze_step_frames
            chains.append("[ar_mean_d]split=2[ar_d1][ar_d2]")
            chains.append(
                "[ar_d1]tblend=all_mode=difference,signalstats,"
                f"{_print('lavfi.signalstats.YAVG', self.paths['struct'])},nullsink")
            chains.append(
                f"[ar_d2]select='not(mod(n,{step}))',tblend=all_mode=difference,signalstats,"
                f"{_print('lavfi.signalstats.YAVG', self.paths['struct_step'])},nullsink")
            # Every frame's mean luma: the edit-gap test compares black
            # frames with their neighbours at whatever level the scene sits.
            chains.append(
                f"[ar_mean_y]scale=w='max(1,trunc(iw/2))':h='max(1,trunc(ih/2))':flags=area,"
                f"signalstats,{_print('lavfi.signalstats.YAVG', self.paths['yavg'])},{tail}")
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
            # Patterns finer than a block (an inverting checkerboard) keep
            # every block's mean light constant. The alpha plane therefore
            # carries, per sample of the fine copy, whether its luminance
            # made a flash transition from the previous frame (a change of
            # FLASH_DELTA_CD with the darker state below FLASH_DARK_CD);
            # area-averaged to the grid it is each block's share of samples
            # in transition (FLASH_FINE). tblend's first output is the
            # second frame: the grid starts one frame late (FLASH_GRID_OFFSET).
            delta = FLASH_DELTA_CD * 255.0 / FLASH_PEAK_CD
            dark = FLASH_DARK_CD * 255.0 / FLASH_PEAK_CD
            chains.append(
                f"[ar_flash]scale=w={cols * sub}:h={rows * sub}:flags=area:"
                f"in_color_matrix={info.matrix}:in_range={rng}:out_range=pc,"
                f"format=gbrap,lutrgb=r='{linear}':g='{linear}':b='{linear}',"
                "colorchannelmixer=aa=0:ar=0.2126:ag=0.7152:ab=0.0722,"
                "tblend=c0_mode=normal:c1_mode=normal:c2_mode=normal:"
                f"c3_expr='if(gte(abs(A-B),{delta:.4g})*lt(min(A,B),{dark:.4g}),255,0)',"
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
                "-c:v", "rawvideo", "-pix_fmt", "gbrap", "-f", "segment",
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
            if not os.path.exists(self.paths[name]):
                # The metadata filter creates its file when the graph starts,
                # so a missing file means this measurement never ran.
                series.missing.add(name)
            values = [default] * len(master)
            for entry in read_print_entries(self.paths[name], cancel):
                position = table.get(entry[key])
                if position is not None and entry[3] != "":
                    values[position] = convert(entry[3])
            return values

        if self.needs_interlace_branch:
            series.idet = sparse("idet", "", str)
            series.idet_single = sparse("idet1", "", str)
            top, bottom = sparse("rep_top", "", str), sparse("rep_bottom", "", str)
            series.repeated = [t or b for t, b in zip(top, bottom)]
            def keyed(name: str) -> Dict[int, float]:
                values = {}
                for position, value in enumerate(sparse(name, None, lambda v: _to_float(v, None),
                                                        by_pts, 1)):
                    if value is not None:
                        values[position] = value
                return values
            same, top, bottom, move = keyed("ft_same"), keyed("ft_top"), keyed("ft_bot"), keyed("ft_move")
            # The analysed frames, in order: tblend pairs each with the one
            # before it in this selection, and parity is the selection index.
            selected = sorted(same)
            timing = field_timings(len(master), selected, same, top, bottom, move)
            col = sparse("colmo", None, lambda v: _to_float(v, None), by_pts, 1)
            row = sparse("rowmo", None, lambda v: _to_float(v, None), by_pts, 1)
            errors = [sparse(_disp_name(d), None, lambda v: _to_float(v, None), by_pts, 1)
                      for d in INTERLACE_DISPLACEMENTS]
            vertical = [vertical_displacement(dict(zip(INTERLACE_DISPLACEMENTS, values)))
                        for values in zip(*errors)]
            shift = {k: keyed(_shift_name(k))
                     for k in range(-INTERLACE_SCROLL_RANGE, INTERLACE_SCROLL_RANGE + 1)}
            order = {position: index for index, position in enumerate(selected)}
            speed = [scroll_speed({k: v.get(n) for k, v in shift.items()}, order[n])
                     if n in order else None for n in range(len(master))]
            series.displacement = [field_verdict(t, v, c, r, sp)
                                   for t, v, c, r, sp in zip(timing, vertical, col, row, speed)]
        if self.needs_levels_branch:
            def fraction(name: str) -> List[float]:
                # Each frame went to one part (by pts: frames were selected).
                merged = [0.0] * len(master)
                for part in range(self.levels_parts):
                    values = sparse(f"{name}@{part}", 0.0, lambda v: _to_float(v) / 255.0, by_pts, 1)
                    if f"{name}@{part}" in series.missing:
                        series.missing.add(name)
                    merged = [a + b for a, b in zip(merged, values)]
                return merged
            series.luma, series.band1, series.band2 = fraction("luma"), fraction("band1"), fraction("band2")
            # The largest single-component share: never above the union of
            # R, G and B violations, so it can only under-report.
            series.gam = [max(r, g, b) for r, g, b in
                          zip(fraction("gam_r"), fraction("gam_g"), fraction("gam_b"))]
        if self.needs_area_branch and (self.config.black or self.config.levels or self.config.freeze):
            series.yavg = sparse("yavg", YAVG_ABSENT, _to_float)
            if self.config.black:
                share = lambda v: _to_float(v, 0.0) / 255.0
                series.nonblack = sparse("blk_n", None, share, by_pts, 1)
                series.nonblack_static = sparse("blk_s", None, share, by_pts, 1)
                series.bright = sparse("blk_b", None, share, by_pts, 1)
                series.black_sum = sparse("blk_k", None, lambda v: _to_float(v, 0.0), by_pts, 1)
                series.black_level = black_pixel_level(0.0, self.info.full_range)
                series.overlay_step = self.overlay_step_frames
                series.overlay_sample = self.overlay_sample_frames
            # tblend's frame k (k >= 1) carries frame k's pts: matched by pts,
            # in % of full scale like scdet's MAFD.
            series.structural = sparse("struct", None, lambda v: _to_float(v, 0.0) / 2.55, by_pts, 1)
            series.structural_step = sparse("struct_step", None, lambda v: _to_float(v, 0.0) / 2.55,
                                            by_pts, 1)
        if self.config.black:
            series.black_runs = black_runs(self.paths["black_start"], self.paths["black_end"],
                                           cancel)
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


def black_runs(start_path: str, end_path: str,
               cancel: Optional[threading.Event] = None) -> Optional[List[Tuple[float, Optional[float]]]]:
    """Black runs from blackdetect's per-frame marks, or None without the files.

    blackdetect tags the first black frame of a run with lavfi.black_start
    and the first frame after it with lavfi.black_end (with d=0, every run).
    A run still open at the end has no end mark (None).
    """
    if not (os.path.exists(start_path) and os.path.exists(end_path)):
        return None
    starts = [e[2] for e in read_print_entries(start_path, cancel)]
    ends = [e[2] for e in read_print_entries(end_path, cancel)]
    runs: List[Tuple[float, Optional[float]]] = []
    k = 0
    for start in starts:
        while k < len(ends) and ends[k] <= start:
            k += 1
        runs.append((start, ends[k] if k < len(ends) else None))
        if k < len(ends):
            k += 1
    return runs


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
    idet: List[str] = field(default_factory=list)          # multiple-frame call
    idet_single: List[str] = field(default_factory=list)   # single-frame call
    repeated: List[str] = field(default_factory=list)      # "top"/"bottom"/""
    displacement: List[str] = field(default_factory=list)  # field timing: "woven"/"progressive"/""
    luma: List[float] = field(default_factory=list)   # tv: outside R 103; pc: extremes used
    gam: List[float] = field(default_factory=list)       # outside under every candidate matrix
    band1: List[float] = field(default_factory=list)
    band2: List[float] = field(default_factory=list)
    yavg: List[float] = field(default_factory=list)
    # black: non-black share per frame, and the share non-black in both the
    # frame and the one overlay_step frames earlier (on those frames)
    nonblack: List[Optional[float]] = field(default_factory=list)
    nonblack_static: List[Optional[float]] = field(default_factory=list)
    bright: List[Optional[float]] = field(default_factory=list)      # share at a graphic's fill level
    black_sum: List[Optional[float]] = field(default_factory=list)   # mean over samples of black codes
    black_level: int = 16
    overlay_step: int = 0
    overlay_sample: int = 1
    structural: List[Optional[float]] = field(default_factory=list)  # freeze: averaged-copy change
    # freeze: averaged-copy change to the frame freeze_step_frames earlier (on those frames)
    structural_step: List[Optional[float]] = field(default_factory=list)
    flash: List[tuple] = field(default_factory=list)   # in-memory grid (tests, batch use)
    flash_stream: Optional["FlashAccumulator"] = None   # streamed grid (scans)
    flash_error: Optional[str] = None
    # (start, end or None while open) black runs from blackdetect's frame
    # marks; None when FFmpeg wrote no mark file (the detector did not run).
    black_runs: Optional[List[Tuple[float, Optional[float]]]] = None
    # Measurement files FFmpeg did not write (the measurement never ran).
    missing: set = field(default_factory=set)

    def align(self) -> None:
        """Truncate every non-empty series to the shortest one.

        All branches see the same decoded frames in the same order, so a
        length difference only arises when FFmpeg stopped mid-flush; the
        common prefix is the part every branch measured.
        """
        names = ("times", "mafd", "idet", "idet_single", "repeated", "displacement", "luma",
                 "gam", "band1", "band2", "yavg", "structural", "structural_step", "nonblack",
                 "nonblack_static", "bright", "black_sum", "flash")
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
    segments: List[Interval]        # >= black_detect_duration
    bursts: List[Tuple[float, int]]  # (start, frames) hard-cut short bursts
    ignored_bursts: int              # short runs judged fades or strobes
    edge_segments: List[Interval] = field(default_factory=list)  # lead-in / run-out black


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


def _hard_side(inside: float, near: Optional[float], far: Optional[float]) -> bool:
    """One side of a black burst is a cut (see BLACK_STEP_CODES)."""
    if near is None:
        return False
    step = near - inside
    if step < BLACK_STEP_CODES:
        return False
    return far is None or abs(near - far) <= step / 2


def black_pixel_level(pix_th: float, full_range: bool) -> int:
    """8-bit code at and below which a pixel is black (blackdetect's scaling)."""
    return int(pix_th * 255) if full_range else int(16 + pix_th * (235 - 16))


def overlay_black_runs(nonblack: Sequence[Optional[float]], static: Sequence[Optional[float]],
                       times: Sequence[float], step: int, pic_th: float,
                       period: float, sample: int = 1,
                       bright: Optional[Sequence[Optional[float]]] = None,
                       black_sum: Optional[Sequence[Optional[float]]] = None,
                       black_level: float = 16.0) -> List[Tuple[float, float]]:
    """(start, end) runs that are black apart from a static overlay.

    ``nonblack`` holds a value on every ``sample``-th frame (None between).
    A measured frame qualifies when its non-black share is at most
    BLACK_OVERLAY_MAX plus the picture threshold's allowance, the overlay is
    graphic-like and the rest is true black (``bright`` / ``black_sum``, see
    BLACK_GRAPHIC_SHARE and BLACK_TRUE_MARGIN; not tested when absent); consecutive
    qualifying measurements form a run, which counts when every pair of
    frames ``step`` apart inside it shows the overlay in place: the share
    non-black in both (``static``) is at most BLACK_OVERLAY_MAX and what is
    non-black beyond it fits the picture threshold's allowance of the rest
    of the picture. A run too short to hold one pair cannot show that and
    does not count. A run spans its first to its last measured frame (up to
    a sample short at each end, never longer than the black).
    """
    allowance = 1.0 - pic_th
    count = min(len(nonblack), len(times))
    measured = [i for i in range(count) if nonblack[i] is not None]

    def candidate(i: int) -> bool:
        share = nonblack[i]
        if share > BLACK_OVERLAY_MAX + allowance:
            return False
        if black_sum is not None:
            total = black_sum[i] if i < len(black_sum) else None
            if total is None or share >= 1.0:
                return False
            if total / (1.0 - share) > black_level + BLACK_TRUE_MARGIN:
                return False                    # dark, not true black
        if bright is not None and share > 0:
            fill = bright[i] if i < len(bright) else None
            if fill is None or fill < BLACK_GRAPHIC_SHARE * share:
                return False                    # soft light, not a graphic
        return True

    runs = []
    k = 0
    while k < len(measured):
        if not candidate(measured[k]):
            k += 1
            continue
        j = k
        while (j + 1 < len(measured) and measured[j + 1] - measured[j] <= sample
               and candidate(measured[j + 1])):
            j += 1
        first, last = measured[k], measured[j]
        pairs = [i for i in range(first + step, last + 1)
                 if i < len(static) and static[i] is not None and nonblack[i - step] is not None]
        if step > 0 and pairs and all(
                static[i] <= BLACK_OVERLAY_MAX
                and max(nonblack[i], nonblack[i - step]) - static[i] <= allowance * (1.0 - static[i])
                for i in pairs):
            runs.append((times[first], times[last] + period))
        k = j + 1
    return runs


def merge_runs(*groups: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Union of (start, end) runs."""
    merged: List[List[float]] = []
    for start, end in sorted(r for group in groups for r in group):
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def classify_black(events: Sequence[Tuple[float, float]], duration: float, fps: float,
                   cfg: VideoCheckConfig, times: Sequence[float] = (),
                   yavg: Sequence[float] = ()) -> BlackFindings:
    """Split blackdetect runs into black segments and short hard-cut bursts.

    ``events`` are (start, end) runs of pictures with at least
    ``black_detect_picture_threshold`` of their pixels at or below the
    ``black_detect_pix_threshold`` level (Y <= 20 at 8-bit limited range by
    default; FFmpeg scales the level for full range and higher bit depths).

    * Black that opens the file (from its first frame) or closes it (to its
      last frame) is a lead-in or run-out: fades from and to black there are
      normal. Up to BLACK_EDGE_ALLOWANCE of it is noted, not judged; black
      that starts after the picture has appeared is judged wherever it is.
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
    edges: List[Interval] = []
    first_time = times[0] if times else 0.0
    for position, (start, length) in enumerate(runs):
        begin, finish = start, start + length
        if edge and start <= first_time + period * 0.5:         # opens the file
            begin = min(finish, max(start, edge))
            edges.append((start, begin - start))
        if edge and finish >= duration - period * 0.5:           # closes the file
            cut = max(begin, min(finish, duration - edge))
            edges.append((cut, finish - cut))
            finish = cut
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
        inside = _mean_luma_at(times, yavg, start + period * 0.25)
        before = _mean_luma_at(times, yavg, start - period * 0.5)
        before2 = _mean_luma_at(times, yavg, start - period * 1.5)
        after = _mean_luma_at(times, yavg, start + length + period * 0.25)
        after2 = _mean_luma_at(times, yavg, start + length + period * 1.25)
        hard_cut = (inside is not None and _hard_side(inside, before, before2)
                    and _hard_side(inside, after, after2))
        if isolated and hard_cut:
            bursts.append((start, frames))
        else:
            ignored += 1
    return BlackFindings(segments, bursts, ignored, [e for e in edges if e[1] > period * 0.5])


def black_entry(findings: BlackFindings, duration: float,
                cfg: VideoCheckConfig) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Black Frames row."""
    level = f"≥{cfg.black_pic_th * 100:g}% of the picture at black level"
    edge_note = (f" Lead-in/run-out black within the {BLACK_EDGE_ALLOWANCE:g}s allowance: "
                 f"{event_list(findings.edge_segments)}." if findings.edge_segments else "")
    if not findings.segments and not findings.bursts:
        note = (f" ({findings.ignored_bursts} brief dip(s)/flash(es) to black ignored)"
                if findings.ignored_bursts else "")
        return "PASS", f"No black segments detected{note}.{edge_note}", 100
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
    return status, ". ".join(parts) + "." + edge_note, confidence


# ---------------------------------------------------------------------------
# Freeze frames
# ---------------------------------------------------------------------------

@dataclass
class FreezeFindings:
    holds: List[Interval]           # freezes to report
    edge_holds: List[Interval]      # static pictures at the head (slates)
    black_holds: int = 0            # candidates that were black (Black Frames' job)
    edge_dominant: bool = False     # a head/tail hold covers half the file
    composed: List[Interval] = field(default_factory=list)  # held shots within the allowance


def _still_runs(still: Sequence[bool], mafd: Sequence[float]) -> List[Tuple[int, int]]:
    """Runs of still frames, bridging long-GOP refresh blips.

    ``still[i]`` says frame i shows no change from frame i-1. A digitally
    frozen picture re-encoded by a long-GOP encoder is not bit identical:
    every I-frame re-codes it, which shows as one to three frames of small
    change (requantisation noise, see FREEZE_BLIP_MAFD), and a starved CBR
    encoder spends the first few frames refining the picture.
    Up to FREEZE_MAX_BLIP_FRAMES consecutive moderate-change frames (below
    FREEZE_BLIP_MAFD, far under real motion) do not end a run. Returns
    (first, last) indices; the frozen picture itself starts one frame earlier.
    """
    runs: List[Tuple[int, int]] = []
    start = last = None
    blips = 0
    for i in range(1, min(len(still), len(mafd))):
        if still[i]:
            if start is None:
                start = i
            last, blips = i, 0
        elif start is not None and mafd[i] < FREEZE_BLIP_MAFD and blips < FREEZE_MAX_BLIP_FRAMES:
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


def structural_limit(mafd: float, side_factor: float) -> float:
    """Largest structural change grain alone explains (see FREEZE_GRAIN_CORRELATION)."""
    return FREEZE_STRUCTURAL_FLOOR + mafd * FREEZE_GRAIN_CORRELATION / max(side_factor, 1.0)


def freeze_step_frames(fps: float, min_duration: float) -> int:
    """Frames between accumulated-change samples: FREEZE_STEP_FRACTION of the
    minimum freeze duration, rounded down so two steps never exceed it."""
    if fps <= 0:
        fps = 25.0
    return max(1, int(fps * min_duration * FREEZE_STEP_FRACTION + 1e-9))


def _static_segments(first: int, last: int, moved: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Maximal sub-runs of [first, last] that contain no moved pair (a, b) entirely."""
    inside = sorted((a, b) for a, b in moved if first <= a and b <= last)
    segments = []
    start = first
    for a, b in inside:
        if a >= start:
            segments.append((start, b - 1))
            start = a + 1
    segments.append((start, last))
    # Drop segments contained in a neighbour (overlapping windows).
    return [seg for k, seg in enumerate(segments)
            if not any(o != seg and o[0] <= seg[0] and seg[1] <= o[1] for o in segments)]


def _in_black(t: float, black: Sequence[Interval], tolerance: float) -> bool:
    return any(start - tolerance <= t <= start + length + tolerance for start, length in black)


def _cut(mafd: Sequence[float], i: int) -> bool:
    """True when frame i is a scene change (scdet's score at its default threshold)."""
    if i <= 0 or i >= len(mafd):
        return False
    return min(mafd[i], abs(mafd[i] - mafd[i - 1])) >= FREEZE_CUT_SCORE


def detect_freezes(mafd: Sequence[float], times: Sequence[float], fps: float,
                   min_duration: float, duration: float,
                   black: Sequence[Interval] = (),
                   structural: Optional[Sequence[Optional[float]]] = None,
                   side_factor: float = 16.0,
                   hold_allowance: float = 0.0,
                   structural_step: Optional[Sequence[Optional[float]]] = None,
                   step: int = 0) -> FreezeFindings:
    """Frozen pictures, told apart from held shots and quiet scenes.

    1. Still frames: no structural change beyond what grain explains
       (structural_limit), so a freeze under finishing grain is still
       found; without the structural series, MAFD under FREEZE_STILL_MAFD.
       Runs bridge long-GOP refresh blips (_still_runs).
    2. A frozen picture is static, not just slow: within a run, frames
       ``step`` apart (``structural_step``, sampled every ``step`` frames)
       must also differ by no more than grain. A run is cut where they do,
       so a slow pan never forms a run of ``min_duration``.
    3. A run that starts on a cut and ends on a cut (or at the end) is a
       composed shot - a title card, an end card, a locked-off or quiet
       scene - not a freeze: by definition a freeze is not creative intent.
       A dip through black is an edit too. Such a shot counts as a freeze
       only when its frames are identical (FREEZE_IDENTICAL_MAFD) and it is
       held longer than ``hold_allowance`` (a spec setting: a long
       identical still is worth checking).
    4. Any other run is a freeze when the picture stopped dead in mid-shot:
       motion runs up to it (FREEZE_LEAD_FRAMES), or its frames are
       identical. A shot that slows to a standstill is not.
    5. Runs at the very head are slates (unless they cover half the file);
       a run reaching the end is judged like any other, so a render that
       ran out of media and froze its last seconds is reported.
    Runs mostly covered by black are left to Black Frames.
    """
    period = _frame_period(fps, times)
    holds: List[Interval] = []
    edges: List[Interval] = []
    composed: List[Interval] = []
    black_holds = 0
    edge_dominant = False
    count = min(len(mafd), len(times))
    structure = list(structural[:count]) if structural is not None else None
    if structure is not None and len(structure) < count:
        structure += [None] * (count - len(structure))

    def limit(i: int) -> float:
        return structural_limit(mafd[i], side_factor)

    def still(i: int) -> bool:
        if structure is not None and structure[i] is not None:
            return structure[i] <= limit(i)
        return mafd[i] < FREEZE_STILL_MAFD

    def edit(i: int) -> bool:
        """Frame i changes picture by an edit: a cut, or it is (next to) black."""
        if _cut(mafd, i):
            return True
        return 0 <= i < count and bool(black) and _in_black(times[i], black, period * 0.5)

    flags = [still(i) for i in range(count)]
    runs = []
    for first, last in _still_runs(flags, mafd[:count]):
        if structural_step is None or step <= 0:
            runs.append((first, last))
            continue
        frozen_from = first - 1
        # Between frames j-step and j a static picture changes by no more
        # than the noisiest frame in between explains, and by no more than
        # an encoder's re-coding of a held picture (one refresh blip,
        # FREEZE_BLIP_MAFD, as the averaged copy sees it).
        recode = structural_limit(FREEZE_BLIP_MAFD, side_factor)
        moved = [(j - step, j) for j in range(max(step, frozen_from + step), last + 1)
                 if j < len(structural_step) and structural_step[j] is not None
                 and structural_step[j] > max(recode, max(limit(i) for i in range(j - step + 1, j + 1)))]
        runs += [(a + 1, b) for a, b in _static_segments(frozen_from, last, moved) if b > a]
    for first, last in runs:
        frozen_from = first - 1
        span = _interval_of(times, frozen_from, last, period)
        if span[1] < min_duration - period * 0.5:
            continue
        if black and _overlap(span, black) >= 0.5 * span[1]:
            black_holds += 1
            continue
        identical = statistics.median(mafd[first:last + 1]) <= FREEZE_IDENTICAL_MAFD
        # At the head: from the first frame, allowing an encoder's opening
        # refinement of the picture (blips, as within a run).
        at_head = frozen_from <= 0 or (
            frozen_from <= FREEZE_MAX_BLIP_FRAMES
            and all(mafd[i] < FREEZE_BLIP_MAFD for i in range(1, frozen_from + 1)))
        at_tail = last >= count - 2
        covers_half = duration > 0 and span[1] >= 0.5 * duration
        if at_head and not covers_half:
            edges.append(span)
            continue
        enters_on_cut = at_head or edit(frozen_from) or edit(frozen_from - 1)
        leaves_on_cut = at_tail or edit(last + 1)
        if enters_on_cut and leaves_on_cut and not covers_half:
            if identical and span[1] > hold_allowance + period * 0.5:
                holds.append(span)
            else:
                composed.append(span)
            continue
        lead = range(max(1, frozen_from - FREEZE_LEAD_FRAMES + 1), frozen_from + 1)
        abrupt = bool(lead) and all(not flags[i] for i in lead)
        if abrupt and structure is not None and all(structure[i] is not None for i in lead):
            noise = statistics.median(structure[i] or 0.0 for i in range(first, last + 1))
            margin = statistics.median(limit(i) for i in range(first, last + 1)) - noise
            margin = max(margin, FREEZE_STRUCTURAL_FLOOR)
            motion = statistics.median(structure[i] for i in lead) - noise
            span_back = step if step > 0 else freeze_step_frames(fps, min_duration)
            earlier = [structure[i] for i in range(max(1, frozen_from - span_back - FREEZE_LEAD_FRAMES + 1),
                                                   frozen_from - span_back + 1)
                       if structure[i] is not None]
            steady = bool(earlier) and motion >= (statistics.median(earlier) - noise) / FREEZE_MOVING_FACTOR
            abrupt = motion >= FREEZE_MOVING_FACTOR * margin or (motion >= margin and steady)
        if abrupt or identical:
            holds.append(span)
            if covers_half and (at_head or at_tail):
                edge_dominant = True
    return FreezeFindings(holds, edges, black_holds, edge_dominant, composed)


def freeze_entry(findings: FreezeFindings, duration: float,
                 cfg: VideoCheckConfig) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Freeze Frames row."""
    notes = ""
    if findings.edge_holds:
        notes += f" Static picture at the head treated as a slate: {event_list(findings.edge_holds)}."
    if findings.composed:
        notes += (f" Held shot(s) starting and ending on a cut, within the {cfg.freeze_hold:g}s "
                  f"hold allowance or not identical: {event_list(findings.composed)}.")
    if not findings.holds:
        return "PASS", f"No freezes ≥{cfg.freeze_duration:g}s detected.{notes}", 100
    total = sum(d for _, d in findings.holds)
    detail = (f"{len(findings.holds)} frozen picture(s) ≥{cfg.freeze_duration:g}s, "
              f"total {total:.1f}s. At: {event_list(findings.holds)}.{notes}")
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

    * Tagged limited: deep sub-black (<= code 10 after the R 103 filter) over more
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
    """EBU R 103 v3.0 luma and RGB-gamut tolerance on the R 103-filtered picture.

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
    # Near the 1 % limit coding noise can tip the share either way: keep
    # confidence below 70 there.
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


_FLASH_KINDS = ("general", "red", "fine")
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
    FRAME_BYTES = 3 * PLANE          # an in-memory gbrp grid; the stream is gbrap (4 planes)

    def __init__(self, fps: float, planes: int = 3, offset: int = 0):
        self.planes = planes
        self.FRAME_BYTES = planes * self.PLANE
        self.offset = offset             # grid frame k is source frame k + offset
        self.fine_share: List[Dict[int, float]] = [{} for _ in range(self.PLANE)]
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
            if self.planes == 4:
                shares = self.fine_share[b]
                for k, a in enumerate(data[3 * plane + b::stride]):
                    if a:
                        shares[start + k] = a / 255.0
                        self._swing("fine", b, start + k)
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
        horizon = through - self.span_frames
        for b, shares in enumerate(self.fine_share):
            if shares and min(shares) < horizon:
                committed = set(self.committed["fine"][b].values())
                for f in [f for f in shares if f < horizon and f not in committed]:
                    del shares[f]
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
        times = times[self.offset:]
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
            # fine: a block's weight over a hazardous run is the smallest
            # share of its samples in transition at any of the run's seven
            # transitions - the area that flashed every time.
            fine: Dict[Tuple[int, int], float] = {}
            for b in range(blocks):
                check_cancel(cancel)
                for first, last, marks in self._exact_spans(kind, b, times, frames, window):
                    weight = (min(self.fine_share[b].get(m, 0.0) for m in marks)
                              if kind == "fine" else 1.0)
                    for f in range(first, last + 1):
                        members = hazard.setdefault(f, [])
                        if not members or members[-1] != b:
                            members.append(b)
                        if kind == "fine":
                            fine[(f, b)] = max(fine.get((f, b), 0.0), weight)
            candidates = sorted(f for f, members in hazard.items()
                                if len(members) > FLASH_AREA * blocks)
            for first, last in _consecutive_runs(candidates):
                check_cancel(cancel)
                members = sorted({b for f in range(first, last + 1) for b in hazard[f]})
                if kind == "fine":
                    areas = [sum(fine.get((f, b), 0.0) for b in hazard[f]) / blocks
                             for f in range(first, last + 1)]
                else:
                    weights = self._coverage_weights(
                        self._amplitudes(kind, range(first, last + 1), members))
                    areas = [sum(weights[b] for b in hazard[f]) / blocks
                             for f in range(first, last + 1)]
                flagged = [first + k for k, area in enumerate(areas) if area > FLASH_AREA]
                for run_first, run_last in _consecutive_runs(flagged):
                    peak = max(areas[run_first - first:run_last - first + 1])
                    events.append(FlashEvent(times[run_first], times[run_last] + period, peak,
                                             "red" if kind == "red" else "general"))
        # The block-mean and fine channels can both see one flash: merge
        # overlapping events of a rule.
        merged: List[FlashEvent] = []
        for event in sorted(events, key=lambda e: (e.kind, e.start)):
            last = merged[-1] if merged else None
            if last is not None and last.kind == event.kind and event.start <= last.end:
                merged[-1] = FlashEvent(last.start, max(last.end, event.end),
                                        max(last.area, event.area), last.kind)
            else:
                merged.append(event)
        return sorted(merged, key=lambda e: e.start)

    def _exact_spans(self, kind: str, block: int, times: Sequence[float], frames: int,
                     window: float) -> List[Tuple[int, int, List[int]]]:
        """(first, last, swing frames) of each run of seven consecutive swings < 1 s.

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
                spans.append((first, last, [committed[seqs[k + j]] for j in range(need)]))
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
        planes = len(series.flash[0]) if series.flash else 3
        accumulator = FlashAccumulator(fps, planes=planes)
        accumulator.feed(b"".join(b"".join(frame) for frame in series.flash))
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


def field_timings(frames: int, selected: Sequence[int], same: Dict[int, float],
                  top: Dict[int, float], bottom: Dict[int, float],
                  move: Dict[int, float]) -> List[str]:
    """field_timing() per frame position from the field-timing series.

    ``selected``: the analysed frame positions in order (idet's TFF/BFF
    frames). ``same[n]`` is d(Ti_n, B_n). With Z = [Ti; B] and the halves
    swapped on odd selection indices, tblend at analysed frame m (previous
    analysed frame n) pairs B_m with Ti_n in one half and Ti_m with B_n in
    the other: n's comparisons with the next field for either field order.
    ``move[m]`` is the top-field change from n to m. Only consecutive
    frames pair.
    """
    out = [""] * frames
    for j in range(len(selected) - 1):
        n, m = selected[j], selected[j + 1]
        if m != n + 1:
            continue
        # The two halves hold d(Ti_m, B_n) and d(B_m, Ti_n) in an order set
        # by the selection index's parity; field_timing treats its two
        # neighbour comparisons alike, so the order does not matter.
        out[n] = field_timing(same.get(n), top.get(m), bottom.get(m), move.get(m), move.get(m))
    return out


def field_timing(same: Optional[float], following: Optional[float],
                 other_order: Optional[float], moved_after: Optional[float] = None,
                 moved_before: Optional[float] = None) -> str:
    """'woven', 'progressive' or '' for one frame (see FIELD_TIMING_COLUMNS).

    ``same`` is d(T_n, B_n), the frame's own two fields; ``following`` is
    d(B_n, T_n+1) and ``other_order`` d(T_n, B_n+1): the next field in time
    if the source is top-first or bottom-first respectively. In a woven
    frame one of them is half a frame from its neighbour, like ``same``; in
    a progressive frame both are a whole frame away. ``moved_after`` /
    ``moved_before`` is the picture's change to the frame that the nearer
    comparison uses: a repeated frame decides nothing.

    Motion must show (the larger comparison exceeds ``same`` by
    INTERLACE_DECISIVE_RATIO). Progressive: both exceed ``same`` by that
    ratio. Woven: the nearer one is within that ratio of ``same`` and the
    picture really changed on that side by as much. Otherwise undecided.
    """
    if same is None or following is None or other_order is None:
        return ""
    near, far = min(following, other_order), max(following, other_order)
    if far < INTERLACE_DECISIVE_RATIO * same:
        return ""
    if near >= INTERLACE_DECISIVE_RATIO * same:
        return "progressive"
    moved = moved_after if following <= other_order else moved_before
    if moved is not None and moved < INTERLACE_DECISIVE_RATIO * same:
        return ""
    return "woven"


def vertical_displacement(errors: Dict[int, Optional[float]]) -> str:
    """'woven', 'progressive' or '' from one frame's row-profile displacement errors.

    ``errors[d]`` is the mean |bottom field - top field interpolated at an
    offset of d lines| on the row profile (missing for frames idet did not
    call interlaced). Woven: some offset d != 0 predicts the bottom field
    INTERLACE_DECISIVE_RATIO times better than d = 0. Progressive: d = 0
    wins by that ratio. Otherwise the profile cannot tell (horizontal
    motion leaves it unchanged) and the frame is undecided.
    """
    if any(v is None for v in errors.values()) or 0 not in errors:
        return ""
    simultaneous = errors[0]
    shifted = min(v for d, v in errors.items() if d != 0)
    if shifted * INTERLACE_DECISIVE_RATIO <= simultaneous:
        return "woven"
    if simultaneous * INTERLACE_DECISIVE_RATIO <= shifted:
        return "progressive"
    return ""


def is_vertical_scroll(col: Optional[float], row: Optional[float]) -> bool:
    """True when a frame's motion is a vertical translation (see INTERLACE_VERTICAL_SHARE)."""
    if col is None or row is None or col + row <= INTERLACE_MOTION_FLOOR:
        return False
    return col / (col + row) < INTERLACE_VERTICAL_SHARE


def scroll_speed(errors: Dict[int, Optional[float]], position: int = 0) -> Optional[int]:
    """Lines per frame the row profile moved since the previous frame, or None.

    ``errors[k]`` is the mean |difference| of the pairing at selection index
    ``position`` for shift k (see _interlace_chains): the profile of the odd
    frame of the pair was moved by k, so the sign depends on the parity.
    """
    if not errors or any(v is None for v in errors.values()):
        return None
    best = min(errors, key=lambda k: (errors[k], abs(k)))
    return -best if position % 2 else best


def field_verdict(timing: str, vertical: str, col: Optional[float], row: Optional[float],
                  speed: Optional[int] = None) -> str:
    """The frame's woven/progressive verdict.

    Not a vertical scroll: the field-timing comparison. A vertical scroll
    (credits): hard-edged graphics defeat field interpolation, so woven
    needs both the timing comparison and the row-profile displacement to
    say so, and a scroll of at most INTERLACE_SCROLL_SLOW lines per frame
    cannot show feathering at all (progressive). A frame whose profiles did
    not change is "static": it cannot show combing and does not vote."""
    if col is not None and row is not None and col + row <= INTERLACE_MOTION_FLOOR:
        return "static"
    if not is_vertical_scroll(col, row):
        return timing
    if speed is not None and abs(speed) <= INTERLACE_SCROLL_SLOW:
        return "progressive"
    if timing == "woven" and vertical == "woven":
        return "woven"
    return "progressive" if "progressive" in (timing, vertical) else ""


@dataclass
class InterlaceFindings:
    combed: List[Interval]           # woven sections in a progressive-flagged stream
    content_order: Optional[str]     # "tff"/"bff" when idet has a majority
    flagged_order: Optional[str]     # from the stream's field_order
    interlaced_share: float
    scrolls: List[Interval] = field(default_factory=list)   # progressive vertical scrolls
    telecine: List[Interval] = field(default_factory=list)  # 3:2 pulldown cadence
    unsteady: List[Interval] = field(default_factory=list)  # field order flips: not woven


def _dense_sections(flags: Sequence[bool], times: Sequence[float], period: float,
                    window: int, share: float) -> List[Interval]:
    """Sections where some ``window``-frame window holding a flagged frame has
    more than ``share`` of its frames flagged; sections closer than 0.5 s
    merge, and only those of at least INTERLACE_MIN_SECONDS are kept."""
    count = len(flags)
    if count < window:
        return []
    cover = [0] * (count + 1)
    running = sum(flags[:window])
    for k in range(count - window + 1):
        if k:
            running += flags[k + window - 1] - flags[k - 1]
        if running > share * window:
            cover[k] += 1
            cover[k + window] -= 1
    dense, depth = [False] * count, 0
    for j in range(count):
        depth += cover[j]
        dense[j] = depth > 0 and flags[j]
    merged: List[Interval] = []
    for first, last in frame_runs(dense, 1):
        start, length = _interval_of(times, first, last, period)
        if merged and start - sum(merged[-1]) <= 0.5:
            merged[-1] = (merged[-1][0], start + length - merged[-1][0])
        else:
            merged.append((start, length))
    min_frames = max(1, int(round(INTERLACE_MIN_SECONDS / period)))
    return [m for m in merged if m[1] >= min_frames * period - 1e-6]


def _telecine_flags(repeated: Sequence[str]) -> List[bool]:
    """Frames whose repeated field recurs TELECINE_PERIOD frames away."""
    count, step = len(repeated), TELECINE_PERIOD
    return [bool(r) and ((k >= step and repeated[k - step] == r)
                         or (k + step < count and repeated[k + step] == r))
            for k, r in enumerate(repeated)]


def _frame_range(times: Sequence[float], interval: Interval) -> Tuple[int, int]:
    start, length = interval
    first = bisect.bisect_left(times, start - 1e-6)
    last = max(first, bisect.bisect_left(times, start + length - 1e-6) - 1)
    return first, last


def assess_interlace(labels: Sequence[str], times: Sequence[float], fps: float,
                     field_order: str, singles: Sequence[str] = (),
                     repeated: Sequence[str] = (),
                     displacement: Sequence[str] = ()) -> InterlaceFindings:
    """Woven or telecined pictures in progressive-flagged video; field order.

    ``labels``/``singles`` are idet's multiple- and single-frame calls,
    ``repeated`` its repeated-field call, ``displacement`` the field-timing
    verdict per frame (field_timing).

    * Combing: more than half the frames of a one-second window called
      TFF/BFF by the multiple-frame detector, for a second or more.
    * Sections are not woven when idet's single-frame calls in them flip
      between TFF and BFF (no FIELD_ORDER_MAJORITY over all of them: one
      source has one field order), or a section where more than half the
      frames, and more than show fields half a frame apart, show fields from
      the same instant (field_timing), unless the timing comparison itself calls more than
      half the frames of some one-second window woven (the R5b rule applied
      to the timing evidence): idet's 4 % prev/next asymmetry also fires on
      progressive motion (moving graphics, whole-line scrolls), the timing
      comparison does not. Static or noise-dominated frames do not vote, so
      idet's evidence stands where timing cannot be measured.
    * Telecine: one-second windows where a repeated field recurs every five
      frames in at least half of the 3:2 cadence's positions.
    * Field order: idet's TFF/BFF majority against the declared order of an
      interlaced stream.
    """
    count = min(len(labels), len(times))
    labels = [str(v).lower() for v in labels[:count]]
    singles = [str(v).lower() for v in list(singles)[:count]] + [""] * (count - len(singles))
    repeated = [str(v).lower() for v in list(repeated)[:count]] + [""] * (count - len(repeated))
    displacement = list(displacement)[:count] + [""] * (count - len(displacement))
    frame_times = list(times[:count])
    period = _frame_period(fps, frame_times)
    window = max(1, int(round(1.0 / period)))
    interlaced = [v in ("tff", "bff") for v in labels]
    share = _share(interlaced)
    flagged = _FIELD_ORDER_NAMES.get(field_order)
    combed: List[Interval] = []
    scrolls: List[Interval] = []
    unsteady: List[Interval] = []
    telecine: List[Interval] = []
    if flagged is None:
        sections = _dense_sections(interlaced, frame_times, period, window, INTERLACE_WINDOW_SHARE)
        ranges = [_frame_range(frame_times, section) for section in sections]
        calls = [c for first, last in ranges for c in singles[first:last + 1]]
        tff, bff = calls.count("tff"), calls.count("bff")
        steady = not (tff + bff) or max(tff, bff) >= FIELD_ORDER_MAJORITY * (tff + bff)
        for section, (first, last) in zip(sections, ranges):
            if not steady:
                unsteady.append(section)
                continue
            verdicts = displacement[first:last + 1]
            progressive, woven = verdicts.count("progressive"), verdicts.count("woven")
            confirmed = _dense_sections([v == "woven" for v in verdicts], frame_times[first:last + 1],
                                        period, window, INTERLACE_WINDOW_SHARE)
            # Simultaneous fields must be the majority view of the section's
            # moving frames (more than half, as R5b asks of combing itself).
            moving = len(verdicts) - verdicts.count("static")
            if progressive > max(woven, INTERLACE_WINDOW_SHARE * moving) and not confirmed:
                scrolls.append(section)
                continue
            combed.append(section)
        cadence = _telecine_flags(repeated)
        # "At least half the expected repeats": >= (the share test is strict).
        telecine = _dense_sections(cadence, frame_times, period, window,
                                   TELECINE_REPEAT_SHARE * 0.5 - 1e-9)
    tff, bff = labels.count("tff"), labels.count("bff")
    content = None
    if tff + bff >= FIELD_ORDER_MIN_FRAMES:
        if tff >= FIELD_ORDER_MAJORITY * (tff + bff):
            content = "tff"
        elif bff >= FIELD_ORDER_MAJORITY * (tff + bff):
            content = "bff"
    return InterlaceFindings(combed, content, flagged, share, scrolls, telecine, unsteady)


def interlace_entry(findings: InterlaceFindings) -> Tuple[str, str, int]:
    """(status, detail, confidence) for the Interlace Detection row.

    The metadata Scan Type check already judges a stream declared
    interlaced, so this row never repeats that verdict: on interlaced
    streams it only checks the field order; on progressive ones it reports
    woven (combed) pictures and 3:2 pulldown the metadata does not admit to.
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
    problems = []
    if findings.combed:
        total = sum(d for _, d in findings.combed)
        problems.append(f"Interlace combing (woven fields) in video declared progressive: "
                        f"{total:.1f}s at {event_list(findings.combed)}")
    if findings.telecine:
        total = sum(d for _, d in findings.telecine)
        problems.append(f"3:2 pulldown (telecine) cadence in video declared progressive: "
                        f"{total:.1f}s at {event_list(findings.telecine)}")
    if problems:
        return "WARN", ". ".join(problems) + f" ({share}).", 70
    notes = []
    if findings.scrolls:
        notes.append(f"both fields from the same instant (progressive motion, e.g. a vertical "
                     f"scroll) at {event_list(findings.scrolls)}")
    if findings.unsteady:
        notes.append(f"field order flips, not woven, at {event_list(findings.unsteady)}")
    note = "; " + "; ".join(notes) if notes else ""
    return "PASS", f"No interlace combing or pulldown found ({share}{note}).", 100


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
        _report_freeze(result, config, series, video_end, fps, black, decoded_frames, reason,
                       analysis)
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
    if series.black_runs is not None:
        # blackdetect's own frame marks: complete whatever happened to the log.
        events = [(start, end if end is not None else duration) for start, end in series.black_runs]
    elif events is None:
        # No mark file and no usable log record (the bounded capture dropped
        # evidence, or this FFmpeg logged in a form not recognised): a black
        # run could have been missed, so the check is not verified.
        result.add_not_verified(BLACK, "FFmpeg's black-frame report is missing or incomplete "
                                       "(its log overflowed or used an unrecognised form).")
        return []
    if series.nonblack and series.overlay_step:
        overlay = overlay_black_runs(series.nonblack, series.nonblack_static, series.times,
                                     series.overlay_step, config.black_pic_th,
                                     _frame_period(fps, series.times), series.overlay_sample,
                                     bright=series.bright or None, black_sum=series.black_sum or None,
                                     black_level=series.black_level)
        events = merge_runs(events, overlay)
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


def _report_freeze(result, config, series, duration, fps, black, decoded, reason,
                   analysis=None) -> None:
    if not config.freeze:
        result.add_skip(FREEZE, "Switched off in the settings.")
        return
    if decoded and not series.mafd:
        result.add_not_verified(FREEZE, reason)
        return
    if decoded and "struct" in series.missing:
        result.add_not_verified(FREEZE, "FFmpeg wrote no structural-change measurements.")
        return
    side = analysis.structural_side_factor if analysis is not None else 16.0
    findings = detect_freezes(series.mafd, series.times, fps, config.freeze_duration,
                              duration, black, structural=series.structural or None,
                              side_factor=side, hold_allowance=config.freeze_hold,
                              structural_step=series.structural_step or None,
                              step=analysis.freeze_step_frames if analysis is not None else 0)
    if findings.holds:
        result.set_metadata("freeze_frame_count", len(findings.holds))
        result.set_metadata("freeze_frame_total_seconds",
                            round(sum(d for _, d in findings.holds), 2))
        result.set_metadata("freeze_frame_timestamps", event_list(findings.holds))
    _add(result, FREEZE, *freeze_entry(findings, duration, config))


def full_range_of(analysis) -> bool:
    return bool(analysis is not None and analysis.info.full_range)


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
    written = {"luma", "band1", "gam_r", "gam_g", "gam_b"} | ({"band2"} if full_range_of(analysis) else set())
    lost = series.missing & written
    if decoded and lost:
        result.add_not_verified(LEVELS, "FFmpeg wrote no R 103 measurements "
                                        f"({', '.join(sorted(lost))}).")
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
        # Switched on but not measurable here: never a SKIP that reads as
        # "not asked for" (review round 3, bug 14).
        # Inherent to the content type, not a failure to measure: visible and
        # recorded, but Strict Mode does not turn every HDR file into a
        # MISMATCH (review round 5, N5).
        result.add_not_verified(FLASH, "PQ/HLG video: the BT.1702-3 thresholds used here "
                                       "assume an SDR display, so flashes were not screened.",
                                inherent=True)
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
    lost = series.missing & ({"idet", "idet1", "rep_top", "rep_bottom", "ft_same", "ft_top", "ft_bot", "ft_move",
                              "colmo", "rowmo"}
                             | {_disp_name(d) for d in INTERLACE_DISPLACEMENTS}
                             | {_shift_name(k) for k in range(-INTERLACE_SCROLL_RANGE,
                                                              INTERLACE_SCROLL_RANGE + 1)})
    if decoded and lost:
        result.add_not_verified(INTERLACE, "FFmpeg wrote no field measurements "
                                           f"({', '.join(sorted(lost))}).")
        return
    field_order = str((video or {}).get("field_order") or "unknown").lower()
    findings = assess_interlace(series.idet, series.times, fps, field_order,
                                series.idet_single, series.repeated, series.displacement)
    result.set_metadata("interlaced_frame_share", round(findings.interlaced_share, 3))
    _add(result, INTERLACE, *interlace_entry(findings))
