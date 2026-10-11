# FrameGuard Desktop

**Video QA for XR Deliverables — Windows and macOS**

FrameGuard validates video files against configurable delivery specifications, running
automated checks across metadata, codec compliance, audio integrity, and deep signal
analysis. All processing happens locally — no data leaves your machine.

## What it does

Point FrameGuard at a folder of deliverables and it checks every file against your
delivery spec — resolution, frame rate, codecs, bitrate, audio, and deep signal
analysis (black/freeze frames, silence, decode integrity) — then hands you a
pass/fail verdict per file and a PDF report for the client or the archive.

Built for XR production pipelines: batch processing, folder-watch auto-scan, and
PDF handoff reports. Everything runs on your machine; nothing is uploaded.

## Quick start

### Installed application

Windows 10 or 11 (64-bit), or macOS 12 or later. Everything else (FFmpeg, FFprobe,
MediaInfo) is bundled — no dependencies for end users.

### Run from source

```bash
cd FGv0.2.2
pip install -r requirements.txt
python main.py
```

| Dependency | Version | Purpose |
|-----------|---------|---------|
| Python | 3.10+ | Runtime |
| PySide6 | 6.6+ | GUI framework |
| fpdf2 | 2.7+ | PDF report generation |
| psutil | 5.9+ | Subprocess cleanup |
| FFmpeg + FFprobe | 4.0+ | Video analysis (required) |
| MediaInfo | any | Enriched metadata (optional) |

FFmpeg is discovered from, in order: the bundled `tools/` directory (inside the
`.app` on macOS), `tools_cache/<platform>/` in the source tree, your `PATH`,
then common install locations including Homebrew and MacPorts.

### Build the installer

```batch
cd FGv0.2.2
build_installer.bat          :: Windows -> Output\FrameGuardInstaller.exe
```

```bash
cd FGv0.2.2
./build_mac.sh               # macOS -> Output/FrameGuard.dmg
```

See [BUILD_README.md](FGv0.2.2/BUILD_README.md). Windows runs the full test suite;
macOS runs its platform and packaging tests. Both self-test the packaged app
before producing an artifact.

