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

_VIDEO_ENCODERS = {
    "h264": ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
    "hevc": ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-x265-params", "log-level=error", "-tag:v", "hvc1"],
    "prores": ["-c:v", "prores_ks", "-profile:v", "2"],
}


def ffmpeg(*args):
    """Run FFmpeg quietly; raise with its stderr if it fails."""
    if not FFMPEG:
        pytest.skip("FFmpeg not available")
    proc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-2000:]}")


def make_clip(path, *, vcodec="h264", rate=30, duration=DURATION, size=SIZE,
              audio=("stereo",), video_filter=None, extra=(), video_source=None):
    """A testsrc2 clip with one 440 Hz sine track per entry in ``audio``.

    ``audio`` entries: "stereo", "mono", "5.1", "silent-stereo", "silent-5.1",
    "dead-right" (stereo with a silent right channel) or "8k" (8 kHz mono).
    """
    path = str(path)
    source = video_source or f"testsrc2=s={size}:r={rate}:d={duration}"
    args = ["-f", "lavfi", "-i", source]
    maps = ["-map", "0:v"]
    filters = []
    for n, kind in enumerate(audio, start=1):
        if kind.startswith("silent"):
            layout = "5.1" if kind.endswith("5.1") else "stereo"
            args += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl={layout}:d={duration}"]
        elif kind == "8k":
            args += ["-f", "lavfi", "-i", f"sine=f=440:sample_rate=8000:d={duration}"]
        else:
            args += ["-f", "lavfi", "-i", f"sine=f=440:sample_rate=48000:d={duration}"]
        label = f"a{n}"
        if kind == "stereo":
            filters.append(f"[{n}:a]pan=stereo|c0=c0|c1=c0[{label}]")
        elif kind == "5.1":
            filters.append(f"[{n}:a]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[{label}]")
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
