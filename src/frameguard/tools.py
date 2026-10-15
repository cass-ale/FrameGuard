"""
FrameGuard Desktop v0.3 -- External Tool Detection & Management.

Provides runtime detection, path resolution, and download helpers for:
  - FFmpeg / FFprobe  (required)
  - MediaConch        (optional — policy-based container/codec validation)
  - MediaInfo         (optional — richer metadata extraction)

When running from a PyInstaller bundle, tools are first searched in the
application directory (dist/FrameGuard/tools/), then on the system PATH.

Auto-download helpers fetch portable binaries for Windows/macOS/Linux
so the installer can bundle them or the user can fetch them on first run.
"""

import os
import sys
import shutil
import stat
import platform
import subprocess
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict

logger = logging.getLogger("frameguard.tools")


def _get_clean_env() -> dict:
    """Return sanitized environment for subprocess calls in frozen mode."""
    env = os.environ.copy()
    if getattr(sys, 'frozen', False):
        if 'LD_LIBRARY_PATH_ORIG' in env:
            env['LD_LIBRARY_PATH'] = env['LD_LIBRARY_PATH_ORIG']
        elif 'LD_LIBRARY_PATH' in env:
            del env['LD_LIBRARY_PATH']
        if 'DYLD_LIBRARY_PATH_ORIG' in env:
            env['DYLD_LIBRARY_PATH'] = env['DYLD_LIBRARY_PATH_ORIG']
        elif 'DYLD_LIBRARY_PATH' in env:
            del env['DYLD_LIBRARY_PATH']
    return env


def _subprocess_kwargs() -> dict:
    """Hide console windows on Windows and force UTF-8 decoding.

    text=True otherwise decodes with the locale encoding under strict error
    handling, so a non-ASCII byte in an FFmpeg banner raises UnicodeDecodeError.
    """
    kwargs = {"encoding": "utf-8", "errors": "replace"}
    if os.name == 'nt':  # Windows
        kwargs['creationflags'] = 0x08000000
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs['startupinfo'] = si
    return kwargs


__all__ = [
    "ToolInfo", "ToolRegistry", "get_tool_registry",
    "check_all_tools", "get_ffmpeg_path", "get_ffprobe_path",
    "get_mediaconch_path", "get_mediainfo_path",
]


# ============================================================================
# CONSTANTS
# ============================================================================

# Download URLs for portable binaries (Windows x64).
# NOTE: informational only — the builders use download_tools.py SOURCES.
# Kept correct so a future caller doesn't inherit a stale URL: evermeet
# serves ffmpeg and ffprobe as SEPARATE archives (the old combined
# .../getrelease/zip endpoint is wrong), and mediaarea versions track
# download_tools.py.
DOWNLOAD_URLS = {
    "ffmpeg": {
        "Windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "Darwin": "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip",
        "Linux": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    },
    "ffprobe": {
        "Windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "Darwin": "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip",
    },
    "mediaconch": {
        "Windows": "https://mediaarea.net/download/binary/mediaconch/26.05/MediaConch_CLI_26.05_Windows_x64.zip",
        "Darwin": "https://mediaarea.net/download/binary/mediaconch/26.05/MediaConch_CLI_26.05_Mac.dmg",
        "Linux": "https://mediaarea.net/download/binary/mediaconch/26.05/mediaconch_26.05-1_amd64.xUbuntu_22.04.deb",
    },
    "mediainfo": {
        "Windows": "https://mediaarea.net/download/binary/mediainfo/26.05/MediaInfo_CLI_26.05_Windows_x64.zip",
        "Darwin": "https://mediaarea.net/download/binary/mediainfo/26.05/MediaInfo_CLI_26.05_Mac.dmg",
        "Linux": "https://mediaarea.net/download/binary/mediainfo/26.05/mediainfo_26.05-1_amd64.xUbuntu_22.04.deb",
    },
}

