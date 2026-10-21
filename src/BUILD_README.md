# Building FrameGuard

FrameGuard ships as a Windows installer (`FrameGuardInstaller.exe`) and a macOS
disk image (`FrameGuard.dmg`). Each is built on its own platform — there is no
cross-compilation.

| Platform | Script | Output |
|----------|--------|--------|
| Windows 10/11 x64 | `build_installer.bat` | `Output\FrameGuardInstaller.exe` |
| macOS 12+ | `./build_mac.sh` | `Output/FrameGuard.dmg` |

Before pushing any source or packaging change, run the fail-closed staged gate
from this directory:

```bash
python3 tools_dev/pre_push.py --profile full
```

A full run intentionally remains incomplete until matching local review metadata
is supplied on a fingerprint-checked resume. Fast runs can never establish local
readiness; the native arm64 `package` profile adds a clean build and final mounted /
copied DMG startup smoke. Local readiness does not authorize a release: GitHub's
exact-SHA Release Candidate workflow must prove the Windows installer and both
native macOS DMGs. See [`../docs/PRE_PUSH.md`](../docs/PRE_PUSH.md) for the
evidence format, profiles, resume command, and failure rules.

Both platform build scripts perform the same gated sequence and fail loudly at each step:

```
1. Check the host toolchain (Python 3.12)
2. Create a CLEAN platform-specific build virtualenv
3. Install requirements through constraints-3.12.txt
4. validate_version.py   - refuses to build if versions disagree
   write_build_stamp.py  - records when/where this build was made
5. Run tests - Windows runs the full suite; macOS runs packaging/macOS tests
6. Generate the app icon (macOS only: .icns)
7. PyInstaller           - and verify the output actually exists
8. Stage FFmpeg/FFprobe/MediaInfo  (macOS: + chmod +x, + codesign)
9. Run `--selftest` against the BUILT app, then package
```

> **Why a clean venv:** building from a dirty Python mixes incompatible
> runtime libraries into the bundle. On Windows the app crashes with
> `0xC0000005`; on macOS it fails to launch with no useful message.

Pass `--fast` to reuse the platform's existing build environment (much quicker
for iteration), or `--skip-tests` to bypass the test gate. Windows owns
`.buildenv`, `build`, and `dist` in this checkout. macOS builds under
`~/Library/Caches/FrameGuard/<checkout>/<architecture>/` because cloud-backed filesystems cannot preserve
Qt's framework symlinks; only the completed DMG is copied to `Output`. The
builders therefore coexist without deleting or reusing each other's state.
Neither fast mode nor skipping tests is appropriate for a build you intend to
distribute.

---

## Windows

### One-time setup
1. **Python 3.12** — CI pins 3.12.10 via `.python-version`; check
   "Add Python to PATH"
