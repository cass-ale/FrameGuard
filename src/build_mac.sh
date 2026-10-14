#!/usr/bin/env bash
# ============================================================================
# FrameGuard Desktop -- macOS Build Script
#
# Produces:  Output/FrameGuard.dmg
#
# The unpacked .app and build environment live under ~/Library/Caches rather
# than this Google Drive checkout. DriveFS cannot preserve the symlink-heavy Qt
# framework layout, which turns valid PySide6 frameworks into empty folders.
#
# WHY A CLEAN VENV:
#   Same reason as the Windows build. Building from a dirty Python mixes
#   incompatible dylibs into the bundle and the app crashes on launch with no
#   useful message. A clean venv guarantees the bundled Qt matches the wheels.
#
# Prerequisites on the BUILD Mac:
#   1. macOS 12+ with Xcode Command Line Tools  (xcode-select --install)
#   2. Python 3.10+  (python.org or Homebrew; the system python3 works too)
#
# Usage:
#   ./build_mac.sh              full clean build
#   ./build_mac.sh --fast       reuse the local macOS build cache (much quicker)
#   ./build_mac.sh --skip-tests skip the pytest gate (not recommended)
#   SIGN_IDENTITY="Developer ID Application: ..." ./build_mac.sh
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

APP_NAME="FrameGuard"
PROJECT_KEY="$(basename "$PWD")"
LOCAL_BUILD_ROOT="${FRAMEGUARD_MAC_BUILD_ROOT:-${HOME}/Library/Caches/FrameGuard/${PROJECT_KEY}}"
case "$LOCAL_BUILD_ROOT" in
    ""|"/"|"$HOME")
        echo "ERROR: Refusing unsafe FRAMEGUARD_MAC_BUILD_ROOT: ${LOCAL_BUILD_ROOT:-<empty>}" >&2
        exit 1
        ;;
esac
VENV="${LOCAL_BUILD_ROOT}/venv"
WORK_DIR="${LOCAL_BUILD_ROOT}/build"
DIST_DIR="${LOCAL_BUILD_ROOT}/dist"
BUNDLE="${DIST_DIR}/${APP_NAME}.app"
LOCAL_DMG_PATH="${LOCAL_BUILD_ROOT}/FrameGuard.dmg"
DMG_DIR="Output"
# Canonical artifact name both platforms' tooling agrees on (updater,
# version.json, docs). The build ARCH is recorded in the manifest and the
# differential updater refuses cross-arch updates, so one name is safe.
DMG_PATH="${DMG_DIR}/FrameGuard.dmg"
# ...but an Apple Silicon build will not run on an Intel Mac and vice versa,
# so the VOLUME name calls out the arch inside the DMG.
ARCH="$(uname -m)"
TARGET_ARCH="${FRAMEGUARD_TARGET_ARCH:-$ARCH}"
VOLNAME="FrameGuard Desktop (${TARGET_ARCH})"
TOTAL_STEPS=9

FAST=0
SKIP_TESTS=0
ALLOW_UNSIGNED=0
for arg in "$@"; do
    case "$arg" in
        --fast) FAST=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        # Permits shipping an ad-hoc/unsigned Developer ID build without
        # aborting at the signature gate (internal testing only).
        --allow-unsigned) ALLOW_UNSIGNED=1 ;;
        *) echo "Unknown option: $arg"; exit 2 ;;
    esac
done

step() { echo; echo "[$1/${TOTAL_STEPS}] $2"; }
die()  { echo; echo "ERROR: $*" >&2; exit 1; }

echo "===================================================="
echo "  FrameGuard Desktop - macOS Build"
echo "===================================================="

# --- 1. Host checks -------------------------------------------------------
step 1 "Checking the build environment..."
[[ "$(uname)" == "Darwin" ]] || die "This script must run on macOS. Use build_installer.bat on Windows."
command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.10+ from python.org"

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'PYEOF' || die "Python 3.10 or newer is required."
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYEOF
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
echo "      Python ${PY_VER}  ($(python3 -c 'import platform;print(platform.machine())'))"
echo "      macOS  $(sw_vers -productVersion)"
echo "      Target arch: ${TARGET_ARCH}"
echo "      Local build root: ${LOCAL_BUILD_ROOT}"
echo "      DMG:   ${DMG_PATH}"

