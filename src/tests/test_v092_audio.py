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
# gives +0.6 dBTP with a -2.4 dBFS sample peak (EBU Tech 3341 case 16 shape),
# with Tech 3341's 10 ms raised-cosine fade at both ends of the 4 s clip (an
# abrupt start is a real transient above the steady peak).
TAPER_4S = ("(if(lt(t,0.01),0.5-0.5*cos(PI*t/0.01),1))"
            "*(if(gt(t,3.99),0.5-0.5*cos(PI*(4-t)/0.01),1))")
INTERSAMPLE_OVER = f"1.07*sin(2*PI*12000*t+PI/4)*{TAPER_4S}"


def _foa_with_head_locked(path, exprs):
    from container_fixtures import add_to_sample_entry, sa3d
    clip = make_audio(path, exprs, layout="5.1")
    return add_to_sample_entry(clip, b"soun", sa3d(6, head_locked=True))


@needs_ffmpeg
class TestAmbisonicPeaksAndHeadLocked:
    def test_true_peak_is_taken_on_every_component(self, media):
        """An inter-sample over on X: 0.9.1 measured W alone and passed it."""
        from media_fixtures import inject_spherical
        clip = media.get("foa_over_x_tapered", lambda p: inject_spherical(make_audio(
            p, [TONE_23, "0", "0", INTERSAMPLE_OVER], layout="4.0")))
        result = _scan(clip, loudness_mode="essential")
        # Tech 3341: +0.2 / -0.4 dB.
        assert 0.6 - 0.4 <= result.metadata["true_peak_dbtp"] <= 0.6 + 0.2, result.metadata
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

    def test_quiet_head_locked_pair_is_reported_not_judged(self, media):
        """Round 4: a clean FOA + head-locked file under R 128 enforcement.
        W reads -23.0 LUFS (on target); the head-locked narration sits 15 LU
        under the target on its own, as it does in a real mix. 0.9.2-rc
        judged the pair alone: "15.3 LU quieter", WARN."""
        w_on_target = tone(1000, MINUS_23 * 10 ** (3 / 20))          # mono -20 dBFS reads -23.0
        quiet = tone(1000, MINUS_23 * 10 ** (-15 / 20))             # stereo -38 LUFS
        clip = media.get("foa_hl_quiet", lambda p: _foa_with_head_locked(
            p, [w_on_target, "0", "0", "0", quiet, quiet]))
        result = _scan(clip, loudness_mode="essential", loudness_profile="ebu_r128")
        loudness = _row(result, "Audio Loudness")
        assert loudness[1] == "PASS", loudness
        assert "head-locked stereo" in loudness[2] and "-38.0 LUFS" in loudness[2], loudness
        assert "quieter" not in loudness[2] and "not judged" in loudness[2], loudness
        assert result.status != "MISMATCH"

    def test_head_locked_over_fails_true_peak(self, media):
        clip = media.get("foa_hl_over_tapered", lambda p: _foa_with_head_locked(
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

    def test_a_pure_continuous_60_hz_sine_is_hum(self, media):
        """Round 2 / review bug 8: a pure 60 Hz sine at -60 dBFS for the whole
        track, no harmonics (induced hum after filtering or from magnetic
        pickup), was PASSed "a test tone or note" because harmonics were
        demanded."""
        exprs = [f"{_uniform_noise(-30, 1)}+0.001*sin(2*PI*60*t)",
                 f"{_uniform_noise(-30, 2)}+0.001*sin(2*PI*60*t)"]
        clip = media.get("pure60_m60", lambda p: make_audio(p, exprs, seconds=10))
        row = _row(_scan(clip), "Audio Hum")
        assert row[1] == "WARN" and "60 Hz" in row[2] and row[3] < 70, row

    def test_an_organ_note_with_an_envelope_near_59_5_hz_is_not_hum(self, media):
        """In the mains band, with harmonics, but played: an attack at 1 s, a
        decay of about 1 dB/s and a release by 9 s. Hum is continuous at a
        constant level; a note is not."""
        envelope = ("if(lt(t,1),0,if(lt(t,1.5),(t-1)/0.5,if(lt(t,8),exp(-(t-1.5)/4),"
                    "if(lt(t,9),exp(-6.5/4)*(9-t),0))))")
        note = f"({_harmonic_tone(59.5, (0.05, 0.025, 0.015))})*{envelope}"
        exprs = [f"{_uniform_noise(-40, 1)}+{note}", f"{_uniform_noise(-40, 2)}+{note}"]
        clip = media.get("organ595_env", lambda p: make_audio(p, exprs, seconds=10))
        row = _row(_scan(clip), "Audio Hum")
        assert row[1] == "PASS" and "59" not in row[2].split("(")[0], row

    @pytest.mark.parametrize("freq", [59.4, 60.6])
    def test_the_mains_tolerance_is_symmetric(self, media, freq):
        """EN 50160's +-1 % on either side of 60 Hz: 0.9.2-rc passed 59.4 and warned on 60.6."""
        exprs = [f"{_uniform_noise(-30, 1)}+0.01*sin(2*PI*{freq}*t)",
                 f"{_uniform_noise(-30, 2)}+0.01*sin(2*PI*{freq}*t)"]
        clip = media.get(f"hum_edge_{freq}", lambda p: make_audio(p, exprs, seconds=6))
        assert _row(_scan(clip), "Audio Hum")[1] == "WARN"


# ---------------------------------------------------------------------------
# 11. Dual mono allowed is a spec decision (held-out h08, review B11)
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestDualMonoAllowed:
    def test_mono_vo_in_stereo_passes_when_the_spec_allows_it(self, media):
        clip = media.get("vo_dualmono", lambda p: make_audio(p, [tone(220)] * 2))
        row = _row(_scan(clip, dual_mono_allowed=True), "Dual Mono")
        assert row[1] == "PASS" and "dual mono allowed" in row[2], row

    def test_default_still_warns(self, media):
        assert DEFAULT_CONFIG["dual_mono_allowed"] is False
        clip = media.get("vo_dualmono", lambda p: make_audio(p, [tone(220)] * 2))
        assert _row(_scan(clip), "Dual Mono")[1] == "WARN"

    def test_setting_has_a_control_and_a_pdf_line(self, qapp):
        from frameguard import presets
        from frameguard.gui import SettingsPanel
        from frameguard.report import spec_rows
        assert presets.validate_preset_config({"dual_mono_allowed": True}) == {"dual_mono_allowed": True}
        panel = SettingsPanel()
        panel._apply_preset_config({**panel.get_config_overrides(), "dual_mono_allowed": True})
        assert panel.get_config_overrides()["dual_mono_allowed"] is True
        rows = dict(spec_rows(core.config_snapshot(dict(DEFAULT_CONFIG, dual_mono_allowed=True))))
        assert "dual mono allowed" in rows["Audio checks"]


# ---------------------------------------------------------------------------
# 12a. Exported loudness metadata: no distrusted value, primary track (review B8)
# ---------------------------------------------------------------------------

def _two_tracks(path, first, second, default=1):
    """Tiny video plus two stereo PCM tracks; track ``default`` (0-based,
    the second by default) carries the default disposition."""
    from media_fixtures import ffmpeg
    path = str(path) + ".mov"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=96x64:r=10:d=4",
           "-f", "lavfi", "-i", f"aevalsrc=exprs='{first}|{first}':s=48000:d=4",
           "-f", "lavfi", "-i", f"aevalsrc=exprs='{second}|{second}':s=48000:d=4",
           "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "pcm_s24le",
           "-disposition:a:0", "default" if default == 0 else "0",
           "-disposition:a:1", "default" if default == 1 else "0", "-t", "4", path)
    return path


@needs_ffmpeg
class TestLoudnessMetadata:
    def test_unmeasured_track_exports_no_loudness(self, media, forced_fallback):
        """7.1 cannot be placed on the fallback's inputs: the row says "not
        measured", and the export must not carry the distrusted ebur128 value."""
        exprs = [TONE_23, TONE_23, "0", LOUD_LFE, "0", "0", "0", "0"]
        clip = media.get("fb_7.1", lambda p: make_audio(p, exprs, layout="7.1"))
        result = _scan(clip, loudness_mode="essential")
        assert "integrated_loudness_lufs" not in result.metadata, result.metadata
        assert "loudness_range_lu" not in result.metadata
        assert result.metadata["loudness_meter"] == "none"
        assert result.metadata["true_peak_dbtp"] is not None

    def test_metadata_describes_the_default_track(self, media):
        clip = media.get("two_tracks", lambda p: _two_tracks(p, tone(1000, MINUS_23 / 3), TONE_23))
        result = _scan(clip, loudness_mode="essential")
        assert abs(result.metadata["integrated_loudness_lufs"] + 23.0) <= 0.1, result.metadata


# ---------------------------------------------------------------------------
# 12b. Meter self-test: every weighted channel, the LFE, the fallback and true peak (review B9)
# ---------------------------------------------------------------------------

class _Proc:
    def __init__(self, stderr):
        self.stderr, self.stdout, self.returncode = stderr, "", 0


def _ebur128_summary(instance, integrated, true_peak=None):
    lines = [f"[{instance} @ 0x1] [info] Summary:", "", "  Integrated loudness:",
             f"    I:         {integrated:.1f} LUFS", "    Threshold: -33.0 LUFS", "",
             "  Loudness range:", "    LRA:         0.0 LU", ""]
    if true_peak is not None:
        lines += ["  Sample peak:", f"    Peak:      {true_peak:.1f} dBFS", "",
                  "  True peak:", f"    Peak:      {true_peak:.1f} dBFS"]
    return "\n".join(lines)


def _loudnorm_json(instance, integrated):
    return "\n".join([f"[{instance} @ 0x2] [info] ", "{", f'\t"input_i" : "{integrated:.2f}",',
                      '\t"input_tp" : "-20.00",', '\t"input_lra" : "0.00",', '\t"input_thresh" : "-33.00",'])


class TestMeterSelfTest:
    def test_a_meter_that_drops_the_centre_is_caught(self, monkeypatch):
        """0.9.1 tested L and R of 3.0 with the centre silent (+-0.3 LU): a
        meter that ignored C, mis-weighted the surrounds or counted the LFE
        passed. Here the binary reads -23.0 for that old signal and -24.2
        for EBU Tech 3341 case 6 in 5.1 (C dropped)."""
        def fake(cmd, *args, **kwargs):
            text = " ".join(cmd)
            if "pan=3.0" in text:
                return _Proc("    I:         -23.0 LUFS")
            return _Proc("\n".join([_ebur128_summary("ebur128@a0", -24.2),
                                    _loudnorm_json("loudnorm@a0", -23.05),
                                    _ebur128_summary("ebur128@a1", -18.0, -6.0)]))
        monkeypatch.setattr(core, "_EBUR128_MULTICHANNEL_OK", {})
        monkeypatch.setattr(core, "_METER_SELF_TEST", {}, raising=False)
        monkeypatch.setattr(core, "_run_owned_subprocess", fake)
        assert core._ebur128_multichannel_ok("ffmpeg") is False

    def test_a_failing_fallback_leaves_loudness_unmeasured(self, monkeypatch):
        def fake(cmd, *args, **kwargs):
            return _Proc("\n".join([_ebur128_summary("ebur128@a0", -5.0),
                                    _loudnorm_json("loudnorm@a0", -21.0),
                                    _ebur128_summary("ebur128@a1", -18.0, -6.0)]))
        monkeypatch.setattr(core, "_EBUR128_MULTICHANNEL_OK", {})
        monkeypatch.setattr(core, "_METER_SELF_TEST", {}, raising=False)
        monkeypatch.setattr(core, "_run_owned_subprocess", fake)
        tracks = [{"codec_type": "audio", "index": 1, "channels": 6, "channel_layout": "5.1"}]
        plan = core._ScanPlan({"streams": tracks}, dict(DEFAULT_CONFIG))
        assert core._multichannel_meter("ffmpeg", plan) == audio_checks.METER_NONE

    def test_a_meter_that_fails_true_peak_is_not_verified_unless_the_samples_prove_it(self):
        """A build whose true peak misses its Tech 3341 self-test cannot vouch
        for a reading; a sample peak over the limit still proves a FAIL (the
        true peak is never below the sample peak)."""
        test = audio_checks.MeterSelfTest(ebur128_multichannel=True, loudnorm=True, true_peak_error_db=-1.0)
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": "stereo"}, names=["FL", "FR"],
                                        label="", duration=60.0, meter_self_test=test)
        cfg = dict(DEFAULT_CONFIG, loudness_mode="essential")
        status, text, _ = audio_checks.true_peak_row({"true_peak": -2.0, "sample_peak": -2.5}, ctx, cfg)
        assert status == "WARN" and core.NOT_VERIFIED_PREFIX in text and "self-test" in text
        status, _, confidence = audio_checks.true_peak_row({"true_peak": -0.5, "sample_peak": -0.4}, ctx, cfg)
        assert status == "FAIL" and confidence == 95
        good = audio_checks.MeterSelfTest(ebur128_multichannel=True, loudnorm=True, true_peak_error_db=0.0)
        ctx.meter_self_test = good
        assert audio_checks.true_peak_row({"true_peak": -0.4, "sample_peak": -0.4}, ctx, cfg)[2] == 95

    def test_fallback_readings_near_a_limit_are_low_confidence(self):
        """loudnorm's meter read EBU Tech 3341 case 5 0.14 LU low on this
        build (outside Tech 3341's +-0.1): a 0.15 LU miss is not proof."""
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": "5.1"},
                                        names=["FL", "FR", "FC", "LFE", "BL", "BR"], label="",
                                        duration=60.0, meter=audio_checks.METER_LOUDNORM)
        row = audio_checks.loudness_row({"integrated": -23.65}, ctx, dict(DEFAULT_CONFIG, loudness_mode="essential"))
        assert row[0] == "FAIL" and row[2] == audio_checks.LOW_CONFIDENCE, row

    @needs_ffmpeg
    def test_real_self_test_on_this_build(self, monkeypatch):
        """End to end on the bundled binary. The true-peak sines carry EBU Tech
        3341's 10 ms fade-in and fade-out: without it the onset transient read
        +1.02 dB on every build and was wrongly blamed on the meter."""
        import platform
        from frameguard.tools import get_ffmpeg_path
        monkeypatch.setattr(core, "_EBUR128_MULTICHANNEL_OK", {})
        monkeypatch.setattr(core, "_METER_SELF_TEST", {})
        path = get_ffmpeg_path()
        verdict = core._ebur128_multichannel_ok(path)
        test = core._METER_SELF_TEST[path]
        assert test.loudnorm is True     # the remapped fallback reads case 6 within +-0.1 LU
        assert abs(test.true_peak_error_db) <= 0.2, test
        assert test.true_peak_ok
        if "darwin-arm64" in path and platform.machine() == "arm64":
            assert verdict is False      # the bundled arm64 9.0.2: broken >2-channel K-weighting

    def test_self_test_sines_are_tapered(self):
        command = " ".join(audio_checks.meter_self_test_command("ffmpeg"))
        assert command.count("afade=t=in:d=0.01") == 2 and command.count("afade=t=out") == 2, command