# Minimum versions (semver-style, compared as tuples)
MIN_VERSIONS = {
    "ffmpeg": (4, 0),
    "ffprobe": (4, 0),
    "mediaconch": (23, 0),
    "mediainfo": (23, 0),
}


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ToolInfo:
    """Information about an external tool."""
    name: str
    required: bool = True
    found: bool = False
    path: Optional[str] = None
    version: Optional[str] = None
    version_tuple: Tuple[int, ...] = ()
    meets_minimum: bool = False
    error: Optional[str] = None

    @property
    def status_text(self) -> str:
        if not self.found:
            return "NOT FOUND" if self.required else "Not installed (optional)"
        if not self.meets_minimum:
            return f"v{self.version} (below minimum)"
        return f"v{self.version} — OK"

    @property
    def is_usable(self) -> bool:
        return self.found and self.meets_minimum


@dataclass
class ToolRegistry:
    """Registry of all external tools and their status."""
    ffmpeg: ToolInfo = field(default_factory=lambda: ToolInfo("ffmpeg", required=True))
    ffprobe: ToolInfo = field(default_factory=lambda: ToolInfo("ffprobe", required=True))
    mediaconch: ToolInfo = field(default_factory=lambda: ToolInfo("mediaconch", required=False))
    mediainfo: ToolInfo = field(default_factory=lambda: ToolInfo("mediainfo", required=False))

    @property
    def all_required_ok(self) -> bool:
        return self.ffmpeg.is_usable and self.ffprobe.is_usable

    @property
    def summary(self) -> str:
        lines = []
        for tool in [self.ffmpeg, self.ffprobe, self.mediaconch, self.mediainfo]:
            icon = "✓" if tool.is_usable else ("✗" if tool.required else "○")
            lines.append(f"  {icon} {tool.name}: {tool.status_text}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, dict]:
        result = {}
        for tool in [self.ffmpeg, self.ffprobe, self.mediaconch, self.mediainfo]:
            result[tool.name] = {
                "found": tool.found,
                "path": tool.path,
                "version": tool.version,
                "usable": tool.is_usable,
            }
        return result


# ============================================================================
# PATH RESOLUTION
# ============================================================================

def _candidate_tools_dirs() -> list:
    """All directories that may hold bundled ffmpeg/ffprobe/mediainfo binaries.

    Covers PyInstaller one-file (``sys._MEIPASS``), one-dir on PyInstaller >= 6
    (data lands under ``_internal/``), a macOS ``.app`` bundle (where
    ``sys.executable`` is ``Contents/MacOS/FrameGuard`` and tools are staged
    into ``Contents/Resources/tools``), and running from the source tree.
    """
    dirs = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        roots = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(exe_dir)
        roots.append(os.path.join(exe_dir, "_internal"))
        if sys.platform == "darwin":
            # Contents/MacOS/.. -> Contents. Resources first: that is where
            # build_mac.sh stages ffmpeg, and it is the conventional location
            # for non-executable bundle payload.
            contents = os.path.abspath(os.path.join(exe_dir, os.pardir))
            roots.insert(0, os.path.join(contents, "Resources"))
            roots.append(os.path.join(contents, "Frameworks"))
            roots.append(contents)
    else:
        source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        roots = [source_root]

    plat_key = "darwin" if sys.platform == "darwin" else (
        "windows" if os.name == "nt" else "linux")

    for root in roots:
        if not root:
            continue
        dirs.append(os.path.join(root, "tools"))
        # Build-time cache is per-platform so a checkout can hold both.
        # download_tools.py splits the macOS cache per architecture
        # (tools_cache/darwin-arm64, tools_cache/darwin-x86_64) so the two
        # builds can never bundle each other's binaries; search the host
        # arch first, then the other arch, then the pre-split layout name.
        if plat_key == "darwin":
            host_arch = "arm64" if platform.machine() == "arm64" else "x86_64"
            other_arch = "x86_64" if host_arch == "arm64" else "arm64"
            dirs.append(os.path.join(root, "tools_cache", f"darwin-{host_arch}"))
            dirs.append(os.path.join(root, "tools_cache", f"darwin-{other_arch}"))
        dirs.append(os.path.join(root, "tools_cache", plat_key))
        dirs.append(os.path.join(root, "tools_cache"))
        dirs.append(root)

    seen = set()
    return [d for d in dirs if os.path.isdir(d) and not (d in seen or seen.add(d))]


def _ensure_executable(path: str) -> bool:
    """True if `path` can actually be executed.

    Zip extraction drops the Unix executable bit, so a bundled macOS binary is
    frequently present but unrunnable. Try to repair it; if that fails, report
    it as unusable rather than handing the caller a path that will raise
    PermissionError in the middle of a scan.
    """
    if os.name == "nt":
        return True
    if os.access(path, os.X_OK):
        return True
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    if os.access(path, os.X_OK):
        logger.info("Restored the executable bit on %s", path)
        return True
    logger.warning("Found %s but it is not executable; skipping.", path)
    return False


def _find_executable(name: str) -> Optional[str]:
    """Find an executable, checking bundled dirs first, then system PATH."""
    ext = ".exe" if platform.system() == "Windows" else ""
    full_name = name + ext

    # 1. Bundled/source tool directories
    for bundled in _candidate_tools_dirs():
        bundled_path = os.path.join(bundled, full_name)
        if os.path.isfile(bundled_path) and _ensure_executable(bundled_path):
            return bundled_path

    # 2. Check system PATH
    found = shutil.which(full_name)
    if found:
        return found

    # 3. Check common install locations
    common_paths = []
    if platform.system() == "Windows":
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        for base in (prog_files, local_appdata, "C:\\"):
            if not base:
                continue
            # Distributions ship as <base>\ffmpeg\bin\ffmpeg.exe as often as
            # <base>\ffmpeg\ffmpeg.exe, so probe both.
            common_paths.append(os.path.join(base, name, full_name))
            common_paths.append(os.path.join(base, name, "bin", full_name))
    elif platform.system() == "Darwin":
        common_paths = [
            f"/opt/homebrew/bin/{name}",   # Apple Silicon Homebrew
            f"/usr/local/bin/{name}",      # Intel Homebrew
            f"/opt/local/bin/{name}",      # MacPorts
        ]
    else:
        common_paths = [
            f"/usr/bin/{name}",
            f"/usr/local/bin/{name}",
            f"/snap/bin/{name}",
        ]

    for p in common_paths:
        if os.path.isfile(p) and _ensure_executable(p):
            return p

    return None


def _parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse a version string into a comparable tuple."""
    import re
    # Extract version numbers from strings like "ffmpeg version 6.1.2" or "v24.06"
    match = re.search(r"(\d+(?:\.\d+)+)", version_str)
    if match:
        parts = match.group(1).split(".")
        return tuple(int(p) for p in parts if p.isdigit())
    return ()


def _get_tool_version(path: str, name: str) -> Tuple[Optional[str], Tuple[int, ...]]:
    """Get version string and tuple for a tool."""
    try:
        if name in ("ffmpeg", "ffprobe"):
            result = subprocess.run(
                [path, "-version"],
                capture_output=True, text=True, timeout=10, env=_get_clean_env(), **_subprocess_kwargs()
            )
            if result.returncode != 0:
                return None, ()
            first_line = result.stdout.strip().split("\n")[0]
            version_tuple = _parse_version(first_line)
            return first_line, version_tuple

        elif name == "mediaconch":
            result = subprocess.run(
                [path, "--Version"],
                capture_output=True, text=True, timeout=10, env=_get_clean_env(), **_subprocess_kwargs()
            )
            if result.returncode != 0:
                return None, ()
            ver_str = result.stdout.strip()
            version_tuple = _parse_version(ver_str)
            return ver_str, version_tuple

        elif name == "mediainfo":
            result = subprocess.run(
                [path, "--Version"],
                capture_output=True, text=True, timeout=10, env=_get_clean_env(), **_subprocess_kwargs()
            )
            if result.returncode != 0:
                return None, ()
            ver_str = result.stdout.strip()
            version_tuple = _parse_version(ver_str)
            return ver_str, version_tuple

    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError) as e:
        logger.warning(f"Could not get version for {name}: {e}")
    return None, ()


def _detect_tool(name: str, required: bool = True) -> ToolInfo:
    """Detect a single tool and return its info."""
    info = ToolInfo(name=name, required=required)
    path = _find_executable(name)

    if not path:
        info.error = f"{name} not found on system PATH or in bundled tools."
        return info

    info.found = True
    info.path = path
    version_str, version_tuple = _get_tool_version(path, name)
    info.version = version_str
    info.version_tuple = version_tuple

    min_ver = MIN_VERSIONS.get(name, (0,))
    if version_str is None:
        # The probe could not run the binary at all (wrong architecture, missing
        # dylib, still not executable). Treating this as "usable" made every
        # subsequent scan fail with PermissionError reported as Internal Error.
        info.meets_minimum = False
        info.error = f"{name} was found at {path} but could not be executed."
    elif not version_tuple:
        # Nightly/custom builds ("ffmpeg version N-114523-g4a1b2c3") have no
        # dotted version but do run. Accept them and record why.
        info.meets_minimum = True
        info.error = f"{name} version could not be parsed from {version_str!r}; assuming it is usable."
    elif version_tuple >= min_ver:
        info.meets_minimum = True
    else:
        info.error = f"{name} version {version_str} is below minimum {'.'.join(str(v) for v in min_ver)}"
        info.meets_minimum = False

    return info


# ============================================================================
# PUBLIC API
# ============================================================================

_registry: Optional[ToolRegistry] = None
_registry_lock = threading.Lock()


def check_all_tools(force_refresh: bool = False) -> ToolRegistry:
    """Detect all external tools and return a ToolRegistry.
    Results are cached unless force_refresh is True.
    """
    global _registry
    if _registry is not None and not force_refresh:
        return _registry

    # Segment scans call get_ffprobe_path() from several worker threads at once;
    # without this every one of them would launch its own version probes.
    with _registry_lock:
        if _registry is not None and not force_refresh:
            return _registry

        registry = ToolRegistry()
        registry.ffmpeg = _detect_tool("ffmpeg", required=True)
        registry.ffprobe = _detect_tool("ffprobe", required=True)
        registry.mediaconch = _detect_tool("mediaconch", required=False)
        registry.mediainfo = _detect_tool("mediainfo", required=False)

        logger.info(f"Tool detection results:\n{registry.summary}")
        _registry = registry
        return registry


def get_tool_registry() -> ToolRegistry:
    """Get the cached tool registry, running detection if needed."""
    return check_all_tools()


def get_ffmpeg_path() -> Optional[str]:
    """Return the path to ffmpeg, or None if not found.

    A found-but-below-minimum binary is still returned: callers pass this
    straight into ``subprocess.run``, and handing them ``None`` raises
    ``TypeError`` deep inside a scan instead of reporting a version problem.
    """
    reg = get_tool_registry()
    return reg.ffmpeg.path if reg.ffmpeg.found else None


def get_ffprobe_path() -> Optional[str]:
    """Return the path to ffprobe, or None if not found."""
    reg = get_tool_registry()
    return reg.ffprobe.path if reg.ffprobe.found else None


def get_mediaconch_path() -> Optional[str]:
    """Return the path to mediaconch, or None if not available."""
    reg = get_tool_registry()
    return reg.mediaconch.path if reg.mediaconch.found else None


def get_mediainfo_path() -> Optional[str]:
    """Return the path to mediainfo, or None if not available."""
    reg = get_tool_registry()
    return reg.mediainfo.path if reg.mediainfo.found else None


# ============================================================================
# FFMPEG CAPABILITY DETECTION
# ============================================================================

_caps_cache: dict = {}
_caps_lock = threading.Lock()


def detect_ffmpeg_capabilities(ffmpeg_path: Optional[str] = None) -> dict:
    """Detect FFmpeg build capabilities relevant to FrameGuard.

    Returns dict with boolean flags:
      - has_libvmaf: bool
      - has_signalstats: bool
      - hwaccel_methods: list[str]  (e.g., ["cuda", "vaapi", "videotoolbox"])
        NOTE: this is the list the binary was COMPILED with (``-hwaccels``),
        not a list of devices present on this machine. Callers must probe
        before trusting a method — see core._get_optimal_hwaccel.
      - has_ebur128: bool

    Cached per ffmpeg path: this is called once per deep-scan segment, and each
    uncached call spawns two extra FFmpeg processes.
    """
    if ffmpeg_path is None:
        ffmpeg_path = get_ffmpeg_path()
    if not ffmpeg_path:
        return {
            "has_libvmaf": False,
            "has_signalstats": False,
            "hwaccel_methods": [],
            "has_ebur128": False,
        }

    cached = _caps_cache.get(ffmpeg_path)
    if cached is not None:
        return dict(cached)

    with _caps_lock:
        cached = _caps_cache.get(ffmpeg_path)
        if cached is not None:
            return dict(cached)

        caps = {
            "has_libvmaf": False,
            "has_signalstats": False,
            "hwaccel_methods": [],
            "has_ebur128": False,
        }

        try:
            # Check filters
            result = subprocess.run(
                [ffmpeg_path, "-filters"],
                capture_output=True, text=True,
                timeout=10, env=_get_clean_env(), **_subprocess_kwargs()
            )
            filter_text = result.stdout or ""
            caps["has_libvmaf"] = "libvmaf" in filter_text
            caps["has_signalstats"] = "signalstats" in filter_text
            caps["has_ebur128"] = "ebur128" in filter_text

            # Check hwaccel methods
            result = subprocess.run(
                [ffmpeg_path, "-hwaccels"],
                capture_output=True, text=True,
                timeout=10, env=_get_clean_env(), **_subprocess_kwargs()
            )
            lines = (result.stdout or "").strip().split("\n")
            # First line is "Hardware acceleration methods:", rest are method names
            if len(lines) > 1:
                caps["hwaccel_methods"] = [
                    line.strip() for line in lines[1:]
                    if line.strip() and line.strip() not in ("Hardware acceleration methods:",)
                ]

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Could not detect FFmpeg capabilities: {e}")
            return caps  # don't cache a failed probe

        _caps_cache[ffmpeg_path] = caps
        return dict(caps)


# ============================================================================
# XR DELIVERY POLICY (MediaConch XML)
# ============================================================================

XR_DELIVERY_POLICY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<policy type="and" name="FrameGuard XR Delivery Policy" license="MIT">
  <description>Policy for XR video deliverables. Validates container, codec, audio, and structure.</description>
  <rule name="Container is MP4 or MOV" value="Format" tracktype="General" occurrence="*" operator="=">
    <test tracktype="General" value="Format" operator="=" term="MPEG-4"/>
  </rule>
  <rule name="Video stream exists" value="Format" tracktype="Video" occurrence="*">
    <test tracktype="Video" value="Format" operator="exists"/>
  </rule>
  <rule name="Audio stream exists" value="Format" tracktype="Audio" occurrence="*">
    <test tracktype="Audio" value="Format" operator="exists"/>
  </rule>
  <rule name="Duration is positive" value="Duration" tracktype="General" occurrence="*">
    <test tracktype="General" value="Duration" operator=">" term="0"/>
  </rule>
  <rule name="Bit depth at least 8" value="BitDepth" tracktype="Video" occurrence="*">
    <test tracktype="Video" value="BitDepth" operator=">=" term="8"/>
  </rule>
</policy>"""


def get_xr_policy_path() -> str:
    """Write the XR delivery policy XML to a temp file and return its path."""
    policy_dir = os.path.join(os.path.expanduser("~"), ".frameguard", "policies")
    os.makedirs(policy_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(policy_dir, 0o700)
    except OSError:
        pass
    policy_path = os.path.join(policy_dir, "xr_delivery_policy.xml")
    with open(policy_path, "w", encoding="utf-8") as f:
        f.write(XR_DELIVERY_POLICY_XML)
    return policy_path

