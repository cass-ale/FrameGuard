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
#   2. Python 3.12  (3.12.10 is pinned for release builds)
#
# Usage:
#   ./build_mac.sh              full clean build
#   ./build_mac.sh --fast       reuse the local macOS build cache (much quicker)
#   ./build_mac.sh --skip-tests skip the pytest gate (not recommended)
#   ./build_mac.sh --skip-delta skip optional external-channel payload
#   SIGN_IDENTITY
#   SIGN_IDENTITY="Developer ID Application: ..." ./build_mac.sh
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"

APP_NAME="FrameGuard"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
PROJECT_PATH_ID="$(printf '%s' "$REPO_ROOT" | cksum | awk '{print $1}')"
PROJECT_KEY="$(basename "$REPO_ROOT")-${PROJECT_PATH_ID}"
ARCH="$(uname -m)"
TARGET_ARCH="${FRAMEGUARD_TARGET_ARCH:-$ARCH}"
case "$TARGET_ARCH" in
    arm64|x86_64) ;;
    *)
        echo "ERROR: Unsupported FRAMEGUARD_TARGET_ARCH: ${TARGET_ARCH}" >&2
        exit 1
        ;;
esac
if [[ "$TARGET_ARCH" != "$ARCH" ]]; then
    echo "ERROR: Cross-architecture macOS builds are unsupported (${ARCH} host, ${TARGET_ARCH} target)." >&2
    exit 1
fi
export FRAMEGUARD_TARGET_ARCH="$TARGET_ARCH"

# The default cache is deterministic for both the checkout and architecture.
# An explicit override remains the exact root so existing CI paths stay valid.
if [[ -n "${FRAMEGUARD_MAC_BUILD_ROOT:-}" ]]; then
    LOCAL_BUILD_ROOT="$FRAMEGUARD_MAC_BUILD_ROOT"
else
    LOCAL_BUILD_ROOT="${HOME}/Library/Caches/FrameGuard/${PROJECT_KEY}/${TARGET_ARCH}"