@needs_ffmpeg
class TestTruePeakEnforcementAsIn091:
    def test_clean_file_0_6_db_over_the_limit_is_a_mismatch(self, media):
        """A -23 LUFS 1 kHz programme with 10 ms bursts whose samples reach
        -0.4 dBFS: 0.6 dB over R 128's -1 dBTP. 0.9.1 said MISMATCH; the
        untapered self-test widened the band and 0.9.2 said PASS_WITH_WARNINGS."""
        burst = "if(lt(mod(t,1),0.01),0.955,0.0707946)*sin(2*PI*1000*t)"
        clip = media.get("tp_bursts", lambda p: make_audio(p, [burst, burst]))
        result = _scan(clip, loudness_mode="essential", loudness_profile="ebu_r128")
        row = _row(result, "Audio True Peak")
        assert row[1] == "FAIL" and row[3] == 95, row
        assert result.status == "MISMATCH"
        assert "over-reads" not in row[2]


# ---------------------------------------------------------------------------
# 12c. Line-up ident exemption follows the real GLITS pattern (review B10)
# ---------------------------------------------------------------------------

@needs_ffmpeg
class TestGlitsPattern:
    TONE400 = "0.1259*sin(2*PI*400*t)"

    def test_a_simultaneous_l_r_gap_in_a_tone_is_a_dropout(self, media):
        """GLITS interrupts one channel at a time; a 250 ms gap on both
        channels at once was set aside as "line-up ident interruption"."""
        gap = f"if(between(t,2,2.25),0,{self.TONE400})"
        clip = media.get("tone400_both", lambda p: make_audio(p, [gap, gap]))
        row = _row(_scan(clip), "Audio Dropouts")
        assert row[1] == "WARN" and "line-up ident" not in row[2], row

    def test_an_isolated_one_channel_gap_is_a_dropout(self, media):
        """One 250 ms gap on R with no partner gap 0.5 s away is not GLITS."""
        right = f"if(between(t,2,2.25),0,{self.TONE400})"
        clip = media.get("tone400_r", lambda p: make_audio(p, [self.TONE400, right]))
        row = _row(_scan(clip), "Audio Dropouts")
        assert row[1] == "WARN" and "FR" in row[2], row


