"""0.9.2 audio fixes from the independent 0.9.1 review (r091_audio, REVIEW §3).

Each class pins one defect: it failed on release/0.9.2 before its fix. Clips
are synthesized sample-exactly with ``aevalsrc`` into 24-bit PCM MOV and run
through the REAL bundled FFmpeg (on Apple Silicon that is the darwin-arm64
9.0.2 build whose multichannel ebur128 is broken, so the fallback meter is
exercised for real there).
"""

import pytest

from frameguard import audio_checks, core
from frameguard.core import DEFAULT_CONFIG
from media_fixtures import MediaLibrary, needs_ffmpeg
from test_v091_audio import NOISE, _row, _scan, make_audio, tone


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return MediaLibrary(str(tmp_path_factory.mktemp("fg092audio")))


def _uniform_noise(rms_dbfs: float, seed: int) -> str:
    """Independent white noise at an RMS level: uniform on [-a/2, a/2] has
    RMS a / (2 sqrt 3). ``random(i)`` starts from the variable's value, so
    each stream is seeded once at the first sample. The seed is a statement
    ("...;"), so other terms must follow the noise: "<noise>+<tone>"."""
    amplitude = 10 ** (rms_dbfs / 20.0) * 2 * 3 ** 0.5
    return f"if(eq(n,0),st({seed},{seed * 7919 + 13}));{amplitude:.8g}*(random({seed})-0.5)"


# ---------------------------------------------------------------------------
# 1. Whole-track silence threshold (review B2, REVIEW #3)
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestRoomToneIsNotSilence:
    def test_default_threshold_is_minus_70(self):
        assert DEFAULT_CONFIG["silence_noise_db"] == -70

    def test_quiet_room_tone_is_audio(self, media):
        """-64 dBFS RMS room tone (peaks about -59 dBFS) carries programme:
        0.9.1's -50 dB peak threshold called it "no audio" and FAILed."""
        room = media.get("roomtone64", lambda p: make_audio(
            p, [_uniform_noise(-64, 1), _uniform_noise(-64, 2)]))
        result = _scan(room)
        silence = _row(result, "Audio Silence")
        assert silence[1] == "PASS", silence
        assert result.status != "MISMATCH"


# ---------------------------------------------------------------------------
# 2. "More than half silent" FAILs at any length (REVIEW §4: unjustified > 30 s gate)
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestMostlySilentShortFile:
    def test_eight_second_clip_75_percent_silent_fails(self, media):
        """6 s of digital silence in an 8 s clip: the >50 % rule applied only
        to files longer than 30 s, so a short clip that is mostly silence
        only warned."""
        gap = f"if(between(t,1,7),0,{tone(440)})"
        clip = media.get("mostly_silent8", lambda p: make_audio(
            p, [gap, gap.replace("440", "554.37")], seconds=8))
        row = _row(_scan(clip), "Audio Silence")
        assert row[1] == "FAIL" and "Extended silence" in row[2], row

    def test_short_pause_still_warns(self, media):
        gap = f"if(between(t,1,4),0,{tone(440)})"
        clip = media.get("pause8", lambda p: make_audio(
            p, [gap, gap.replace("440", "554.37")], seconds=8))
        assert _row(_scan(clip), "Audio Silence")[1] == "WARN"


# ---------------------------------------------------------------------------
# 3. Severity weights follow delivery rejection risk (REVIEW §4: weights cut)
# ---------------------------------------------------------------------------

