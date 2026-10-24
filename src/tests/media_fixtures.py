"""Synthesized media for engine regression tests.

Every clip is generated on demand with the real FFmpeg (lavfi sources), small
and short so the whole set builds in a few seconds. Tests that need a clip
skip cleanly when FFmpeg is unavailable.
"""

import os
import struct
import subprocess

import pytest

from frameguard.tools import get_ffmpeg_path

FFMPEG = get_ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg not available")

SIZE = "320x240"
DURATION = 6

# Clean deliverables signal their colour completely: an untagged clip
# (0.9.1) and a partly tagged one (0.9.2: matrix only, x264's default when
# only -colorspace is given) are advisory Color / HDR findings, so the clean
# fixtures carry BT.709 primaries, transfer and matrix.
_COLOUR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
# FFmpeg 9 writes the primaries and transfer the frames carry, not the
# encoder options, so the sources are tagged too.
COLOUR_TAGGED = ("scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                 "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709")
_VIDEO_ENCODERS = {
    "h264": ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *_COLOUR_TAGS],
    "hevc": ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-x265-params", "log-level=error", "-tag:v", "hvc1", *_COLOUR_TAGS],
    "prores": ["-c:v", "prores_ks", "-profile:v", "2", *_COLOUR_TAGS],
}


def ffmpeg(*args):
    """Run FFmpeg quietly; raise with its stderr if it fails."""
    if not FFMPEG:
        pytest.skip("FFmpeg not available")
    proc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-2000:]}")


# Per-channel tone frequencies (Hz). The LFE gets 440 Hz, which AAC's LFE
# low-pass removes, so that channel decodes silent as before.
_DISTINCT_TONES = {
    "stereo": (440, 554.37),
    "5.1": (440, 554.37, 659.25, 440, 739.99, 830.61),
}


def make_clip(path, *, vcodec="h264", rate=30, duration=DURATION, size=SIZE,
              audio=("stereo",), video_filter=None, extra=(), video_source=None):
    """A testsrc2 clip with one 440 Hz sine track per entry in ``audio``.

    ``audio`` entries: "stereo", "mono", "5.1", "silent-stereo", "silent-5.1",
    "dead-right" (stereo with a silent right channel) or "8k" (8 kHz mono).
    """
    path = str(path)
    source = video_source or f"testsrc2=s={size}:r={rate}:d={duration}"
    sets_colour = any(k in str(a) for a in extra for k in ("colorprim", "color_primaries"))
    if ";" not in source and "setparams" not in source and not sets_colour:
        source += f",{COLOUR_TAGGED}"
    args = ["-f", "lavfi", "-i", source]
    maps = ["-map", "0:v"]
    filters = []
    for n, kind in enumerate(audio, start=1):
        if kind.startswith("silent"):
            layout = "5.1" if kind.endswith("5.1") else "stereo"
            args += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl={layout}:d={duration}"]
        elif kind == "8k":
            args += ["-f", "lavfi", "-i", f"sine=f=440:sample_rate=8000:d={duration}"]
        elif kind in _DISTINCT_TONES:
            # A different tone per channel: identical channels are dual mono,
            # which the 0.9.1 Dual Mono check (rightly) flags.
            tones = "|".join(f"0.125*sin(2*PI*{f}*t)" for f in _DISTINCT_TONES[kind])
            args += ["-f", "lavfi", "-i", f"aevalsrc={tones}:s=48000:d={duration}"]
        else:
            args += ["-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:d={duration}"]
        label = f"a{n}"
        if kind == "stereo":
            filters.append(f"[{n}:a]aformat=channel_layouts=stereo[{label}]")
        elif kind == "5.1":
            filters.append(f"[{n}:a]aformat=channel_layouts=5.1[{label}]")
        elif kind == "dead-right":
            filters.append(f"[{n}:a]pan=stereo|c0=c0|c1=0*c0[{label}]")
        else:
            filters.append(f"[{n}:a]anull[{label}]")
        maps += ["-map", f"[{label}]"]
    if video_filter:
        filters.insert(0, f"[0:v]{video_filter}[v]")
        maps[1] = "[v]"
    if filters:
        args += ["-filter_complex", ";".join(filters)]
    args += maps + _VIDEO_ENCODERS[vcodec]
    if audio:
        audio_codec = ["-c:a", "pcm_s24le"] if vcodec == "prores" else ["-c:a", "aac"]
        args += audio_codec
    args += ["-t", str(duration), *extra, path]
    ffmpeg(*args)
    return path