# ---------------------------------------------------------------------------
# 12d. Ambisonic W normalization: FuMa reads 3 LU low without SA3D (review §4)
# ---------------------------------------------------------------------------

FOA_W_23 = [TONE_23, NOISE, "0", NOISE.replace("0.1", "0.05")]   # W reads -26.0 LUFS (AmbiX)
NEAR_TARGET = dict(loudness_mode="essential", loudness_profile="custom", target_loudness_lufs=-23.5,
                   loudness_tolerance_lu=1.0)


@needs_ffmpeg
class TestAmbisonicNormalization:
    def test_unsignalled_normalization_is_not_verified_when_it_decides(self, media):
        """No SA3D: W may be FuMa (-3.01 dB), so -26.0 could be -23.0. Against
        -23.5 +-1 the reading fails as AmbiX and passes as FuMa."""
        from media_fixtures import inject_spherical
        clip = media.get("foa_w_heuristic", lambda p: inject_spherical(make_audio(p, FOA_W_23, layout="4.0")))
        row = _row(_scan(clip, **NEAR_TARGET), "Audio Loudness")
        assert row[1] == "WARN" and core.NOT_VERIFIED_PREFIX in row[2] and "FuMa" in row[2], row

    def test_sa3d_signalled_ambix_is_judged(self, media):
        from container_fixtures import add_to_sample_entry, sa3d
        clip = media.get("foa_w_sa3d", lambda p: add_to_sample_entry(
            make_audio(p, FOA_W_23, layout="4.0"), b"soun", sa3d(4)))
        result = _scan(clip, **NEAR_TARGET)
        assert result.metadata["ambisonic_audio_tracks"] == [1]
        row = _row(result, "Audio Loudness")
        assert core.NOT_VERIFIED_PREFIX not in row[2] and "quieter" in row[2], row

    def test_unsignalled_but_decided_either_way_is_judged(self, media):
        """-26.0 or -23.0 are both inside -24.5 +-2: no ambiguity to report."""
        from media_fixtures import inject_spherical
        clip = media.get("foa_w_heuristic", lambda p: inject_spherical(make_audio(p, FOA_W_23, layout="4.0")))
        row = _row(_scan(clip, loudness_mode="essential", loudness_profile="custom",
                         target_loudness_lufs=-24.5, loudness_tolerance_lu=2.0), "Audio Loudness")
        assert row[1] == "PASS" and "FuMa" in row[2], row


# ---------------------------------------------------------------------------
# 13. No summary from FFmpeg -> "Not verified", never PASS (review B6)
# ---------------------------------------------------------------------------

# FFmpeg 6.x names filter instances by their id alone ("[a0 @ 0x...]"), so
# every summary regex misses; any build can also simply print none.
_OLD_CONTEXT_RE = __import__("re").compile(r"\[[a-z0-9_]+@(a\d+[a-z]*\d*) @ ")


def _old_style(log_text: str) -> str:
    return _OLD_CONTEXT_RE.sub(lambda m: f"[{m.group(1)} @ ", log_text)


AUDIO_ROWS = ("Audio Loudness", "Audio True Peak", "Audio Clipping", "Audio Phase", "Dual Mono",
              "DC Offset")


@needs_ffmpeg
class TestMissingSummariesAreNotVerified:
    @pytest.fixture
    def old_ffmpeg_logs(self, monkeypatch):
        real = core._run_scan_process

        def rewritten(*args, **kwargs):
            outcome = real(*args, **kwargs)
            if outcome is not None:
                outcome.log_text = _old_style(outcome.log_text)
            return outcome
        monkeypatch.setattr(core, "_run_scan_process", rewritten)

    @pytest.mark.parametrize("mode", ["report", "essential"])
    def test_every_summary_row_is_not_verified(self, media, old_ffmpeg_logs, mode):
        """0.9.1 on FFmpeg 6.0: Loudness and True Peak were SKIP, and Dual Mono
        read PASS "not measured" (the review's B6); Phase passed too."""
        clip = media.get("vo_dualmono", lambda p: make_audio(p, [tone(220)] * 2))
        result = _scan(clip, loudness_mode=mode)
        for name in AUDIO_ROWS:
            row = _row(result, name)
            assert row[1] == "WARN" and core.NOT_VERIFIED_PREFIX in row[2], row

    def test_rewrite_matches_what_ffmpeg_6_prints(self):
        assert _old_style("[ebur128@a0 @ 0x1] [info] Summary:") == "[a0 @ 0x1] [info] Summary:"
        assert _old_style("[astats@a3p1 @ 0x2] [info] Channel: 1") == "[a3p1 @ 0x2] [info] Channel: 1"

    def test_missing_head_locked_summary_is_named(self):
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": "ambisonic 1+stereo"},
                                        names=["W", "Y", "Z", "X", "HL-L", "HL-R"], label="",
                                        duration=60.0, ambisonic=True)
        row = audio_checks.loudness_row({"integrated": -26.0, "true_peak": -10.0}, ctx,
                                        dict(DEFAULT_CONFIG, loudness_mode="essential"))
        assert row[0] == "WARN" and "head-locked stereo" in row[1] and "not reported" in row[1], row