class TestAudioSeverityWeights:
    W = core.CHECK_SEVERITY_WEIGHTS

    def test_published_limits_outrank_content_advisories(self):
        w = self.W
        assert w["Audio Loudness"] >= w["Audio True Peak"] >= w["Audio Clipping"]
        assert w["Audio Clipping"] > w["Audio Dropouts"] > w["Audio Phase"]
        assert w["Audio Phase"] > max(w["Dual Mono"], w["Audio Hum"]) >= w["DC Offset"]

    def test_a_loudness_rejection_outranks_a_codec_or_rate_note(self):
        assert core.check_severity("Audio Loudness", "FAIL") > core.check_severity("Audio Codec", "FAIL")
        assert core.check_severity("Audio Loudness", "FAIL") > core.check_severity("Frame Rate", "FAIL")
        # ...but an undecodable file still comes first.
        assert core.check_severity("File Integrity", "FAIL") > core.check_severity("Audio Loudness", "FAIL")

    def test_loudness_weight_is_owned_by_the_audio_module(self):
        assert audio_checks.SEVERITY_WEIGHTS["Audio Loudness"] == self.W["Audio Loudness"]


# ---------------------------------------------------------------------------
# 8. LFE recognised by layout position, not only by a name 0.9.1 knew (review B4)
# ---------------------------------------------------------------------------

FRONT_TONES = [tone(f) for f in (440, 554.37, 659.26, 739.99, 830.61, 987.77, 1108.7, 1318.5,
                                 1479.98, 1661.2, 1760.0)]
GAPPY_LFE = "if(lt(mod(t,0.5),0.1),0,0.3*sin(2*PI*60*t))"   # 60 Hz bursts with 100 ms rests


def _with_lfe(lfe: str, count: int, at: int) -> list:
    exprs = FRONT_TONES[:count - 1]
    return exprs[:at] + [lfe] + exprs[at:]


def make_mkv(path, exprs, layout, seconds=4):
    """Like make_audio, but Matroska: a PCM track with no channel mask reads
    as layout "unknown" (multichannel field recorders, NLE exports)."""
    from media_fixtures import ffmpeg
    path = str(path) + ".mkv"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=96x64:r=10:d={seconds}",
           "-f", "lavfi", "-i", f"aevalsrc=exprs='{'|'.join(exprs)}':s=48000:d={seconds}:c={layout}",
           "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "pcm_s24le", "-t", str(seconds), path)
    return path


class TestLayoutNames:
    @pytest.mark.parametrize("layout,count,lfe", [
        ("3.1", 4, [3]), ("4.1", 5, [3]), ("6.1", 7, [3]), ("6.1(front)", 7, [2]),
        ("7.1(wide-side)", 8, [3]), ("5.1.2", 8, [3]), ("7.1.4", 12, [3]), ("7.2.3", 12, [3, 11]),
        ("5.1", 6, [3]), ("stereo", 2, []), ("quad", 4, [])])
    def test_lfe_position_of_every_standard_layout(self, layout, count, lfe):
        track = {"channel_layout": layout, "channels": count}
        names = audio_checks.channel_names(track, count)
        assert len(names) == count and "ch1" not in names, names
        assert sorted(audio_checks.lfe_positions(track, names)) == lfe

    def test_unsignalled_six_and_eight_channels_assume_the_standard_order(self):
        """ITU-R BS.775 / SMPTE ST 2036-2 order L, R, C, LFE, ...: the fourth
        channel of an unsignalled 6- or 8-channel track is the LFE position."""
        for count in (6, 8):
            track = {"channel_layout": "unknown", "channels": count}
            assert audio_checks.lfe_positions(track, audio_checks.channel_names(track, count)) == {3}
        four = {"channel_layout": "", "channels": 4}
        assert audio_checks.lfe_positions(four, audio_checks.channel_names(four, 4)) == set()

    def test_ffmpeg_custom_layout_description_is_read(self):
        track = {"channel_layout": "3 channels (FL+FR+LFE)", "channels": 3}
        names = audio_checks.channel_names(track, 3)
        assert names == ["FL", "FR", "LFE"] and audio_checks.lfe_positions(track, names) == {2}

    def test_lfe_position_is_excluded_from_dropouts_and_hum_in_the_graph(self):
        track = {"codec_type": "audio", "index": 0, "channels": 4, "channel_layout": "3.1"}
        plan = core._ScanPlan({"streams": [track]}, dict(DEFAULT_CONFIG))
        from test_v091_audio import _graph
        graph = _graph(core._build_scan_command("ffmpeg", "x.mov", plan, dict(DEFAULT_CONFIG)))
        assert "silencedetect@a0d3" not in graph and "silencedetect@a0d2" in graph
        hum_mix = next(chain for chain in graph.split(";") if chain.startswith("[a0h]"))
        assert "*c3" not in hum_mix and "*c2" in hum_mix


