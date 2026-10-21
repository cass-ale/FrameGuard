"""
FrameGuard audio signal QC: loudness delivery profiles and per-channel checks.

Everything here runs inside the single software-decode pass that
``core.run_deep_scan`` already makes (``core._build_scan_command`` asks
``track_filter_chains`` for each audio track's branches), so no audio is
decoded twice. The module is a leaf: it imports nothing from ``core``.
``core._report_audio`` hands it the captured FFmpeg log through
``ScanEvidence`` and records the rows this module returns.

Measurements and the standard each threshold comes from:

* Integrated loudness, loudness range and true peak: ITU-R BS.1770-5 /
  EBU Tech 3341 via FFmpeg ``ebur128`` (validated against Tech 3341 cases
  1-6, 9, 12, 15-23 and Tech 3342 cases 1-4; see the 0.9.1 audio report).
* Per-channel silence at -70 dBFS for >= 2 s: the default of Telestream
  Qualify's silence test (AQ-110/AQ-111). Whole-track silence keeps the
  0.9.0 rules in ``core``.
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
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Check names, classification and configuration
# ---------------------------------------------------------------------------

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

SEVERITY_WEIGHTS = {
    TRUE_PEAK: 30,
    CLIPPING: 45,
    DROPOUTS: 35,
    PHASE: 30,
    DUAL_MONO: 15,
    DC_OFFSET: 15,
    HUM: 15,
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
HUM_RATE = 4000
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
TONE_RATE = 12000             # the detector runs on a copy resampled to this
DROPOUT_SILENCE_GAP_S = 0.25  # a dropout this close to a long silence is part of it
DROPOUT_JOIN_S = 0.005        # dropout / tone regions closer than this are one event


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
_NAMED_LAYOUTS = ("mono", "stereo", "2.1", "3.0", "5.0", "5.0(side)", "5.1", "5.1(side)",
                  "6.1", "7.0", "7.1", "7.1(wide)", "octagonal", "hexadecagonal")


def treated_as_ambisonic(track: dict) -> bool:
    """Tracks measured and analysed as B-format.

    Four channels are first-order ambisonics in practice for 360/XR work
    (Quest and YouTube spatial audio): an AAC FOA track even decodes as
    "4.0" with its SA3D box present, so the layout name cannot tell. 9 and
    16 channels without a named loudspeaker layout are 2nd/3rd order.
    """
    count = int(track.get("channels") or 0)
    layout = str(track.get("channel_layout") or "").lower()
    if is_ambisonic(track) or count == 4:
        return True
    return count in _AMBISONIC_COUNTS and layout not in _NAMED_LAYOUTS


def stereo_pairs(track: dict, names: Sequence[str]) -> List[Tuple[int, int, str]]:
    """Left/right pairs whose correlation means something.

    Only true L/R pairs are correlated: FL/FR, BL/BR and SL/SR of named
    layouts, or the two channels of a 2-channel track whose layout is
    unknown (cameras write that). Ambisonic W/Y etc. legitimately correlate
    negatively for sources on one side, so they are never paired.
    """
    count = len(names)
    if count < 2 or treated_as_ambisonic(track):
        return []
    if count == 2:
        return [(0, 1, "L/R")]
    pairs = []
    for left, right in (("FL", "FR"), ("BL", "BR"), ("SL", "SR")):
        if left in names and right in names:
            pairs.append((names.index(left), names.index(right), f"{left}/{right}"))
    return pairs


def _non_lfe(names: Sequence[str]) -> List[int]:
    return [k for k, name in enumerate(names) if name != "LFE"]


def _hum_mix(track: dict, names: Sequence[str]) -> Optional[str]:
    """pan expression averaging the non-LFE channels (LFE legitimately
    carries 50/60 Hz programme). Ambiguous 4-channel tracks use channel 0
    (W for ambisonics), since summing B-format components is meaningless."""
    count = len(names)
    if count == 1:
        return None
    channels = [0] if treated_as_ambisonic(track) else _non_lfe(names)
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
LOUDNORM_MAX_CHANNELS = 6

_SIGNAL_STATS = "DC_offset+Peak_level+Flat_factor+Peak_count+RMS_level"
# Full-scale clip runs, counted exactly: aeval keeps the current run length of
# samples at |x| >= 0.9999 (s16 32767 and the s24 maximum both qualify) in
# expression variable 0 and outputs 1 on the third sample of each run, so
# astats' "Abs Peak count" of that 0/1 signal is the number of runs of
# three or more samples at full scale (Peak level -inf means none).
FULL_SCALE_AMPLITUDE = 0.9999
CLIP_RUN_SAMPLES = 3
_FULL_SCALE_RUNS = (f"aeval=exprs='st(0,if(gte(abs(val(0)),{FULL_SCALE_AMPLITUDE}),ld(0)+1,0));"
                    f"eq(ld(0),{CLIP_RUN_SAMPLES})'")
_RUN_STATS = "measure_perchannel=Abs_Peak_count+Peak_level:measure_overall=none"
_PAIR_STATS = "Peak_level+RMS_level"
_HUM_STATS = "RMS_level+RMS_trough"

Branch = Tuple[str, str]  # (pad label, chain body ending before anullsink)


def _phase_angle(cfg: dict) -> float:
    """aphasemeter flags out-of-phase when phase < cos(angle)."""
    threshold = max(-1.0, min(0.0, _number(cfg, "phase_correlation_min")))
    return max(90.0, min(180.0, math.degrees(math.acos(threshold))))


def _bandpass(freq: float, width: float) -> str:
    stage = f"bandpass=f={freq:g}:width_type=h:width={width:g}"
    return ",".join([stage] * 3)


def _full_scale_run_counter(count: int) -> str:
    """aeval with one expression per channel: each expression has its own
    variable store, so every channel keeps its own run length."""
    expr = ("st(0,if(gte(abs(val({k})),{amp}),ld(0)+1,0));eq(ld(0),{run})")
    exprs = "|".join(expr.format(k=k, amp=FULL_SCALE_AMPLITUDE, run=CLIP_RUN_SAMPLES)
                     for k in range(count))
    return f"aeval=exprs='{exprs}':c=same"


def _signal_branches(i: int, names: Sequence[str], cfg: dict, max_split: int) -> List[Branch]:
    """Per-channel peak, RMS, DC, flat factor and peak count (clipping, DC,
    and the L/R levels of the correlation), then the exact full-scale run
    counter on the same samples."""
    wanted = (CLIPPING, DC_OFFSET, PHASE, DUAL_MONO)
    if not any(check_enabled(cfg, name) for name in wanted):
        return []
    body = (f"aformat=sample_fmts=dblp,astats@a{i}s="
            f"measure_perchannel={_SIGNAL_STATS}:measure_overall=none")
    if check_enabled(cfg, CLIPPING) and len(names) <= max_split:
        body += f",{_full_scale_run_counter(len(names))},astats@a{i}k={_RUN_STATS}"
    return [(f"a{i}s", body)]


def _steady_tone_detector(sample_rate: int) -> str:
    """aeval that outputs 0 while the channel is a steady sinusoid, else 1.

    Per sample it keeps a 10 ms and a 40 ms one-pole average of x^2 and a
    50 ms-decay peak follower. Steady: the two averages agree within
    +-0.43 dB (a level that is not moving). Sinusoidal: peak^2 over mean
    power below 2.6 (a sine is 2, i.e. a 3 dB crest factor; noise, speech
    and chords exceed 9). Audible: above -50 dBFS. Frequency- and
    level-independent above ~200 Hz, which covers line-up tones (1 kHz
    GLITS/EBU R 68, 400 Hz); the 1-pole coefficients are computed for the
    track's sample rate so the expression stays a few multiply-adds.
    """
    rate = max(8000, int(sample_rate or 48000))
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


def _channel_branches(i: int, track: dict, names: Sequence[str], cfg: dict,
                      max_split: int) -> List[Branch]:
    """One mono branch per channel: silence at the channel threshold (dead
    channels, per-channel stretches) and dropouts (not on the LFE, which is
    legitimately near-silent). With dropouts on, the steady-tone detector
    feeds ``silencedetect@a<N>t<K>``, whose "silences" are the stretches
    of steady sinusoid that line-up gaps are judged against."""
    if not 1 <= len(names) <= max_split:
        return []
    noise = _number(cfg, "channel_silence_noise_db")
    duration = float(cfg["silence_duration"])
    dropout = (f"silencedetect@a{i}d{{k}}=n={_number(cfg, 'dropout_noise_db')}dB"
               f":d={_number(cfg, 'dropout_min_ms') / 1000.0}")
    # The detector needs no more than a 12 kHz rate for tones up to ~4 kHz
    # (line-up tones are 1 kHz or 400 Hz); aeval interprets per sample, so
    # this quarters its cost at 48 kHz.
    rate = min(int(track.get("sample_rate") or 48000), TONE_RATE)
    tone = f"aresample={rate},{_steady_tone_detector(rate)}"
    branches = []
    for k, name in enumerate(names):
        body = f"pan=mono|c0=c{k},silencedetect@a{i}c{k}=n={noise}dB:d={duration}"
        if check_enabled(cfg, DROPOUTS) and name != "LFE":
            body += "," + dropout.format(k=k)
            body += f",{tone},silencedetect@a{i}t{k}=n=0.5:d={LINEUP_TONE_MIN_S:g}"
        branches.append((f"a{i}c{k}", body))
    return branches


def _pair_branches(i: int, track: dict, names: Sequence[str], cfg: dict) -> List[Branch]:
    """Per L/R pair: astats over L+R and L-R (with the L and R levels from
    ``astats@a<N>s``: correlation and the dual-mono residual) and
    aphasemeter's sustained out-of-phase regions."""
    if not (check_enabled(cfg, PHASE) or check_enabled(cfg, DUAL_MONO)):
        return []
    branches = []
    for j, (left, right, _) in enumerate(stereo_pairs(track, names)):
        branches.append((f"a{i}p{j}", (
            f"pan=stereo|c0=c{left}+c{right}|c1=c{left}-c{right},aformat=sample_fmts=dblp,"
            f"astats@a{i}p{j}=measure_perchannel={_PAIR_STATS}:measure_overall=none")))
        if check_enabled(cfg, PHASE):
            # pan gives the pair a definite stereo layout: cameras write
            # 2-channel tracks with an unknown layout, which aphasemeter rejects.
            branches.append((f"a{i}q{j}", (
                f"pan=stereo|c0=c{left}|c1=c{right},"
                f"aphasemeter@a{i}p{j}=video=0:phasing=1:angle={_phase_angle(cfg):.4f}"
                f":duration={_number(cfg, 'phase_min_duration'):g}")))
    return branches