# ---------------------------------------------------------------------------
# R3 bug 12. AGENTS.md: new settings carry no helper line or secondary label
# ---------------------------------------------------------------------------

class TestNewSettingsCopy:
    NEW = ("Phase Threshold", "Dialogue-Gated Target", "Allow Dual Mono")

    def test_new_audio_settings_have_no_helper_line(self, qapp):
        from frameguard.gui import SettingsPanel
        panel = SettingsPanel()
        rows = {name.text(): desc for _, name, _, _, desc in panel._setting_rows}
        for title in self.NEW:
            assert rows[title].text() == "" and rows[title].isHidden(), title
        # 0.9.1's own settings keep their copy.
        assert rows["Phase"].text() == "Warn when left and right are out of phase for a second or more"

    def test_option_labels_carry_no_secondary_text(self):
        from frameguard.gui import SettingsPanel
        for _, key, kind, _, _, choices in SettingsPanel._RULE_CONTROLS:
            if key in ("phase_correlation_min", "dialogue_gated_loudness") and kind == "choice":
                for _, label in choices:
                    assert "(" not in label and ":" not in label, label


# ---------------------------------------------------------------------------
# R2 item 2. Advisory "low-level audio" tier: dead air with a noise floor
# ---------------------------------------------------------------------------

PROGRAMME_23 = [TONE_23, tone(1000, MINUS_23)]          # -23 LUFS stereo


@needs_ffmpeg
class TestLowLevelAudio:
    def test_drop_to_hiss_inside_a_programme_is_flagged(self, media):
        """4 s of -62 dBFS RMS hiss inside a -23 LUFS programme: above the
        -70 dBFS silence level, so the essential tier no longer sees it."""
        def channel(seed):
            return (f"{_uniform_noise(-62, seed)}*between(t,3,7)"
                    f"+(1-between(t,3,7))*{TONE_23}")
        clip = media.get("dead_air_hiss", lambda p: make_audio(p, [channel(1), channel(2)], seconds=10))
        result = _scan(clip)
        row = _row(result, "Low-Level Audio")
        assert row[1] == "WARN" and row[3] <= audio_checks.LOW_CONFIDENCE, row
        assert "00:00:03" in row[2], row
        assert result.status != "MISMATCH"
        assert _row(result, "Audio Silence")[1] == "PASS"

    def test_room_tone_throughout_is_not_flagged(self, media):
        room = media.get("roomtone64", lambda p: make_audio(
            p, [_uniform_noise(-64, 1), _uniform_noise(-64, 2)]))
        result = _scan(room)
        assert _row(result, "Low-Level Audio")[1] == "PASS"
        assert result.status != "MISMATCH"

    def test_quiet_lead_in_is_not_dead_air_inside_the_programme(self, media):
        def channel(seed):
            return f"{_uniform_noise(-62, seed)}*lt(t,3)+gte(t,3)*{TONE_23}"
        clip = media.get("hiss_lead_in", lambda p: make_audio(p, [channel(1), channel(2)], seconds=8))
        assert _row(_scan(clip), "Low-Level Audio")[1] == "PASS"

    def test_threshold_and_switch_are_settings(self, media, qapp):
        from frameguard import presets
        from frameguard.gui import SettingsPanel
        from frameguard.report import spec_rows
        assert DEFAULT_CONFIG["low_level_noise_db"] == -40.0
        assert DEFAULT_CONFIG["audio_low_level_check"] is True
        assert presets.validate_preset_config({"low_level_noise_db": -50}) == {"low_level_noise_db": -50}
        with pytest.raises(presets.PresetValidationError):
            presets.validate_preset_config({"low_level_noise_db": "-40dB,anullsink"})
        panel = SettingsPanel()
        assert panel.get_config_overrides()["low_level_noise_db"] == -40.0
        rows = dict(spec_rows(core.config_snapshot(dict(DEFAULT_CONFIG))))
        assert "low-level below -40 dBFS" in rows["Audio checks"]
        assert core.CHECK_SEVERITY_WEIGHTS["Low-Level Audio"] < core.CHECK_SEVERITY_WEIGHTS["Audio Clipping"]
        assert "Low-Level Audio" in core.ADVISORY_CHECKS

    def test_switched_off(self, media):
        room = media.get("roomtone64", lambda p: make_audio(
            p, [_uniform_noise(-64, 1), _uniform_noise(-64, 2)]))
        assert _row(_scan(room, audio_low_level_check=False), "Low-Level Audio")[1] == "SKIP"


# ---------------------------------------------------------------------------
# R3 bug 13. No benchmark / inferred justifications left in the audio module
# ---------------------------------------------------------------------------

def test_audio_module_cites_no_benchmark_or_inferred_value():
    import inspect
    source = inspect.getsource(audio_checks).lower()
    for word in ("benchmark", "inferred", "corpus"):
        assert word not in source, word


# ---------------------------------------------------------------------------
# R3 mutation gate: rules each 0.9.2 commit claims, pinned at their boundaries
# ---------------------------------------------------------------------------

def _files_evidence(tmp_path, contents):
    """ScanEvidence over measurement files written by the test: contents maps
    an instance name to its file text."""
    import os
    os.makedirs(str(tmp_path), exist_ok=True)
    files = audio_checks.MeasurementFiles(str(tmp_path))
    for instance, text in contents.items():
        with open(files.path(instance), "w", encoding="utf-8") as handle:
            handle.write(text)
    return audio_checks.ScanEvidence(log_text="", silence_events=lambda *a, **k: [], files=files)


def _band_text(levels):
    return "".join(f"frame:{k} pts:{k * 1000} pts_time:{k}\nlavfi.astats.1.RMS_level={v}\n"
                   for k, v in enumerate(levels))


def _hum_case(tmp_path, centre, near=-90.0, extra=None, freq=60):
    """Hum band files: 60 Hz centre windows as given (2 settling windows are
    prepended), every other band at -110 dBFS, near bands at ``near``."""
    contents = {}
    count = len(centre) + 2
    for name, _, _ in audio_checks._hum_bands():
        level = [-110.0] * count
        if name == f"{freq}c":
            level = [-110.0, -110.0] + list(centre)
        elif name in (f"{freq}l", f"{freq}u"):
            level = [near] * count
        if extra and name in extra:
            level = [extra[name]] * count
        contents[f"astats@a0h{name}"] = _band_text(level)
    return _files_evidence(tmp_path, contents)


HUM_CTX = dict(position=0, track={"channel_layout": "stereo"}, names=["FL", "FR"], label="", duration=12.0)


