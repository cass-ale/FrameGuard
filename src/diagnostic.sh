#!/usr/bin/env bash
# ============================================================================
# FrameGuard Desktop -- macOS Diagnostic Launcher
#
# If FrameGuard.app doesn't open normally, run this from a Terminal. It checks
# the bundle layout the same way diagnostic.bat does on Windows, then runs the
# built-in self-test with console output visible.
#
# Usage:
#   ./diagnostic.sh [path/to/FrameGuard.app]
# ============================================================================
set -u

APP="${1:-/Applications/FrameGuard.app}"
EXE="$APP/Contents/MacOS/FrameGuard"

echo
echo "  FrameGuard Desktop - Diagnostic Mode (macOS)"
echo "  ============================================"
echo
echo "  App bundle: $APP"

ok()  { echo "  $1: YES"; }
bad() { echo "  $1: NO - PROBLEM!"; }

[[ -d "$APP" ]] && ok "Bundle exists" || { bad "Bundle exists"; exit 1; }
[[ -f "$APP/Contents/Info.plist" ]] && ok "Info.plist" || bad "Info.plist"
[[ -x "$EXE" ]] && ok "Executable" || bad "Executable"
[[ -d "$APP/Contents/Resources/tools" ]] && ok "tools/" || bad "tools/ (FFmpeg missing?)"
[[ -x "$APP/Contents/Resources/tools/ffmpeg" ]] && ok "ffmpeg binary" || bad "ffmpeg binary (re-run build_mac.sh)"
[[ -x "$APP/Contents/Resources/tools/ffprobe" ]] && ok "ffprobe binary" || bad "ffprobe binary (re-run build_mac.sh)"
[[ -f "$APP/Contents/Resources/tools/mediainfo" ]] && ok "mediainfo (optional)" || echo "  mediainfo (optional): not bundled"
[[ -d "$APP/Contents/Frameworks" ]] && ok "Frameworks/ (Qt)" || bad "Frameworks/ (Qt missing?)"
if compgen -G "$APP/Contents/Frameworks/*.framework" > /dev/null 2>&1; then
    ok "Qt frameworks"
else
    echo "  Qt frameworks: could not confirm (check Frameworks/)"
fi
[[ -f "$APP/Contents/PlugIns/platforms/libqcocoa.dylib" ]] \
    && ok "libqcocoa (platform plugin)" \
    || bad "libqcocoa (platform plugin - app cannot start without it)"
[[ -d "$APP/Contents/Resources/presets" || -d "$APP/Contents/MacOS/presets" ]] \
    && ok "presets/" || bad "presets/ (minor)"

echo
echo "  Architecture:"
echo "  Host: $(uname -m)"
if command -v file >/dev/null 2>&1; then
    echo "  FrameGuard: $(file -b "$EXE" | cut -c1-80)"
    if [[ -x "$APP/Contents/Resources/tools/ffmpeg" ]]; then
        echo "  ffmpeg:     $(file -b "$APP/Contents/Resources/tools/ffmpeg" | cut -c1-80)"
    fi
fi
if [[ "$(uname -m)" == "arm64" ]]; then
    if /usr/bin/pgrep -q oahd 2>/dev/null || [[ -d "/Library/Apple/usr/share/rosetta" ]]; then
        ok "Rosetta 2 (for Intel-only tools)"
    else
        echo "  Rosetta 2: not detected - Intel-only ffmpeg needs it:"
        echo "    softwareupdate --install-rosetta --agree-to-license"
    fi
fi

echo
echo "  Tool versions:"
if [[ -x "$APP/Contents/Resources/tools/ffmpeg" ]]; then
    "$APP/Contents/Resources/tools/ffmpeg" -version 2>/dev/null | head -1 | sed 's/^/  /' \
        || echo "  ffmpeg: FAILED TO RUN"
fi

echo
echo "  Quarantine / Gatekeeper:"
xattr -l "$APP" 2>/dev/null | sed 's/^/  /' || echo "  (no extended attributes)"
spctl -a -vv "$APP" 2>&1 | sed 's/^/  /' || true

echo
echo "  Code signature:"
codesign --verify --deep --strict "$APP" 2>&1 \
    && echo "  Signature: valid" \
    || echo "  Signature: INVALID (reinstall from the DMG, or: codesign --force --sign - \$APP)"

echo
echo "  Attempting the built-in self-test (errors appear below)..."
echo "  --------------------------------------------------------"
echo
"$EXE" --selftest