def _hum_chains(i: int, track: dict, names: Sequence[str], cfg: dict) -> List[str]:
    """``[a<N>h]`` -> 2 Hz bands at 50/60 Hz and 6 Hz either side."""
    mix = _hum_mix(track, names)
    if not check_enabled(cfg, HUM) or (len(names) != 1 and not mix):
        return []
    bands = [(freq, tag, freq + offset) for freq in HUM_FREQUENCIES
             for tag, offset in (("c", 0), ("l", -HUM_NEIGHBOUR_HZ), ("u", HUM_NEIGHBOUR_HZ))]
    outputs = "".join(f"[a{i}h{freq}{tag}]" for freq, tag, _ in bands)
    # 50-66 Hz bands need no more than a 4 kHz rate: resampling first makes
    # the 18 biquads twelve times cheaper and better conditioned.
    chains = [f"[a{i}h]{mix + ',' if mix else ''}aresample={HUM_RATE},asplit={len(bands)}{outputs}"]
    chains.extend(
        f"[a{i}h{freq}{tag}]{_bandpass(centre, HUM_BAND_HZ)},astats@a{i}h{freq}{tag}="
        f"measure_perchannel={_HUM_STATS}:measure_overall=none:length=1,anullsink"
        for freq, tag, centre in bands)
    return chains