class TestHumRuleBoundaries:
    def _row(self, evidence, **cfg):
        return audio_checks.hum_row(evidence, audio_checks.TrackContext(**HUM_CTX), dict(DEFAULT_CONFIG, **cfg))

    @pytest.mark.parametrize("near,status", [(-66.9, "WARN"), (-65.5, "PASS")])
    def test_prominence_boundary_is_6_db(self, tmp_path, near, status):
        assert self._row(_hum_case(tmp_path, [-60.0] * 10, near=near))[0] == status

    @pytest.mark.parametrize("level,status", [(-64.5, "WARN"), (-65.5, "PASS")])
    def test_floor_is_hum_min_db(self, tmp_path, level, status):
        assert self._row(_hum_case(tmp_path, [level] * 10))[0] == status

    def test_continuity_needs_nine_windows_in_ten(self, tmp_path):
        one_off = [-60.0] * 9 + [-62.5]
        assert self._row(_hum_case(tmp_path, one_off))[0] == "WARN"

    def test_two_windows_outside_the_spread_are_a_note(self, tmp_path):
        two_off = [-60.0] * 8 + [-62.5, -62.5]
        status, text, _ = self._row(_hum_case(tmp_path, two_off))
        assert status == "PASS" and "8 of 10" in text and "note" in text

    def test_level_spread_is_2_db(self, tmp_path):
        inside = [-60.0] * 8 + [-61.9, -58.1]
        assert self._row(_hum_case(tmp_path, inside))[0] == "WARN"

    def test_harmonics_are_reported_not_required(self, tmp_path):
        bare = self._row(_hum_case(tmp_path, [-60.0] * 10))
        assert bare[0] == "WARN" and "no harmonics" in bare[1] and bare[2] == audio_checks.LOW_CONFIDENCE
        with_third = self._row(_hum_case(tmp_path / "h", [-60.0] * 10, extra={"60x3c": -70.0}))
        assert "180 Hz at -70.0 dBFS" in with_third[1] and "120 Hz" not in with_third[1]
        with_second = self._row(_hum_case(tmp_path / "h2", [-60.0] * 10, extra={"60x2c": -72.0}))
        assert "120 Hz at -72.0 dBFS" in with_second[1]

    def test_50_hz_is_judged_too(self, tmp_path):
        row = self._row(_hum_case(tmp_path, [-60.0] * 10, freq=50))
        assert row[0] == "WARN" and "50 Hz tone" in row[1]

    def test_band_layout(self):
        bands = {name: (centre, width) for name, centre, width in audio_checks._hum_bands()}
        assert bands["60c"] == pytest.approx((60.0, 0.9))
        assert bands["60u"][0] == pytest.approx(60 * 1.03) and bands["60l"][0] == pytest.approx(60 / 1.03)
        assert bands["50x2c"] == pytest.approx((100.0, 1.5)) and bands["60x3c"][0] == pytest.approx(180.0)
        assert len(bands) == 18

    def test_settling_windows_are_dropped(self, tmp_path):
        evidence = _files_evidence(tmp_path, {"astats@a0h60c": _band_text([-90, -80, -60, -60])})
        assert audio_checks._band_windows(evidence, "astats@a0h60c") == [-60.0, -60.0]
        evidence = _files_evidence(tmp_path / "one", {"astats@a0h60c": _band_text([-60])})
        assert audio_checks._band_windows(evidence, "astats@a0h60c") == [-60.0]

    @needs_ffmpeg
    def test_a_tone_1_5_percent_off_is_not_hum(self, media):
        exprs = [f"{_uniform_noise(-30, 1)}+0.01*sin(2*PI*60.9*t)",
                 f"{_uniform_noise(-30, 2)}+0.01*sin(2*PI*60.9*t)"]
        clip = media.get("hum_off_1p5", lambda p: make_audio(p, exprs, seconds=6))
        assert _row(_scan(clip), "Audio Hum")[1] == "PASS"


def _level_text(blocks, channels=2):
    """astats@aNv file: blocks of (rms dBFS, peak dBFS) per 400 ms, the same on every channel
    unless a block gives a list per channel."""
    out = []
    for k, block in enumerate(blocks):
        out.append(f"frame:{k} pts:{k * 19200} pts_time:{k * 0.4:g}")
        per_channel = block if isinstance(block, list) else [block] * channels
        for c, (rms, peak) in enumerate(per_channel, 1):
            out.append(f"lavfi.astats.{c}.RMS_level={rms}")
            out.append(f"lavfi.astats.{c}.Peak_level={peak}")
    return "\n".join(out) + "\n"


LOUD, QUIET = (-26.0, -20.0), (-62.0, -57.0)


class TestLowLevelRuleBoundaries:
    def _row(self, tmp_path, blocks, names=("FL", "FR"), layout="stereo", **cfg):
        evidence = _files_evidence(tmp_path, {"astats@a0v": _level_text(blocks, len(names))})
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": layout}, names=list(names),
                                        label="", duration=len(blocks) * 0.4)
        return audio_checks.low_level_row(evidence, ctx, dict(DEFAULT_CONFIG, **cfg))

    def test_five_blocks_are_two_seconds(self, tmp_path):
        row = self._row(tmp_path, [LOUD] * 10 + [QUIET] * 5 + [LOUD] * 10)
        assert row[0] == "WARN" and "00:00:04.000 (2.0s" in row[1], row

    def test_four_blocks_are_too_short(self, tmp_path):
        assert self._row(tmp_path, [LOUD] * 10 + [QUIET] * 4 + [LOUD] * 10)[0] == "PASS"

    @pytest.mark.parametrize("rms,status", [(-43.5, "WARN"), (-42.5, "PASS")])
    def test_twenty_db_under_the_gated_level(self, tmp_path, rms, status):
        # Programme blocks at -26 dBFS per channel: gated level -23.0 dB (two channels).
        assert self._row(tmp_path, [LOUD] * 10 + [(rms - 3.0, -45.0)] * 6 + [LOUD] * 10)[0] == status

    @pytest.mark.parametrize("peak,status", [(-40.0, "WARN"), (-39.9, "PASS"), (-70.0, "PASS"), (-69.9, "WARN")])
    def test_peak_window_between_silence_and_the_ceiling(self, tmp_path, peak, status):
        assert self._row(tmp_path, [LOUD] * 10 + [(-75.0, peak)] * 6 + [LOUD] * 10)[0] == status

    def test_stretch_touching_either_end_is_not_inside_the_programme(self, tmp_path):
        assert self._row(tmp_path, [QUIET] * 6 + [LOUD] * 10)[0] == "PASS"
        assert self._row(tmp_path, [LOUD] * 10 + [QUIET] * 6)[0] == "PASS"

    def test_lfe_power_is_left_out(self, tmp_path):
        """A loud LFE under a dead-air stretch of the other channels."""
        names = ("FL", "FR", "FC", "LFE", "BL", "BR")
        stretch = [QUIET, QUIET, QUIET, (-10.0, -5.0), QUIET, QUIET]
        row = self._row(tmp_path, [LOUD] * 10 + [stretch] * 6 + [LOUD] * 10, names=names, layout="5.1")
        assert row[0] == "WARN", row

    def test_gate_is_bs1770_two_stage(self):
        powers = [10 ** -2.0] * 4 + [10 ** -4.0] * 4 + [10 ** -8.0] * 4   # -20, -40, -80 dB
        assert audio_checks._gated_level(powers) == pytest.approx(-20.0)  # -40 is > 10 dB under
        assert audio_checks._gated_level([10 ** -8.0]) is None
        assert audio_checks._gated_level([10 ** -2.0, 10 ** -2.5]) == pytest.approx(
            10 * __import__("math").log10((10 ** -2.0 + 10 ** -2.5) / 2))

    def test_silent_track_has_no_reference(self, tmp_path):
        row = self._row(tmp_path, [(-100.0, -95.0)] * 10)
        assert row[0] == "PASS" and "No programme level" in row[1]

    def test_missing_file_is_not_verified(self, tmp_path):
        evidence = _files_evidence(tmp_path, {})
        ctx = audio_checks.TrackContext(**HUM_CTX)
        with pytest.raises(audio_checks.EvidenceMissing):
            audio_checks.low_level_row(evidence, ctx, dict(DEFAULT_CONFIG))

    def test_more_than_five_stretches_are_summarised(self, tmp_path):
        blocks = [LOUD] * 3
        for _ in range(7):
            blocks += [QUIET] * 6 + [LOUD] * 3
        row = self._row(tmp_path, blocks)
        assert "7 low-level" in row[1] and "+2 more" in row[1]