2. **Inno Setup 6.2+** — [jrsoftware.org](https://jrsoftware.org/isdl.php),
   install to the default location

### Build
```batch
build_installer.bat
```

### Distribute
Send **only** `FrameGuardInstaller.exe`. When teammates run it:

- No Python required
- FFmpeg, FFprobe and MediaInfo are included
- Desktop and Start Menu shortcuts are created
- `Documents\FrameGuard` is created for exported reports
- `.fg` preset files get the FrameGuard icon and open on double-click
- Proper uninstaller in Add/Remove Programs

---

## macOS

### One-time setup
1. **Xcode Command Line Tools** — `xcode-select --install`
2. **Python 3.12** — CI pins 3.12.10 via `.python-version`

### Build
```bash
chmod +x build_mac.sh     # first time only
./build_mac.sh

# Google Drive may drop the executable bit; this equivalent form always works:
bash build_mac.sh
```

Both builders install through `constraints-3.12.txt`, the dependency set
proven by Windows, Apple Silicon, and Intel CI. Update the constraints only
through a reviewed cross-platform build; do not bypass them for a release.

### Gatekeeper — read this before distributing

By default the build is **ad-hoc signed**, which is enough to run locally but
*not* enough to satisfy Gatekeeper on someone else's Mac. The build carries a
valid seal (required for Apple Silicon to run it at all) but no Apple-issued
identity and no notarization ticket. On first launch of a downloaded copy:

- **macOS 15 or later:** double-click → **Done** → System Settings →
  Privacy & Security → **Open Anyway** → authenticate → relaunch and confirm
  **Open**. (Apple removed the right-click → Open bypass in macOS 15.)
- **macOS 14 or earlier:** right-click **FrameGuard** → **Open** → **Open**.
- Any version, if the app is reported damaged or Open Anyway doesn't appear:
  `xattr -dr com.apple.quarantine /Applications/FrameGuard.app`

A Developer ID build without `--allow-unsigned` **fails the build** if signing
or verification fails, instead of shipping a half-signed app. Pass
`--allow-unsigned` only for internal test builds.

To remove the Gatekeeper prompt entirely, build with a Developer ID and
notarize. Set all three and the script notarizes automatically (otherwise it
prints the manual commands):

```bash
SIGN_IDENTITY="Developer ID Application: NAME (TEAMID)" \
APPLE_ID=you@example.com APPLE_TEAM_ID=TEAMID \
APPLE_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
./build_mac.sh
```

or manually after any Developer ID build:

```bash
SIGN_IDENTITY="Developer ID Application: NAME (TEAMID)" ./build_mac.sh

xcrun notarytool submit Output/FrameGuard.dmg \
      --apple-id APPLE_ID --team-id TEAMID --wait
xcrun stapler staple Output/FrameGuard.dmg
```

For GitHub-hosted release builds, configure all six secrets together:
`MACOS_SIGN_IDENTITY`, `MACOS_CERTIFICATE_P12_BASE64`,
`MACOS_CERTIFICATE_PASSWORD`, `APPLE_ID`, `APPLE_TEAM_ID`, and
`APPLE_APP_PASSWORD`. The workflow imports the P12 into a temporary keychain,
scopes credentials only to first-party signing steps, and deletes the keychain
after the build. A partial official-signing configuration fails before build.

Notarization requires a paid Apple Developer account (no free path exists).
Until then, include the version-appropriate Gatekeeper instruction above
whenever you share the DMG — never just "right-click → Open", which is wrong
for macOS 15+.

### Architecture

The build targets the architecture of the build Mac (`uname -m`) and exports
that value as `FRAMEGUARD_TARGET_ARCH` for PyInstaller and the release
manifest. The updater refuses cross-architecture deltas. Build once on each
native architecture; universal2 builds are intentionally unsupported because
Python and every binary wheel would also need to be universal2.

Each build leaves an arch-tagged copy (`Output/FrameGuard-arm64.dmg` or
`Output/FrameGuard-x86_64.dmg`) beside the canonical `FrameGuard.dmg`.
FFmpeg and FFprobe come from pinned Martin Riedl releases, separately for
arm64 and x86_64 (see Tool Cache below for how every tool is pinned). The
builder rejects a tool whose Mach-O architecture does not match the target;
Rosetta is not required.

### Known macOS differences

| Behaviour | Windows | macOS |
|-----------|---------|-------|
| Window chrome | Custom title bar, Aero Snap | Native titlebar and traffic lights |
| Launch at login | Registry `Run` key | `~/Library/LaunchAgents` plist |
| `.fg` association | Registry | `CFBundleDocumentTypes` + Apple Event |
| Update flow | Silent Inno Setup install | Opens the DMG for a manual drag |
| Logs | `%LOCALAPPDATA%\FrameGuard\Logs` | `~/Library/Logs/FrameGuard` |
| Default watch folder | `~/Videos` | `~/Movies` |

macOS will prompt for Documents/Desktop access (TCC) the first time FrameGuard
reads a watched folder in one of those locations. That is expected.

---

## Before You Ship (either platform)

Every push runs the full constrained suite on Windows, Apple Silicon, and
Intel macOS. Release builders keep an additional platform packaging gate and
self-test the packaged application before producing an artifact.
Still verify by hand:

1. Install from the produced artifact
2. Launch from the Start Menu / Applications
3. Scan one known-good and one known-bad file
4. Export a **multi-file** PDF report (single-file reports take a different
   code path)
5. Drag a `.fg` preset onto the window, confirm Settings updates
6. Double-click a `.fg` in Explorer/Finder, confirm it imports

---

## Bumping the Version

`validate_version.py` gates the build on these agreeing, and
`tests/test_packaging.py` checks the same set:

| File | Field |
|------|-------|
| `frameguard/__init__.py` | `__version__` |
| `frameguard.spec` | `APP_VERSION`, the Windows `filevers`/`prodvers` tuples and the `FileVersion`/`ProductVersion` strings |
| `installer.iss` | `AppVersion` (`VersionInfoVersion`, the registry `Version` and the wizard labels derive from it) |
| `version.json` | `version`, and a new first `changelog` entry starting `vX.Y.Z - <title>`; only that entry becomes the release notes and the in-app "what's new" text |
| `installer_info.txt` | The title line `FrameGuard Desktop vX.Y.Z` and the `WHAT'S NEW IN vX.Y.Z` heading |

---

## Tool Cache

FFmpeg, FFprobe and MediaInfo are cached per-platform:

```
tools_cache/
├── windows/          ffmpeg.exe, ffprobe.exe, mediainfo.exe
├── darwin-arm64/     ffmpeg, ffprobe, mediainfo
└── darwin-x86_64/    ffmpeg, ffprobe, mediainfo
```

Every tool is pinned in `download_tools.py` to a versioned, immutable URL and
the SHA-256 of that exact archive. A download whose hash differs is refused,
and there is no "latest" fallback, so a build bundles exactly the pinned bytes
or stops. A cached tool is reused only if it was extracted from the currently
pinned archive; otherwise it is downloaded again. To bump a tool, change its
version, URL and hash together, after checking the new archive against the
publisher's own checksum where one exists.

Downloaded tools are reused across builds. Useful commands:

```bash
python download_tools.py --verify-only          # check the cache
python download_tools.py --force                # re-download
python download_tools.py --platform darwin --arch arm64   # Apple Silicon
python download_tools.py --platform darwin --arch x86_64  # Intel
```

The cache is gitignored — these are large binaries, not source.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Python not found" | Install Python 3.12 and ensure it's on PATH |
| "Version numbers disagree" | Reconcile the four files in the table above |
| "Tests failed" | Fix them. Do not ship with `--skip-tests` |
| "failed its self-test" | Run the printed `--selftest` command to see why |
| App crashes on launch with 0xC0000005 | Windows build used a dirty Python — delete `.buildenv` and rebuild |
| macOS build environment is invalid | Delete `~/Library/Caches/FrameGuard/<checkout>/venv` and rebuild; never reuse the Windows `.buildenv` |
| macOS: "FrameGuard is damaged" | Gatekeeper. See the quarantine step above |
| macOS: `bad interpreter` | `build_mac.sh` has CRLF endings — `dos2unix build_mac.sh` |
| macOS: `Permission denied` | `chmod +x build_mac.sh` |
| "Inno Setup not found" | Install from jrsoftware.org to the default path |
| FFmpeg download fails or reports a SHA-256 mismatch | The build stops; check that the pinned URL still serves the pinned archive (`download_tools.py`), then retry. Never replace a hash without verifying the new file |
| Antivirus flags the exe | Add this folder to exclusions during the build |

---

## File Overview

| File | Purpose |
|------|---------|
| `build_installer.bat` | Windows build — run this |
| `build_mac.sh` | macOS build using isolated state under `~/Library/Caches/FrameGuard/<checkout>/` |
| `frameguard.spec` | PyInstaller config for both platforms (`BUNDLE()` = the .app) |
| `download_tools.py` | Fetches and stages FFmpeg/MediaInfo per platform |
| `assets/make_icns.py` | Packs the macOS `.icns` from `branding/icon_mac_*.png` (no imaging library needed) |
| `tools_dev/make_brand_assets.py` | Regenerates `branding/` and `assets/frameguard.ico` from the vector mark in `frameguard/brand.py`; `--check` verifies they are current |
| `write_build_stamp.py` | Records build metadata for the About dialog |
| `validate_version.py` | Pre-build version consistency check |
| `installer.iss` | Inno Setup script (Windows shortcuts, `.fg` association) |
| `version.json` | Published update manifest for the in-app updater (`installer_windows` / `installer_darwin` keys) |
| `diagnostic.bat` | Collects environment info when debugging a user's machine (Windows) |
| `diagnostic.sh` | Same for macOS (bundle layout, signature, `--selftest`) |
| `entitlements.plist` | Hardened-runtime entitlements for Developer ID / notarized Mac builds (ad-hoc builds do not use it) |
| `tests/` | pytest suite, including macOS build validation |