def _main_chain(i: int, track: dict, main: str, cfg: dict, meter: str, loudness: bool) -> str:
    """0.9.0's whole-track silence plus the BS.1770 meter, ending in [a<N>]."""
    chain = f"{main}silencedetect@a{i}=n={float(cfg['silence_noise_db'])}dB:d={float(cfg['silence_duration'])}"
    if loudness and meter in (METER_EBUR128, METER_NONE):
        if meter == METER_EBUR128 and treated_as_ambisonic(track):
            # W only: BS.1770-5 defines no scene-based method; summing B-format
            # components over-reads by ~3.5 LU (benchmark F13).
            chain += ",pan=mono|c0=c0"
        # With METER_NONE the loudness is discarded, but the true peak is exact.
        chain += f",ebur128@a{i}=peak=true+sample:framelog=quiet"
    return chain + f"[a{i}]"


def track_filter_chains(position: int, track: dict, names: Sequence[str], cfg: dict,
                        loudness: bool, max_split: int,
                        multichannel_meter: str = METER_EBUR128) -> List[str]:
    """Every audio branch for one track; the main branch ends in ``[a<N>]``.

    The main branch keeps 0.9.0's whole-track ``silencedetect@a<N>`` and the
    loudness meter; the measurement branches (see the ``_*_branches``
    helpers) split off it with ``asplit`` and end in ``anullsink``, so the
    track is still decoded once.
    """
    i = position
    source = f"[0:{int(track['index'])}]"
    meter = loudness_meter(track, len(names), multichannel_meter) if loudness else METER_NONE
    branches = (_signal_branches(i, names, cfg, max_split) + _channel_branches(i, track, names, cfg, max_split)
                + _pair_branches(i, track, names, cfg))
    if meter == METER_LOUDNORM:
        # loudnorm resamples to 192 kHz internally: keep it off the mapped output.
        branches.append((f"a{i}l", f"loudnorm@a{i}=print_format=summary"))
    hum = _hum_chains(i, track, names, cfg)
    labels = [f"[{label}]" for label, _ in branches] + ([f"[a{i}h]"] if hum else [])
    if not labels:
        return [_main_chain(i, track, source, cfg, meter, loudness)]
    main = f"[a{i}m]"
    chains = [f"{source}asplit={len(labels) + 1}{main}{''.join(labels)}"]
    chains += [f"[{label}]{body},anullsink" for label, body in branches]
    chains += hum
    chains.append(_main_chain(i, track, main, cfg, meter, loudness))
    return chains