class TestSelfTestAndMeterBoundaries:
    @pytest.mark.parametrize("error,ok", [(0.0, True), (0.2, True), (0.25, False), (-0.4, True),
                                          (-0.45, False)])
    def test_true_peak_tolerance_is_tech_3341s(self, error, ok):
        assert audio_checks.MeterSelfTest(True, True, error).true_peak_ok is ok
        assert audio_checks.MeterSelfTest(True, True).true_peak_ok is True

    def test_case_6_at_the_edge_of_its_tolerance_passes(self):
        log = "\n".join([_ebur128_summary("ebur128@a0", -23.1), _loudnorm_json("loudnorm@a0", -22.9),
                         _ebur128_summary("ebur128@a1", -18.0, -6.0)])
        test = audio_checks.evaluate_meter_self_test(log)
        assert test.ebur128_multichannel and test.loudnorm and test.true_peak_error_db == pytest.approx(0.02)
        log = log.replace("-23.1 LUFS", "-23.2 LUFS")
        assert audio_checks.evaluate_meter_self_test(log).ebur128_multichannel is False

    def test_a_partial_self_test_log_says_nothing(self):
        assert audio_checks.evaluate_meter_self_test(_ebur128_summary("ebur128@a0", -23.0)) is None

    def test_partial_loudnorm_report_keeps_what_it_has(self):
        log = "[loudnorm@a2 @ 0x2] [info] \nInput Integrated:    -23.1 LUFS\nInput LRA: 3.0 LU"
        assert audio_checks.parse_loudnorm(log)[2]["integrated"] == -23.1
        assert audio_checks.parse_loudnorm("[loudnorm@a2 @ 0x2] [info] \n") == {}

    def test_default_track_is_not_ambisonic_for_the_meter_choice(self):
        track = {"channel_layout": "7.1", "channels": 8}
        assert audio_checks.loudness_meter(track, 8, audio_checks.METER_LOUDNORM) == audio_checks.METER_NONE
        assert audio_checks.loudness_meter(track, 8, audio_checks.METER_LOUDNORM, True) == audio_checks.METER_EBUR128

    def test_mapping_of_5_1(self):
        names = audio_checks.channel_names({"channel_layout": "5.1"}, 6)
        assert audio_checks.loudnorm_mapping({"channel_layout": "5.1"}, names) == \
            "aformat=sample_fmts=dbl,pan=5.1|c0=c0|c1=c1|c2=c2|c4=c4|c5=c5"
        quad = audio_checks.channel_names({"channel_layout": "4.0"}, 4)       # BC on a surround input
        assert audio_checks.loudnorm_mapping({"channel_layout": "4.0"}, quad) == \
            "aformat=sample_fmts=dbl,pan=5.1|c0=c0|c1=c1|c2=c2|c4=c3"
        down, up = (1 / 1.41) ** 0.5, 1.41 ** 0.5
        tops = audio_checks.channel_names({"channel_layout": "3.1.2"}, 6)     # 5 fronts: 2 on surround inputs
        assert audio_checks.loudnorm_mapping({"channel_layout": "3.1.2"}, tops) == \
            f"aformat=sample_fmts=dbl,pan=5.1|c0=c0|c1=c1|c2=c2|c4={down:.9f}*c4|c5={down:.9f}*c5"
        track = {"channel_layout": "5 channels (FL+FR+BL+BR+BC)"}           # 3 surrounds: 1 on a front input
        names = audio_checks.channel_names(track, 5)
        assert audio_checks.loudnorm_mapping(track, names) == \
            f"aformat=sample_fmts=dbl,pan=5.1|c0=c0|c1=c1|c2={up:.9f}*c4|c4=c2|c5=c3"
        assert audio_checks.loudnorm_mapping({"channel_layout": "unknown"}, ["ch1", "ch2", "ch3"]) is None

    def test_unmeasured_reason_counts_weighted_channels(self):
        names = audio_checks.channel_names({"channel_layout": "7.1"}, 8)
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": "7.1"}, names=names, label="",
                                        duration=10.0, meter=audio_checks.METER_NONE)
        assert "this 7.1 track has 7 weighted channels" in audio_checks._unmeasured_reason(ctx, "loudness summary")

    def test_weights_are_the_documented_rejection_risk_scale(self):
        assert audio_checks.SEVERITY_WEIGHTS == {
            "Audio Loudness": 70, "Audio True Peak": 65, "Audio Clipping": 60, "Audio Dropouts": 40,
            "Audio Phase": 35, "Dual Mono": 20, "Audio Hum": 20, "DC Offset": 15, "Low-Level Audio": 30}


class TestAmbisonicSignalling:
    BOX = {"ambisonic_type": 0, "order": 1, "ordering": 0, "normalization": 0, "head_locked_stereo": True}

    def test_head_locked_from_the_layout_or_the_sa3d_flag(self):
        named = {"channels": 6, "channel_layout": "ambisonic 1+stereo"}
        aac = {"channels": 6, "channel_layout": "5.1"}
        assert audio_checks.head_locked_positions([named], [None], [0]) == [0]
        assert audio_checks.head_locked_positions([aac], [self.BOX], [0]) == [0]
        assert audio_checks.head_locked_positions([aac], [dict(self.BOX, head_locked_stereo=False)], [0]) == []
        assert audio_checks.head_locked_positions([{"channels": 5, "channel_layout": "5.0"}], [self.BOX], [0]) == []
        assert audio_checks.head_locked_positions([aac], [self.BOX], [1]) == []

    def test_sn3d_from_the_layout_or_the_sa3d_box(self):
        aac = {"channels": 4, "channel_layout": "4.0"}
        assert audio_checks.sn3d_positions([aac], [self.BOX], [0]) == [0]
        assert audio_checks.sn3d_positions([aac], [dict(self.BOX, normalization=1)], [0]) == []
        assert audio_checks.sn3d_positions([aac], [dict(self.BOX, ordering=1)], [0]) == []
        assert audio_checks.sn3d_positions([aac], [{"error": "short box"}], [0]) == []
        assert audio_checks.sn3d_positions([{"channels": 4, "channel_layout": "ambisonic 1"}], [None], [0]) == [0]
        assert audio_checks.sn3d_positions([aac], [None], [0]) == []

    def test_second_order_names(self):
        names = audio_checks.channel_names({"channel_layout": "ambisonic 2"}, 9, ambisonic=True)
        assert names == ["W", "Y", "Z", "X", "ACN4", "ACN5", "ACN6", "ACN7", "ACN8"]
        hl = audio_checks.channel_names({"channel_layout": "5.1"}, 6, ambisonic=True, head_locked=True)
        assert hl[-2:] == ["HL-L", "HL-R"]

    def test_fuma_reading_is_three_db_up(self):
        assert audio_checks.FUMA_W_OFFSET_DB == pytest.approx(3.0103, abs=1e-4)