# --- 2. Clean virtualenv --------------------------------------------------
step 2 "Preparing the build environment..."
mkdir -p "$LOCAL_BUILD_ROOT"
if [[ $FAST -eq 1 && -x "${VENV}/bin/python" ]]; then
    echo "      Reusing existing $VENV (--fast)"
else
    rm -rf "$VENV"
    python3 -m venv "$VENV" || die "Could not create the virtual environment."
    echo "      Created $VENV"
fi
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
PY="${VENV}/bin/python"

# Some Python distributions produce a venv whose bundled pip is incomplete.
if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "      Repairing the virtual environment's pip..."
    "$PY" -m ensurepip --upgrade >/dev/null 2>&1 \
        || die "Could not bootstrap pip inside ${VENV}. Delete it and retry."
fi

# --- 3. Dependencies ------------------------------------------------------
step 3 "Installing build dependencies..."
"$PY" -m pip install --quiet --upgrade pip setuptools wheel
"$PY" -m pip install --quiet -r requirements.txt "pyinstaller>=6.0,<7.0" "Pillow>=10.0"
[[ $SKIP_TESTS -eq 1 ]] || "$PY" -m pip install --quiet pytest pytest-timeout
echo "      PySide6    $("$PY" -c 'import PySide6;print(PySide6.__version__)')"
echo "      PyInstaller $("$PY" -m PyInstaller --version)"

# --- 4. Version + build stamp --------------------------------------------
step 4 "Validating version consistency..."
"$PY" validate_version.py || die "Version numbers disagree. Fix them and re-run."

# Keep platform-generated metadata out of the source tree so a Mac build cannot
# replace the stamp associated with an existing Windows build.
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
BUILD_STAMP="${WORK_DIR}/_build_stamp.json"
"$PY" write_build_stamp.py --output "$BUILD_STAMP" \
    || die "Could not write the build stamp."
export FRAMEGUARD_BUILD_STAMP_PATH="$BUILD_STAMP"

# Fetch the bundled tools NOW, before the expensive stages: a dead tool
# source must fail this build in the first minutes, not after PyInstaller
# (the evermeet.cx 410 outage produced exactly that failure shape: a full
# build, then death at the final smoke test). This populates the per-arch
# tool cache; step 8 re-runs the same command, which then stages from cache.
echo "      Pre-fetching analysis tools (${TARGET_ARCH})..."
"$PY" download_tools.py --platform darwin --arch "$TARGET_ARCH" \
    || die "Could not fetch the required analysis tools. Do not ship this build."

# --- 5. Icon --------------------------------------------------------------
# The icon is generated BEFORE the test gate on purpose: the gate asserts
# the .icns exists and is valid, and the file is a gitignored build product,
# so a clean checkout (CI) has none until this step runs.
step 5 "Generating the app icon..."
"$PY" assets/make_icns.py || die "Could not generate assets/frameguard.icns"

# --- 6. Tests -------------------------------------------------------------
step 6 "Running the test suite..."
if [[ $SKIP_TESTS -eq 1 ]]; then
    echo "      SKIPPED (--skip-tests)"
else
    QT_QPA_PLATFORM=offscreen "$PY" -m pytest \
        tests/test_macos.py tests/test_packaging.py \
        || die "macOS packaging tests failed. Fix them before shipping a build."
fi

# --- 7. PyInstaller -------------------------------------------------------
step 7 "Building ${APP_NAME}.app..."
rm -rf "$DIST_DIR"
# Some managed Python framework builds expose their shared library at
# sys.base_prefix, while their sysconfig metadata names a nested framework
# path. Limit this discovery override to PyInstaller so the packaged app
# cannot depend on the build host.
PY_BASE="$("$PY" -c 'import sys; print(sys.base_prefix)')"
PYINSTALLER_DYLD_PATH="${PY_BASE}:${PY_BASE}/lib"
if [[ -n "${DYLD_LIBRARY_PATH:-}" ]]; then
    PYINSTALLER_DYLD_PATH="${PYINSTALLER_DYLD_PATH}:${DYLD_LIBRARY_PATH}"
