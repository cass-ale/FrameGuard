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