class TestRecorderAndPhaseDetails:
    def test_recorders_strip_by_default(self):
        files = audio_checks.MeasurementFiles("/tmp")
        assert audio_checks._record("x", files).endswith(",ametadata=mode=delete")
        assert "mode=delete" not in audio_checks._record("x", files, strip=False)
        assert audio_checks._record("x", None) == ""

    def test_exact_inversion_reports_its_fold_down_loss(self, tmp_path):
        """R = -L: L+R is digital silence, the fold-down loses everything."""
        log = "\n".join(["[astats@a0p0 @ 0x1] [info] Channel: 1", "[astats@a0p0 @ 0x1] [info] RMS level dB: -inf",
                         "[astats@a0p0 @ 0x1] [info] Channel: 2", "[astats@a0p0 @ 0x1] [info] RMS level dB: -14.0"])
        evidence = audio_checks.ScanEvidence(log_text=log, silence_events=lambda *a, **k: [])
        ctx = audio_checks.TrackContext(**HUM_CTX)
        assert audio_checks._fold_down_lu(evidence, ctx, 0) <= -100
        log2 = log.replace("RMS level dB: -inf", "RMS level dB: -18.0")
        evidence2 = audio_checks.ScanEvidence(log_text=log2, silence_events=lambda *a, **k: [])
        expected = 10 * __import__("math").log10(2 * 10 ** -1.8 / (10 ** -1.8 + 10 ** -1.4))
        assert audio_checks._fold_down_lu(evidence2, ctx, 0) == pytest.approx(expected)

    @needs_ffmpeg
    def test_wide_inversion_states_its_loss(self, media):
        clip = media.get("wide_inverted", lambda p: make_audio(p, _wide_stereo(True), seconds=6))
        assert "loses 2.0 LU" in _row(_scan(clip), "Audio Phase")[2]

    @needs_ffmpeg
    def test_a_mostly_antiphase_but_net_positive_track_has_no_loss_text(self, media):
        """Anti-phase for 4 of 6 s, quieter than the in-phase 2 s: share 67 %,
        net correlation +0.1, so no fold-down loss to state."""
        left = "if(lt(t,2),0.156,0.1)*(random(0)-0.5)"
        right = "if(lt(t,2),0.156,-0.1)*(random(0)-0.5)"
        clip = media.get("antiphase_net_positive", lambda p: make_audio(p, [left, right], seconds=6))
        row = _row(_scan(clip), "Audio Phase")
        assert "polarity-inverted" in row[2] and "fold-down loses" not in row[2], row


@needs_ffmpeg
class TestChannelAndTrackDetails:
    def test_lfe_silent_for_a_while_is_not_a_channel_silence(self, media):
        lfe = f"if(between(t,0.5,3.5),0,{tone(60, 0.2)})"
        clip = media.get("lfe_gap_5.1", lambda p: make_audio(
            p, [tone(440), tone(554), tone(659), lfe, tone(740), tone(831)], layout="5.1"))
        assert _row(_scan(clip), "Audio Silence")[1] == "PASS"

    def test_glits_shaped_gaps_in_programme_are_dropouts(self, media):
        """L once, R twice 0.5 s apart, but in noise programme, not a tone."""
        left = f"if(between(t,2,2.25),0,{NOISE})"
        right = f"if(between(t,2.5,2.75)+between(t,3,3.25),0,{NOISE.replace('0.1', '0.09')})"
        clip = media.get("glits_in_noise", lambda p: make_audio(p, [left, right], seconds=5))
        row = _row(_scan(clip), "Audio Dropouts")
        assert row[1] == "WARN" and "line-up ident" not in row[2], row

    def test_a_partner_dropout_overlapping_an_ident_gap_disqualifies_it(self, media):
        tone400 = "0.1259*sin(2*PI*400*t)"
        left = f"if(between(t,2,2.25),0,{tone400})"
        right = f"if(between(t,1.9,2.1)+between(t,2.5,2.75)+between(t,3,3.25),0,{tone400})"
        clip = media.get("glits_overlap", lambda p: make_audio(p, [left, right], seconds=5))
        row = _row(_scan(clip), "Audio Dropouts")
        assert row[1] == "WARN" and "FL" in row[2], row

    def test_metadata_follows_the_default_track_when_it_is_first(self, media):
        clip = media.get("two_tracks_first_default", lambda p: _two_tracks(
            p, TONE_23, tone(1000, MINUS_23 / 3), default=0))
        result = _scan(clip, loudness_mode="essential")
        assert abs(result.metadata["integrated_loudness_lufs"] + 23.0) <= 0.1, result.metadata


class TestTrackSilenceShare:
    CFG = dict(DEFAULT_CONFIG)

    @pytest.mark.parametrize("silent,status", [(4.4, "WARN"), (5.0, "WARN"), (5.6, "FAIL")])
    def test_more_than_half_fails(self, silent, status):
        status_got, _ = core._track_silence_entry("", [(1.0, silent)], 10.0, self.CFG)
        assert status_got == status

    def test_unknown_duration_never_fails_on_share(self):
        assert core._track_silence_entry("", [(1.0, 3.0)], 0.0, self.CFG)[0] == "WARN"