def loudness_meter(track: dict, count: int, multichannel_meter: str) -> str:
    """Which BS.1770 meter measures this track.

    ``ebur128`` for mono, stereo and ambisonic W. For three or more channels
    the caller passes what the bundled FFmpeg can do: the darwin-arm64 9.0.2
    build's ebur128 K-weights >2-channel input wrongly (a 1 kHz -23 LUFS
    5.1 reference reads -5.0 LUFS, +33 dB on pink noise; the x86_64 build
    of the same release reads -23.0), so core self-tests the binary and
    falls back to ``loudnorm``'s independent libebur128 meter, which is
    correct for up to six channels but ignores channels 7+ (7.1 reads
    without its side pair). Beyond six channels that leaves ``none``.
    """
    if count <= 2 or treated_as_ambisonic(track):
        return METER_EBUR128
    if multichannel_meter == METER_LOUDNORM and count > LOUDNORM_MAX_CHANNELS:
        return METER_NONE
    return multichannel_meter


_EVIDENCE_RE = re.compile(r"\[(?:astats|aphasemeter)@a\d+[a-z]\d*[a-z]? @ |\[loudnorm@a\d+ @ "
                          r"|^Input (?:Integrated|True Peak|LRA|Threshold):")


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


_SUMMARY_RE = re.compile(r"\[ebur128@a(\d+) @ [0-9A-Fa-fx]+\].*Summary:")
_NUMBER = r"(-?inf|-?\d+(?:\.\d+)?)"
_SECTION_KEYS = {
    "Integrated loudness:": ("I", "integrated"),
    "Loudness range:": ("LRA", "lra"),
    "Sample peak:": ("Peak", "sample_peak"),
    "True peak:": ("Peak", "true_peak"),
}


def parse_loudness(log_text: str) -> Dict[int, dict]:
    """I, LRA, sample peak and true peak per ``ebur128@a<N>`` summary.

    The summary prints "Sample peak:" before "True peak:", each followed by
    a "Peak:" line, so values are attributed by section, not by order.
    """
    results: Dict[int, dict] = {}
    current, section = None, None
    for line in log_text.splitlines():
        header = _SUMMARY_RE.search(line)
        if header:
            current, section = int(header.group(1)), None
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
_LOUDNORM_KEYS = {"Input Integrated": "integrated", "Input True Peak": "true_peak",
                  "Input LRA": "lra"}