fi
DYLD_LIBRARY_PATH="$PYINSTALLER_DYLD_PATH" \
    "$PY" -m PyInstaller frameguard.spec --noconfirm --clean \
        --workpath "$WORK_DIR" --distpath "$DIST_DIR" \
    || die "PyInstaller build failed. See the output above."

[[ -d "$BUNDLE" ]] || die "$BUNDLE was not produced. Does frameguard.spec still contain the BUNDLE() step?"
[[ -f "${BUNDLE}/Contents/Info.plist" ]] || die "The bundle has no Info.plist."
echo "      Built $BUNDLE"

# --- 8. Tools, permissions, signing --------------------------------------
step 8 "Staging analysis tools and signing..."
TOOLS_DEST="${BUNDLE}/Contents/Resources/tools"
# Required tools are not a warning. A bundle without ffmpeg cannot scan
# video, and the step-9 smoke test would only fail later with a less clear
# message; fail here, where the cause is.
"$PY" download_tools.py --platform darwin --arch "$TARGET_ARCH" --dest "$TOOLS_DEST" \
    || die "Required analysis tools could not be staged. Do not ship this build."

# PyInstaller and unzip both lose the executable bit; without it every scan
# fails with PermissionError reported as "Internal Error".
if [[ -d "$TOOLS_DEST" ]]; then
    chmod +x "$TOOLS_DEST"/* 2>/dev/null || true
    echo "      Tools: $(ls "$TOOLS_DEST" | tr '\n' ' ')"
fi

# Strip the quarantine flag the downloaded tools inherit.
xattr -cr "$BUNDLE" 2>/dev/null || true

# Sign AFTER staging so the signature covers the bundled binaries. An ad-hoc
# signature (-) is enough for internal distribution; a real Developer ID plus
# notarization is required to avoid the Gatekeeper prompt entirely.
#
# --timestamp (omitted for ad-hoc, forced for Developer ID) embeds an Apple
# timestamp so the app still launches when the certificate later expires.
# A Developer ID signature WITHOUT a timestamp dies with the certificate.
#
# Inside-out signing (binaries first, bundle last): Apple deprecated
# `codesign --deep`, which signs in an undefined order and can leave nested
# code (ffmpeg, Qt frameworks) improperly sealed. Sign the tools, then the
# frameworks, then the bundle itself.
SIGN_IDENTITY="${SIGN_IDENTITY:--}"
ENT_ARGS=()
if [[ -f "entitlements.plist" ]]; then
    ENT_ARGS=(--entitlements "entitlements.plist")
else
    echo "      WARNING: entitlements.plist missing; hardened-runtime sign may fail."
fi
if [[ "$SIGN_IDENTITY" != "-" ]]; then
    SIGN_FLAGS=(--timestamp --options runtime)
else
    SIGN_FLAGS=(--timestamp=none)
fi
_sign_one() { # _sign_one <path>
    # ${#ENT_ARGS[@]} (length) is safe under `set -u` even when empty;
    # expanding an empty "${ENT_ARGS[@]}" directly is not on bash 3.2
    # (macOS default), where it aborts with "unbound variable".
    if ((${#ENT_ARGS[@]})); then
        codesign --force --sign "$SIGN_IDENTITY" "${SIGN_FLAGS[@]}" \
            "${ENT_ARGS[@]}" "$1" 2>/dev/null
    else
        codesign --force --sign "$SIGN_IDENTITY" "${SIGN_FLAGS[@]}" \
            "$1" 2>/dev/null
    fi
}
SIGN_OK=1
if [[ -d "$TOOLS_DEST" ]]; then
    for tool in "$TOOLS_DEST"/*; do
        [[ -f "$tool" ]] || continue
        _sign_one "$tool" || { echo "      WARNING: could not sign $tool"; SIGN_OK=0; }
    done
fi
# Qt frameworks inside the bundle, deepest first.
if [[ -d "${BUNDLE}/Contents/Frameworks" ]]; then
    while IFS= read -r -d '' fw; do
        _sign_one "$fw" || { echo "      WARNING: could not sign $fw"; SIGN_OK=0; }
    done < <(find "${BUNDLE}/Contents/Frameworks" \
        \( -name "*.dylib" -o -name "*.so" \) -print0 2>/dev/null)
fi
if _sign_one "$BUNDLE"; then
    if [[ "$SIGN_IDENTITY" == "-" ]]; then
        echo "      Signed ad-hoc (internal distribution)"
    else
        echo "      Signed with: $SIGN_IDENTITY"
    fi
else
    echo "      WARNING: codesign failed; the app may not launch on other Macs."
    SIGN_OK=0
fi
if codesign --verify --deep --strict "$BUNDLE" 2>/dev/null; then
    echo "      Signature verified"
else
    echo "      WARNING: signature verification failed"
    SIGN_OK=0
fi
if [[ $SIGN_OK -eq 0 && $ALLOW_UNSIGNED -eq 0 && "$SIGN_IDENTITY" != "-" ]]; then
    die "Code signing failed and --allow-unsigned was not passed. Do not ship this build."
fi

# Arch check: the tool sources are per-architecture now, so a staged ffmpeg
# that does not contain the target architecture is a defect in this build,
# not a caveat — an x86_64-only ffmpeg inside an arm64 app would silently
# depend on Rosetta 2 being installed on the user's machine.
if [[ -f "${TOOLS_DEST}/ffmpeg" ]]; then
    TOOL_ARCH="$(file -b "${TOOLS_DEST}/ffmpeg" 2>/dev/null || true)"
    echo "      ffmpeg: ${TOOL_ARCH}"
    case "$TARGET_ARCH" in
        arm64)
            [[ "$TOOL_ARCH" == *"arm64"* ]] \
                || die "Staged ffmpeg does not contain an arm64 binary (${TOOL_ARCH}). Refusing to bundle the wrong architecture."
            ;;
        x86_64)
            [[ "$TOOL_ARCH" == *"x86_64"* ]] \
                || die "Staged ffmpeg does not contain an x86_64 binary (${TOOL_ARCH}). Refusing to bundle the wrong architecture."
            ;;
    esac
fi

# --- 9. Smoke test + DMG --------------------------------------------------
step 9 "Smoke testing and packaging..."
# --selftest imports every module, builds the main window, resolves FFmpeg and
# exits with a real status code. Checking "is the process still alive" would
# pass even when the app is stuck behind a modal error dialog.
if "${BUNDLE}/Contents/MacOS/${APP_NAME}" --selftest; then
    echo "      Smoke test passed"
else
    die "The built app failed its self-test. Do not ship this build.
      Reproduce with:  ${BUNDLE}/Contents/MacOS/${APP_NAME} --selftest"
fi

mkdir -p "$DMG_DIR" "$LOCAL_BUILD_ROOT"
# Build the image on the local filesystem. Google Drive cannot preserve the
# symlink-heavy Qt framework layout while an .app is unpacked, but a completed
# DMG preserves that layout as a single file in the shared folder.
rm -f "$LOCAL_DMG_PATH" "$DMG_PATH"
DMG_STAGE="$(mktemp -d)"
trap 'rm -rf "$DMG_STAGE"' EXIT
cp -R "$BUNDLE" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create -volname "${VOLNAME}" \
    -srcfolder "$DMG_STAGE" -ov -format UDZO -quiet "$LOCAL_DMG_PATH" \
    || die "Could not create the disk image."
hdiutil verify "$LOCAL_DMG_PATH" >/dev/null \
    || die "The disk image failed verification."

# Sign the DMG itself so tampering in transit is detectable (previously the
# DMG was never signed, only the .app inside it).
if [[ "$SIGN_IDENTITY" != "-" ]]; then
    codesign --force --sign "$SIGN_IDENTITY" "$LOCAL_DMG_PATH" 2>/dev/null \
        && echo "      DMG signed" \
        || echo "      WARNING: could not sign the DMG (app inside is still signed)"
fi

DMG_SIZE=$(du -h "$LOCAL_DMG_PATH" | cut -f1)

# Opt-in notarization: needs Apple credentials, so it never runs by default
# and never fails an offline/internal build. Set all three to notarize:
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD (app-specific password)
if [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
    echo "      Notarizing with Apple (this takes a few minutes)..."
    if xcrun notarytool submit "$LOCAL_DMG_PATH" --apple-id "$APPLE_ID" \
            --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" \
            --wait 2>&1 | tail -3; then
        xcrun stapler staple "$LOCAL_DMG_PATH" \
            && echo "      Notarized + stapled" \
            || echo "      WARNING: stapler failed; Gatekeeper will check online on first launch"
    else
        echo "      WARNING: notarization failed; the DMG still works but"
        echo "      Gatekeeper will prompt on first launch."
    fi
else
    echo "      Notarization skipped (set APPLE_ID/APPLE_TEAM_ID/APPLE_APP_PASSWORD to enable)"
fi

# Copy only the completed disk image into the shared checkout. Both copies are
# made after optional notarization so the canonical and arch-specific files are
# byte-for-byte identical.
cp -f "$LOCAL_DMG_PATH" "$DMG_PATH"
cp -f "$LOCAL_DMG_PATH" "${DMG_DIR}/FrameGuard-${TARGET_ARCH}.dmg"
echo "      Arch copy: ${DMG_DIR}/FrameGuard-${TARGET_ARCH}.dmg"

# Publish a differential-update release
# downloading only the files that changed rather than the whole DMG.
# macOS releases go to Output/release-macos (NOT Output/release): the Windows
# build publishes to Output/release-windows, and sharing one folder lets one
# platform's publish silently overwrite the other's (FrameGuard.exe vs the
# .app tree, .pyd vs .so, ffmpeg.exe vs ffmpeg).
echo "      Publishing differential-update release..."
"$PY" publish_release.py --platform darwin --build-dir "$BUNDLE" \
    --compare-to "${DMG_DIR}/release-macos" \
    || echo "      WARNING: could not publish the release manifest."

cat <<EOF

====================================================
  BUILD SUCCESSFUL
====================================================

  App:  $BUNDLE
  DMG:  $DMG_PATH  (${DMG_SIZE})

  To distribute:
    Send FrameGuard.dmg to the team. They open it and
    drag FrameGuard into Applications.

EOF

if [[ "$SIGN_IDENTITY" == "-" ]]; then
cat <<EOF
  IMPORTANT - Gatekeeper (ad-hoc signed build):
    macOS blocks apps that are not notarized by Apple.
    On first launch of a downloaded copy:

      * macOS 15+: double-click -> Done -> System Settings ->
        Privacy & Security -> Open Anyway -> authenticate -> relaunch.
        (Right-click -> Open no longer works on macOS 15+.)
      * macOS 14 and earlier: Right-click FrameGuard -> Open -> Open.
      * If reported damaged:  xattr -dr com.apple.quarantine /Applications/FrameGuard.app

    DMG: ${DMG_PATH}  (volume: ${VOLNAME})
    Arch copy: ${DMG_DIR}/FrameGuard-${TARGET_ARCH}.dmg
    Install: open the DMG, drag FrameGuard into Applications.

    To remove this step entirely, build with a Developer ID
    and notarize (or set APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD
    and the script notarizes automatically):

      SIGN_IDENTITY="Developer ID Application: NAME (TEAMID)" ./build_mac.sh
      xcrun notarytool submit "${DMG_PATH}" \\
            --apple-id APPLE_ID --team-id TEAMID --wait
      xcrun stapler staple "${DMG_PATH}"

EOF
fi

echo "===================================================="
deactivate