@needs_ffmpeg
class TestLfeOnFiles:
    @pytest.mark.parametrize("layout,count", [("3.1", 4), ("6.1", 7), ("7.1.4", 12)])
    def test_silent_lfe_is_not_a_dead_channel(self, media, layout, count):
        clip = media.get(f"lfe0_{layout}", lambda p: make_audio(p, _with_lfe("0", count, 3), layout=layout))
        row = _row(_scan(clip), "Dead Channel")
        assert row[1] == "PASS" and "LFE" in row[2], row

    @pytest.mark.parametrize("layout,count", [("6.1", 7), ("7.1.4", 12)])
    def test_gappy_lfe_is_neither_dropouts_nor_hum(self, media, layout, count):
        clip = media.get(f"lfegap_{layout}", lambda p: make_audio(
            p, _with_lfe(GAPPY_LFE, count, 3), layout=layout))
        result = _scan(clip)
        assert _row(result, "Audio Dropouts")[1] == "PASS", _row(result, "Audio Dropouts")
        assert _row(result, "Audio Hum")[1] == "PASS", _row(result, "Audio Hum")

    def test_unknown_layout_six_channels(self, media):
        clip = media.get("lfegap_unknown6", lambda p: make_mkv(p, _with_lfe(GAPPY_LFE, 6, 3), "6c"))
        result = _scan(clip, accepted_containers=[".mkv"])
        assert _row(result, "Audio Dropouts")[1] == "PASS", _row(result, "Audio Dropouts")
        assert _row(result, "Audio Hum")[1] == "PASS", _row(result, "Audio Hum")

    def test_dead_front_channel_of_a_3_1_track_still_warns(self, media):
        exprs = _with_lfe(tone(60, 0.2), 4, 3)
        exprs[2] = "0"
        clip = media.get("deadfc_3.1", lambda p: make_audio(p, exprs, layout="3.1"))
        row = _row(_scan(clip), "Dead Channel")
        assert row[1] == "WARN" and "FC" in row[2], row


# ---------------------------------------------------------------------------
# 5. Fallback loudness meter (loudnorm) placed by layout, not channel index (review B3)
# ---------------------------------------------------------------------------

MINUS_23 = 0.0707946           # 1 kHz at -23 dBFS: one channel at G = 1 reads -26.0 LUFS
TONE_23 = tone(1000, MINUS_23)
LOUD_LFE = "0.5*sin(2*PI*60*t)"


@pytest.fixture
def forced_fallback(monkeypatch):
    """What the self-test caches on darwin-arm64 9.0.2 (its >2-channel
    ebur128 is broken), forced so every platform tests the fallback."""
    from frameguard.tools import get_ffmpeg_path
    monkeypatch.setattr(core, "_EBUR128_MULTICHANNEL_OK", {get_ffmpeg_path(): False})


