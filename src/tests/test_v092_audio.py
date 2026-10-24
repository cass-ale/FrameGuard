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
    each stream is seeded once at the first sample."""
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