def truncate(src, dst, fraction):
    """Copy the first ``fraction`` of the bytes: an incomplete copy/upload."""
    with open(src, "rb") as handle:
        data = handle.read()
    with open(dst, "wb") as handle:
        handle.write(data[:int(len(data) * fraction)])
    return str(dst)


def corrupt_middle(src, dst, spots=6, length=600):
    """Overwrite a few byte runs in the middle half of the file (moov intact
    because the mov muxer writes it at the end)."""
    with open(src, "rb") as handle:
        data = bytearray(handle.read())
    size = len(data)
    for i in range(spots):
        offset = int(size * (0.3 + 0.3 * i / spots))
        data[offset:offset + length] = bytes((offset + k * 37) & 0xFF for k in range(length))
    with open(dst, "wb") as handle:
        handle.write(bytes(data))
    return str(dst)


_SPHERICAL_UUID = bytes.fromhex("ffcc8263f8554a938814587a02521fdd")


def _boxes(buf, start, end):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[pos:pos + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        yield pos, size, kind, header
        pos += size


def inject_spherical(path, stereo=None):
    """Add Google Spherical Video V1 metadata (equirectangular, optional
    StereoMode) to the video track of an MP4 whose moov box is last.

    FFmpeg can read this metadata but has no CLI to write it from scratch.
    """
    with open(path, "rb") as handle:
        data = bytearray(handle.read())
    moov = next(b for b in _boxes(data, 0, len(data)) if b[2] == b"moov")
    m_pos, m_size, _, m_header = moov
    if m_pos + m_size != len(data):
        raise ValueError("moov must be the last box (do not use +faststart)")
    xml = ('<?xml version="1.0"?><rdf:SphericalVideo '
           'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/">'
           '<GSpherical:Spherical>true</GSpherical:Spherical>'
           '<GSpherical:Stitched>true</GSpherical:Stitched>'
           '<GSpherical:StitchingSoftware>FrameGuard tests</GSpherical:StitchingSoftware>'
           '<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>'
           + (f'<GSpherical:StereoMode>{stereo}</GSpherical:StereoMode>' if stereo else '')
           + '</rdf:SphericalVideo>').encode()
    box = struct.pack(">I4s", 8 + 16 + len(xml), b"uuid") + _SPHERICAL_UUID + xml
    for t_pos, t_size, kind, _ in _boxes(data, m_pos + m_header, m_pos + m_size):
        if kind == b"trak" and b"vide" in data[t_pos:t_pos + t_size]:
            data[t_pos + t_size:t_pos + t_size] = box
            struct.pack_into(">I", data, t_pos, t_size + len(box))
            struct.pack_into(">I", data, m_pos, m_size + len(box))
            with open(path, "wb") as handle:
                handle.write(bytes(data))
            return str(path)
    raise ValueError("no video track")


class MediaLibrary:
    """Builds each named clip once per test session."""

    def __init__(self, root):
        self.root = root
        self._built = {}

    def get(self, name, builder):
        if name not in self._built:
            self._built[name] = builder(os.path.join(self.root, name))
        return self._built[name]


def make_open_gop_source(path, vcodec="h264", rate="30", duration=12):
    """A long-GOP, open-GOP clip with B-frames, the shape camera and
    mezzanine encodes have, so a stream-copy cut lands mid-GOP."""
    if vcodec == "hevc":
        video = ["-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1",
                 "-x265-params", "log-level=error:keyint=48:min-keyint=48:open-gop=1:"
                                 "bframes=4:scenecut=0"]
    else:
        video = ["-c:v", "libx264", "-preset", "ultrafast",
                 "-x264-params", "keyint=45:min-keyint=45:open-gop=1:bframes=3:scenecut=0"]
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=s={SIZE}:r={rate}:d={duration},{COLOUR_TAGGED}",
           "-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:d={duration}",
           *video, "-pix_fmt", "yuv420p", *_COLOUR_TAGS, "-c:a", "aac", str(path))
    return str(path)


def stream_copy_trim(src, dst, start, length):
    """Lossless cut (no re-encode), as LosslessCut or ``ffmpeg -c copy`` makes:
    the muxer keeps the GOP before ``start`` and hides it with an edit list."""
    ffmpeg("-ss", str(start), "-i", str(src), "-t", str(length), "-c", "copy", str(dst))
    return str(dst)