@needs_ffmpeg
class TestLayoutAwareFallbackMeter:
    # Expected values follow FFmpeg's ebur128 filter (the primary meter on
    # every other platform): LFE excluded, back/side/top-back channels
    # weighted 1.41, everything else 1.0 (libavfilter/af_ebur128.c).
    @pytest.mark.parametrize("layout,exprs,expected", [
        ("2.1", [TONE_23, TONE_23, LOUD_LFE], -23.0),            # LFE excluded, not counted as C
        ("4.0", ["0", "0", TONE_23, "0"], -26.0),                # FC at 1.0, not as a surround
        ("3.0(back)", ["0", "0", TONE_23], -24.5),               # BC at 1.41, not as a centre
        ("3.1.2", ["0", "0", "0", LOUD_LFE, TONE_23, "0"], -26.0),  # TFL at 1.0
        ("quad", ["0", "0", TONE_23, "0"], -24.5),               # BL at 1.41
        ("5.1", [TONE_23, TONE_23, "0", LOUD_LFE, "0", "0"], -23.0),
    ])
    def test_fallback_reads_what_ebur128_reads(self, media, forced_fallback, layout, exprs, expected):
        clip = media.get(f"fb_{layout}", lambda p: make_audio(p, exprs, layout=layout))
        result = _scan(clip, loudness_mode="essential", target_loudness_lufs=expected,
                       loudness_profile="custom", loudness_tolerance_lu=0.5)
        assert result.metadata["loudness_meter"] == "loudnorm", result.metadata
        assert abs(result.metadata["integrated_loudness_lufs"] - expected) <= 0.1, result.metadata
        assert _row(result, "Audio Loudness")[1] == "PASS", _row(result, "Audio Loudness")

    @pytest.mark.parametrize("layout,count", [("6.0", 6), ("hexagonal", 6)])
    def test_layouts_beyond_its_inputs_are_not_verified(self, media, forced_fallback, layout, count):
        """Six non-LFE channels cannot be placed on loudnorm's five weighted
        inputs: 0.9.1 dropped one and read a tone there as silence."""
        exprs = ["0"] * count
        exprs[-1] = TONE_23
        clip = media.get(f"fb_{layout}", lambda p: make_audio(p, exprs, layout=layout))
        row = _row(_scan(clip, loudness_mode="essential"), "Audio Loudness")
        assert row[1] == "WARN" and core.NOT_VERIFIED_PREFIX in row[2] and layout in row[2], row

    def test_unknown_layout_is_not_verified(self, media, forced_fallback):
        clip = media.get("fb_unknown6", lambda p: make_mkv(p, [TONE_23] * 2 + ["0"] * 4, "6c"))
        row = _row(_scan(clip, loudness_mode="essential", accepted_containers=[".mkv"]), "Audio Loudness")
        assert row[1] == "WARN" and core.NOT_VERIFIED_PREFIX in row[2] and "not signalled" in row[2], row

    def test_true_peak_still_covers_the_lfe(self, media, forced_fallback):
        """The remap drops the LFE from the loudness sum; the true peak of
        every channel still comes from ebur128 (exact on every build)."""
        clip = media.get("fb_2.1", lambda p: make_audio(p, [TONE_23, TONE_23, LOUD_LFE], layout="2.1"))
        result = _scan(clip, loudness_mode="essential")
        assert abs(result.metadata["true_peak_dbtp"] - (-6.0)) <= 0.2, result.metadata


# ---------------------------------------------------------------------------
# 4. Ambisonic true peak on every channel; head-locked stereo measured (review B5)
# ---------------------------------------------------------------------------

# fs/4 at 45 degrees: samples sit at A * 0.707, the true peak is A. A = 1.07
# gives +0.6 dBTP with a -2.4 dBFS sample peak (EBU Tech 3341 case 16 shape).
INTERSAMPLE_OVER = "1.07*sin(2*PI*12000*t+PI/4)"


def _foa_with_head_locked(path, exprs):
    from container_fixtures import add_to_sample_entry, sa3d
    clip = make_audio(path, exprs, layout="5.1")
    return add_to_sample_entry(clip, b"soun", sa3d(6, head_locked=True))