fi
case "$LOCAL_BUILD_ROOT" in
    ""|"/"|"$HOME")
        echo "ERROR: Refusing unsafe FRAMEGUARD_MAC_BUILD_ROOT: ${LOCAL_BUILD_ROOT:-<empty>}" >&2
        exit 1
        ;;
    /*) ;;
    *) LOCAL_BUILD_ROOT="${SCRIPT_DIR}/${LOCAL_BUILD_ROOT}" ;;
esac
VENV="${LOCAL_BUILD_ROOT}/venv"
WORK_DIR="${LOCAL_BUILD_ROOT}/build"
DIST_DIR="${LOCAL_BUILD_ROOT}/dist"
LOG_DIR="${LOCAL_BUILD_ROOT}/logs"
CONSTRAINTS="${SCRIPT_DIR}/constraints-3.12.txt"
BUNDLE="${DIST_DIR}/${APP_NAME}.app"
LOCAL_DMG_PATH="${LOCAL_BUILD_ROOT}/FrameGuard.dmg"
DMG_DIR="Output"
# Canonical artifact name both platforms' tooling agrees on (updater,
# version.json, docs). The build ARCH is recorded in the manifest and the
# differential updater refuses cross-arch updates, so one name is safe.
DMG_PATH="${DMG_DIR}/FrameGuard.dmg"
# ...but an Apple Silicon build will not run on an Intel Mac and vice versa,
# so the VOLUME name calls out the arch inside the DMG.
VOLNAME="FrameGuard Desktop (${TARGET_ARCH})"
TOTAL_STEPS=9

FAST=0
SKIP_TESTS=0
SKIP_DELTA=0
ALLOW_UNSIGNED=0
for arg in "$@"; do
    case "$arg" in
        --fast) FAST=1 ;;
        --skip-tests) SKIP_TESTS=1 ;;
        --skip-delta) SKIP_DELTA=1 ;;
        # Retained
        # Retained for compatibility with internal ad-hoc builds. It never
        # downgrades failures when a Developer ID identity is configured.
        --allow-unsigned) ALLOW_UNSIGNED=1 ;;
        *) echo "Unknown option: $arg"; exit 2 ;;
    esac
done

step() { echo; echo "[$1/${TOTAL_STEPS}] $2"; }
die()  { echo; echo "ERROR: $*" >&2; exit 1; }

create_dmg() { # create_dmg <staging-directory> <output-image>
    local source_dir="$1"
    local image="$2"
    local diagnostic="${LOG_DIR}/hdiutil-create.log"
    local attempt attempt_log delay

    mkdir -p "$LOG_DIR"
    : > "$diagnostic"
    for attempt in 1 2 3; do
        attempt_log="${diagnostic}.attempt-${attempt}"
        rm -f "$image"
        if hdiutil create -volname "$VOLNAME" -srcfolder "$source_dir" \
                -ov -format UDZO "$image" >"$attempt_log" 2>&1; then
            cat "$attempt_log" >> "$diagnostic"
            rm -f "$attempt_log"
            echo "      Disk image created (attempt ${attempt})"
            return 0
        fi
        {
            echo "--- hdiutil create attempt ${attempt} failed ---"
            cat "$attempt_log"
        } >> "$diagnostic"
        rm -f "$attempt_log" "$image"
        if [[ $attempt -lt 3 ]]; then
            delay=$((attempt * 3))
            echo "      hdiutil create failed; retrying in ${delay}s..." >&2
            sleep "$delay"
            sync
        fi
    done

    echo "      hdiutil create failed after 3 attempts; diagnostics:" >&2
    cat "$diagnostic" >&2
    hdiutil info >&2 || true
    return 1
}

verify_dmg() { # verify_dmg <path> <diagnostic-label>
    local image="$1"
    local label="$2"
    local diagnostic="${LOG_DIR}/hdiutil-verify-${label}.log"
    local attempt attempt_log delay

    [[ -s "$image" ]] || {
        echo "      ERROR: disk image is missing or empty: $image" >&2
        return 1
    }
    mkdir -p "$LOG_DIR"
    : > "$diagnostic"
    sync
    attempt=1
    while [[ $attempt -le 3 ]]; do
        attempt_log="${diagnostic}.attempt-${attempt}"
        if hdiutil verify "$image" >"$attempt_log" 2>&1; then
            cat "$attempt_log" >> "$diagnostic"
            rm -f "$attempt_log"
            echo "      Disk image verified (${label}, attempt ${attempt})"
            return 0
        fi
        {
            echo "--- hdiutil verify attempt ${attempt}/3 ---"
            cat "$attempt_log"
        } >> "$diagnostic"
        echo "      hdiutil verify failed (${label}, attempt ${attempt}/3):" >&2
        cat "$attempt_log" >&2
        rm -f "$attempt_log"
        if [[ $attempt -lt 3 ]]; then
            delay=$attempt
            echo "      Retrying disk image verification in ${delay}s..." >&2
            sleep "$delay"
            sync
        fi
        attempt=$((attempt + 1))
    done
    {
        echo "--- hdiutil imageinfo after verify exhaustion ---"
        hdiutil imageinfo "$image" 2>&1 || true
    } | tee -a "$diagnostic" >&2
    echo "      Full hdiutil diagnostics: $diagnostic" >&2
    return 1
}

print_notary_log() { # Redact credentials before diagnostics reach build logs.
    local log_path="$1"
    "$PY" - "$log_path" <<'PYEOF'
import os
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
for name in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"):
    value = os.environ.get(name)
    if value:
        text = text.replace(value, "<redacted>")
pathlib.Path(sys.argv[1]).write_text(text, encoding="utf-8")
print(text, end="" if text.endswith("\n") else "\n")
PYEOF
}

SIGN_IDENTITY="${SIGN_IDENTITY:--}"
NOTARIZE=0
if [[ -n "${APPLE_ID:-}" || -n "${APPLE_TEAM_ID:-}" || -n "${APPLE_APP_PASSWORD:-}" ]]; then
    [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]] \
        || die "Notarization is partially configured. Set APPLE_ID, APPLE_TEAM_ID, and APPLE_APP_PASSWORD together."
    [[ "$SIGN_IDENTITY" != "-" ]] \
        || die "Notarization credentials require a configured Developer ID SIGN_IDENTITY."
    NOTARIZE=1
fi
if [[ $ALLOW_UNSIGNED -eq 1 && "$SIGN_IDENTITY" != "-" ]]; then
    echo "      NOTE: --allow-unsigned cannot bypass configured Developer ID signing failures."
fi

echo "===================================================="
echo "  FrameGuard Desktop - macOS Build"
echo "===================================================="

# --- 1. Host checks -------------------------------------------------------
step 1 "Checking the build environment..."
[[ "$(uname)" == "Darwin" ]] || die "This script must run on macOS. Use build_installer.bat on Windows."
for required_tool in python3 hdiutil codesign xcrun file xattr; do
    command -v "$required_tool" >/dev/null 2>&1 \
        || die "Required build tool not found: ${required_tool}. Install the Xcode Command Line Tools and Python 3.12."
done

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'PYEOF' || die "Python 3.12 is required for reproducible release builds."
import sys
sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)
PYEOF
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
echo "      Python ${PY_VER}  ($(python3 -c 'import platform;print(platform.machine())'))"
echo "      macOS  $(sw_vers -productVersion)"
echo "      Target arch: ${TARGET_ARCH}"
echo "      Local build root: ${LOCAL_BUILD_ROOT}"
echo "      DMG:   ${DMG_PATH}"

# --- 2. Clean virtualenv --------------------------------------------------
step 2 "Preparing the build environment..."
mkdir -p "$LOCAL_BUILD_ROOT" "$LOG_DIR"
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
[[ -f "$CONSTRAINTS" ]] || die "Missing shared dependency constraints: $CONSTRAINTS"
"$PY" -m pip install --quiet --upgrade -c "$CONSTRAINTS" pip setuptools wheel
"$PY" -m pip install --quiet -c "$CONSTRAINTS" -r requirements.txt \
    "pyinstaller>=6.0,<7.0" "Pillow>=10.0"
[[ $SKIP_TESTS -eq 1 ]] \
    || "$PY" -m pip install --quiet -c "$CONSTRAINTS" pytest pytest-timeout
"$PY" -m pip check \
    || die "The build environment has incompatible packages. Re-run without --fast."
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
PYINSTALLER_LOG="${LOG_DIR}/pyinstaller.log"
PYINSTALLER_WARN_SOURCE="${WORK_DIR}/frameguard/warn-frameguard.txt"
PYINSTALLER_WARN_LOG="${LOG_DIR}/pyinstaller-warnings.txt"
PYINSTALLER_MISSING_LOG="${LOG_DIR}/pyinstaller-missing-frameguard-modules.txt"
rm -f "$PYINSTALLER_LOG" "$PYINSTALLER_WARN_LOG" "$PYINSTALLER_MISSING_LOG"
set +e
DYLD_LIBRARY_PATH="$PYINSTALLER_DYLD_PATH" \
    "$PY" -m PyInstaller frameguard.spec --noconfirm --clean \
        --workpath "$WORK_DIR" --distpath "$DIST_DIR" 2>&1 \
    | tee "$PYINSTALLER_LOG"
PYINSTALLER_PIPESTATUS=("${PIPESTATUS[@]}")
set -e
PYINSTALLER_STATUS="${PYINSTALLER_PIPESTATUS[0]}"
TEE_STATUS="${PYINSTALLER_PIPESTATUS[1]}"
if [[ -f "$PYINSTALLER_WARN_SOURCE" ]]; then
    cp -f "$PYINSTALLER_WARN_SOURCE" "$PYINSTALLER_WARN_LOG"
fi
[[ "$TEE_STATUS" -eq 0 ]] \
    || die "Could not preserve the PyInstaller log at ${PYINSTALLER_LOG}."
[[ "$PYINSTALLER_STATUS" -eq 0 ]] \
    || die "PyInstaller build failed. Full log: ${PYINSTALLER_LOG}"

PYINSTALLER_DIAGNOSTICS=("$PYINSTALLER_LOG")
if [[ -f "$PYINSTALLER_WARN_LOG" ]]; then
    PYINSTALLER_DIAGNOSTICS+=("$PYINSTALLER_WARN_LOG")
fi
if grep -E -i \
        "missing module named ['\"]?frameguard([.]|['\"]|[[:space:]])|Hidden import ['\"]frameguard([.]|['\"]).*not found" \
        "${PYINSTALLER_DIAGNOSTICS[@]}" > "$PYINSTALLER_MISSING_LOG"; then
    cat "$PYINSTALLER_MISSING_LOG" >&2
    die "PyInstaller reported missing internal FrameGuard modules. Diagnostics: ${PYINSTALLER_MISSING_LOG}"
fi
rm -f "$PYINSTALLER_MISSING_LOG"
echo "      PyInstaller log: $PYINSTALLER_LOG"
[[ ! -f "$PYINSTALLER_WARN_LOG" ]] \
    || echo "      PyInstaller warnings: $PYINSTALLER_WARN_LOG"

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
ENT_ARGS=()
if [[ -f "entitlements.plist" ]]; then
    ENT_ARGS=(--entitlements "entitlements.plist")
elif [[ "$SIGN_IDENTITY" != "-" ]]; then
    die "entitlements.plist is required for configured Developer ID signing."
else
    echo "      WARNING: entitlements.plist missing; continuing with ad-hoc signing."
fi
if [[ "$SIGN_IDENTITY" != "-" ]]; then
    SIGN_FLAGS=(--timestamp --options runtime)
else
    SIGN_FLAGS=(--timestamp=none)
fi
_sign_one() { # _sign_one <nested-code-path>
    codesign --force --sign "$SIGN_IDENTITY" "${SIGN_FLAGS[@]}" "$1"
}
_sign_outer_app() {
    # Entitlements belong on the main application, not every nested library.
    # ${#ENT_ARGS[@]} is safe under macOS's Bash 3.2 when the array is empty.
    if ((${#ENT_ARGS[@]})); then
        codesign --force --sign "$SIGN_IDENTITY" "${SIGN_FLAGS[@]}" \
            "${ENT_ARGS[@]}" "$BUNDLE"
    else
        _sign_one "$BUNDLE"
    fi
}
_sign_failure() { # _sign_failure <path>
    if [[ "$SIGN_IDENTITY" != "-" ]]; then
        die "Configured Developer ID signing failed for $1. Do not ship this build."
    fi
    if [[ $ALLOW_UNSIGNED -ne 1 ]]; then
        die "Ad-hoc signing failed for $1. Use --allow-unsigned only for an internal diagnostic build."
    fi
    echo "      WARNING: ad-hoc signing failed for $1 (--allow-unsigned)"
}

# Sign every nested Mach-O file, including extensionless framework executables
# and Qt/PyInstaller helpers. Filename suffixes alone miss code that Apple then
# rejects during notarization.
while IFS= read -r -d '' candidate; do
    if [[ "$(file -b "$candidate" 2>/dev/null || true)" == *"Mach-O"* ]]; then
        _sign_one "$candidate" || _sign_failure "$candidate"
    fi
done < <(find "${BUNDLE}/Contents" -type f -print0)

# Seal nested code bundles after their contents, deepest first, then seal the
# outer app with its entitlements. `find -depth` guarantees inside-out order.
while IFS= read -r -d '' nested_bundle; do
    _sign_one "$nested_bundle" || _sign_failure "$nested_bundle"
done < <(find "${BUNDLE}/Contents" -depth -type d \
    \( -name "*.framework" -o -name "*.app" -o -name "*.appex" \
       -o -name "*.bundle" -o -name "*.xpc" -o -name "*.plugin" \) -print0)

if _sign_outer_app; then
    if [[ "$SIGN_IDENTITY" == "-" ]]; then
        echo "      Signed ad-hoc (internal distribution)"
    else
        echo "      Signed with: $SIGN_IDENTITY"
    fi
else
    _sign_failure "$BUNDLE"
fi
if codesign --verify --deep --strict --verbose=2 "$BUNDLE"; then
    echo "      App signature verified"
elif [[ "$SIGN_IDENTITY" != "-" ]]; then
    die "Configured Developer ID app signature validation failed. Do not ship this build."
elif [[ $ALLOW_UNSIGNED -ne 1 ]]; then
    die "Ad-hoc app signature validation failed. Use --allow-unsigned only for an internal diagnostic build."
else
    echo "      WARNING: ad-hoc app signature verification failed (--allow-unsigned)"
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

create_dmg "$DMG_STAGE" "$LOCAL_DMG_PATH" \
    || die "Could not create the disk image after bounded retries."
verify_dmg "$LOCAL_DMG_PATH" "created" \
    || die "The newly created disk image failed verification."

# Sign the DMG itself so tampering in transit is detectable (previously the
# DMG was never signed, only the .app inside it). A configured Developer ID is
# a release contract: signing and immediate validation must both succeed.
if [[ "$SIGN_IDENTITY" != "-" ]]; then
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$LOCAL_DMG_PATH" \
        || die "Configured Developer ID DMG signing failed. Do not ship this build."
    codesign --verify --verbose=2 "$LOCAL_DMG_PATH" \
        || die "Configured Developer ID DMG signature validation failed."
    echo "      DMG signed and signature verified"
fi

# Opt-in notarization: no credentials keeps ad-hoc/internal builds offline.
# Once any credential is supplied, the complete fail-closed contract was
# validated before the build began and every Apple operation is mandatory.
if [[ $NOTARIZE -eq 1 ]]; then
    NOTARY_LOG="${LOG_DIR}/notarytool.log"
    rm -f "$NOTARY_LOG"
    : > "$NOTARY_LOG"
    chmod 600 "$NOTARY_LOG"
    echo "      Notarizing with Apple (this takes a few minutes)..."
    if xcrun notarytool submit "$LOCAL_DMG_PATH" --apple-id "$APPLE_ID" \
            --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" \
            --wait >"$NOTARY_LOG" 2>&1; then
        print_notary_log "$NOTARY_LOG"
    else
        print_notary_log "$NOTARY_LOG" >&2
        die "Configured notarization failed. Full redacted diagnostics: ${NOTARY_LOG}"
    fi
    xcrun stapler staple "$LOCAL_DMG_PATH" \
        || die "Configured notarization succeeded but stapling failed."
    xcrun stapler validate "$LOCAL_DMG_PATH" \
        || die "The stapled notarization ticket failed validation."
    echo "      Notarized, stapled, and validated"
else
    echo "      Notarization skipped (set APPLE_ID/APPLE_TEAM_ID/APPLE_APP_PASSWORD to enable)"
fi

# codesign and stapler mutate the image. Verify the final bytes, then repeat
# the applicable signature/ticket validations before publishing either copy.
verify_dmg "$LOCAL_DMG_PATH" "final" \
    || die "The disk image failed final post-signing/notarization verification."
if [[ "$SIGN_IDENTITY" != "-" ]]; then
    codesign --verify --verbose=2 "$LOCAL_DMG_PATH" \
        || die "Final DMG signature validation failed after image mutation."
fi
if [[ $NOTARIZE -eq 1 ]]; then
    xcrun stapler validate "$LOCAL_DMG_PATH" \
        || die "Final stapled ticket validation failed after image mutation."
fi
DMG_SIZE=$(du -h "$LOCAL_DMG_PATH" | cut -f1)

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
if [[ $SKIP_DELTA -eq 0 ]]; then
    echo "      Preparing optional differential-update payload..."
    "$PY" publish_release.py --platform darwin --build-dir "$BUNDLE" \
        --compare-to "${DMG_DIR}/release-macos" \
        || echo "      WARNING: could not prepare the optional delta payload."
else
    echo "      Optional differential payload skipped (--skip-delta)."
fi

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
