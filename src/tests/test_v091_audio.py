"""0.9.1 audio signal QC (frameguard.audio_checks), run against the REAL FFmpeg.

Each detector gets a positive clip, a clean negative and, where the rule has
a numeric limit, clips on both sides of it. Clips are synthesized sample-
exactly with ``aevalsrc`` into 24-bit PCM MOV so the decoded samples are the
generated ones (no lossy codec in the way).
"""

import pytest

from frameguard import audio_checks, core, presets
from frameguard.core import DEFAULT_CONFIG, run_qa_checks
from media_fixtures import MediaLibrary, ffmpeg, needs_ffmpeg

SECONDS = 4
MINUS_23_DBFS = 0.0707946  # 1 kHz at this amplitude in L and R reads -23.0 LUFS (Tech 3341 #1)


def _cfg(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(target_width=96, target_height=64, target_fps=10.0, min_bitrate_mbps=0,
               accepted_containers=[".mov"])
    cfg.update(overrides)
    return cfg


def _scan(path, **overrides):
    result = run_qa_checks(path, cfg=_cfg(**overrides))
    result.finalize()
    return result


def _row(result, name):
    for check in result.checks:
        if check[0] == name:
            return check
    raise AssertionError(f"{name} missing: {[c[:2] for c in result.checks]}")


def _graph(command):
    return command[command.index("-filter_complex") + 1]


def make_audio(path, exprs, *, layout="stereo", rate=48000, seconds=SECONDS):
    """Tiny video plus one PCM track whose channels are the aevalsrc ``exprs``."""
    path = str(path) + ".mov"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=96x64:r=10:d={seconds}",
           "-f", "lavfi", "-i", f"aevalsrc=exprs='{'|'.join(exprs)}':s={rate}:d={seconds}:c={layout}",
           "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "pcm_s24le", "-t", str(seconds), path)
    return path


def tone(freq=1000, amp=0.25):
    return f"{amp}*sin(2*PI*{freq}*t)"


NOISE = "0.1*(random(0)-0.5)"


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg091audio")))


@pytest.fixture(scope="module")
def clean(media):
    """Two different tones: a clean stereo programme for every detector."""
    return media.get("clean", lambda p: make_audio(p, [tone(440), tone(554.37)]))


# ---------------------------------------------------------------------------
# Delivery profiles and configuration
# ---------------------------------------------------------------------------

class TestProfiles:
    def test_profile_values_follow_the_standards(self):
        expected = {
            "ebu_r128": (-23.0, 0.5, -1.0, 48000), "atsc_a85": (-24.0, 2.0, -2.0, 48000),
            "netflix": (-27.0, 2.0, -2.0, 48000), "streaming": (-14.0, 1.0, -1.0, None),
            "streaming_16": (-16.0, 1.0, -1.0, None),
        }
        for key, (target, tol, peak, rate) in expected.items():
            profile = audio_checks.resolve_profile({"loudness_profile": key})
            assert (profile.target, profile.tolerance, profile.true_peak_max, profile.sample_rate) == \
                (target, tol, peak, rate), key
        assert audio_checks.resolve_profile({"loudness_profile": "netflix"}).dialogue_gated

    def test_custom_uses_the_legacy_keys(self):
        profile = audio_checks.resolve_profile(_cfg(loudness_profile="custom", target_loudness_lufs=-20,
                                                    loudness_tolerance_lu=1.5, true_peak_max_dbtp=-3))
        assert (profile.target, profile.tolerance, profile.true_peak_max) == (-20, 1.5, -3)

    def test_defaults(self):
        assert DEFAULT_CONFIG["loudness_mode"] == "report"
        assert DEFAULT_CONFIG["loudness_profile"] == "ebu_r128"
        assert DEFAULT_CONFIG["channel_silence_noise_db"] == -70.0
        assert core.LOUDNESS_PROFILES == audio_checks.LOUDNESS_PROFILE_CHOICES

    def test_preset_round_trip_and_rejection(self):
        config = presets.validate_preset_config({"loudness_profile": "ATSC A85", "audio_hum_check": False,
                                                 "dropout_noise_db": -96, "clipping_min_events": 5})
        assert config == {"loudness_profile": "atsc_a85", "audio_hum_check": False,
                          "dropout_noise_db": -96, "clipping_min_events": 5}
        with pytest.raises(presets.PresetValidationError) as error:
            presets.validate_preset_config({"loudness_profile": "bogus",
                                            "dropout_noise_db": "-90dB,ametadata=mode=print",
                                            "phase_correlation_min": 0.5})
        assert len(error.value.problems) == 3

    def test_090_custom_target_without_a_profile_stays_custom(self):
        assert presets.normalize_preset_config({"target_loudness_lufs": -24})["loudness_profile"] == "custom"
        assert presets.validate_preset_config({"loudness_tolerance_lu": 1})["loudness_profile"] == "custom"
        assert "loudness_profile" not in presets.normalize_preset_config({"loudness_mode": "advisory"})


# ---------------------------------------------------------------------------
# Loudness / true-peak verdict rules (fabricated measurements)
# ---------------------------------------------------------------------------

def _ctx(names=("FL", "FR"), duration=60.0, **extra):
    return audio_checks.TrackContext(position=0, track={"channel_layout": "stereo"}, names=list(names),
                                     label="", duration=duration, **extra)


class TestLoudnessRules:
    @pytest.mark.parametrize("integrated,status,confidence", [
        (-23.4, "PASS", 100),   # inside +-0.5 LU
        (-23.55, "FAIL", 60),   # beyond by 0.05 LU: inside the meter's +-0.1 LU
        (-23.7, "FAIL", 95),
        (-21.0, "FAIL", 95),
    ])
    def test_ebu_r128_boundaries(self, integrated, status, confidence):
        row = audio_checks.loudness_row({"integrated": integrated, "lra": 5.0}, _ctx(),
                                        _cfg(loudness_mode="essential"))
        assert row[0] == status and row[2] == confidence, row

    def test_report_mode_never_judges(self):
        row = audio_checks.loudness_row({"integrated": -3.0}, _ctx(), _cfg())
        assert row[0] == "PASS" and "not enforced" in row[1]

    def test_netflix_says_it_is_not_dialogue_gated(self):
        row = audio_checks.loudness_row({"integrated": -24.0}, _ctx(),
                                        _cfg(loudness_mode="essential", loudness_profile="netflix"))
        assert row[0] == "FAIL" and "not dialogue-gated" in row[1]
        assert row[2] < 70  # a near miss on the full mix is not proof of a dialogue miss
        far = audio_checks.loudness_row({"integrated": -15.0}, _ctx(),
                                        _cfg(loudness_mode="essential", loudness_profile="netflix"))
        assert far[2] >= 70

    def test_silent_track_is_left_to_audio_silence(self):
        row = audio_checks.loudness_row({"integrated": -70.0}, _ctx(), _cfg(loudness_mode="essential"))
        assert row[0] == "SKIP"

    def test_four_channel_track_never_fails_loudness(self):
        ctx = audio_checks.TrackContext(position=0, track={"channels": 4, "channel_layout": "4.0"},
                                        names=["FL", "FR", "FC", "BC"], label="", duration=60.0)
        row = audio_checks.loudness_row({"integrated": -10.0}, ctx, _cfg(loudness_mode="essential"))
        assert row[0] == "WARN" and row[2] < 70 and "W-channel loudness" in row[1]

    @pytest.mark.parametrize("peak,status,confidence", [
        (-1.2, "PASS", 100), (-0.9, "FAIL", 60), (-0.5, "FAIL", 95), (2.0, "FAIL", 95)])
    def test_true_peak_boundaries(self, peak, status, confidence):
        row = audio_checks.true_peak_row({"true_peak": peak, "sample_peak": -3.0}, _ctx(),
                                         _cfg(loudness_mode="essential"))
        assert row[0] == status and row[2] == confidence, row

    def test_summary_parsing_attributes_peaks_by_section(self):
        log = "\n".join([
            "[ebur128@a0 @ 0x1] [info] Summary:", "", "  Integrated loudness:",
            "    I:         -21.3 LUFS", "    Threshold: -31.4 LUFS", "", "  Loudness range:",
            "    LRA:         6.1 LU", "", "  Sample peak:", "    Peak:       -2.5 dBFS", "",
            "  True peak:", "    Peak:       -1.4 dBFS"])
        assert audio_checks.parse_loudness(log) == {
            0: {"integrated": -21.3, "lra": 6.1, "sample_peak": -2.5, "true_peak": -1.4}}
        loudnorm = "\n".join(["[loudnorm@a1 @ 0x2] [info] ", "Input Integrated:    -23.1 LUFS",
                              "Input True Peak:     +0.4 dBTP", "Input LRA:             3.0 LU"])
        assert audio_checks.parse_loudnorm(loudnorm)[1]["true_peak"] == 0.4


# ---------------------------------------------------------------------------
# Loudness, true peak and sample rate on real files
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestLoudnessOnFiles:
    def test_reference_tone_meets_r128(self, media):
        clip = media.get("ref23", lambda p: make_audio(p, [tone(1000, MINUS_23_DBFS)] * 2))
        result = _scan(clip, loudness_mode="essential")
        assert _row(result, "Audio Loudness")[1] == "PASS", _row(result, "Audio Loudness")
        assert abs(result.metadata["integrated_loudness_lufs"] + 23.0) <= 0.1
        assert _row(result, "Audio True Peak")[1] == "PASS"

    def test_hot_tone_fails_loudness_and_true_peak_only_when_enforced(self, media):
        clip = media.get("ref_hot", lambda p: make_audio(p, [tone(1000, 0.95)] * 2))
        enforced = _scan(clip, loudness_mode="essential")
        assert _row(enforced, "Audio Loudness")[1] == "FAIL"
        assert _row(enforced, "Audio True Peak")[1] == "FAIL"
        assert enforced.status == "MISMATCH"
        reported = _scan(clip)
        assert _row(reported, "Audio Loudness")[1] == "PASS"
        assert _row(reported, "Audio True Peak")[1] == "PASS"

    def test_multichannel_loudness_is_measured_correctly(self, media):
        """The arm64 9.0.2 ebur128 reads this 5.1 reference as -5.0 LUFS;
        the self-tested fallback must read -23."""
        clip = media.get("ref51", lambda p: make_audio(
            p, [tone(1000, MINUS_23_DBFS)] * 2 + ["0"] * 4, layout="5.1"))
        result = _scan(clip, loudness_mode="essential")
        assert abs(result.metadata["integrated_loudness_lufs"] + 23.0) <= 0.15, result.metadata
        assert _row(result, "Audio Loudness")[1] == "PASS"

    def test_first_order_ambisonics_is_measured_on_w(self, media):
        """BS.1770 weighting must never be applied to B-format: W alone,
        here a -23 dBFS 1 kHz tone, reads -26.0 LUFS as a mono programme."""
        clip = media.get("foa_w", lambda p: make_audio(
            p, [tone(1000, MINUS_23_DBFS), NOISE, "0", NOISE.replace("0.1", "0.05")], layout="4.0"))
        result = _scan(clip, loudness_mode="essential")
        assert abs(result.metadata["integrated_loudness_lufs"] + 26.0) <= 0.15, result.metadata
        row = _row(result, "Audio Loudness")
        assert row[1] == "WARN" and row[3] < 70 and "W-channel loudness" in row[2], row

    def test_self_test_detects_a_broken_meter(self, monkeypatch):
        monkeypatch.setattr(core, "_EBUR128_MULTICHANNEL_OK", {"ffmpeg": False})
        tracks = [{"codec_type": "audio", "index": 1, "channels": 6, "channel_layout": "5.1"},
                  {"codec_type": "audio", "index": 2, "channels": 8, "channel_layout": "7.1"}]
        plan = core._ScanPlan({"streams": tracks}, _cfg())
        graph = _graph(core._build_scan_command("ffmpeg", "x.mov", plan, _cfg()))
        assert "loudnorm@a0" in graph and "ebur128@a0" not in graph
        # Beyond loudnorm's six channels only the (exact) true peak is kept.
        assert "loudnorm@a1" not in graph and "ebur128@a1" in graph

    @pytest.mark.parametrize("mode,status", [("report", "PASS"), ("advisory", "WARN"), ("essential", "FAIL")])
    def test_delivery_profile_requires_48k(self, media, mode, status):
        clip = media.get("sr44", lambda p: make_audio(p, [tone(440), tone(554.37)], rate=44100))
        assert _row(_scan(clip, loudness_mode=mode), "Audio Sample Rate")[1] == status

    def test_streaming_profile_accepts_44k(self, media):
        clip = media.get("sr44", lambda p: make_audio(p, [tone(440), tone(554.37)], rate=44100))
        row = _row(_scan(clip, loudness_mode="essential", loudness_profile="streaming"), "Audio Sample Rate")
        assert row[1] == "PASS"


# ---------------------------------------------------------------------------
# Signal checks
# ---------------------------------------------------------------------------

def _runs_at_full_scale(count):
    """A -12 dBFS tone with ``count`` runs of exactly three samples at 0 dBFS."""
    marks = "+".join(f"between(n,{24000 * (k + 1)},{24000 * (k + 1) + 2})" for k in range(count))
    return f"if({marks},1,{tone(1000, 0.25)})"


@needs_ffmpeg
class TestClipping:
    @pytest.mark.parametrize("runs,status", [(2, "PASS"), (3, "FAIL")])
    def test_run_count_boundary(self, media, runs, status):
        clip = media.get(f"clip{runs}", lambda p: make_audio(p, [_runs_at_full_scale(runs), tone(554.37)]))
        row = _row(_scan(clip, loudness_mode="essential"), "Audio Clipping")
        assert row[1] == status, row
        if status == "FAIL":
            assert "FL 3 run(s)" in row[2] and row[3] == 75

    def test_hard_clipped_programme_fails_confidently(self, media):
        clip = media.get("hardclip", lambda p: make_audio(p, [tone(1000, 2.0), tone(554.37)]))
        enforced = _scan(clip, loudness_mode="essential")
        row = _row(enforced, "Audio Clipping")
        assert row[1] == "FAIL" and row[3] == 90, row
        assert enforced.status == "MISMATCH"
        assert _row(_scan(clip, loudness_mode="advisory"), "Audio Clipping")[1] == "WARN"

    def test_camera_originals_are_reported_not_judged(self, media):
        clip = media.get("hardclip", lambda p: make_audio(p, [tone(1000, 2.0), tone(554.37)]))
        row = _row(_scan(clip), "Audio Clipping")
        assert row[1] == "PASS" and "Clipped at full scale" in row[2] and "not enforced" in row[2]

    def test_clipped_then_attenuated_warns(self, media):
        attenuated = "0.5*max(-1,min(1,2*sin(2*PI*1000*t)))"
        clip = media.get("clip_atten", lambda p: make_audio(p, [attenuated, tone(554.37)]))
        row = _row(_scan(clip, loudness_mode="essential"), "Audio Clipping")
        assert row[1] == "WARN" and "below full scale" in row[2] and row[3] < 70, row

    def test_true_peak_test_tone_is_not_flat_topped(self, media):
        """fs/4 at 45 degrees samples as +A, +A, -A, -A: runs of exactly two."""
        clip = media.get("fs4", lambda p: make_audio(p, ["0.5*sin(2*PI*12000*t+PI/4)"] * 2))
        assert _row(_scan(clip, loudness_mode="essential"), "Audio Clipping")[1] == "PASS"

    def test_clean_is_not_clipped(self, clean):
        assert _row(_scan(clean, loudness_mode="essential"), "Audio Clipping")[1] == "PASS"


@needs_ffmpeg
class TestDropouts:
    def test_twenty_ms_gap_is_found_with_its_timecode(self, media):
        gap = f"if(between(t,2,2.02),0,{tone(440)})"
        clip = media.get("drop20", lambda p: make_audio(p, [gap, gap.replace("440", "554.37")]))
        row = _row(_scan(clip), "Audio Dropouts")
        assert row[1] == "WARN" and "00:00:02.0" in row[2] and "FL+FR" in row[2], row

    def test_leading_digital_silence_is_a_fade_not_a_dropout(self, media):
        head = f"if(lt(t,0.03),0,{tone(440)})"
        clip = media.get("drophead", lambda p: make_audio(p, [head, tone(554.37)]))
        assert _row(_scan(clip), "Audio Dropouts")[1] == "PASS"

    def test_long_gap_is_silence_not_a_dropout(self, media):
        gap = f"if(between(t,0.8,3.2),0,{tone(440)})"
        clip = media.get("droplong", lambda p: make_audio(p, [gap, gap.replace("440", "554.37")]))
        result = _scan(clip)
        assert _row(result, "Audio Dropouts")[1] == "PASS"
        assert _row(result, "Audio Silence")[1] == "WARN"

    def test_clean_has_no_dropouts(self, clean):
        assert _row(_scan(clean), "Audio Dropouts")[1] == "PASS"


@needs_ffmpeg
class TestPhaseAndDualMono:
    def test_inverted_channel(self, media):
        clip = media.get("inverted", lambda p: make_audio(p, [NOISE, NOISE.replace("0.1", "-0.09")]))
        row = _row(_scan(clip), "Audio Phase")
        assert row[1] == "WARN" and "polarity-inverted" in row[2] and row[3] == 85, row

    def test_identical_channels_are_dual_mono(self, media):
        clip = media.get("dualmono", lambda p: make_audio(p, [tone(440)] * 2))
        row = _row(_scan(clip), "Dual Mono")
        assert row[1] == "WARN" and row[3] == 95 and "-inf" in row[2], row

    def test_level_offset_dual_mono_is_found_by_correlation(self, media):
        clip = media.get("dualmono_gain", lambda p: make_audio(p, [NOISE, NOISE.replace("0.1", "0.08")]))
        row = _row(_scan(clip), "Dual Mono")
        assert row[1] == "WARN" and "correlation" in row[2], row

    def test_clean_stereo(self, clean):
        result = _scan(clean)
        assert _row(result, "Audio Phase")[1] == "PASS"
        assert _row(result, "Dual Mono")[1] == "PASS"

    def test_four_channel_tracks_are_never_paired(self, media):
        """W and Y of first-order ambisonics correlate negatively for a
        source on the right; pairing them would be a false alarm."""
        clip = media.get("foa", lambda p: make_audio(
            p, [NOISE, NOISE.replace("0.1", "-0.07"), "0", "0"], layout="4.0"))
        result = _scan(clip)
        assert _row(result, "Audio Phase")[1] == "SKIP"
        assert _row(result, "Dual Mono")[1] == "SKIP"


@needs_ffmpeg
class TestDcAndHum:
    @pytest.mark.parametrize("dc,status", [(0.0025, "PASS"), (0.004, "WARN")])  # -52 / -48 dBFS vs -50
    def test_dc_boundary(self, media, dc, status):
        clip = media.get(f"dc{dc}", lambda p: make_audio(p, [f"{dc}+{tone(440)}", tone(554.37)]))
        assert _row(_scan(clip), "DC Offset")[1] == status

    def test_steady_60hz_hum_under_programme(self, media):
        hum = f"{NOISE}+0.014*sin(2*PI*60*t)+0.005*sin(2*PI*120*t)"
        clip = media.get("hum", lambda p: make_audio(p, [hum, hum.replace("random(0)", "random(1)")]))
        row = _row(_scan(clip), "Audio Hum")
        assert row[1] == "WARN" and "60 Hz" in row[2] and row[3] < 70, row

    def test_moving_bass_line_is_not_hum(self, media):
        glide = "0.2*sin(2*PI*(50*t+1.25*t*t))"  # sweeps 50 -> 60 Hz over 4 s
        clip = media.get("bass", lambda p: make_audio(p, [glide, glide]))
        assert _row(_scan(clip), "Audio Hum")[1] == "PASS"

    def test_clean_has_no_hum(self, clean):
        assert _row(_scan(clean), "Audio Hum")[1] == "PASS"


@needs_ffmpeg
class TestPerChannelSilence:
    def test_one_channel_silent_for_three_seconds(self, media):
        right = f"if(between(t,0.5,3.5),0,{tone(554.37)})"
        clip = media.get("rsilent", lambda p: make_audio(p, [tone(440), right]))
        row = _row(_scan(clip), "Audio Silence")
        assert row[1] == "WARN" and "FR silent" in row[2] and "00:00:00.5" in row[2], row

    def test_quiet_but_live_channel_is_not_silent(self, media):
        """-60 dBFS room tone is above the -70 dBFS per-channel threshold."""
        clip = media.get("quietR", lambda p: make_audio(p, [tone(440), "0.001*sin(2*PI*554*t)"]))
        result = _scan(clip)
        assert _row(result, "Audio Silence")[1] == "PASS"
        assert _row(result, "Dead Channel")[1] == "PASS"

    def test_silent_lfe_is_not_reported(self, media):
        clip = media.get("lfe0", lambda p: make_audio(
            p, [tone(440), tone(554), tone(659), "0", tone(740), tone(831)], layout="5.1"))
        result = _scan(clip)
        assert _row(result, "Audio Silence")[1] == "PASS"
        assert _row(result, "Dead Channel")[1] == "PASS"
        assert _row(result, "Audio Dropouts")[1] == "PASS"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class TestWiring:
    def test_switches_disable_checks(self, clean):
        result = _scan(clean, audio_hum_check=False, audio_phase_check=False)
        assert _row(result, "Audio Hum")[1] == "SKIP"
        assert _row(result, "Audio Phase")[1] == "SKIP"
        plan = core._ScanPlan({"streams": [{"codec_type": "audio", "index": 0, "channels": 2}]},
                              _cfg(audio_hum_check=False, audio_phase_check=False))
        graph = _graph(core._build_scan_command(
            "ffmpeg", "x.mov", plan, _cfg(audio_hum_check=False, audio_phase_check=False)))
        assert "bandpass" not in graph and "aphasemeter" not in graph

    def test_quick_scan_marks_audio_checks_not_run_by_choice(self, clean):
        result = run_qa_checks(clean, cfg=_cfg(), quick=True)
        result.finalize()
        for name in ("Audio True Peak", "Audio Clipping", "Audio Hum"):
            assert name in result.metadata["not_run_by_choice"]

    def test_lfe_is_excluded_from_dropouts_and_hum(self):
        track = {"codec_type": "audio", "index": 0, "channels": 6, "channel_layout": "5.1"}
        plan = core._ScanPlan({"streams": [track]}, _cfg())
        graph = _graph(core._build_scan_command("ffmpeg", "x.mov", plan, _cfg()))
        assert "silencedetect@a0d3" not in graph and "silencedetect@a0d2" in graph
        assert "*c3" not in graph.split("[a0h]")[1].split(";")[0]

    def test_audio_records_survive_the_bounded_capture(self):
        assert core._stderr_evidence_categories("[astats@a0s @ 0x1] [info] Peak level dB: -1.0") == ("audio",)
        assert "audio" in core._stderr_evidence_categories("[aphasemeter@a0p0 @ 0x2] out_phase_start: 1")
        assert core._stderr_evidence_categories("Input Integrated:    -23.1 LUFS") == ("audio",)

    def test_classification_weights_and_defaults(self):
        for name in audio_checks.ESSENTIAL_CHECKS | audio_checks.ADVISORY_CHECKS:
            assert name in core.CHECK_SEVERITY_WEIGHTS
            assert (name in core.ESSENTIAL_CHECKS) != (name in core.ADVISORY_CHECKS)
        for key in audio_checks.DEFAULT_CONFIG:
            assert key in DEFAULT_CONFIG and key in presets.PRESET_CONFIG_KEYS