class TestMutationSurvivorsRound3:
    """Second mutation pass on the final audio_checks.py (reviewer's operators,
    seed 92): each test pins a rule a surviving mutant broke."""

    def _low(self, tmp_path, blocks):
        return TestLowLevelRuleBoundaries()._row(tmp_path, blocks)

    @pytest.mark.parametrize("count,more", [(5, None), (6, "+1 more")])
    def test_stretch_list_summary(self, tmp_path, count, more):
        blocks = [LOUD] * 3
        for _ in range(count):
            blocks += [QUIET] * 6 + [LOUD] * 3
        text = self._low(tmp_path, blocks)[1]
        assert (more in text) if more else (" more" not in text.split(": ", 1)[1]), text

    def test_stretch_level_is_its_mean_power(self, tmp_path):
        text = self._low(tmp_path, [LOUD] * 10 + [QUIET] * 6 + [LOUD] * 10)[1]
        assert "(2.4s at -59.0 dBFS)" in text, text   # -62 dBFS on each of two channels

    def test_absolute_gate_is_minus_70(self):
        assert audio_checks._gated_level([10 ** -7.1] * 3) is None

    def test_loudnorm_reading_beyond_0_1_lu_fails_the_self_test(self):
        log = "\n".join([_ebur128_summary("ebur128@a0", -23.0), _loudnorm_json("loudnorm@a0", -23.12),
                         _ebur128_summary("ebur128@a1", -18.0, -6.0)])
        assert audio_checks.evaluate_meter_self_test(log).loudnorm is False

    def test_short_sa3d_list_is_no_box(self):
        track = {"channels": 6, "channel_layout": "ambisonic 1+stereo"}
        assert audio_checks.head_locked_positions([track], [], [0]) == [0]
        assert audio_checks.sn3d_positions([{"channels": 4, "channel_layout": "4.0"}], [], [0]) == []

    def test_mismatched_names_on_a_signalled_layout_are_not_placed(self):
        assert audio_checks.loudnorm_mapping({"channel_layout": "stereo"}, ["ch1", "ch2", "ch3"]) is None

    def test_silent_or_missing_hum_bands(self, tmp_path):
        ctx = audio_checks.TrackContext(**HUM_CTX)
        silent_harmonic = _hum_case(tmp_path, [-60.0] * 10, extra={"60x2c": "-inf"})
        assert audio_checks.hum_row(silent_harmonic, ctx, dict(DEFAULT_CONFIG))[0] == "WARN"
        empty_near = _hum_case(tmp_path / "e", [-60.0] * 10)
        for name in ("60x3l", "60x3u"):
            with open(empty_near.files.paths[f"astats@a0h{name}"], "w") as handle:
                handle.write("")
        assert "180 Hz" not in audio_checks.hum_row(empty_near, ctx, dict(DEFAULT_CONFIG))[1]
        no_near = _hum_case(tmp_path / "n", [-60.0] * 10)
        for name in ("50l", "50u", "60l", "60u"):
            with open(no_near.files.paths[f"astats@a0h{name}"], "w") as handle:
                handle.write("")
        status, text, _ = audio_checks.hum_row(no_near, ctx, dict(DEFAULT_CONFIG))
        assert status == "WARN" and core.NOT_VERIFIED_PREFIX in text, text

    def test_floor_is_inclusive(self, tmp_path):
        ctx = audio_checks.TrackContext(**HUM_CTX)
        assert audio_checks.hum_row(_hum_case(tmp_path, [-65.0] * 10), ctx, dict(DEFAULT_CONFIG))[0] == "WARN"

    def test_glits_step_tolerance_is_60_ms(self):
        gaps = {0: [(2.0, 0.25)], 1: [(2.58, 0.25)]}
        assert audio_checks._glits_gaps(gaps, gaps, (0, 1)) == set()
        gaps = {0: [(2.0, 0.25)], 1: [(2.55, 0.25), (3.05, 0.25)]}
        assert audio_checks._glits_gaps(gaps, gaps, (0, 1)) == {(0, 2.0), (1, 2.55), (1, 3.05)}

    def test_silent_w_of_unsignalled_normalization_stays_a_skip(self):
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": "4.0"}, names=["W", "Y", "Z", "X"],
                                        label="", duration=60.0, ambisonic=True, sn3d_signalled=False)
        row = audio_checks.loudness_row({"integrated": -70.0}, ctx, dict(DEFAULT_CONFIG, loudness_mode="essential"))
        assert row[0] == "SKIP", row

    def test_not_verified_rows_carry_full_confidence(self, tmp_path):
        test = audio_checks.MeterSelfTest(True, True, -1.0)
        ctx = audio_checks.TrackContext(position=0, track={"channel_layout": "stereo"}, names=["FL", "FR"],
                                        label="", duration=60.0, meter_self_test=test)
        assert audio_checks.true_peak_row({"true_peak": -5.0}, ctx, dict(DEFAULT_CONFIG))[2] == 100
        evidence = _files_evidence(tmp_path, {"aphasemeter@a0p0.start": "", "aphasemeter@a0p0.end": ""})
        audio_checks._phase_chains(0, {"channel_layout": "stereo"}, ["FL", "FR"], dict(DEFAULT_CONFIG), False,
                                   evidence.files)
        row = audio_checks.phase_row(evidence, audio_checks.TrackContext(**HUM_CTX), dict(DEFAULT_CONFIG))
        assert row[0] == "WARN" and core.NOT_VERIFIED_PREFIX in row[1] and row[2] == 100, row


# ---------------------------------------------------------------------------
# Round 5 C2: Low-Level Audio is informational - it never changes the verdict
# ---------------------------------------------------------------------------

def _dialogue_with_pauses(seed, freq):
    """-23 LUFS-ish programme with 2.8 s pauses of -59 dBFS RMS room tone
    (the reviewer's drama2.mov shape): an ordinary dialogue scene."""
    pause = "(between(t,3,5.8)+between(t,8,10.8))"
    return f"{_uniform_noise(-59, seed)}*{pause}+(1-{pause})*{tone(freq, MINUS_23)}"


@needs_ffmpeg
class TestLowLevelAudioIsInformational:
    @pytest.mark.parametrize("strict", [False, True])
    def test_dialogue_pauses_never_change_the_verdict(self, media, strict):
        """0.9.2-rc: default PASS_WITH_WARNINGS, Strict MISMATCH, on a clean
        dialogue programme (0.9.1 and 2b9d4ef: PASS)."""
        clip = media.get("dialogue_pauses_stereo", lambda p: make_audio(
            p, [_dialogue_with_pauses(1, 1000), _dialogue_with_pauses(2, 1250)], seconds=14))
        cfg = dict(target_width=96, target_height=64, target_fps=10.0, min_bitrate_mbps=0,
                   accepted_containers=[".mov"], signal_levels_mode="off", warn_untagged_color=False)
        with_row = core.run_qa_checks(clip, cfg=dict(DEFAULT_CONFIG, **cfg), strict=strict)
        with_row.finalize()
        without = core.run_qa_checks(clip, cfg=dict(DEFAULT_CONFIG, audio_low_level_check=False, **cfg),
                                     strict=strict)
        without.finalize()
        row = _row(with_row, "Low-Level Audio")
        assert row[1] == "WARN" and row[3] <= audio_checks.LOW_CONFIDENCE, row
        assert with_row.status == without.status, (with_row.status, without.status)
        assert with_row.status != "MISMATCH"
        assert "Low-Level Audio" in with_row.metadata.get("informational", [])

    def test_hiss_dropout_still_produces_the_row(self, media):
        def channel(seed):
            return (f"{_uniform_noise(-62, seed)}*between(t,3,7)"
                    f"+(1-between(t,3,7))*{TONE_23}")
        clip = media.get("dead_air_hiss", lambda p: make_audio(p, [channel(1), channel(2)], seconds=10))
        result = core.run_qa_checks(clip, cfg=dict(DEFAULT_CONFIG, target_width=96, target_height=64,
                                                   target_fps=10.0, min_bitrate_mbps=0,
                                                   accepted_containers=[".mov"]), strict=True)
        result.finalize()
        row = _row(result, "Low-Level Audio")
        assert row[1] == "WARN" and "00:00:03" in row[2], row
        assert not any(c[0] == "Low-Level Audio" for c in result.checks if c[1] == "FAIL")

    def test_an_informational_row_alone_leaves_a_clean_pass(self):
        result = core.QAResult("x.mov")
        result.add_pass("Container Format", "ok")
        result.add_informational("Low-Level Audio", "1 low-level stretch", 60)
        for strict in (False, True):
            assert result.overall_status(strict=strict) == "PASS"
        assert result.warnings == [] and core.result_severity(result) == (0.0, "")
        assert result.checks[-1] == ("Low-Level Audio", "WARN", "1 low-level stretch", 60)
        result.strict = True
        from frameguard.gui import _result_status
        assert _result_status(result) == "PASS"       # the unfinalized fallback agrees
        result.strict = False
        result.add_warn("Audio Hum", "possible hum", 60)
        assert result.overall_status() == "PASS_WITH_WARNINGS"
        assert result.overall_status(strict=True) == "MISMATCH"