def parse_loudnorm(log_text: str) -> Dict[int, dict]:
    """Input I / true peak / LRA per ``loudnorm@a<N>`` summary (the
    multichannel fallback meter; see ``loudness_meter``)."""
    results: Dict[int, dict] = {}
    current = None
    for line in log_text.splitlines():
        header = _LOUDNORM_RE.search(line)
        if header:
            current = int(header.group(1))
            results[current] = {"meter": METER_LOUDNORM}
            continue
        if current is None:
            continue
        key, _, value = line.strip().partition(":")
        if key in _LOUDNORM_KEYS:
            match = re.match(rf"\s*{_NUMBER}", value.replace("+", ""))
            if match:
                results[current][_LOUDNORM_KEYS[key]] = float(match.group(1))
            if key == "Input LRA":
                current = None
    return results


def parse_meters(log_text: str) -> Dict[int, dict]:
    """Loudness per track from whichever meter measured it."""
    merged = {track: dict(values, meter=METER_EBUR128) for track, values in parse_loudness(log_text).items()}
    merged.update(parse_loudnorm(log_text))
    return merged


# ---------------------------------------------------------------------------
# Evidence handed over by core
# ---------------------------------------------------------------------------

Events = List[Tuple[float, float]]


@dataclass
class ScanEvidence:
    """What core captured for one scan.

    ``silence_events(instance, end[, join_gap])`` returns merged (start,
    duration) silencedetect regions for a filter instance, open regions
    closed at ``end`` and regions closer than ``join_gap`` (0.25 s) fused; ``detect_events(text, prefix)`` pairs ``<prefix>_start/_end``
    tokens. Both are core's own parsers, so there is one implementation.
    """

    log_text: str
    silence_events: Callable[..., Events]
    detect_events: Callable[[str, str], Events]


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
    if is_ambisonic(ctx.track):
        return (" (ambisonic: W-channel loudness; BS.1770-5 defines no scene-based method)",
                LOW_CONFIDENCE)
    if treated_as_ambisonic(ctx.track):
        return (f" ({len(ctx.names)}-channel track treated as ambisonics: W-channel loudness; "
                f"BS.1770-5 defines no scene-based method)", LOW_CONFIDENCE)
    if ctx.duration and ctx.duration < 3.0:
        return " (under 3 s: gating is unreliable)", LOW_CONFIDENCE
    return "", None


def _unmeasured_reason(ctx: TrackContext, what: str) -> str:
    if ctx.meter == METER_NONE and what == "loudness summary":
        return (f"loudness not measured: the bundled FFmpeg's ebur128 mis-measures audio with more "
                f"than two channels and the fallback meter handles at most "
                f"{LOUDNORM_MAX_CHANNELS} ({len(ctx.names)} here)")
    return f"{what} not reported by FFmpeg"


def track_sample_peak(evidence: "ScanEvidence", ctx: TrackContext) -> Optional[float]:
    peaks = [c.get("peak_db") for c in parse_astats(evidence.log_text, f"astats@a{ctx.position}s")]
    peaks = [p for p in peaks if p is not None]
    return max(peaks) if peaks else None


def loudness_row(measured: Optional[dict], ctx: TrackContext, cfg: dict) -> Row:
    """Integrated loudness against the delivery profile."""
    mode = cfg.get("loudness_mode", "report")
    profile = resolve_profile(cfg)
    if not measured or "integrated" not in measured or ctx.meter == METER_NONE:
        reason = _unmeasured_reason(ctx, "loudness summary")
        if mode == "report":
            return "SKIP", _with_label(ctx.label, reason[0].upper() + reason[1:] + "."), 100
        return "WARN", _with_label(ctx.label, f"Not verified: {reason}."), 100
    integrated = measured["integrated"]
    lra = measured.get("lra")
    lra_text = f", LRA {lra:.1f} LU" if lra is not None else ""
    if integrated <= -69.9:
        return "SKIP", _with_label(ctx.label, "No measurable programme loudness: the track is silent "
                                              "(see Audio Silence)."), 100
    method = "programme loudness (not dialogue-gated)" if profile.dialogue_gated else "integrated"
    caveat, cap = _loudness_caveat(ctx)
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


