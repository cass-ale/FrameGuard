"""
FrameGuard audio signal QC: loudness delivery profiles and per-channel checks.

Everything here runs inside the single software-decode pass that
``core.run_deep_scan`` already makes (``core._build_scan_command`` asks
``track_filter_chains`` for each audio track's graph), so no audio is
decoded twice. The module is a leaf: it imports nothing from ``core``.

Event detectors (silencedetect, aphasemeter) write their events to files in
the scan's private temporary directory (``MeasurementFiles``), not to the
log: a file is complete however many events there are, while the bounded
stderr capture has to drop records under pressure. Only short end-of-stream
summaries (astats, ebur128, loudnorm) are read from the log, and when the
capture dropped evidence records those rows are reported not verified.
``core._report_audio`` hands both over through ``ScanEvidence`` and records
the rows this module returns.

Measurements and the standard each threshold comes from:

* Integrated loudness, loudness range and true peak: ITU-R BS.1770-5 /
  EBU Tech 3341 via FFmpeg ``ebur128`` (validated against Tech 3341 cases
  1-6, 9, 12, 15-23 and Tech 3342 cases 1-4; see the 0.9.1 audio report).
* Silence at -70 dBFS (sample peak) for >= 2 s, per channel and for the
  whole track: the default level of Telestream Qualify's Audio Silence test
  (AQ-110/AQ-111). The whole-track tiers live in ``core``.
* Dropouts: 5 ms - 2 s at or below -90 dBFS (digital zero for 16-bit
  material) inside otherwise active audio.
* Clipping: runs of >= 3 identical samples at the channel's peak, the
  "successive samples of equal value" rule of Qualify AQ-105.
* Phase: sustained L/R correlation below -0.2 (Qualify AQ-102 publishes the
  -1..+1 scale but no default; -0.2 is the benchmark's inferred value).
* Dual mono: L-R residual below -90 dBFS (Qualify AQ-103), or a Pearson
  correlation of at least 0.999 for lossy tracks whose residual is coding
  noise.
* DC offset (advisory; no Qualify test exists) and 50/60 Hz hum (Qualify
  AQ-118 "50/60 Hz +-1 Hz", thresholds inferred).
"""

import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Check names, classification and configuration
# ---------------------------------------------------------------------------

LOUDNESS = "Audio Loudness"   # reported by core; its weight lives here
TRUE_PEAK = "Audio True Peak"
CLIPPING = "Audio Clipping"
DROPOUTS = "Audio Dropouts"
PHASE = "Audio Phase"
DUAL_MONO = "Dual Mono"
DC_OFFSET = "DC Offset"
HUM = "Audio Hum"

# True peak and clipping follow the delivery-profile enforcement in
# ``loudness_mode``: "report" (the default) measures and shows them, because
# camera originals are routinely hot and clip (the owner's Canon set: 16 of
# 52 files hold genuine runs at 0 dBFS); "advisory" warns; "essential" FAILs,
# as file-delivery QC rejects full-scale clipping (Qualify AQ-105, DPP).
# Everything else is content-dependent and therefore advisory.
ESSENTIAL_CHECKS = frozenset({TRUE_PEAK, CLIPPING})
ADVISORY_CHECKS = frozenset({DROPOUTS, PHASE, DUAL_MONO, DC_OFFSET, HUM})

# Severity weights (core.check_severity: weight x confidence, WARN at half)
# rank a row by the risk that the defect gets the delivery rejected, on
# core's scale (File Integrity 90: unplayable; Resolution 70: an
# unconditional spec rejection; Bitrate 60; Dead Channel 40):
# * Loudness 70: a measured miss of a published, mandatory limit. Broadcast
#   and streaming QC reject it outright (EBU R 128 / DPP AS-11 technical QC,
#   ATSC A/85 under the US CALM Act, Netflix's mix specification), and the
#   fix is a re-render - the same tier as a wrong resolution.
# * True peak 65: the same specifications and the same rejection, but fixed
#   by a limiter pass without re-mixing, so just below loudness.
# * Clipping 60: Qualify AQ-105 and DPP reject full-scale clipping and it is
#   unrecoverable, but a few clipped runs are often a QC note, not a reject.
# * Dropouts 40 and phase 35: audible faults that QC rejects when they are
#   real, judged here from content (advisory), so at Dead Channel's level.
#   A polarity inversion loses content only on a mono fold-down.
# * Dual mono 20 and hum 20: spec- and content-dependent (EBU QC 0124B
#   makes dual mono a per-specification limit); reported, rarely rejected.
# * DC offset 15: inaudible and removed by any high-pass; no delivery
#   specification rejects it on its own.
SEVERITY_WEIGHTS = {
    LOUDNESS: 70,
    TRUE_PEAK: 65,
    CLIPPING: 60,
    DROPOUTS: 40,
    PHASE: 35,
    DUAL_MONO: 20,
    HUM: 20,
    DC_OFFSET: 15,
}

PLAIN_SUMMARIES = {
    TRUE_PEAK: "audio peaks exceed the delivery limit",
    CLIPPING: "audio is clipped (flat-topped peaks)",
    DROPOUTS: "short audio dropouts found",
    PHASE: "stereo channels are out of phase",
    DUAL_MONO: "stereo track is dual mono",
    DC_OFFSET: "audio has a DC offset",
    HUM: "mains hum detected in the audio",
}

# Per-check switches (Settings toggles). Loudness and true peak are governed
# by ``loudness_mode`` instead.
CHECK_SWITCHES = {
    CLIPPING: "audio_clipping_check",
    DROPOUTS: "audio_dropout_check",
    PHASE: "audio_phase_check",
    DUAL_MONO: "audio_dual_mono_check",
    DC_OFFSET: "audio_dc_offset_check",
    HUM: "audio_hum_check",
}

LOUDNESS_PROFILE_CHOICES = (
    "ebu_r128", "atsc_a85", "netflix", "streaming", "streaming_16", "custom",
)

DEFAULT_CONFIG = {
    # Delivery profile for Audio Loudness / Audio True Peak (and, when the
    # profile is enforced, the 48 kHz sample rate). "custom" uses
    # target_loudness_lufs / loudness_tolerance_lu / true_peak_max_dbtp.
    "loudness_profile": "ebu_r128",
    # Per-channel silence: Qualify's documented default.
    "channel_silence_noise_db": -70.0,
    "audio_clipping_check": True,
    "audio_dropout_check": True,
    "audio_phase_check": True,
    "audio_dual_mono_check": True,
    "audio_dc_offset_check": True,
    "audio_hum_check": True,
    "dropout_noise_db": -90.0,
    "dropout_min_ms": 5.0,
    "clipping_min_events": 3,
    "phase_correlation_min": -0.2,
    "phase_min_duration": 1.0,
    "dc_offset_max_db": -50.0,
    "hum_min_db": -65.0,
}

# Validation ranges for presets (values are interpolated into filter graphs).
VALUE_RANGES = {
    "channel_silence_noise_db": (-120.0, 0.0),
    "dropout_noise_db": (-140.0, -40.0),
    "dropout_min_ms": (1.0, 1000.0),
    "clipping_min_events": (1, 100000),
    "phase_correlation_min": (-1.0, 0.0),
    "phase_min_duration": (0.1, 600.0),
    "dc_offset_max_db": (-120.0, 0.0),
    "hum_min_db": (-120.0, 0.0),
}
INTEGER_KEYS = frozenset({"clipping_min_events"})
ENUM_CHOICES = {"loudness_profile": LOUDNESS_PROFILE_CHOICES}
PRESET_KEYS = tuple(DEFAULT_CONFIG)
# Keys that define a custom loudness target. A preset written by 0.9.0 sets
# them without naming a profile; it must keep meaning "custom".
CUSTOM_LOUDNESS_KEYS = ("target_loudness_lufs", "loudness_tolerance_lu", "true_peak_max_dbtp")

# Meter tolerances (EBU Tech 3341 V4 table 1): integrated loudness +-0.1 LU,
# true peak +0.2/-0.4 dB. A reading beyond a limit by less than the meter's
# own error band cannot be called a violation with confidence.
METER_TOLERANCE_LU = 0.1
METER_TOLERANCE_TP_DB = 0.2
LOW_CONFIDENCE = 60

CLIP_MIN_PEAK_DB = -20.0      # below this, equal runs are quantisation, not clipping
# Run-weighted mean length of the samples at a channel's own peak that marks
# flat tops below full scale. Above 2 at least one run of 3+ exists; 2.5
# keeps a margin over the exact 2.0 of an fs/4 tone sampled at 45 degrees
# (+A, +A, -A, -A: EBU Tech 3341 true-peak signals 16 and 20-23).
CLIP_BELOW_FS_MEAN_RUN = 2.5
DUAL_MONO_RESIDUAL_DB = -90.0
DUAL_MONO_MIN_CORRELATION = 0.999
PHASE_INVERTED_SHARE = 0.5    # out of phase for half the active programme
HUM_FREQUENCIES = (50, 60)
HUM_RATE = 1000
HUM_BAND_HZ = 2               # each band: three cascaded 2 Hz-wide biquads
HUM_NEIGHBOUR_HZ = 6          # reference bands 6 Hz either side of the mains tone
HUM_PROMINENCE_DB = 6.0       # mains band at least 6 dB above both neighbours
HUM_STATIONARY_DB = 6.0       # 1 s RMS trough within 6 dB of the mean
EDGE_MARGIN_S = 0.05          # dropouts this close to either end are fades
# Line-up idents interrupt a steady tone on purpose: GLITS (BBC; also the
# stereo part of EBU Tech 3304's BLITS) cuts L once and R twice for 250 ms
# in a 4 s cycle of 1 kHz at -18 dBFS. A gap in this range with a steady
# sinusoid on both sides is the ident, not a dropout.
LINEUP_GAP_S = (0.20, 0.30)
LINEUP_TONE_MIN_S = 0.08      # shortest steady-tone stretch the detector reports
LINEUP_BEFORE_S = 0.15        # the tone must end within this of the gap start
LINEUP_AFTER_S = 0.25         # ...and resume within this of its end (onset settling)
# Steady-sinusoid detector (see _steady_tone_detector).
TONE_FAST_S, TONE_SLOW_S, TONE_PEAK_S = 0.010, 0.040, 0.050
TONE_STEADY_RATIO = 1.105     # fast/slow power within +-0.43 dB (e^+-0.1)
TONE_CREST_POWER = 2.6        # peak^2 / mean power: sine 2 (3 dB crest), noise > 9
TONE_FLOOR_DB = -50.0         # quieter than this is not a line-up tone
TONE_RATE = 4000              # the detector runs on a copy resampled to this
DROPOUT_SILENCE_GAP_S = 0.25  # a dropout this close to a long silence is part of it
DROPOUT_JOIN_S = 0.005        # dropout / tone regions closer than this are one event