The macOS build is ad-hoc signed rather than notarized, so on first launch
right-click FrameGuard and choose **Open**. See
[BUILD_README.md](FGv0.2.2/BUILD_README.md#gatekeeper--read-this-before-distributing).

### Run the tests

```bash
cd FGv0.2.2
pip install -r requirements-dev.txt
pytest
```

---

## Features

### Scan engine

- Automated checks across container, codec, resolution, frame rate, CFR, bitrate,
  audio codec/channels, A/V sync, and frame count
- **Deep scan** — black frame, freeze frame, and silence detection with absolute
  timestamps
- **Decode integrity** — detects dropped frames and decode errors during a full decode
- **Keyframe-aware segment splitting** for faster parallel deep scans on long files
- **Hardware-accelerated decode** (CUDA / VAAPI / D3D11VA / VideoToolbox, auto-detected)
- **MediaInfo enrichment** when MediaInfo is available
- **9 built-in delivery presets** (4K 24/30/60, 1080p 24/30/60, 720p30, 5.7K VR, 8K)
- **Confidence-weighted results** — borderline failures are reported as advisory rather
  than hard failures
- **Plain-language summaries** — one line per file, free of technical jargon

### Desktop GUI

- Drag-and-drop files, folders, and `.fg` presets — anywhere in the window
- Results stream in one file at a time as the scan progresses
- **Failures first, severity-weighted** — failed files sort to the top with
  critical corruption above minor framerate issues, tinted red with a headline
  banner; toggle back to scan order at any time
- Verdict banner auto-dismisses clean results and clears on new files,
  navigation, or errors — no stale messages
- **Checks matrix** — every file against every check in one grid, so a systemic
  problem ("all 40 files failing Bitrate") is obvious at a glance
- **Issues only** filter to hide files that passed cleanly
- Right-click any result to open it in VLC, reveal it in Explorer, or copy its path
- **PDF handoff reports** with a batch summary and per-file detail
- **15 color themes** with live switching and persistence — the
  theme also recolors the app icon, title bar, and system tray icon
- Scan settings persist between sessions
- Watch folder with automatic scanning and tray notifications
- Standard window behaviour: Aero Snap, drag-to-maximize, Win+Arrow tiling
- Keyboard shortcuts: `Ctrl+1..5` panels, `Ctrl+O` browse, `F5` scan, `Ctrl+E` export

### Packaging

- Single-file installer with FFmpeg, FFprobe, and MediaInfo bundled — no dependencies
  for end users
- `.fg` preset files are registered with Windows: they show the FrameGuard icon and
  import on double-click

---

## Presets

Presets use the `.fg` extension (JSON internally). They capture a complete delivery
spec — resolution, frame rate and tolerance, minimum bitrate, accepted codecs and
containers, audio requirements, and deep-scan tuning.

| Action | How |
|--------|-----|
| Use a built-in | Pick it from the dropdown on the Scan page |
| Enter your own values | Scan page → **Edit values…** (jumps to the delivery controls and selects the Custom profile) |
| Save your setup as a `.fg` | Settings → Scan Presets → **Export Current**, then give it a name |
| Load one | **Import Preset** in Settings, drag the `.fg` onto the window, or double-click it |
| Share with the team | Send the `.fg` file — it is a few KB of plain JSON |

### Creating a `.fg` file

There is no separate file format to learn: a preset is just the values already on
the Settings page, written to disk.

1. Scan page → **Edit values…** (or Settings → Delivery Profile).
2. Set resolution, frame rate, tolerance and minimum bitrate. Expand
   **Show Advanced Settings** for audio and deep-scan parameters.
3. Settings → Scan Presets → **Export Current** → name it → Save.

The result is a small JSON file you can email, drop in a shared folder, or commit.
Anyone who drags it onto FrameGuard gets your exact spec. Hand-authoring one also
works — see `PRESET_CONFIG_KEYS` in `FGv0.2.2/frameguard/presets.py` for the accepted keys,
and `FGv0.2.2/presets/` for worked examples.

User-imported presets are stored in `~/.frameguard/presets/` and persist across
reinstalls.

---

## Project layout

```
FrameGuard/
├── README.md                 # This file
├── LICENSE                   # MIT
├── FGv0.2.2/
│   ├── main.py               # Entry point (handles .fg paths from argv)
│   ├── requirements.txt      # Runtime dependencies
│   ├── requirements-dev.txt  # + pytest, pyflakes, pyinstaller
│   ├── pytest.ini
│   ├── README.md             # Pointer to the root README
│   ├── BUILD_README.md       # Build instructions
│   ├── frameguard/
│   │   ├── __init__.py       # Single source of truth for __version__
│   │   ├── core.py           # Scan engine, checks, deep scan
│   │   ├── worker.py         # Background scan thread
│   │   ├── gui.py            # PySide6 UI
│   │   ├── report.py         # PDF report generation
│   │   ├── presets.py        # .fg preset schema, import/export
│   │   ├── theme.py          # 15 color themes
│   │   ├── tools.py          # FFmpeg/MediaInfo discovery
│   │   ├── watchfolder.py    # Watch folder monitor
│   │   ├── manifest.py       # File manifest for differential updates
│   │   ├── deltaupdate.py    # Differential update staging and swap
│   │   └── updater.py        # Auto-update
│   ├── presets/              # Bundled .json presets (shipped in the installer)
│   ├── assets/               # Icons and logos
│   ├── tests/                # pytest suite
│   ├── tools_dev/            # Developer-only utilities (GUI audit)
│   ├── build_installer.bat   # Windows build
│   ├── build_mac.sh          # macOS build
│   ├── frameguard.spec       # PyInstaller config
│   ├── installer.iss         # Inno Setup config
│   └── validate_version.py   # Build-time version consistency check
```

Build state is platform-owned. Windows uses `.buildenv/`, `build/`, and
`dist/` in the checkout. macOS uses `~/Library/Caches/FrameGuard/<checkout>/`
because Google Drive cannot preserve Qt's framework symlinks; only the finished
DMG is copied to `Output/`. Building one platform therefore does not delete or
overwrite the other's artifacts.

---

## Configuration

Everything is configurable from the Settings panel and persists between sessions.
Defaults live in `DEFAULT_CONFIG` in `frameguard/core.py`; the Settings panel emits
those exact key names, and `tests/test_config_and_presets.py` enforces that they stay
in sync.

| Group | Controls |
|-------|----------|
| Delivery | Target resolution, frame rate, fps tolerance, minimum bitrate |
| Audio | Audio required, silence duration threshold |
| Deep scan | Hardware acceleration, segmented scanning, segment count |
| Strict mode | Warnings are treated as failures |

---

## Check reference

| Category | Check | Severity | Notes |
|----------|-------|----------|-------|
| Container | Format | FAIL | Extension must be in accepted containers |
| Video | Codec | FAIL | Must be in accepted codec list |
| Video | Resolution | FAIL/WARN | FAIL when short of target, WARN within 1% tolerance |
| Video | Frame Rate | FAIL/WARN | FAIL below target, WARN above target (conformance-friendly) |
| Video | CFR | FAIL/WARN | Constant frame rate (configurable) |
| Video | Bitrate | FAIL/WARN | WARN 10-25% under target, FAIL beyond |
| Video | Frame Count | WARN | Frame count vs duration × fps |
| Audio | Codec | FAIL | Must be in accepted audio codec list |
| Audio | Channels | INFO | Reports channel count and layout |
| Audio | Silence | FAIL/WARN | Sustained silence detection |
| Duration | A/V Sync | FAIL | Audio vs video duration mismatch |
| Deep scan | Decode Integrity | WARN | Decode errors during full decode |
| Deep scan | Black Frames | FAIL/WARN | Fade-filtered, absolute timestamps |
| Deep scan | Freeze Frames | FAIL/WARN | Sustained frozen frames |
| Metadata | MediaInfo | INFO | Enriched metadata when available |

Only **essential** checks determine pass/fail. Advisory checks (decode integrity, frame
count, black frames, freeze frames) produce `PASS_WITH_WARNINGS` rather than a failure.

---

## Privacy

All video processing is local. Nothing is uploaded, transmitted, or shared.

---

## License

MIT License. See [LICENSE](LICENSE).