@needs_ffmpeg
class TestAmbisonicPeaksAndHeadLocked:
    def test_true_peak_is_taken_on_every_component(self, media):
        """An inter-sample over on X: 0.9.1 measured W alone and passed it."""
        from media_fixtures import inject_spherical
        clip = media.get("foa_over_x", lambda p: inject_spherical(make_audio(
            p, [TONE_23, "0", "0", INTERSAMPLE_OVER], layout="4.0")))
        result = _scan(clip, loudness_mode="essential")
        # Tech 3341 allows -0.4 dB; the darwin-arm64 build over-reads 12 kHz
        # (+1.2 here), so only the lower bound is asserted.
        assert result.metadata["true_peak_dbtp"] >= 0.6 - 0.4, result.metadata
        row = _row(result, "Audio True Peak")
        assert row[1] == "FAIL" and row[3] >= 70, row
        # Loudness is still the W-channel reading (-26.0 for a -23 dBFS W tone).
        assert abs(result.metadata["integrated_loudness_lufs"] + 26.0) <= 0.15, result.metadata

    def test_head_locked_stereo_is_its_own_programme(self, media):
        """SA3D FOA + head-locked stereo (6 channels, FFmpeg layout
        "ambisonic 1+stereo"): the narration on the head-locked pair was
        invisible (W read -58 LUFS, Phase and Dual Mono skipped it)."""
        bed = "0.001*sin(2*PI*300*t)"
        clip = media.get("foa_hl", lambda p: _foa_with_head_locked(
            p, [bed, "0", "0", "0", TONE_23, tone(1000, MINUS_23 * 0.5)]))
        result = _scan(clip, loudness_mode="essential")
        assert result.metadata["ambisonic_audio_tracks"] == [1]
        loudness = _row(result, "Audio Loudness")
        assert "head-locked stereo" in loudness[2] and "-25.0 LUFS" in loudness[2], loudness
        assert "HL-L/HL-R" in _row(result, "Audio Phase")[2]
        assert "HL-L/HL-R" in _row(result, "Dual Mono")[2]

    def test_head_locked_over_fails_true_peak(self, media):
        clip = media.get("foa_hl_over", lambda p: _foa_with_head_locked(
            p, [TONE_23, "0", "0", "0", INTERSAMPLE_OVER, tone(1000, MINUS_23)]))
        row = _row(_scan(clip, loudness_mode="essential"), "Audio True Peak")
        assert row[1] == "FAIL", row

    def test_names_of_a_head_locked_track(self):
        track = {"channel_layout": "ambisonic 1+stereo", "channels": 6}
        assert audio_checks.channel_names(track, 6, ambisonic=True) == ["W", "Y", "Z", "X", "HL-L", "HL-R"]
        assert audio_checks.stereo_pairs(track, audio_checks.channel_names(track, 6, True), True) == \
            [(4, 5, "HL-L/HL-R")]


# ---------------------------------------------------------------------------
# 6. The phase check keeps its own end marker (review B1)
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestPhaseEndMarker:
    def test_a_short_antiphase_section_ends_where_it_ends(self, media):
        """2.2 s of anti-phase (2.0-4.2 s) in a 6 s clip: the start recorder's
        ametadata=mode=delete stripped out_phase_end, so every region ran to
        the end of the file ("4.0 s, 67 %, polarity-inverted")."""
        left = "0.1*(random(0)-0.5)"
        right = "if(between(t,2,4.2),-0.09,0.09)*(random(0)-0.5)"
        clip = media.get("phase_section", lambda p: make_audio(p, [left, right], seconds=6))
        row = _row(_scan(clip), "Audio Phase")
        assert row[1] == "WARN", row
        assert "4.0s" not in row[2] and "polarity-inverted" not in row[2], row
        assert "00:00:02.0" in row[2], row

    def test_the_end_recorder_sees_the_end_key(self):
        chains = audio_checks._phase_chains(0, {"channel_layout": "stereo"}, ["FL", "FR"],
                                            dict(DEFAULT_CONFIG), False,
                                            audio_checks.MeasurementFiles("/tmp"))
        chain = chains[0][1]
        start = chain.index("out_phase_start")
        end = chain.index("out_phase_end")
        assert "mode=delete" not in chain[start:end], chain