def _whole(value, default: int = 0) -> int:
    """A probe field as an int; ffprobe output can be malformed ("stereo")."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(cfg: dict, key: str) -> float:
    return float(cfg.get(key, DEFAULT_CONFIG[key]))


def check_enabled(cfg: dict, name: str) -> bool:
    key = CHECK_SWITCHES.get(name)
    return bool(cfg.get(key, True)) if key else True


def enabled_signal_checks(cfg: dict) -> List[str]:
    return [name for name in (CLIPPING, DROPOUTS, PHASE, DUAL_MONO, DC_OFFSET, HUM)
            if check_enabled(cfg, name)]


def frame_check_names(cfg: dict, loudness: bool) -> List[str]:
    """The rows this module adds to a scan, for "Not verified" bookkeeping."""
    names = [TRUE_PEAK] if loudness else []
    return names + enabled_signal_checks(cfg)


def normalize_custom_profile(config: dict) -> dict:
    """A config carrying a custom target but no profile means "custom".

    0.9.0 had only the three custom keys. Without this, a preset written
    then would silently switch to the EBU R 128 default.
    """
    if "loudness_profile" not in config and any(k in config for k in CUSTOM_LOUDNESS_KEYS):
        config = dict(config)
        config["loudness_profile"] = "custom"
    return config


# ---------------------------------------------------------------------------
# Delivery profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoudnessProfile:
    key: str
    label: str
    target: float
    tolerance: float
    true_peak_max: float
    dialogue_gated: bool = False
    sample_rate: Optional[int] = None
    source: str = ""


_PROFILES = {
    # DPP AS-11 file delivery: -23 LUFS +-0.5 LU, -1 dBTP. R 128 itself
    # allows +-1 LU only where the target is "not achievable practically
    # (for example, live programmes)" and gives +-0.2 LU as QC tolerance.
    "ebu_r128": LoudnessProfile("ebu_r128", "EBU R 128", -23.0, 0.5, -1.0, False, 48000,
                                "EBU R 128 (2023) h), m); DPP AS-11 +-0.5 LU"),
    # A/85:2013 §5.2.1 / §6: -24 LKFS +-2 dB, true peak -2 dBTP.
    "atsc_a85": LoudnessProfile("atsc_a85", "ATSC A/85", -24.0, 2.0, -2.0, False, 48000,
                                "ATSC A/85 §5.2.1, §6"),
    # Netflix Sound Mix Specifications: -27 LKFS dialogue-gated (BS.1770-1)
    # +-2 LU, -2 dBTP. FFmpeg has no dialogue gate, so FrameGuard measures
    # programme loudness and says so.
    "netflix": LoudnessProfile("netflix", "Netflix", -27.0, 2.0, -2.0, True, 48000,
                               "Netflix Sound Mix Specifications"),
    # Spotify for Artists (-14 LUFS, -1 dBTP); tolerance +-1 LU inferred.
    "streaming": LoudnessProfile("streaming", "Streaming -14", -14.0, 1.0, -1.0, False, None,
                                 "Spotify for Artists"),
    # Apple Podcasts / AES TD1006 Annex D: -16 LUFS +-1 LU, -1 dBTP.
    "streaming_16": LoudnessProfile("streaming_16", "Streaming -16", -16.0, 1.0, -1.0, False, None,
                                    "Apple Podcasts; AES TD1006"),
}


def resolve_profile(cfg: dict) -> LoudnessProfile:
    key = str(cfg.get("loudness_profile", DEFAULT_CONFIG["loudness_profile"]))
    if key in _PROFILES:
        return _PROFILES[key]
    return LoudnessProfile(
        "custom", "custom target",
        float(cfg.get("target_loudness_lufs", -23.0)),
        float(cfg.get("loudness_tolerance_lu", 2.0)),
        float(cfg.get("true_peak_max_dbtp", -1.0)))


def profile_enforced(cfg: dict) -> bool:
    return cfg.get("loudness_mode", "report") in ("advisory", "essential")


def required_sample_rate(cfg: dict) -> Optional[int]:
    """The exact rate an enforced delivery profile requires (48 kHz for
    broadcast and Netflix), or None when only the configured floor applies."""
    if not profile_enforced(cfg):
        return None
    return resolve_profile(cfg).sample_rate


def sample_rate_entry(rate: int, cfg: dict) -> Optional[Tuple[str, str]]:
    """(status, text) when an enforced profile rejects ``rate``, else None."""
    required = required_sample_rate(cfg)
    if not required or rate <= 0 or rate == required:
        return None
    profile = resolve_profile(cfg)
    status = "FAIL" if cfg.get("loudness_mode") == "essential" else "WARN"
    return status, f"{rate} Hz; {profile.label} delivery requires {required} Hz"


# ---------------------------------------------------------------------------
# Track geometry
# ---------------------------------------------------------------------------

def is_ambisonic(track: dict) -> bool:
    """FFmpeg names SA3D-signalled PCM layouts "ambisonic N[+...]"."""
    return str(track.get("channel_layout") or "").lower().startswith("ambisonic")


# Channel counts of full-sphere ambisonics: (order + 1)^2 for orders 1-3.
_AMBISONIC_COUNTS = (4, 9, 16)
# The layout FFmpeg assigns when a file signals none: AAC channel
# configuration 4 decodes as "4.0", 16 channels default to "hexadecagonal".
_DEFAULT_LAYOUTS = {4: ("4.0",), 9: (), 16: ("hexadecagonal",)}
_UNNAMED = ("", "unknown")


def _sa3d_is_ambisonic(sa3d: Optional[dict]) -> bool:
    """SA3D (Google Spatial Audio RFC v0): ambisonic_type 0 is periphonic."""
    return bool(sa3d) and "error" not in sa3d and sa3d.get("ambisonic_type") == 0


def ambisonic_positions(tracks: Sequence[dict], sa3d: Sequence[Optional[dict]],
                        spherical: bool) -> List[int]:
    """Positions of the audio tracks measured and analysed as B-format.

    Ambisonic when the file says so: an SA3D box marks the track ambisonic,
    or FFmpeg names the layout "ambisonic N". Without SA3D, only on a
    spherical (projection-tagged) picture, and only when the channel count
    is an ambisonic one and the layout is unsignalled (unnamed, or the
    default FFmpeg assigns for that count, as AAC first-order ambisonics
    decodes as "4.0"). Camera and field-recorder 4.0/quad tracks on flat
    video are discrete loudspeaker channels.
    """
    found = []
    for position, track in enumerate(tracks):
        box = sa3d[position] if position < len(sa3d) else None
        count = _whole(track.get("channels"))
        layout = str(track.get("channel_layout") or "").lower()
        if is_ambisonic(track) or _sa3d_is_ambisonic(box):
            found.append(position)
        elif (spherical and count in _AMBISONIC_COUNTS
              and (layout in _UNNAMED or layout.endswith(" channels")
                   or layout in _DEFAULT_LAYOUTS.get(count, ()))):
            found.append(position)
    return found


# FFmpeg's standard channel layouts in decoded channel order (``ffmpeg
# -layouts``; libavutil/channel_layout.c, channel_layout_map), as ffprobe
# names them in ``channel_layout``.
FFMPEG_LAYOUTS = {
    "mono": "FC", "stereo": "FL+FR", "2.1": "FL+FR+LFE", "3.0": "FL+FR+FC",
    "3.0(back)": "FL+FR+BC", "4.0": "FL+FR+FC+BC", "quad": "FL+FR+BL+BR",
    "quad(side)": "FL+FR+SL+SR", "3.1": "FL+FR+FC+LFE", "5.0": "FL+FR+FC+BL+BR",
    "5.0(side)": "FL+FR+FC+SL+SR", "4.1": "FL+FR+FC+LFE+BC", "5.1": "FL+FR+FC+LFE+BL+BR",
    "5.1(side)": "FL+FR+FC+LFE+SL+SR", "6.0": "FL+FR+FC+BC+SL+SR",
    "6.0(front)": "FL+FR+FLC+FRC+SL+SR", "3.1.2": "FL+FR+FC+LFE+TFL+TFR",
    "hexagonal": "FL+FR+FC+BL+BR+BC", "6.1": "FL+FR+FC+LFE+BC+SL+SR",
    "6.1(back)": "FL+FR+FC+LFE+BL+BR+BC", "6.1(front)": "FL+FR+LFE+FLC+FRC+SL+SR",
    "7.0": "FL+FR+FC+BL+BR+SL+SR", "7.0(front)": "FL+FR+FC+FLC+FRC+SL+SR",
    "7.1": "FL+FR+FC+LFE+BL+BR+SL+SR", "7.1(wide)": "FL+FR+FC+LFE+BL+BR+FLC+FRC",
    "7.1(wide-side)": "FL+FR+FC+LFE+FLC+FRC+SL+SR", "5.1.2": "FL+FR+FC+LFE+SL+SR+TFL+TFR",
    "5.1.2(back)": "FL+FR+FC+LFE+BL+BR+TFL+TFR", "octagonal": "FL+FR+FC+BL+BR+BC+SL+SR",
    "cube": "FL+FR+BL+BR+TFL+TFR+TBL+TBR", "5.1.4": "FL+FR+FC+LFE+SL+SR+TFL+TFR+TBL+TBR",
    "7.1.2": "FL+FR+FC+LFE+BL+BR+SL+SR+TFL+TFR",
    "7.1.4": "FL+FR+FC+LFE+BL+BR+SL+SR+TFL+TFR+TBL+TBR",
    "7.2.3": "FL+FR+FC+LFE+BL+BR+SL+SR+TFL+TFR+TBC+LFE2",
    "9.1.4": "FL+FR+FC+LFE+BL+BR+FLC+FRC+SL+SR+TFL+TFR+TBL+TBR",
    "9.1.6": "FL+FR+FC+LFE+BL+BR+FLC+FRC+SL+SR+TFL+TFR+TBL+TBR+TSL+TSR",
    "hexadecagonal": "FL+FR+FC+BL+BR+BC+SL+SR+TFL+TFC+TFR+TBL+TBC+TBR+WL+WR",
    "binaural": "BIL+BIR", "downmix": "DL+DR",
    "22.2": "FL+FR+FC+LFE+BL+BR+FLC+FRC+BC+SL+SR+TC+TFL+TFC+TFR+TBL+TBC+TBR+LFE2+TSL+TSR+BFC+BFL+BFR",
}
LFE_NAMES = frozenset({"LFE", "LFE2"})
# FFmpeg describes a layout outside its table as "3 channels (FL+FR+LFE)".
_DESCRIBED_RE = re.compile(r"^\d+ channels \(([A-Z0-9+]+)\)$")
# An unsignalled 6- or 8-channel track: the fourth channel is the LFE
# position of the standard order L, R, C, LFE, Ls, Rs[, Lb, Rb] (ITU-R
# BS.775, SMPTE ST 2036-2, WAVE_FORMAT_EXTENSIBLE). Field recorders also
# write unsignalled iso tracks, so the assumption is named in the row text.
_UNSIGNALLED_LFE = {6: 3, 8: 3}


def _layout_text(track: dict) -> str:
    return str(track.get("channel_layout") or "").strip().lower()


def layout_signalled(track: dict) -> bool:
    layout = _layout_text(track)
    return bool(layout) and layout != "unknown" and not layout.endswith(" channels")


def ambisonic_head_locked(track: dict, count: int) -> int:
    """Channels of head-locked (non-diegetic) stereo after the ambisonic
    ones: FFmpeg names an SA3D track with head_locked_stereo
    "ambisonic N+stereo" (Spatial Audio RFC: (order + 1)^2 + 2 channels)."""
    match = re.match(r"^ambisonic (\d+)\+stereo$", _layout_text(track))
    if match and count == (int(match.group(1)) + 1) ** 2 + 2:
        return 2
    return 0


def head_locked_positions(tracks: Sequence[dict], sa3d: Sequence[Optional[dict]],
                          ambisonic: Sequence[int]) -> List[int]:
    """Ambisonic tracks that carry head-locked stereo after their ambisonic
    channels: named so by FFmpeg, or flagged by an SA3D box whose channel
    count matches (order + 1)^2 + 2 (AAC tracks keep FFmpeg's 5.1 name)."""
    found = []
    for position in ambisonic:
        if position >= len(tracks):
            continue
        track = tracks[position]
        count = _whole(track.get("channels"))
        box = sa3d[position] if position < len(sa3d) else None
        flagged = (bool(box) and "error" not in box and box.get("head_locked_stereo")
                   and count == (_whole(box.get("order")) + 1) ** 2 + 2)
        if ambisonic_head_locked(track, count) or flagged:
            found.append(position)
    return found


def channel_names(track: dict, count: int, ambisonic: bool = False,
                  head_locked: Optional[bool] = None) -> List[str]:
    """Channel names in decoded order: FFmpeg's layout table, its "N channels
    (A+B+...)" description, ACN component names for ambisonics (W, Y, Z, X,
    then ACN4...; head-locked stereo as HL-L, HL-R), else ch1..chN.
    ``head_locked`` None: as FFmpeg's layout name says."""
    if ambisonic:
        extra = ambisonic_head_locked(track, count) if head_locked is None else (2 if head_locked else 0)
        acn = ["W", "Y", "Z", "X"] + [f"ACN{k}" for k in range(4, max(4, count - extra))]
        return acn[:count - extra] + ["HL-L", "HL-R"][:extra]
    layout = _layout_text(track)
    decomposition = FFMPEG_LAYOUTS.get(layout)
    if decomposition is None:
        match = _DESCRIBED_RE.match(str(track.get("channel_layout") or "").strip())
        decomposition = match.group(1) if match else None
    names = decomposition.split("+") if decomposition else []
    if len(names) == count:
        return names
    return [f"ch{k + 1}" for k in range(count)]


def lfe_positions(track: dict, names: Sequence[str]) -> frozenset:
    """Channels that are LFE by layout: named LFE/LFE2, or the LFE position
    of an unsignalled 6/8-channel track (see _UNSIGNALLED_LFE). An LFE is
    legitimately silent, gappy and full of 50/60 Hz programme."""
    named = frozenset(k for k, name in enumerate(names) if name in LFE_NAMES)
    if named or layout_signalled(track) or is_ambisonic(track):
        return named
    position = _UNSIGNALLED_LFE.get(len(names))
    return frozenset() if position is None else frozenset({position})


def lfe_note(track: dict, names: Sequence[str], k: int) -> str:
    """How a channel was identified as the LFE, for row texts."""
    if k < len(names) and names[k] in LFE_NAMES:
        return names[k]
    return (f"{names[k] if k < len(names) else f'ch{k + 1}'} (the LFE position of the standard "
            f"L, R, C, LFE order; the layout is not signalled)")


def stereo_pairs(track: dict, names: Sequence[str],
                 ambisonic: bool = False) -> List[Tuple[int, int, str]]:
    """Left/right pairs whose correlation means something.

    Only true L/R pairs are correlated: FL/FR, BL/BR and SL/SR of named
    layouts, or the two channels of a 2-channel track whose layout is
    unknown (cameras write that). Ambisonic W/Y etc. legitimately correlate
    negatively for sources on one side, so they are never paired; an
    ambisonic track's head-locked stereo pair is.
    """
    count = len(names)
    if ambisonic:
        # Head-locked stereo is an ordinary stereo programme.
        if count >= 2 and names[-2:] == ["HL-L", "HL-R"]:
            return [(count - 2, count - 1, "HL-L/HL-R")]
        return []
    if count < 2:
        return []
    if count == 2:
        return [(0, 1, "L/R")]
    pairs = []
    for left, right in (("FL", "FR"), ("BL", "BR"), ("SL", "SR")):
        if left in names and right in names:
            pairs.append((names.index(left), names.index(right), f"{left}/{right}"))
    return pairs


def _hum_mix(names: Sequence[str], ambisonic: bool, lfe: frozenset = frozenset()) -> Optional[str]:
    """pan expression averaging the non-LFE channels (LFE legitimately
    carries 50/60 Hz programme). Ambisonic tracks use channel 0 (W), since
    summing B-format components is meaningless."""
    count = len(names)
    if count == 1:
        return None
    channels = [0] if ambisonic else [k for k in range(count) if k not in lfe]
    if not channels:
        return None
    gain = 1.0 / len(channels)
    terms = "+".join(f"{gain:.6f}*c{k}" for k in channels)
    return f"pan=mono|c0={terms}"


# ---------------------------------------------------------------------------
# Filter graph
# ---------------------------------------------------------------------------

METER_EBUR128 = "ebur128"
METER_LOUDNORM = "loudnorm"
METER_NONE = "none"

# BS.1770 channel weights as FFmpeg's ebur128 filter applies them
# (libavfilter/af_ebur128.c, BACK_MASK): LFE and LFE2 0, every back, side,
# top-back and surround-direct channel 1.41, everything else 1.0. The
# fallback reproduces the primary meter so a file reads the same on every
# platform. (BS.1770-5 weights by position instead: 1.41 only for 60-120
# degrees azimuth, so it would weigh a back centre or 7.1's rear pair 1.0.)
SURROUND_WEIGHT = 1.41
_SURROUND_CHANNELS = frozenset({"BL", "BR", "BC", "SL", "SR", "TBL", "TBR", "TBC", "SDL", "SDR"})
# loudnorm's meter (libavfilter/ebur128.c, ebur128_init_channel_map, taken
# from libebur128) ignores the layout and weighs input channels by index:
# 0 L, 1 R, 2 C (1.0), 3 unused, 4 Ls, 5 Rs (1.41), 6+ unused - with 4- and
# 5-channel inputs special-cased as L R Ls Rs and L R C Ls Rs. So the track
# is first remapped onto a 6-channel input: one source channel per weighted
# input, its gain sqrt(G_channel / G_input) so each channel contributes
# exactly G * mean square (the K-filter and the mean square are linear in
# power). Channels cannot share an input (their cross term would count), so
# a track with more than five non-LFE channels cannot be measured.
_LOUDNORM_FRONT_INPUTS = (0, 1, 2)
_LOUDNORM_SURROUND_INPUTS = (4, 5)

_SIGNAL_STATS = "DC_offset+Peak_level+Flat_factor+Peak_count+RMS_level"
FULL_SCALE_AMPLITUDE = 0.9999
CLIP_RUN_SAMPLES = 3
_PAIR_STATS = "Peak_level+RMS_level"

Branch = Tuple[str, str]  # (pad label, chain body ending before anullsink)


def _filter_path(path: str) -> str:
    """A path quoted for a filter option inside a filter graph: escaped for
    the option parser (``\\ ' :``), then for the graph parser
    (``\\ ' [ ] , ;``) - FFmpeg filters documentation, "Notes on
    filtergraph escaping". Windows paths carry a drive colon and backslashes."""
    def escape(text: str, specials: str) -> str:
        return "".join("\\" + ch if ch in specials else ch for ch in text)
    return escape(escape(path, "\\':"), "\\'[],;")


class MeasurementFiles:
    """Where FFmpeg writes one scan's event series: a private directory the
    caller owns (core creates and removes it). One file per detector
    instance, written by ``ametadata=mode=print:file=`` - unbounded and
    lossless, unlike the stderr capture."""

    def __init__(self, directory: str):
        self.directory = directory
        self.paths: Dict[str, str] = {}

    def path(self, instance: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", instance)
        path = os.path.join(self.directory, f"{name}.txt")
        self.paths[instance] = path
        return path

    def read(self, instance: str) -> Optional[str]:
        """The file's text; None when the detector never ran (not requested,
        or FFmpeg did not create the file)."""
        path = self.paths.get(instance)
        if not path or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()


def _record(instance: str, files: Optional[MeasurementFiles], key: Optional[str] = None) -> str:
    """Filters after a detector that copy its frame metadata to the
    instance's file, then strip it so the next detector's file holds only
    its own events. Nothing without a directory (the rows then report not
    verified)."""
    if files is None:
        return ""
    selector = f":key={key}" if key else ""
    return (f",ametadata=mode=print{selector}:file={_filter_path(files.path(instance))}"
            f",ametadata=mode=delete")


def _phase_angle(cfg: dict) -> float:
    """aphasemeter flags out-of-phase when phase < cos(angle)."""
    threshold = max(-1.0, min(0.0, _number(cfg, "phase_correlation_min")))
    return max(90.0, min(180.0, math.degrees(math.acos(threshold))))


def _bandpass(freq: float, width: float) -> str:
    stage = f"bandpass=f={freq:g}:width_type=h:width={width:g}"
    return ",".join([stage] * 3)


def _signal_branches(i: int, cfg: dict) -> List[Branch]:
    """Per-channel peak, RMS, DC, flat factor and peak count (DC, flat tops
    below full scale, and the L/R levels of the correlation)."""
    wanted = (CLIPPING, DC_OFFSET, PHASE, DUAL_MONO)
    if not any(check_enabled(cfg, name) for name in wanted):
        return []
    return [(f"a{i}s", f"aformat=sample_fmts=dblp,astats@a{i}s="
                       f"measure_perchannel={_SIGNAL_STATS}:measure_overall=none")]


def _steady_tone_detector(sample_rate: int) -> str:
    """aeval that outputs 0 while the channel is a steady sinusoid, else 1.

    Per sample it keeps a 10 ms and a 40 ms one-pole average of x^2 and a
    50 ms-decay peak follower. Steady: the two averages agree within
    +-0.43 dB (a level that is not moving). Sinusoidal: peak^2 over mean
    power below 2.6 (a sine is 2, i.e. a 3 dB crest factor; noise, speech
    and chords exceed 9). Audible: above -50 dBFS. Frequency- and
    level-independent from ~200 Hz to the copy's Nyquist, which covers
    line-up tones (1 kHz GLITS/EBU R 68, 400 Hz); the one-pole coefficients
    are computed for the copy's rate.
    """
    rate = int(sample_rate or TONE_RATE)
    fast = 1 - math.exp(-1 / (TONE_FAST_S * rate))
    slow = 1 - math.exp(-1 / (TONE_SLOW_S * rate))
    decay = math.exp(-1 / (TONE_PEAK_S * rate))
    floor = 10 ** (TONE_FLOOR_DB / 10)
    low = 1 / TONE_STEADY_RATIO
    return ("aeval=exprs='"
            f"st(0,ld(0)+{fast:.8g}*(val(0)*val(0)-ld(0)));"
            f"st(1,ld(1)+{slow:.8g}*(val(0)*val(0)-ld(1)));"
            f"st(2,max(abs(val(0)),ld(2)*{decay:.10g}));"
            f"if(gt(ld(1),{floor:g})*lt(ld(0),{TONE_STEADY_RATIO:g}*ld(1))*gt(ld(0),{low:.6g}*ld(1))"
            f"*lt(ld(2)*ld(2),{TONE_CREST_POWER:g}*ld(0)),0,1)'")


# Full-scale clip runs, counted exactly: aeval keeps the current run length of
# samples at |x| >= 0.9999 (s16 32767 and the s24 maximum both qualify) and
# outputs 0 from the third sample of a run to its end, 1 elsewhere; each run
# of three or more is then one "silence" event (n=0.5, any length) in the
# channel's run file.
_FULL_SCALE_RUNS = (f"aeval=exprs='st(0,if(gte(abs(val(0)),{FULL_SCALE_AMPLITUDE}),ld(0)+1,0));"
                    f"if(gte(ld(0),{CLIP_RUN_SAMPLES}),0,1)'")


def _channel_chain(i: int, k: int, track: dict, is_lfe: bool, cfg: dict,
                   files: Optional[MeasurementFiles]) -> List[str]:
    """``[a<N>c<K>]``: one channel's event detectors, all writing files.

    Silence at the channel threshold (``c``: dead channels, per-channel
    stretches), dropouts (``d``; not on the LFE, which is legitimately
    near-silent), the steady-tone stretches line-up gaps are judged against
    (``t``, on a 4 kHz copy: line-up tones are 1 kHz or 400 Hz, and the
    per-sample aeval costs a twelfth of what it does at 48 kHz) and the
    full-scale runs (``k``).
    """
    head = (f"[a{i}c{k}]pan=mono|c0=c{k},"
            f"silencedetect@a{i}c{k}=n={_number(cfg, 'channel_silence_noise_db')}dB"
            f":d={float(cfg['silence_duration'])}" + _record(f"silencedetect@a{i}c{k}", files))
    tails = []
    if check_enabled(cfg, DROPOUTS) and not is_lfe:
        rate = min(_whole(track.get("sample_rate"), 48000) or 48000, TONE_RATE)
        head += (f",silencedetect@a{i}d{k}=n={_number(cfg, 'dropout_noise_db')}dB"
                 f":d={_number(cfg, 'dropout_min_ms') / 1000.0}"
                 + _record(f"silencedetect@a{i}d{k}", files))
        tails.append(f"aresample={rate},{_steady_tone_detector(rate)},"
                     f"silencedetect@a{i}t{k}=n=0.5:d={LINEUP_TONE_MIN_S:g}"
                     + _record(f"silencedetect@a{i}t{k}", files))
    if check_enabled(cfg, CLIPPING):
        tails.append(f"{_FULL_SCALE_RUNS},silencedetect@a{i}k{k}=n=0.5:d=0"
                     + _record(f"silencedetect@a{i}k{k}", files))
    if not tails:
        return [head + ",anullsink"]
    if len(tails) == 1:
        return [f"{head},{tails[0]},anullsink"]
    labels = [f"[a{i}c{k}x{n}]" for n in range(len(tails))]
    return ([f"{head},asplit={len(tails)}{''.join(labels)}"]
            + [f"{label}{tail},anullsink" for label, tail in zip(labels, tails)])


def _phase_chains(i: int, track: dict, names: Sequence[str], cfg: dict, ambisonic: bool,
                  files: Optional[MeasurementFiles]) -> List[Tuple[str, str]]:
    """(label, chain) per L/R pair: aphasemeter's sustained out-of-phase
    regions, written to a start and an end file."""
    if not check_enabled(cfg, PHASE):
        return []
    chains = []
    for j, (left, right, _) in enumerate(stereo_pairs(track, names, ambisonic)):
        # pan gives the pair a definite stereo layout: cameras write 2-channel
        # tracks with an unknown layout, which aphasemeter rejects.
        chains.append((f"[a{i}q{j}]", (
            f"[a{i}q{j}]pan=stereo|c0=c{left}|c1=c{right},"
            f"aphasemeter@a{i}p{j}=video=0:phasing=1:angle={_phase_angle(cfg):.4f}"
            f":duration={_number(cfg, 'phase_min_duration'):g}"
            + _record(f"aphasemeter@a{i}p{j}.start", files, "lavfi.aphasemeter.out_phase_start")
            + _record(f"aphasemeter@a{i}p{j}.end", files, "lavfi.aphasemeter.out_phase_end")
            + ",anullsink")))
    return chains


def _hum_chains(i: int, names: Sequence[str], cfg: dict, ambisonic: bool,
                files: Optional[MeasurementFiles], lfe: frozenset = frozenset()) -> List[str]:
    """``[a<N>h]`` -> 2 Hz bands at 50/60 Hz and 6 Hz either side, each
    band's RMS per 1 s window written to a file (``astats@a<N>h<F><tag>``).
    44-66 Hz bands need no more than a 1 kHz rate: resampling first makes the
    18 biquads 48 times cheaper and better conditioned."""
    mix = _hum_mix(names, ambisonic, lfe)
    if not check_enabled(cfg, HUM) or (len(names) != 1 and not mix):
        return []
    bands = [(freq, tag, freq + offset) for freq in HUM_FREQUENCIES
             for tag, offset in (("c", 0), ("l", -HUM_NEIGHBOUR_HZ), ("u", HUM_NEIGHBOUR_HZ))]
    outputs = "".join(f"[a{i}h{freq}{tag}]" for freq, tag, _ in bands)
    chains = [f"[a{i}h]{mix + ',' if mix else ''}aresample={HUM_RATE},asplit={len(bands)}{outputs}"]
    chains.extend(
        f"[a{i}h{freq}{tag}]{_bandpass(centre, HUM_BAND_HZ)},asetnsamples=n={HUM_RATE}:p=0,"
        f"astats@a{i}h{freq}{tag}=metadata=1:reset=1:measure_perchannel=RMS_level:measure_overall=none"
        + _record(f"astats@a{i}h{freq}{tag}", files, "lavfi.astats.1.RMS_level")
        + ",anullsink"
        for freq, tag, centre in bands)
    return chains


def _main_chain(i: int, main: str, cfg: dict, meter: str, loudness: bool, ambisonic: bool,
                files: Optional[MeasurementFiles]) -> str:
    """0.9.0's whole-track silence plus the BS.1770 meter, ending in [a<N>]."""
    chain = (f"{main}silencedetect@a{i}=n={float(cfg['silence_noise_db'])}dB"
             f":d={float(cfg['silence_duration'])}" + _record(f"silencedetect@a{i}", files))
    if loudness:
        # Always on every channel as decoded, ambisonic components and
        # head-locked stereo included. Its loudness is used only for
        # METER_EBUR128 non-ambisonic tracks (track_measurement); the true
        # peak is exact on every build (it is taken before the K-weighting
        # the arm64 build gets wrong; verified there on 5.1 and 4.0 tones).
        chain += f",ebur128@a{i}=peak=true+sample:framelog=quiet"
    return chain + f"[a{i}]"


@dataclass
class MeasurementUnit:
    """One file-writing detector chain fed from ``source`` through ``label``."""

    source: str
    label: str
    chains: List[str]


@dataclass
class TrackGraphs:
    """One track's share of the scan command.

    ``summary``: chains for the scan's first filter graph, the one whose
    filters print end-of-scan summaries to the log (ebur128, astats,
    loudnorm); the main branch ends in ``[a<N>]``. They share one graph, and
    so one thread, because a filter graph frees its filters on its own
    thread at the end: ebur128 prints its peaks without a line end, and a
    line another thread prints meanwhile is glued to it without its source
    prefix. ``units``: the file-writing detectors, which
    ``measurement_graphs`` packs into parallel graphs.
    """

    summary: List[str]
    units: List[MeasurementUnit]


# At most this many measurement graphs (threads) per scan: more threads than
# cores buys nothing, and every graph adds a file path to the command line.
MAX_MEASUREMENT_GRAPHS = 16


def measurement_graphs(units: Sequence[MeasurementUnit],
                       max_graphs: int = MAX_MEASUREMENT_GRAPHS) -> List[Tuple[str, str]]:
    """(graph, stub label) per graph: consecutive units in at most
    ``max_graphs`` graphs, each splitting the streams it reads and giving
    FFmpeg the one-sample output it requires of every graph."""
    if not units:
        return []
    count = min(max_graphs, len(units))
    size = -(-len(units) // count)
    graphs = []
    for number, start in enumerate(range(0, len(units), size)):
        part = units[start:start + size]
        sources: Dict[str, List[MeasurementUnit]] = {}
        for unit in part:
            sources.setdefault(unit.source, []).append(unit)
        stub = f"[ag{number}]"
        lines = []
        for position, (source, members) in enumerate(sources.items()):
            labels = "".join(unit.label for unit in members)
            extra = f"[ag{number}s]" if position == 0 else ""
            lines.append(f"{source}asplit={len(members) + bool(extra)}{extra}{labels}")
        lines.append(f"[ag{number}s]atrim=end_sample=1{stub}")
        for unit in part:
            lines.extend(unit.chains)
        graphs.append((";".join(lines), stub))
    return graphs


def track_graphs(position: int, track: dict, names: Sequence[str], cfg: dict,
                 loudness: bool, max_split: int, multichannel_meter: str = METER_EBUR128,
                 ambisonic: bool = False,
                 files: Optional[MeasurementFiles] = None) -> TrackGraphs:
    """Every audio branch for one track (see TrackGraphs)."""
    i = position
    source = f"[0:{int(track['index'])}]"
    meter = (loudness_meter(track, len(names), multichannel_meter, ambisonic, names)
             if loudness else METER_NONE)
    branches = _signal_branches(i, cfg)
    if check_enabled(cfg, DUAL_MONO) or check_enabled(cfg, PHASE):
        for j, (left, right, _) in enumerate(stereo_pairs(track, names, ambisonic)):
            branches.append((f"a{i}p{j}", (
                f"pan=stereo|c0=c{left}+c{right}|c1=c{left}-c{right},aformat=sample_fmts=dblp,"
                f"astats@a{i}p{j}=measure_perchannel={_PAIR_STATS}:measure_overall=none")))
    if loudness and ambisonic:
        # W only: BS.1770-5 defines no scene-based method; summing B-format
        # components over-reads by ~3.5 LU (benchmark F13). Head-locked
        # stereo is an ordinary stereo programme and is measured as one.
        branches.append((f"a{i}w", f"pan=mono|c0=c0,ebur128@a{i}w=peak=true:framelog=quiet"))
        if names[-2:] == ["HL-L", "HL-R"]:
            left, right = len(names) - 2, len(names) - 1
            branches.append((f"a{i}hl", f"pan=stereo|c0=c{left}|c1=c{right},"
                                        f"ebur128@a{i}h=peak=true:framelog=quiet"))
    if meter == METER_LOUDNORM:
        # loudnorm resamples to 192 kHz internally: keep it off the mapped output.
        # JSON prints the input loudness to 0.01 LU (the summary rounds to 0.1).
        branches.append((f"a{i}l", f"{loudnorm_mapping(track, names)},loudnorm@a{i}=print_format=json"))
    if branches:
        main = f"[a{i}m]"
        summary = [f"{source}asplit={len(branches) + 1}{main}"
                   + "".join(f"[{label}]" for label, _ in branches)]
        summary += [f"[{label}]{body},anullsink" for label, body in branches]
        summary.append(_main_chain(i, main, cfg, meter, loudness, ambisonic, files))
    else:
        summary = [_main_chain(i, source, cfg, meter, loudness, ambisonic, files)]

    units = []
    lfe = frozenset() if ambisonic else lfe_positions(track, names)
    if 1 <= len(names) <= max_split:
        units += [MeasurementUnit(source, f"[a{i}c{k}]", _channel_chain(i, k, track, k in lfe, cfg, files))
                  for k in range(len(names))]
    units += [MeasurementUnit(source, label, [body])
              for label, body in _phase_chains(i, track, names, cfg, ambisonic, files)]
    hum = _hum_chains(i, names, cfg, ambisonic, files, lfe)
    if hum:
        units.append(MeasurementUnit(source, f"[a{i}h]", hum))
    return TrackGraphs(summary, units)


def track_filter_chains(position: int, track: dict, names: Sequence[str], cfg: dict,
                        loudness: bool, max_split: int,
                        multichannel_meter: str = METER_EBUR128, ambisonic: bool = False,
                        files: Optional[MeasurementFiles] = None) -> List[str]:
    """Every chain of ``track_graphs`` as one list (for inspection)."""
    graphs = track_graphs(position, track, names, cfg, loudness, max_split,
                          multichannel_meter, ambisonic, files)
    return graphs.summary + [chain for unit in graphs.units for chain in unit.chains]


def channel_weight(name: str) -> float:
    if name in LFE_NAMES:
        return 0.0
    return SURROUND_WEIGHT if name in _SURROUND_CHANNELS else 1.0


def loudnorm_mapping(track: dict, names: Sequence[str]) -> Optional[str]:
    """The filters that place a track's channels on loudnorm's weighted
    inputs (see _LOUDNORM_FRONT_INPUTS), or None when they cannot be placed:
    the layout is not signalled (no positions to weigh by) or more than five
    channels carry weight."""
    if not layout_signalled(track) or any(name.startswith("ch") for name in names):
        return None
    fronts = [k for k, n in enumerate(names) if channel_weight(n) == 1.0]
    surrounds = [k for k, n in enumerate(names) if channel_weight(n) == SURROUND_WEIGHT]
    if len(fronts) + len(surrounds) > len(_LOUDNORM_FRONT_INPUTS) + len(_LOUDNORM_SURROUND_INPUTS):
        return None
    front_inputs, surround_inputs = list(_LOUDNORM_FRONT_INPUTS), list(_LOUDNORM_SURROUND_INPUTS)
    placed = []
    for k in fronts:
        placed.append((front_inputs.pop(0) if front_inputs else surround_inputs.pop(0), k))
    for k in surrounds:
        placed.append((surround_inputs.pop(0) if surround_inputs else front_inputs.pop(0), k))
    terms = []
    for slot, k in sorted(placed):
        slot_weight = SURROUND_WEIGHT if slot in _LOUDNORM_SURROUND_INPUTS else 1.0
        gain = math.sqrt(channel_weight(names[k]) / slot_weight)
        terms.append(f"c{slot}=c{k}" if abs(gain - 1.0) < 1e-12 else f"c{slot}={gain:.9f}*c{k}")
    # Double precision first: a gain above 1 must not clip integer samples.
    return "aformat=sample_fmts=dbl,pan=5.1|" + "|".join(terms)


def loudness_meter(track: dict, count: int, multichannel_meter: str,
                   ambisonic: bool = False, names: Optional[Sequence[str]] = None) -> str:
    """Which BS.1770 meter measures this track.

    ``ebur128`` for mono, stereo and ambisonic W. For three or more channels
    the caller passes what the bundled FFmpeg can do: the darwin-arm64 9.0.2
    build's ebur128 K-weights >2-channel input wrongly (a 1 kHz -23 LUFS 3.0
    reference reads -5.0 LUFS on that binary; the x86_64 build of the same
    release reads -23.0), so core self-tests the binary and falls back to
    ``loudnorm``'s independent libebur128 meter, with the track remapped by
    layout (``loudnorm_mapping``). A track that cannot be remapped is
    ``none``: its loudness is not verified (its true peak still is).
    """
    if count <= 2 or ambisonic:
        return METER_EBUR128
    if multichannel_meter == METER_LOUDNORM:
        names = list(names) if names is not None else channel_names(track, count)
        if loudnorm_mapping(track, names) is None:
            return METER_NONE
    return multichannel_meter


_EVIDENCE_RE = re.compile(r"\[astats@a\d+[sp]\d* @ |\[loudnorm@a\d+ @ "
                          r"|^Input (?:Integrated|True Peak|LRA|Threshold):"
                          r'|^\s*"input_(?:i|tp|lra|thresh)" :')
# Detectors whose events are read from measurement files: their log lines
# are diagnostics, not evidence (with thousands of dropouts they would
# otherwise crowd the summaries out of the bounded capture).
_FILE_ROUTED_RE = re.compile(r"\[(?:silencedetect@a\d+[a-z]?\d*|aphasemeter@a\d+p\d+"
                             r"|astats@a\d+h\d+[clu]) @ ")


def is_file_routed_line(line: str) -> bool:
    return bool(_FILE_ROUTED_RE.search(line))


def is_evidence_line(line: str) -> bool:
    """Records this module parses; the bounded stderr capture must keep them."""
    return bool(_EVIDENCE_RE.search(line))


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def _context_lines(log_text: str, instance: str) -> List[str]:
    pattern = re.compile(rf"\[{re.escape(instance)} @ [0-9A-Fa-fx]+\]")
    return [line for line in log_text.splitlines() if pattern.search(line)]


# "[astats@a0s @ 0x...] [info] Channel: 1": context and severity tags.
_LEADING_TAGS_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+")


def _to_float(text: str) -> Optional[float]:
    try:
        value = float(text)
    except ValueError:
        return None
    return None if math.isnan(value) else value


_ASTATS_KEYS = {
    "DC offset": "dc",
    "Peak level dB": "peak_db",
    "RMS level dB": "rms_db",
    "RMS trough dB": "trough_db",
    "RMS through dB": "trough_db",  # FFmpeg's own spelling
    "Flat factor": "flat_db",
    "Peak count": "peak_count",
    "Abs Peak count": "abs_peak_count",
}


def parse_astats(log_text: str, instance: str) -> List[Dict[str, float]]:
    """Per-channel astats summary of one filter instance, in channel order."""
    channels: List[Dict[str, float]] = []
    for line in _context_lines(log_text, instance):
        message = _LEADING_TAGS_RE.sub("", line).strip()
        key, _, value = message.partition(":")
        key = key.strip()
        if key == "Channel":
            channels.append({})
            continue
        if channels and key in _ASTATS_KEYS:
            number = _to_float(value.strip())
            if number is not None:
                channels[-1][_ASTATS_KEYS[key]] = number
    return channels


_SUMMARY_RE = re.compile(r"\[ebur128@a(\d+)([a-z]?) @ [0-9A-Fa-fx]+\].*Summary:")
# ebur128 instances: "" every channel (true peak; loudness of ordinary
# tracks), "w" ambisonic W, "h" head-locked stereo.
EBUR128_W, EBUR128_HEAD_LOCKED = "w", "h"
_NUMBER = r"(-?inf|-?\d+(?:\.\d+)?)"
_SECTION_KEYS = {
    "Integrated loudness:": ("I", "integrated"),
    "Loudness range:": ("LRA", "lra"),
    "Sample peak:": ("Peak", "sample_peak"),
    "True peak:": ("Peak", "true_peak"),
}


def parse_loudness(log_text: str, suffix: str = "") -> Dict[int, dict]:
    """I, LRA, sample peak and true peak per ``ebur128@a<N><suffix>`` summary.

    The summary prints "Sample peak:" before "True peak:", each followed by
    a "Peak:" line, so values are attributed by section, not by order. A
    summary without peaks (peak=none) ends at the next summary.
    """
    results: Dict[int, dict] = {}
    current, section = None, None
    for line in log_text.splitlines():
        header = _SUMMARY_RE.search(line)
        if header:
            current, section = None, None
            if header.group(2) == suffix:
                current = int(header.group(1))
                results[current] = {}
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped in _SECTION_KEYS:
            section = _SECTION_KEYS[stripped]
            continue
        if section is None:
            continue
        match = re.match(rf"^{section[0]}:\s+{_NUMBER}\s", stripped + " ")
        if match:
            results[current][section[1]] = float(match.group(1))
            if section[1] == "true_peak":
                current = None
            section = None
    return results


_LOUDNORM_RE = re.compile(r"\[loudnorm@a(\d+) @ [0-9A-Fa-fx]+\]")
# Summary ("Input Integrated: -23.1 LUFS") and JSON ("input_i" : "-23.05") keys.
_LOUDNORM_KEYS = {"Input Integrated": "integrated", "Input True Peak": "true_peak",
                  "Input LRA": "lra", '"input_i"': "integrated", '"input_tp"': "true_peak",
                  '"input_lra"': "lra"}
_LOUDNORM_LAST = ("Input LRA", '"input_thresh"')


def parse_loudnorm(log_text: str) -> Dict[int, dict]:
    """Input I / true peak / LRA per ``loudnorm@a<N>`` report, summary or
    JSON (the multichannel fallback meter; see ``loudness_meter``)."""
    results: Dict[int, dict] = {}
    current = None
    for line in log_text.splitlines():
        header = _LOUDNORM_RE.search(line)
        if header:
            current = int(header.group(1))
            results.setdefault(current, {"meter": METER_LOUDNORM})
            continue
        if current is None:
            continue
        key, _, value = line.strip().partition(":")
        key = key.strip()
        if key in _LOUDNORM_KEYS:
            match = re.match(rf"\s*{_NUMBER}", value.replace("+", "").replace('"', "").strip())
            if match:
                results[current][_LOUDNORM_KEYS[key]] = float(match.group(1))
        if key in _LOUDNORM_LAST:
            current = None
    return {track: values for track, values in results.items() if len(values) > 1}


def parse_meters(log_text: str) -> Dict[int, Dict[str, dict]]:
    """Every meter's report per track: {track: {"ebur128": ..., "loudnorm": ...}}
    (``track_measurement`` picks what each track's meter vouches for)."""
    merged: Dict[int, Dict[str, dict]] = {}
    for suffix in ("", EBUR128_W, EBUR128_HEAD_LOCKED):
        for track, values in parse_loudness(log_text, suffix).items():
            merged.setdefault(track, {})[METER_EBUR128 + suffix] = values
    for track, values in parse_loudnorm(log_text).items():
        merged.setdefault(track, {})[METER_LOUDNORM] = values
    return merged


def track_measurement(meters: Dict[int, Dict[str, dict]], ctx: "TrackContext") -> Optional[dict]:
    """One track's loudness entry from what its meter vouches for.

    Integrated loudness and LRA come only from the track's own meter: on a
    ``loudnorm`` track the ebur128 loudness is the broken arm64 reading,
    and a ``none`` track has no loudness at all. The true and sample peak
    always come from ebur128 on every channel as decoded.
    """
    sources = meters.get(ctx.position) or {}
    main = sources.get(METER_EBUR128) or {}
    entry = {key: main[key] for key in ("true_peak", "sample_peak") if key in main}
    if ctx.ambisonic:
        loud = sources.get(METER_EBUR128 + EBUR128_W) or {}
        head_locked = sources.get(METER_EBUR128 + EBUR128_HEAD_LOCKED)
        if head_locked and "integrated" in head_locked:
            entry["head_locked"] = head_locked
    else:
        loud = {METER_EBUR128: main, METER_LOUDNORM: sources.get(METER_LOUDNORM) or {}}.get(ctx.meter, {})
    entry.update({key: loud[key] for key in ("integrated", "lra") if key in loud})
    if not entry:
        return None
    entry["meter"] = ctx.meter
    return entry


# ---------------------------------------------------------------------------
# Evidence handed over by core
# ---------------------------------------------------------------------------

Events = List[Tuple[float, float]]


class EvidenceMissing(Exception):
    """A row's evidence was not produced: report it not verified."""


@dataclass
class ScanEvidence:
    """What core captured for one scan.

    ``silence_events(instance, end[, join_gap])`` returns merged (start,
    duration) silencedetect regions read from the instance's measurement
    file (core's parser), open regions closed at ``end`` and regions closer
    than ``join_gap`` (0.25 s) fused; it raises EvidenceMissing when there
    is no file. ``log_complete`` is False when the bounded stderr capture
    dropped evidence records, so the log's summaries cannot be trusted.
    """

    log_text: str
    silence_events: Callable[..., Events]
    files: Optional[MeasurementFiles] = None
    log_complete: bool = True
    dropped_records: int = 0


@dataclass
class TrackContext:
    position: int
    track: dict
    names: List[str]
    label: str
    duration: float
    dead_channels: List[int] = field(default_factory=list)
    track_silent: bool = False
    meter: str = METER_EBUR128
    ambisonic: bool = False
    # Channels that are LFE by layout (lfe_positions); derived when omitted.
    lfe: Optional[frozenset] = None

    def __post_init__(self):
        if self.lfe is None:
            self.lfe = frozenset() if self.ambisonic else lfe_positions(self.track, self.names)


Row = Tuple[str, str, int]  # (status, detail, confidence)


def _with_label(label: str, text: str) -> str:
    return f"{label}: {text}" if label else text


def _timecode(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def _event_list(events: Events, limit: int = 5, unit: str = "s") -> str:
    def length(d):
        return f"{d * 1000:.0f} ms" if unit == "ms" else f"{d:.1f}s"
    shown = [f"{_timecode(s)} ({length(d)})" for s, d in events[:limit]]
    if len(events) > limit:
        shown.append(f"+{len(events) - limit} more")
    return ", ".join(shown)


def _subtract(events: Events, covers: Events) -> Events:
    """Parts of ``events`` not covered by ``covers`` (both merged lists)."""
    out = []
    for start, dur in events:
        pieces = [(start, start + dur)]
        for c_start, c_dur in covers:
            c_end = c_start + c_dur
            next_pieces = []
            for a, b in pieces:
                if c_end <= a or c_start >= b:
                    next_pieces.append((a, b))
                    continue
                if a < c_start:
                    next_pieces.append((a, c_start))
                if c_end < b:
                    next_pieces.append((c_end, b))
            pieces = next_pieces
        out.extend((a, b - a) for a, b in pieces if b - a > 1e-6)
    return out


def _merge(events: Events, gap: float) -> Events:
    merged: Events = []
    for start, dur in sorted(events):
        if merged and start <= merged[-1][0] + merged[-1][1] + gap:
            prev_start, prev_dur = merged[-1]
            merged[-1] = (prev_start, max(prev_start + prev_dur, start + dur) - prev_start)
        else:
            merged.append((start, dur))
    return merged


def aggregate(entries: List[Row]) -> Optional[Row]:
    """One row from per-track rows: the worst status; among those, the most
    confident entry decides the confidence (a confident FAIL on any track
    must still make the file a MISMATCH)."""
    if not entries:
        return None
    rank = {"FAIL": 3, "WARN": 2, "PASS": 1, "SKIP": 0}
    worst = max(rank.get(s, 0) for s, _, _ in entries)
    confidence = max(c for s, _, c in entries if rank.get(s, 0) == worst)
    status = next(s for s, _, _ in entries if rank.get(s, 0) == worst)
    return status, "; ".join(d for _, d, _ in entries), confidence


# ---------------------------------------------------------------------------
# Loudness and true peak
# ---------------------------------------------------------------------------

def _violation_confidence(margin: float, meter_tolerance: float) -> int:
    return LOW_CONFIDENCE if margin <= meter_tolerance else 95


def _loudness_caveat(ctx: TrackContext) -> Tuple[str, Optional[int]]:
    """(note, confidence cap) for measurements no standard fully defines."""
    if ctx.ambisonic:
        return (" (ambisonic: W-channel loudness; BS.1770-5 defines no scene-based method)",
                LOW_CONFIDENCE)
    if ctx.duration and ctx.duration < 3.0:
        return " (under 3 s: gating is unreliable)", LOW_CONFIDENCE
    return "", None


def _unmeasured_reason(ctx: TrackContext, what: str) -> str:
    if ctx.meter == METER_NONE and what == "loudness summary":
        layout = str(ctx.track.get("channel_layout") or "").strip() or "unknown"
        weighted = sum(1 for n in ctx.names if channel_weight(n) > 0)
        why = ("the channel layout is not signalled, so the channels cannot be placed"
               if not layout_signalled(ctx.track) or any(n.startswith("ch") for n in ctx.names)
               else f"this {layout} track has {weighted} weighted channels and the fallback "
                    f"meter takes five")
        return (f"loudness not measured: the bundled FFmpeg's ebur128 mis-measures audio with more "
                f"than two channels, and {why} (true peak is still measured)")
    return f"{what} not reported by FFmpeg"


def track_sample_peak(evidence: "ScanEvidence", ctx: TrackContext) -> Optional[float]:
    peaks = [c.get("peak_db") for c in parse_astats(evidence.log_text, f"astats@a{ctx.position}s")]
    peaks = [p for p in peaks if p is not None]
    return max(peaks) if peaks else None


def loudness_row(measured: Optional[dict], ctx: TrackContext, cfg: dict) -> Row:
    """Integrated loudness against the delivery profile (for an ambisonic
    track with head-locked stereo, the W reading and the head-locked pair)."""
    mode = cfg.get("loudness_mode", "report")
    if not measured or "integrated" not in measured or ctx.meter == METER_NONE:
        reason = _unmeasured_reason(ctx, "loudness summary")
        if mode == "report":
            return "SKIP", _with_label(ctx.label, reason[0].upper() + reason[1:] + "."), 100
        return "WARN", _with_label(ctx.label, f"Not verified: {reason}."), 100
    row = _programme_row(measured, ctx, cfg)
    head_locked = measured.get("head_locked")
    if not head_locked:
        return row
    # The head-locked pair as its own stereo programme (exact BS.1770), but
    # advisory: the listener hears it mixed with the ambisonic render.
    hl_ctx = TrackContext(position=ctx.position, track=ctx.track, names=["HL-L", "HL-R"],
                          label="", duration=ctx.duration, meter=METER_EBUR128)
    hl_status, hl_text, hl_conf = _programme_row(head_locked, hl_ctx, cfg, _HEAD_LOCKED_CAVEAT)
    status, text, confidence = row
    worst = aggregate([(status, "", confidence), (hl_status, "", hl_conf)])
    return worst[0], f"{text}; head-locked stereo: {hl_text}", worst[2]


_HEAD_LOCKED_CAVEAT = (" (head-locked stereo measured as its own stereo programme; the delivered "
                       "mix adds the ambisonic render)", LOW_CONFIDENCE)


def _programme_row(measured: dict, ctx: TrackContext, cfg: dict,
                   caveat_override: Optional[Tuple[str, int]] = None) -> Row:
    mode = cfg.get("loudness_mode", "report")
    profile = resolve_profile(cfg)
    integrated = measured["integrated"]
    lra = measured.get("lra")
    lra_text = f", LRA {lra:.1f} LU" if lra is not None else ""
    if integrated <= -69.9:
        return "SKIP", _with_label(ctx.label, "No measurable programme loudness: the track is silent "
                                              "(see Audio Silence)."), 100
    method = "programme loudness (not dialogue-gated)" if profile.dialogue_gated else "integrated"
    caveat, cap = caveat_override or _loudness_caveat(ctx)
    measured_text = f"{integrated:.1f} LUFS {method}{lra_text}{caveat}"
    limit_text = f"{profile.label} target {profile.target:g} ± {profile.tolerance:g} LU"
    if mode == "report":
        return "PASS", _with_label(ctx.label, f"{measured_text}; not enforced ({limit_text})"), 100
    deviation = integrated - profile.target
    margin = abs(deviation) - profile.tolerance
    if margin <= 0:
        return "PASS", _with_label(ctx.label, f"{measured_text}, within {limit_text}"), 100
    confidence = _violation_confidence(margin, METER_TOLERANCE_LU)
    if profile.dialogue_gated and margin <= 4.0:
        # Programme and dialogue-gated loudness of a typical mix differ by a
        # few LU, so a near miss on the full mix is not proof of a miss.
        confidence = min(confidence, LOW_CONFIDENCE)
    if cap is not None:
        confidence = min(confidence, cap)
    status = "FAIL" if mode == "essential" and cap is None else "WARN"
    direction = "louder" if deviation > 0 else "quieter"
    text = f"{measured_text} is {abs(deviation):.1f} LU {direction} than the {limit_text}"
    return status, _with_label(ctx.label, text), confidence


def true_peak_row(measured: Optional[dict], ctx: TrackContext, cfg: dict,
                  sample_peak: Optional[float] = None) -> Row:
    """True peak against the profile maximum (``sample_peak`` from astats
    fills in when the meter, e.g. loudnorm, reports none)."""
    mode = cfg.get("loudness_mode", "report")
    profile = resolve_profile(cfg)
    if not measured or "true_peak" not in measured:
        reason = _unmeasured_reason(ctx, "true peak")
        if mode == "report":
            return "SKIP", _with_label(ctx.label, reason[0].upper() + reason[1:] + "."), 100
        return "WARN", _with_label(ctx.label, f"Not verified: {reason}."), 100
    peak = measured["true_peak"]
    sample = measured.get("sample_peak", sample_peak)
    if not math.isfinite(peak):
        return "PASS", _with_label(ctx.label, "No signal (true peak -inf dBTP)."), 100
    sample_text = (f", sample peak {sample:.1f} dBFS" if sample is not None and math.isfinite(sample)
                   else "")
    measured_text = f"true peak {peak:+.1f} dBTP{sample_text}"
    limit_text = f"{profile.label} max {profile.true_peak_max:g} dBTP"
    if mode == "report":
        return "PASS", _with_label(ctx.label, f"{measured_text}; not enforced ({limit_text})"), 100
    margin = peak - profile.true_peak_max
    if margin <= 0:
        return "PASS", _with_label(ctx.label, f"{measured_text}, within {limit_text}"), 100
    confidence = _violation_confidence(margin, METER_TOLERANCE_TP_DB)
    status = "FAIL" if mode == "essential" else "WARN"
    return status, _with_label(ctx.label, f"{measured_text} exceeds the {limit_text} by {margin:.1f} dB"), confidence


# ---------------------------------------------------------------------------
# Signal checks
# ---------------------------------------------------------------------------

def _channel_text(ctx: TrackContext, k: int) -> str:
    return ctx.names[k] if k < len(ctx.names) else f"ch{k + 1}"


_START_RE = re.compile(r"silence_start\s*=")


def _full_scale_runs(evidence: "ScanEvidence", ctx: "TrackContext", k: int) -> int:
    """Runs of >= 3 full-scale samples in channel ``k``: one event each in
    the channel's run file (see ``_FULL_SCALE_RUNS``)."""
    instance = f"silencedetect@a{ctx.position}k{k}"
    text = evidence.files.read(instance) if evidence.files else None
    if text is None:
        raise EvidenceMissing(instance)
    return len(_START_RE.findall(text))


def clipping_row(stats: List[Dict[str, float]], evidence: "ScanEvidence", ctx: "TrackContext",
                 cfg: dict) -> Row:
    """Runs of >= 3 consecutive samples at full scale (Qualify AQ-105).

    Full scale is counted exactly per channel (see ``_FULL_SCALE_RUNS``);
    ``clipping_min_events`` or more runs are clipping, fewer are reported as
    isolated. The verdict follows ``loudness_mode`` like true peak: "report"
    shows the counts (camera originals clip), "advisory" warns and
    "essential" FAILs, because flat-topped samples are unrecoverable. Clipping that
    happened before a gain change leaves flat tops below full scale; astats
    reports how many samples sit at the channel's own extreme values (Peak
    count) and their flatness, 20*log10(sum(run^2)/sum(run)). A run-weighted
    mean of at least ``CLIP_BELOW_FS_MEAN_RUN`` means runs of 3 or more, and
    Peak count over that mean estimates the number of runs. A sustained low tone at 16 bits
    can also repeat its crest value, so this only warns, and channels whose
    peak is below -20 dBFS (quantisation) are ignored.
    """
    minimum = max(1, int(_number(cfg, "clipping_min_events")))
    full, isolated, below = [], [], []
    for k in range(len(ctx.names)):
        runs = _full_scale_runs(evidence, ctx, k)
        if runs >= minimum:
            full.append((k, runs))
            continue
        elif runs > 0:
            isolated.append((k, runs))
            continue
        channel = stats[k] if k < len(stats) else {}
        peak, flat, count = channel.get("peak_db"), channel.get("flat_db"), channel.get("peak_count")
        if peak is None or flat is None or count is None or peak < CLIP_MIN_PEAK_DB:
            continue
        mean_run = 10 ** (flat / 20.0)
        estimate = int(round(count / mean_run)) if mean_run > 0 else 0
        if mean_run >= CLIP_BELOW_FS_MEAN_RUN and estimate >= minimum:
            below.append((k, estimate, mean_run, peak))

    if not stats:
        return "WARN", _with_label(ctx.label, "Not verified: sample statistics were not reported."), 100
    enforced = {"essential": "FAIL", "advisory": "WARN"}.get(cfg.get("loudness_mode", "report"))
    note = "" if enforced else "; not enforced (camera originals clip; enforce a delivery profile to judge)"
    if full:
        most = max(runs for _, runs in full)
        listed = ", ".join(f"{_channel_text(ctx, k)} {runs} run(s)" for k, runs in full)
        text = (f"Clipped at full scale: {listed} of ≥{CLIP_RUN_SAMPLES} consecutive samples at "
                f"0 dBFS ({minimum} or more runs count as clipping){note}.")
        if not enforced:
            return "PASS", _with_label(ctx.label, text), 100
        return enforced, _with_label(ctx.label, text), (90 if most >= 10 else 75)
    if below:
        listed = ", ".join(f"{_channel_text(ctx, k)} ~{runs} flat run(s) at {peak:.1f} dBFS"
                           for k, runs, _, peak in below)
        text = (f"Flat-topped peaks below full scale (clipped before a gain change, or a sustained "
                f"low tone): {listed}{note}.")
        return ("WARN" if enforced else "PASS"), _with_label(ctx.label, text), (LOW_CONFIDENCE if enforced else 100)
    if isolated:
        listed = ", ".join(f"{_channel_text(ctx, k)} {runs}" for k, runs in isolated)
        return "PASS", _with_label(ctx.label, f"Isolated full-scale runs ({listed}), fewer than the "
                                              f"{minimum} that count as clipping."), 100
    return "PASS", _with_label(ctx.label, "No clipped runs (≥3 samples at full scale or at the channel peak)."), 100


def dc_offset_row(stats: List[Dict[str, float]], ctx: TrackContext, cfg: dict) -> Row:
    if not stats:
        return "WARN", _with_label(ctx.label, "Not verified: sample statistics were not reported."), 100
    limit = _number(cfg, "dc_offset_max_db")
    offenders, worst = [], -math.inf
    for k, channel in enumerate(stats):
        dc = channel.get("dc")
        if dc is None or dc == 0:
            continue
        level = 20 * math.log10(abs(dc))
        worst = max(worst, level)
        if level > limit:
            offenders.append(f"{_channel_text(ctx, k)} {level:.1f} dBFS ({dc:+.4f})")
    if offenders:
        return "WARN", _with_label(ctx.label, f"DC offset above {limit:g} dBFS: {', '.join(offenders)}."), 80
    shown = f"{worst:.1f} dBFS" if math.isfinite(worst) else "none"
    return "PASS", _with_label(ctx.label, f"Largest DC offset {shown} (limit {limit:g} dBFS)."), 100


def _active_seconds(ctx: TrackContext, evidence: ScanEvidence, channels: Sequence[int]) -> float:
    """Seconds where at least one of ``channels`` is above the channel
    silence threshold (silence of the whole pair does not count against it)."""
    if not ctx.duration:
        return 0.0
    silent = None
    for k in channels:
        events = evidence.silence_events(f"silencedetect@a{ctx.position}c{k}", ctx.duration)
        intervals = [(s, s + d) for s, d in events]
        if silent is None:
            silent = intervals
        else:  # intersection: silent only where every channel is silent
            silent = [(max(a, c), min(b, d)) for a, b in silent for c, d in intervals
                      if min(b, d) > max(a, c)]
    silent_total = sum(b - a for a, b in (silent or []))
    return max(0.0, ctx.duration - silent_total)


def _pearson(rms_db: Sequence[float]) -> Optional[float]:
    """Correlation of L and R from RMS of L, R, L+R and L-R:
    E[LR] = (E[(L+R)^2] - E[(L-R)^2]) / 4."""
    if len(rms_db) < 4 or any(v is None or not math.isfinite(v) for v in rms_db[:2]):
        return None
    power = [10 ** (v / 10.0) if v is not None and math.isfinite(v) else 0.0 for v in rms_db[:4]]
    left, right, total, side = power
    if left <= 0 or right <= 0:
        return None
    return max(-1.0, min(1.0, (total - side) / (4.0 * math.sqrt(left * right))))


def _pair_stats(evidence: ScanEvidence, ctx: TrackContext, j: int, left: int,
                right: int) -> Tuple[Optional[float], Optional[float]]:
    """(Pearson correlation, L-R residual peak dBFS) of one L/R pair."""
    pair = parse_astats(evidence.log_text, f"astats@a{ctx.position}p{j}")
    levels = parse_astats(evidence.log_text, f"astats@a{ctx.position}s")
    if len(pair) < 2:
        return None, None
    residual = pair[1].get("peak_db")
    if max(left, right) >= len(levels):
        return None, residual
    rms = [levels[left].get("rms_db"), levels[right].get("rms_db"),
           pair[0].get("rms_db"), pair[1].get("rms_db")]
    return _pearson(rms), residual


def _pair_usable(ctx: TrackContext, left: int, right: int) -> bool:
    """A pair with a dead or silent channel has no stereo image to judge
    (Dead Channel / Audio Silence already report it)."""
    return not ctx.track_silent and left not in ctx.dead_channels and right not in ctx.dead_channels


_VALUE_RE = re.compile(r"=\s*(-?\d+(?:\.\d+)?(?:e-?\d+)?)")


def _file_values(evidence: ScanEvidence, instance: str) -> List[float]:
    text = evidence.files.read(instance) if evidence.files else None
    if text is None:
        raise EvidenceMissing(instance)
    return [float(m.group(1)) for m in _VALUE_RE.finditer(text)]


def phase_events(evidence: ScanEvidence, ctx: TrackContext, j: int) -> Events:
    """aphasemeter's sustained out-of-phase regions of pair ``j``, from its
    start and end files; a region still open at the end of the stream (its
    end is only logged, never attached to a frame) closes at the track end."""
    base = f"aphasemeter@a{ctx.position}p{j}"
    marks = sorted([(t, 0) for t in _file_values(evidence, f"{base}.start")]
                   + [(t, 1) for t in _file_values(evidence, f"{base}.end")])
    events, open_start = [], None
    for t, kind in marks:
        if kind == 0 and open_start is None:
            open_start = t
        elif kind == 1 and open_start is not None:
            events.append((open_start, max(0.0, t - open_start)))
            open_start = None
    if open_start is not None and ctx.duration:
        events.append((open_start, max(0.0, ctx.duration - open_start)))
    return _merge(events, 0.25)


def phase_row(evidence: ScanEvidence, ctx: TrackContext, cfg: dict) -> Row:
    pairs = stereo_pairs(ctx.track, ctx.names, ctx.ambisonic)
    if not pairs:
        reason = ("ambisonic track: W, Y, Z, X are not a stereo pair"
                  if len(ctx.names) >= 2 else "single-channel track")
        return "SKIP", _with_label(ctx.label, f"Not applicable ({reason})."), 100
    threshold = _number(cfg, "phase_correlation_min")
    texts, flagged, confidence = [], False, 100
    for j, (left, right, pair_name) in enumerate(pairs):
        if not _pair_usable(ctx, left, right):
            continue
        correlation, _ = _pair_stats(evidence, ctx, j, left, right)
        events = phase_events(evidence, ctx, j)
        active = _active_seconds(ctx, evidence, (left, right)) or ctx.duration
        total = sum(d for _, d in events)
        corr_text = f"correlation {correlation:+.2f}" if correlation is not None else "correlation n/a"
        if not events:
            texts.append(f"{pair_name} {corr_text}, never below {threshold:g} for "
                         f"{_number(cfg, 'phase_min_duration'):g} s or more")
            continue
        flagged = True
        share = total / active if active else 0.0
        if share >= PHASE_INVERTED_SHARE or (correlation is not None and correlation < threshold):
            confidence = min(confidence, 85)
            texts.append(f"{pair_name} {corr_text}, below {threshold:g} for {total:.1f}s "
                         f"({share * 100:.0f}% of the active audio): one channel is probably "
                         f"polarity-inverted; a mono fold-down will cancel")
        else:
            confidence = min(confidence, LOW_CONFIDENCE)
            texts.append(f"{pair_name} {corr_text}; sustained correlation below {threshold:g} at "
                         f"{_event_list(events)}")
    if not texts:
        return "SKIP", _with_label(ctx.label, "No live stereo pair to analyse."), 100
    return ("WARN" if flagged else "PASS"), _with_label(ctx.label, "; ".join(texts) + "."), confidence


def dual_mono_row(evidence: ScanEvidence, ctx: TrackContext, cfg: dict) -> Row:
    pairs = stereo_pairs(ctx.track, ctx.names, ctx.ambisonic)
    if not pairs:
        return "SKIP", _with_label(ctx.label, "Not applicable (no L/R pair)."), 100
    texts, flagged, confidence = [], False, 100
    for j, (left, right, pair_name) in enumerate(pairs):
        if not _pair_usable(ctx, left, right):
            continue
        correlation, residual = _pair_stats(evidence, ctx, j, left, right)
        if residual is None and correlation is None:
            texts.append(f"{pair_name} not measured")
            continue
        if residual is not None and residual < DUAL_MONO_RESIDUAL_DB:
            flagged = True
            confidence = min(confidence, 95)
            shown = "-inf" if not math.isfinite(residual) else f"{residual:.1f}"
            texts.append(f"{pair_name} identical: L-R residual peak {shown} dBFS "
                         f"(< {DUAL_MONO_RESIDUAL_DB:g} dBFS)")
        elif correlation is not None and correlation >= DUAL_MONO_MIN_CORRELATION:
            flagged = True
            confidence = min(confidence, 80 if correlation >= 0.9999 else LOW_CONFIDENCE)
            texts.append(f"{pair_name} identical up to level/coding noise: correlation "
                         f"{correlation:.4f} (≥ {DUAL_MONO_MIN_CORRELATION:g})")
        else:
            corr_text = f"{correlation:.3f}" if correlation is not None else "n/a"
            texts.append(f"{pair_name} distinct (correlation {corr_text})")
    if not texts:
        return "SKIP", _with_label(ctx.label, "No live stereo pair to analyse."), 100
    suffix = ". Intended for a stereo deliverable?" if flagged else "."
    return ("WARN" if flagged else "PASS"), _with_label(ctx.label, "; ".join(texts) + suffix), confidence


def _inside_steady_tone(tones: Events, start: float, end: float) -> bool:
    """A steady sinusoid ends just before the gap and resumes just after it
    (one merged stretch spanning a short gap also counts)."""
    before = any(s <= start - LINEUP_TONE_MIN_S and s + d >= start - LINEUP_BEFORE_S for s, d in tones)
    after = any(s <= end + LINEUP_AFTER_S and s + d >= end + LINEUP_TONE_MIN_S for s, d in tones)
    return before and after


def _dropout_candidates(evidence: ScanEvidence, ctx: TrackContext,
                        cfg: dict) -> Tuple[List[Tuple[float, float, int, bool]], int]:
    """(start, duration, channel, inside a steady tone) per candidate, and
    the number of line-up ident gaps set aside."""
    longest = float(cfg["silence_duration"])
    candidates, lineup = [], 0
    for k in range(len(ctx.names)):
        if k in ctx.lfe or k in ctx.dead_channels:
            continue
        silences = evidence.silence_events(f"silencedetect@a{ctx.position}c{k}", ctx.duration)
        # Short gaps 250 ms apart (GLITS R) are separate events: the 0.25 s
        # merge used for long silences would fuse them.
        tones = evidence.silence_events(f"silencedetect@a{ctx.position}t{k}", ctx.duration,
                                        DROPOUT_JOIN_S)
        for start, dur in evidence.silence_events(f"silencedetect@a{ctx.position}d{k}",
                                                  ctx.duration, DROPOUT_JOIN_S):
            end = start + dur
            if dur >= longest or start < EDGE_MARGIN_S:
                continue
            if ctx.duration and end > ctx.duration - EDGE_MARGIN_S:
                continue
            if any(s - DROPOUT_SILENCE_GAP_S < end and start < s + d + DROPOUT_SILENCE_GAP_S
                   for s, d in silences):
                continue
            in_tone = _inside_steady_tone(tones, start, end)
            if in_tone and LINEUP_GAP_S[0] <= dur <= LINEUP_GAP_S[1]:
                lineup += 1
                continue
            candidates.append((start, dur, k, in_tone))
    return candidates, lineup


def dropout_row(evidence: ScanEvidence, ctx: TrackContext, cfg: dict) -> Row:
    """Short falls to digital silence inside otherwise active audio.

    A -90 dBFS region of 5 ms to ``silence_duration`` that does not touch a
    long (-70 dB, >= silence_duration) silence of the same channel and does
    not sit at either end of the track. Regions of different channels that
    overlap are one event. A 200-300 ms gap with a steady sinusoid on both
    sides is a line-up ident interruption (GLITS) and is not reported; any
    other gap inside a tone still is, as a dropout in the line-up.
    """
    longest = float(cfg["silence_duration"])
    candidates, lineup = _dropout_candidates(evidence, ctx, cfg)
    ident = (f" {lineup} line-up ident interruption(s) (250 ms gaps in a steady tone) ignored."
             if lineup else "")
    if not candidates:
        return "PASS", _with_label(ctx.label, f"No dropouts to ≤ {_number(cfg, 'dropout_noise_db'):g} dBFS "
                                              f"between {_number(cfg, 'dropout_min_ms'):g} ms and "
                                              f"{longest:g} s.{ident}"), 100
    merged = _merge([(s, d) for s, d, _, _ in candidates], gap=0.005)
    labelled = []
    for start, dur in merged:
        hits = [c for c in candidates if c[0] < start + dur and start < c[0] + c[1]]
        channels = "+".join(_channel_text(ctx, k) for k in sorted({c[2] for c in hits}))
        where = ", in a steady tone" if all(c[3] for c in hits) else ""
        labelled.append((start, dur, channels + where))
    shown = ", ".join(f"{_timecode(s)} ({d * 1000:.0f} ms, {names})" for s, d, names in labelled[:5])
    if len(labelled) > 5:
        shown += f", +{len(labelled) - 5} more"
    return "WARN", _with_label(ctx.label, f"{len(labelled)} dropout(s) to digital silence "
                                          f"(≤ {_number(cfg, 'dropout_noise_db'):g} dBFS): {shown}.{ident}"), 85


def channel_silence_entries(evidence: ScanEvidence, ctx: TrackContext, cfg: dict,
                            track_events: Events) -> List[str]:
    """Per-channel silent stretches (>= silence_duration at the channel
    threshold) that the whole-track rule cannot see because another channel
    is still active. Dead and LFE channels are reported elsewhere."""
    if len(ctx.names) < 2 or ctx.track_silent:
        return []
    noise = _number(cfg, "channel_silence_noise_db")
    longest = float(cfg["silence_duration"])
    texts = []
    for k, name in enumerate(ctx.names):
        if k in ctx.lfe or k in ctx.dead_channels:
            continue
        events = evidence.silence_events(f"silencedetect@a{ctx.position}c{k}", ctx.duration)
        alone = [e for e in _subtract(events, track_events) if e[1] >= longest]
        if alone:
            texts.append(f"{name} silent (≤ {noise:g} dBFS) while other channels play: {_event_list(alone)}")
    return texts


def _band_windows(evidence: ScanEvidence, instance: str) -> List[float]:
    """1 s RMS levels (dBFS) of a hum band, from its file. The first window
    is dropped when there are three or more: the 2 Hz filters ring up over
    about half a second."""
    text = evidence.files.read(instance) if evidence.files else None
    if text is None:
        raise EvidenceMissing(instance)
    levels = []
    for match in re.finditer(r"RMS_level=(\S+)", text):
        value = _to_float(match.group(1))
        levels.append(value if value is not None else -math.inf)
    return levels[1:] if len(levels) >= 3 else levels


def _mean_level(levels: Sequence[float]) -> Optional[float]:
    if not levels:
        return None
    power = sum(10 ** (v / 10) for v in levels if math.isfinite(v)) / len(levels)
    return 10 * math.log10(power) if power > 0 else -math.inf


def hum_row(evidence: ScanEvidence, ctx: TrackContext, cfg: dict) -> Row:
    """Mains hum: a steady tone in a 2 Hz band at 50 or 60 Hz.

    The mains band must stand ``HUM_PROMINENCE_DB`` above both 2 Hz bands
    6 Hz away (broadband programme fills all three alike; a tone fills one),
    sit above ``hum_min_db``, and be steady: its quietest 1 s window within
    6 dB of its mean, which a bass line or a passing note is not. The rules
    and the -65 dBFS floor are inferred (Qualify AQ-118 publishes only
    "50/60 Hz +-1 Hz"), so the result is advisory with confidence below 70.
    """
    floor = _number(cfg, "hum_min_db")
    found, measured = [], []
    for freq in HUM_FREQUENCIES:
        windows = {tag: _band_windows(evidence, f"astats@a{ctx.position}h{freq}{tag}")
                   for tag in ("c", "l", "u")}
        level = _mean_level(windows["c"])
        neighbours = [_mean_level(windows[t]) for t in ("l", "u")]
        if level is None or any(n is None for n in neighbours):
            continue
        if not math.isfinite(level):
            measured.append(f"{freq} Hz silent")
            continue
        prominence = level - max(n if math.isfinite(n) else -math.inf for n in neighbours)
        measured.append(f"{freq} Hz {level:.1f} dBFS")
        trough = min(windows["c"])
        steady = math.isfinite(trough) and level - trough <= HUM_STATIONARY_DB
        if level >= floor and prominence >= HUM_PROMINENCE_DB and steady:
            found.append(f"{freq} Hz tone at {level:.1f} dBFS, {prominence:.1f} dB above the bands "
                         f"±{HUM_NEIGHBOUR_HZ} Hz, steady throughout")
    if not measured:
        return "WARN", _with_label(ctx.label, "Not verified: hum bands were not measured."), 100
    if found:
        return "WARN", _with_label(ctx.label, "Possible mains hum: " + "; ".join(found) + "."), LOW_CONFIDENCE
    return "PASS", _with_label(ctx.label, f"No steady 50/60 Hz tone ({', '.join(measured)}; "
                                          f"floor {floor:g} dBFS)."), 100


# ---------------------------------------------------------------------------
# Whole-file reporting
# ---------------------------------------------------------------------------

# Rows judged from the end-of-scan summaries in the log (astats); a capture
# that dropped evidence records cannot vouch for them.
LOG_DERIVED = frozenset({TRUE_PEAK, CLIPPING, PHASE, DUAL_MONO, DC_OFFSET})


def log_overflow_text(evidence: ScanEvidence) -> str:
    return (f"Not verified: FFmpeg's log overflowed the capture ({evidence.dropped_records} "
            f"evidence record(s) dropped), so its end-of-scan measurements are incomplete.")


def missing_text(error: EvidenceMissing) -> str:
    return f"Not verified: the measurement file for {error} was not written."


def _guarded(row: Callable[[], Row], ctx: TrackContext) -> Row:
    try:
        return row()
    except EvidenceMissing as error:
        return "WARN", _with_label(ctx.label, missing_text(error)), 100


def signal_rows(evidence: ScanEvidence, contexts: List[TrackContext], cfg: dict) -> Dict[str, Row]:
    """One aggregated row per enabled signal check."""
    rows: Dict[str, List[Row]] = {name: [] for name in enabled_signal_checks(cfg)}
    for ctx in contexts:
        stats = parse_astats(evidence.log_text, f"astats@a{ctx.position}s")
        builders = {
            CLIPPING: lambda: clipping_row(stats, evidence, ctx, cfg),
            DC_OFFSET: lambda: dc_offset_row(stats, ctx, cfg),
            DROPOUTS: lambda: dropout_row(evidence, ctx, cfg),
            PHASE: lambda: phase_row(evidence, ctx, cfg),
            DUAL_MONO: lambda: dual_mono_row(evidence, ctx, cfg),
            HUM: lambda: hum_row(evidence, ctx, cfg),
        }
        for name in rows:
            if name in LOG_DERIVED and not evidence.log_complete:
                rows[name].append(("WARN", _with_label(ctx.label, log_overflow_text(evidence)), 100))
            else:
                rows[name].append(_guarded(builders[name], ctx))
    return {name: aggregate(entries) for name, entries in rows.items() if entries}


def disabled_rows(cfg: dict) -> Dict[str, Row]:
    return {name: ("SKIP", "Disabled in settings.", 100)
            for name in CHECK_SWITCHES if not check_enabled(cfg, name)}