def _full_scale_runs(runs: List[Dict[str, float]], k: int) -> Optional[int]:
    """Runs counted for channel ``k`` by the aeval counter (None: not run)."""
    if k >= len(runs):
        return None
    peak = runs[k].get("peak_db")
    if peak is None or not math.isfinite(peak):
        return 0
    return int(runs[k].get("abs_peak_count", 0))


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
    counted = parse_astats(evidence.log_text, f"astats@a{ctx.position}k")
    full, isolated, below, unmeasured = [], [], [], 0
    for k in range(len(ctx.names)):
        runs = _full_scale_runs(counted, k)
        if runs is None:
            unmeasured += 1
        elif runs >= minimum:
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

    if unmeasured == len(ctx.names) and not stats:
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


def phase_row(evidence: ScanEvidence, ctx: TrackContext, cfg: dict) -> Row:
    pairs = stereo_pairs(ctx.track, ctx.names)
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
        text = evidence.log_text and "\n".join(_context_lines(evidence.log_text, f"aphasemeter@a{ctx.position}p{j}"))
        events = evidence.detect_events(text or "", "out_phase")
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
    pairs = stereo_pairs(ctx.track, ctx.names)
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
        if ctx.names[k] == "LFE" or k in ctx.dead_channels:
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
        if name == "LFE" or k in ctx.dead_channels:
            continue
        events = evidence.silence_events(f"silencedetect@a{ctx.position}c{k}", ctx.duration)
        alone = [e for e in _subtract(events, track_events) if e[1] >= longest]
        if alone:
            texts.append(f"{name} silent (≤ {noise:g} dBFS) while other channels play: {_event_list(alone)}")
    return texts


def hum_row(evidence: ScanEvidence, ctx: TrackContext, cfg: dict) -> Row:
    """Mains hum: a steady tone in a 2 Hz band at 50 or 60 Hz.

    The mains band must stand ``HUM_PROMINENCE_DB`` above both 2 Hz bands
    6 Hz away (broadband programme fills all three alike; a tone fills one),
    sit above ``hum_min_db``, and be steady: its 1 s RMS trough within 6 dB
    of its mean, which a bass line or a passing note is not. The rules and
    the -65 dBFS floor are inferred (Qualify AQ-118 publishes only
    "50/60 Hz +-1 Hz"), so the result is advisory with confidence below 70.
    """
    floor = _number(cfg, "hum_min_db")
    found, measured = [], []
    for freq in HUM_FREQUENCIES:
        bands = {}
        for tag in ("c", "l", "u"):
            stats = parse_astats(evidence.log_text, f"astats@a{ctx.position}h{freq}{tag}")
            bands[tag] = stats[0] if stats else {}
        level = bands["c"].get("rms_db")
        trough = bands["c"].get("trough_db")
        neighbours = [bands[t].get("rms_db") for t in ("l", "u")]
        if level is None or any(n is None for n in neighbours):
            continue
        if not math.isfinite(level):
            measured.append(f"{freq} Hz silent")
            continue
        prominence = level - max(n if math.isfinite(n) else -math.inf for n in neighbours)
        measured.append(f"{freq} Hz {level:.1f} dBFS")
        steady = trough is not None and math.isfinite(trough) and level - trough <= HUM_STATIONARY_DB
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

def signal_rows(evidence: ScanEvidence, contexts: List[TrackContext], cfg: dict) -> Dict[str, Row]:
    """One aggregated row per enabled signal check."""
    rows: Dict[str, List[Row]] = {name: [] for name in enabled_signal_checks(cfg)}
    for ctx in contexts:
        stats = parse_astats(evidence.log_text, f"astats@a{ctx.position}s")
        if CLIPPING in rows:
            rows[CLIPPING].append(clipping_row(stats, evidence, ctx, cfg))
        if DC_OFFSET in rows:
            rows[DC_OFFSET].append(dc_offset_row(stats, ctx, cfg))
        if DROPOUTS in rows:
            rows[DROPOUTS].append(dropout_row(evidence, ctx, cfg))
        if PHASE in rows:
            rows[PHASE].append(phase_row(evidence, ctx, cfg))
        if DUAL_MONO in rows:
            rows[DUAL_MONO].append(dual_mono_row(evidence, ctx, cfg))
        if HUM in rows:
            rows[HUM].append(hum_row(evidence, ctx, cfg))
    return {name: aggregate(entries) for name, entries in rows.items() if entries}


def disabled_rows(cfg: dict) -> Dict[str, Row]:
    return {name: ("SKIP", "Disabled in settings.", 100)
            for name in CHECK_SWITCHES if not check_enabled(cfg, name)}