# ---------------------------------------------------------------------------
# 7. Phase: whole-programme correlation and mono fold-down loss (REVIEW §2, held-out 100 % -> 50 %)
# ---------------------------------------------------------------------------

def _wide_stereo(inverted: bool):
    """A shared source plus independent ambience per side, correlation
    +0.37 (shared power 37 %): wide-stereo music. Inverting R gives -0.37."""
    common = "0.1*(random(0)-0.5)"
    left = f"if(eq(n,0),st(1,104729));{common}+0.1305*(random(1)-0.5)"
    sign = "-" if inverted else ""
    right = f"if(eq(n,0),st(2,1299709));{sign}({common}+0.1305*(random(2)-0.5))"
    return [left, right]


@needs_ffmpeg
class TestPhaseInversionOfWideStereo:
    def test_whole_file_inversion_of_wide_stereo_is_found(self, media):
        """aphasemeter's per-sample measure of a -0.37 Pearson correlation
        sits near -0.19 and never crossed -0.2, so 0.9.1 passed it."""
        clip = media.get("wide_inverted", lambda p: make_audio(p, _wide_stereo(True), seconds=6))
        row = _row(_scan(clip), "Audio Phase")
        assert row[1] == "WARN" and row[3] == 85 and "polarity-inverted" in row[2], row
        assert "correlation -0.3" in row[2] and "fold-down" in row[2] and "LU" in row[2], row

    def test_the_same_music_in_phase_passes(self, media):
        clip = media.get("wide_inphase", lambda p: make_audio(p, _wide_stereo(False), seconds=6))
        row = _row(_scan(clip), "Audio Phase")
        assert row[1] == "PASS" and "correlation +0.3" in row[2], row

    def test_threshold_is_a_setting(self, media):
        clip = media.get("wide_inverted", lambda p: make_audio(p, _wide_stereo(True), seconds=6))
        assert _row(_scan(clip, phase_correlation_min=-0.5), "Audio Phase")[1] == "PASS"

    def test_default_threshold_is_a_one_lu_fold_down_loss(self):
        threshold = DEFAULT_CONFIG["phase_correlation_min"]
        assert audio_checks.fold_down_change_lu(threshold) == pytest.approx(-1.0, abs=0.05)

    def test_threshold_has_a_settings_control_and_a_pdf_line(self, qapp):
        from frameguard.gui import SettingsPanel
        from frameguard.report import spec_rows
        panel = SettingsPanel()
        assert panel.get_config_overrides()["phase_correlation_min"] == -0.2
        panel._apply_preset_config({**panel.get_config_overrides(), "phase_correlation_min": -0.3})
        assert panel.get_config_overrides()["phase_correlation_min"] == -0.3
        rows = dict(spec_rows(core.config_snapshot(dict(DEFAULT_CONFIG, phase_correlation_min=-0.3))))
        assert "phase below -0.3" in rows["Audio checks"]


# ---------------------------------------------------------------------------
# 9. Netflix (dialogue-gated) target judged on programme loudness is advisory (review B7)
# ---------------------------------------------------------------------------

class TestDialogueGatedTarget:
    def _row(self, integrated, **cfg):
        from test_v091_audio import _cfg, _ctx
        return audio_checks.loudness_row({"integrated": integrated, "lra": 8.0}, _ctx(),
                                         _cfg(loudness_mode="essential", loudness_profile="netflix", **cfg))

    def test_a_loud_full_mix_is_not_a_confident_fail(self):
        """Dialogue stem at -28.9 (on spec), full mix -17.9: 0.9.1 gave FAIL 95,
        a MISMATCH, for a compliant mix."""
        status, text, confidence = self._row(-17.9)
        assert status == "WARN" and confidence <= audio_checks.LOW_CONFIDENCE, (status, confidence)
        assert "not dialogue-gated" in text and "estimate" in text, text

    def test_quiet_full_mix_is_advisory_too(self):
        status, _, confidence = self._row(-35.0)
        assert status == "WARN" and confidence <= audio_checks.LOW_CONFIDENCE

    def test_on_target_passes(self):
        assert self._row(-27.5)[0] == "PASS"

    def test_a_spec_that_judges_programme_loudness_can_enforce_it(self):
        assert DEFAULT_CONFIG["dialogue_gated_loudness"] == "advisory"
        status, _, confidence = self._row(-17.9, dialogue_gated_loudness="enforce")
        assert status == "FAIL" and confidence == 95

    def test_setting_is_validated_and_has_a_control(self, qapp):
        from frameguard import presets
        from frameguard.gui import SettingsPanel
        assert presets.validate_preset_config({"dialogue_gated_loudness": "Enforce"}) == \
            {"dialogue_gated_loudness": "enforce"}
        with pytest.raises(presets.PresetValidationError):
            presets.validate_preset_config({"dialogue_gated_loudness": "sometimes"})
        panel = SettingsPanel()
        assert panel.get_config_overrides()["dialogue_gated_loudness"] == "advisory"
        from frameguard.report import spec_rows
        rows = dict(spec_rows(core.config_snapshot(dict(DEFAULT_CONFIG, loudness_mode="essential",
                                                        loudness_profile="netflix"))))
        assert "Netflix -27" in rows["Loudness"] and "warn only" in rows["Loudness"], rows["Loudness"]


# ---------------------------------------------------------------------------
# 10. Hum: mains tolerance (EN 50160, +-1 %) and the harmonic series (held-out h19)
# ---------------------------------------------------------------------------

def _harmonic_tone(f0, amps):
    return "+".join(f"{a}*sin(2*PI*{f0 * (k + 1):g}*t)" for k, a in enumerate(amps))


ORGAN = _harmonic_tone(58.27, (0.1, 0.05, 0.03))    # sustained B-flat-1 with its harmonics


@needs_ffmpeg
class TestHumFrequencyAndHarmonics:
    def test_a_58_hz_bass_note_is_not_60_hz_hum(self, media):
        """58.27 Hz is 2.9 % below 60 Hz, outside any mains tolerance; it
        leaked through 0.9.1's 2 Hz-wide band as "60 Hz tone at -43 dBFS"."""
        exprs = [f"{_uniform_noise(-50, 1)}+{ORGAN}", f"{_uniform_noise(-50, 2)}+{ORGAN}"]
        clip = media.get("organ58", lambda p: make_audio(p, exprs, seconds=6))
        row = _row(_scan(clip), "Audio Hum")
        assert row[1] == "PASS", row

    def test_hum_inside_the_mains_tolerance_is_found(self, media):
        """60.4 Hz (+0.67 %, inside EN 50160's +-1 %) with its 2nd and 3rd harmonics."""
        hum = _harmonic_tone(60.4, (0.014, 0.005, 0.004))
        exprs = [f"{NOISE}+{hum}", f"{_uniform_noise(-30, 3)}+{hum}"]
        clip = media.get("hum604", lambda p: make_audio(p, exprs, seconds=6))
        row = _row(_scan(clip), "Audio Hum")
        assert row[1] == "WARN" and "60 Hz" in row[2] and "harmonic" in row[2], row

    def test_a_lone_mains_frequency_sine_is_not_hum(self, media):
        """No 2f/3f: a test tone or a synth note at 50 Hz, not mains hum."""
        exprs = [f"{_uniform_noise(-30, 1)}+0.014*sin(2*PI*50*t)",
                 f"{_uniform_noise(-30, 2)}+0.014*sin(2*PI*50*t)"]
        clip = media.get("pure50", lambda p: make_audio(p, exprs, seconds=6))
        row = _row(_scan(clip), "Audio Hum")
        assert row[1] == "PASS" and "harmonic" in row[2], row
