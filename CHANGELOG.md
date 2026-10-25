# Changelog

All notable changes to FrameGuard are documented in this file.

Generated from the changelog text in `src/version.json` at main head
`fd53dc4a36ee434bc8c16cd00be3cccde3560cb5` (verified via commit-pinned raw URL,
cross-checked with `gh api`). Version entries and text are reproduced as recorded
there — nothing backdated or invented. Dates are given only where a published
GitHub Release proves them; tagged-but-unpublished versions are marked as such.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9.2] — 2026-10-25 — Review fixes: safer flash screening, real-footage interlace, exact picture levels, cleaner audio verdicts

### Fixed
- Flash screening now judges each region of the screen, down to fine patterns: flashing that alternates between halves, checkerboards, turning patterns or red/blue lights is flagged (ITU-R BT.1702-3 combined area). It remains a screening, not a certificate; HDR (PQ/HLG) video shows "Not verified" instead of skipping.
- Interlaced (woven) pictures and 3:2 pulldown in files declared progressive are found in real camera footage, not just test graphics. Progressive test patterns and rolling credits are no longer called interlaced.
- Signal Levels applies the EBU R 103 filter to every pixel at native resolution, within each field for interlaced video. Violations on UHD and 8K masters are no longer averaged away.
- File Integrity fails a file missing more than 2 frames or 0.1 s, whatever its length (it allowed 2% before: 144 s of a two-hour master). Decoder errors that FFmpeg conceals are counted.
- Black Frames ignores a small static graphic such as burnt-in timecode or a channel logo when the rest of the picture is true black, and no longer calls a dark grade, low-contrast scene or credit roll black.
- Freeze Frames finds freezes under film grain and mid-programme, does not flag slow pans or dips through black, lets title cards and still shots hold up to the Title Card Hold allowance (3 s), and reports a picture that freezes to the end.
- Quiet room tone no longer fails as silent: whole-track silence is judged at -70 dBFS (Telestream Qualify's default). A clip that is mostly silence fails at any length.
- Ambisonic true peak is measured on every channel; head-locked stereo is checked for peaks and reported as part of the mix.
- On Apple Silicon, the fallback loudness meter places channels by their layout; layouts it cannot place show "Not verified".
- Out-of-phase sections end where they end; a whole track in reverse polarity is caught.
- Mains hum is judged on a steady tone within 1% of 50/60 Hz (EN 50160); bass notes and organ tones no longer read as hum. The LFE channel is recognised by position in every layout.
- Any loudness, true peak, phase or picture measurement FFmpeg did not produce shows "Not verified", never PASS.
- A file whose content is not what its extension says (a renamed or junk-prefixed MPEG-TS, FLV or PS) fails Container Format; an unrecognised file is "Not verified".
- Top-bottom 360 pictures tagged mono are found at any disparity; 180-degree files are checked for square eyes.
- An unread or oversized moov makes the container, XR, spatial audio and timecode rows "Not verified" instead of passing. The MP4/MOV parser is hardened against hostile files.
- A/V Sync Offset reads AAC encoder priming from the audio track's edit list or the identified encoder. When neither tells it, the offset is "Not verified" unless no priming could explain it; Strict Mode does not escalate that.
- A remuxed file whose timestamps are rounded to its container's timebase is no longer called variable frame rate; isolated whole-frame gaps are reported as dropped frames, and frequent ones as variable frame rate.
- The Quest 360 presets accept HEVC and AV1 Main only (Meta Horizon media requirements). 8K 360 is labelled as the Horizon OS streaming target; a new 360 Mono 7200 preset matches Media Studio's maximum.

### Improved
- New checks: Low-Level Audio notes stretches 20 dB under the programme between -70 and -40 dBFS without changing the verdict; colour tags that contradict each other or are only partly set are flagged.
- New settings: Phase Threshold, Dual Mono Allowed, Dialogue-Gated Loudness, Low-Level Audio, Title Card Hold and AAC Encoder Priming, each in Settings, presets and the PDF.
- Presets from 0.9.1 load leniently: out-of-range values are clamped and contradictions resolved towards the stricter value, and defaults that changed are migrated; every adjustment is shown on import and in the PDF.
- Measured on three known-answer test sets against 0.9.1, as mean detection per defect type with default / broadcast settings: original set 64.3% → 65.0% / 71.7% → 72.4%; held-out set 67.7% → 73.0% / 76.3% → 81.5%; a sealed set never used for tuning 56.9% → 66.8% / 65.6% → 75.5%. No clean file fails on any of them.

### Changed
- FFmpeg 8.1.2 or newer is required for frame-level checks (tested floor; the bundled build is 9.0.2). Older builds show "Not verified" with a startup notice. FRAMEGUARD_TOOLS_DIR selects another FFmpeg build.
- Deep scans with Signal Levels on take up to about 3× as long as 0.9.1 at 1080p, because R 103 is now computed exactly. Turning Signal Levels off returns to 0.9.1's speed.
- Correction to the 0.9.1 notes: their "17 to 31 of 45" benchmark baseline was 0.9.0. Measured against 0.8.3, 0.9.1 raised detected defect types from 12 to 31.

## [0.9.1] — 2026-10-21 — Signal QC: audio, picture and conformance checks from the benchmark

### Fixed
- Windows: the window now uses the native Windows frame, tinted to the theme (title bar in the sidebar colour, themed caption text and border, Windows 11 rounded corners and Snap Layouts). The hand-drawn title bar clipped its own close button and the right and bottom edge of every page.
- Windows: resizing no longer drags the page sideways. The content sat under the invisible resize border, and the page gutter and sidebar width stepped at 1100 and 1400 px mid-drag; the sidebar now keeps one width and the gutter eases by at most 8 px.
- Windows: the pinned taskbar button, Start menu and desktop shortcuts follow the theme's icon colour, not just the running window. (A button already pinned may keep the old image until Windows next redraws the taskbar.)
- Windows 11: the interface uses Segoe UI Variable, the system typeface.
- 29.97 fps no longer passes a 30 fps spec silently (and 23.976/59.94 likewise): an NTSC rate against its integer target now warns (fails in Strict Mode) unless "Treat NTSC Rates as Integer" is on. The FPS tolerance now only absorbs measurement jitter.
- Audio shorter than the video by more than 2 frames warns, and by more than 0.5 s fails (Telestream FC-108 practice); audio up to 2 s long passes.
- A rotated (portrait) file no longer fails both Resolution and Aspect Ratio for the same reason; a flat 180° or mirrored picture now warns.
- Black frames: a dark scene is no longer called black (stricter black level, 98% of the picture, 0.3 s), and short isolated black flashes are now caught.
- Freeze frames are found in long-GOP H.264/HEVC as well as ProRes; the old detector restarted at every I-frame and missed them.
- On Apple Silicon, loudness of tracks with three or more channels was wrong (the bundled FFmpeg meter misreads multichannel audio). FrameGuard now self-tests the meter and uses FFmpeg's independent meter when needed; 4-channel ambisonic tracks are measured on the W channel.
- The 8K preset is labelled as flat 8K UHD; 8K 360 (7680x3840) has its own preset.

### Improved
- Loudness targets: EBU R 128 (±0.5 LU file tolerance), ATSC A/85, Netflix (programme loudness), Streaming −14 and −16, or custom. True peak and clipping get their own rows. Enforcement stays "report only" by default, because camera originals rarely meet broadcast targets.
- New audio checks: clipping, dropouts (line-up ident tones such as GLITS are recognised), phase, dual mono, DC offset and mains hum; per-channel silence at −70 dBFS.
- New picture checks: EBU R 103 signal levels and RGB gamut (warn by default, fail for broadcast), BT.1702 flash screening (a warning, not a photosensitivity certificate), and interlace combing and field-order detection that ignores rolling credits.
- New conformance checks: real container type vs extension, MP4/MOV structure (truncation, duplicate indexes, fast start), edit lists, A/V start offset (DPP ±5 ms warning; fails beyond 40 ms early or 60 ms late), timecode continuity, codec profile/level against resolution and frame rate, and track layout.
- XR: Spherical Video V1 vs V2 and conflicting tags, per-eye 2:1 geometry, top-bottom vs side-by-side layout, no rotation on 360 files, and spatial audio (SA3D) matching the ambisonic channel count. New Quest 360 presets: 8K 360, 5760x5760 and 3840x3840 top-bottom.
- Colour and HDR: untagged colour, a BT.601 matrix on HD, PQ on 8-bit, and PQ without mastering-display metadata or with MaxCLL 0 are flagged.
- Settings has controls for every new check and rule, and the PDF lists the exact settings each verdict was made against.
- Measured on the benchmark's 158-file known-answer corpus: defect types detected rose from 17 to 31 of 45 (macro recall 30% to 64%), and defective files given a clean PASS fell from 79 to 38, with no clean file failed.

### Changed
- Default black-frame level 0.10 → 0.02 and duration 0.5 → 0.3 s; freeze duration 2 → 1 s. freeze_detect_noise is retired (old presets still load).
- Deep scans read the coded picture without auto-rotation (cheaper on rotated 4K, needed for interlace detection).
- Loudness checks set to "Warn" or "Fail outside target" now judge against a delivery profile, EBU R 128 by default (−23 LUFS ±0.5 LU, true peak −1 dBTP, 48 kHz audio required), instead of 0.9.0's −23 LUFS ±2 LU; choose ATSC A/85, Netflix, Streaming −14/−16 or Custom in Settings. Clipping is graded the same way, and "Report only" (the default) still judges nothing.
- A/V Sync Offset flags audio and video that start at different times in the container: a warning beyond ±5 ms (DPP file tolerance), a failure beyond EBU R37's 40 ms sound-early / 60 ms sound-late budget. This catches MP4/MOV files with B-frame video written without an edit list, whose first picture really is presented after the sound; AAC whose encoder priming is not signalled is judged at the most favourable priming, so such files usually warn. FFmpeg-written MPEG-TS and camera originals are unaffected.
- On Apple Silicon, loudness of tracks with 7 or more channels is shown as not verified when a loudness target is enforced (the bundled meter misreads them and the fallback meter handles up to six channels); true peak is still measured. Intel Macs and Windows measure them normally.
- Deep scans write their filter graphs and detector events to the scan's private temporary folder (removed afterwards), so files with many audio tracks scan on Windows and very long programmes keep every event.

## [0.9.0] — 2026-10-21 — New identity, stricter verdicts and a deeper app

### Fixed
- A truncated or corrupt file now FAILS. Decode errors used to be advisory, so an incomplete copy could only ever be a warning; a new essential File Integrity check fails any file that decodes less than 98% of what its header declares or that has decoder errors.
- Matroska (MKV) files have their audio/video durations compared again; a probe option had been discarding the tags the check needs.
- Frame-level analysis is never skipped silently. Files with no duration in the header are still scanned, the 8 GB size limit that skipped most ProRes masters is gone, and anything that genuinely cannot be checked shows as "Not verified" so the file cannot read as a clean pass.
- Damaged files get the same verdict on every Mac: the scan now always decodes in software, because hardware decoders hid errors and invented freezes.
- Every audio track is checked, not just the first, including a silent second track or a dead channel.
- Variable frame rate is measured from the actual frame timestamps instead of two header fields.
- A file name containing "1080" or "vr" no longer switches your chosen delivery profile; FrameGuard only suggests a matching profile.
- Frame rate and resolution must match the spec (4K no longer passes a 1080p spec, 25p no longer passes a 24p spec), with optional allowances for larger frames or integer frame-rate multiples.
- Presets with out-of-range or unknown settings are rejected with a message naming every problem, instead of being silently loosened.
- Scan progress updates and report-saved messages now run on the interface thread, removing a source of random crashes.
- A slow folder search can no longer replace a newer selection, or refill the queue after Clear.
- Unattended watch-folder errors are logged and notified instead of stacking dialogs on a hidden window.
- Settings, window size and the sidebar state are saved as you change them, not only on a clean quit.
- macOS: the Dock icon now follows the theme at full resolution (it showed the old icon, upscaled, whatever the theme).
- Windows: in-app updates run silently instead of stopping at a confirmation box after FrameGuard has closed.

### Improved
- New Monogram identity: an F whose crossbar becomes the tick, drawn from vectors so it is sharp at every size. The Dock, taskbar, window and sidebar marks take the theme's colours; the macOS menu-bar icon is a template that fills its slot and adapts to light and dark menu bars like other professional apps.
- New checks: interlaced content, bit depth and HDR transfer (SDR, PQ, HLG), XR projection and stereo metadata for 360 deliverables, aspect ratio, audio sample rate, dead channels, and EBU R128 loudness and true peak (measured and reported by default; can warn or fail against a target).
- Every stricter rule can be relaxed in Settings: allow larger resolution, allow frame-rate multiples, require progressive, minimum sample rate, loudness and allowed decode errors.
- JSON and CSV data are saved next to every PDF report, and the PDF records the exact specification, thresholds and tool versions used.
- The watch folder scans subfolders (camera-card layouts) and waits longer for copies to finish.
- macOS has a full menu bar: About, Settings (Cmd+,), File, View, Window and Help, with the familiar shortcuts.
- Open With: FrameGuard appears in Finder's and Explorer's Open With menus for video files and folders, without becoming the default app. On Windows a second launch hands its files to the running app.
- Accessibility: summary badges and the file queue work from the keyboard and are named for screen readers, every slider and field has a name, and control outlines meet 3:1 contrast in all 15 themes.
- Dialogs, file panels and title bars follow the app's light or dark theme.
- The window's minimum size fits the layout, and narrow windows keep file names readable.

### Changed
- Bundled FFmpeg and MediaInfo are pinned to exact versions and verified against checksums committed in the repository.
- Update checks verify GitHub's certificate with the operating system's own trust store.
- The local-folder update fallback was removed; updates come only from GitHub Releases over HTTPS. Optional delta channels must use HTTPS and hash every file.
- Hardware Acceleration, Segmented Scan and Segment Count were removed from Settings.
- The installer now shows the repository's standard MIT licence text.

## [0.8.3] — 2026-10-20 — Menu-bar crash fix and sidebar polish

### Fixed
- macOS: clicking the FrameGuard icon in the menu bar no longer crashes the app. macOS 27 opens menu-bar menus without a mouse event, and Qt's menu-bar code asked that missing event for its click count, which aborted FrameGuard before any of its own code ran. The menu now opens normally.
- The sidebar's show/hide button is a full square on hover. The button was wider than the column it sits in, so the page cut off the right edge of its hover outline.
- Long file names in the Results and Checks tables are shortened in the middle instead of at the end, so the clip number that tells similar names apart stays visible.

### Changed
- Build and release workflows use the reviewed actions/checkout v7.0.1, actions/setup-python v7.0.0 and actions/attest-build-provenance v4.2.2. Dependabot now proposes action updates as one grouped pull request.
- The README demo and screenshots show a scan with passes, warnings and failures.

## [0.8.2] — 2026-10-19 — Open With, reliable quitting and a polished interface and report

### Fixed
- Quitting always quits. Cmd+Q, the app and Dock menus' Quit and the menu-bar icon's Quit no longer hide FrameGuard to the menu bar while a watch folder is active; settings are saved, scans and FFmpeg processes are stopped, and the process exits promptly instead of needing Force Quit.
- Deep scans now measure a freeze, silence or black region that lasts until the end of the file or scan segment. A clip that froze for its final seconds, or an entirely static deliverable, was reported as a 0.0s freeze and could pass.
- Very small sizes and bitrates no longer read as "0.0 MB" or "0.0 Mbps is 100% below minimum".
- Dropdown lists on macOS use the themed list with comfortable rows, are wide enough for long preset names, and no longer show a native checkmark gutter.
- Results "More" menu items line up; the duplicate "Clear History" entry is gone (Clear Results is on the toolbar).
- After a theme switch, the Scan page's file count no longer keeps the previous theme's colour, which could make it invisible.
- The selected scan mode stays visible while a scan is running.

### Improved
- Open With: open one or many results in any video player the system knows about (QuickTime Player, VLC, IINA, TV, Premiere Pro and others on macOS; VLC, MPC-HC/BE, PotPlayer, mpv and Windows Media Player Legacy on Windows, plus any app through Choose Another App). The last player used is remembered and offered as "Open in ..."; launches never block the window and failures are reported clearly. Return opens the highlighted result; Ctrl+Shift+O (Cmd+Shift+O) opens the checked results.
- PDF reports: file names in any language render correctly, long names wrap instead of being cut off, a file's checks are kept together with widow and orphan control and a "(continued)" heading when they must split, status badges meet text-contrast guidelines on screen and in print, pages read "Page X of Y", and the PDF carries title, author and subject properties.
- Sidebar: Import Preset is centred as an action, the active row's label lines up with the others, and the bottom badge was removed.
- Settings: Scan Presets and Updates buttons sit in single even rows, and the delta channel field is labelled like every other setting.
- Results toolbar: consistent spacing between the Issues only filter and the action buttons.
- Watch Folder: the activity log and notifications use plain PASS / WARN / FAIL words instead of colour emoji, and the Sync button reads Sync Now.

### Changed
- The "Meta Dark" theme is now called "Blue Hour" (same colours). A saved Meta Dark preference switches to Blue Hour automatically.
- The "DSS-2 Compliant" badge and the matching PDF footer text were removed.

## [0.8.1] — 2026-10-19 — Seamless interface, working updates and crash fixes

### Fixed
- Deep scans no longer fuse separate silences or freezes into one long false event or drop black-frame events after the second, which could mark clean files as failed. Every detection now reaches the verdict.
- An entirely black file, or one that opens or ends on a long stretch of black, is now reported instead of passing; short fades are still ignored.
- A silence that starts at a segment boundary no longer produces a fabricated multi-minute silence.
- File names and tags containing words such as "corrupt" no longer fail Decode Integrity.
- Matroska and WebM files no longer carry spurious A/V Duration and Frame Count warnings, an unreadable frame rate is a low-confidence warning instead of a 0 fps failure, and files without audio pass when audio is not required.
- Preset values are type-checked: values that could alter FFmpeg filter graphs are rejected, and codec and container lists are case-insensitive.
- Batches larger than 2,000 files now check every file.
- macOS: clicking the menu-bar icon, using its menu, and quitting no longer crash FrameGuard.
- The startup splash appears sooner, keeps moving while the window is built, and fades only after the window is on screen.
- macOS update checks now trust the system keychain, so FrameGuard can reach GitHub Releases; a check that cannot connect says so instead of reporting "Up to date".
- In-app updates show download progress and errors, close FrameGuard so the installer can replace it, and reopen FrameGuard on Windows.

### Improved
- The sidebar is one continuous surface with a full-height divider, a single logo and version, aligned rows and a vector collapse control.
- The macOS title bar now takes the current theme's colour and light or dark appearance, and follows theme changes.
- Real chevrons, ticks and radio dots in every theme; checked boxes and radios no longer grow; tighter, aligned spacing on Scan, Results, Checks and Settings; the Results header no longer floods with colour when a row is selected, and its checkbox header selects or clears all files.
- Update checks repeat hourly for long-running sessions, and downloaded installers from earlier updates are cleaned up.

### Changed
- Updates still come from the exact architecture-specific GitHub Release assets and published SHA-256 evidence. On macOS, v0.8.0 cannot verify GitHub's certificate, so install this release from the Releases page once; in-app updates work from v0.8.1 onward.

## [0.8.0] — 2026-10-15 — Shipped-path reliability

### Fixed
- Real startup now follows the same bounded, tested path in source and frozen applications, preventing startup-only crashes that a MainWindow-only self-test could miss.
- Scan subprocess cancellation is owned per worker, malformed metadata fails safely, and bounded FFmpeg diagnostics retain early and late defect evidence without unbounded memory growth.
- Watch-folder ingestion distinguishes same-size rewrites and replacements, rejects unsafe symlinks, preserves batches across low-disk aborts, and remains duplicate-safe across normal lifecycle transitions.
- Preset parsing, PDF export lifetime, media launching, and full-installer update downloads now enforce deterministic limits, integrity checks, cancellation, and cleanup.
- Windows installers and both macOS disk images must execute the final installed or mounted application startup path before release assets can be uploaded.

### Improved
- A fail-closed local pre-push command fingerprints the exact source tree, toolchain, logs, manifests, review metadata and local package bytes, while explicitly refusing to claim cross-platform release authorization.
- GitHub now blocks a release tag unless Windows, Apple Silicon and Intel native package jobs all passed for the exact commit SHA; the release rebuilds that verified SHA and revalidates the annotated tag immediately before publication.

### Changed
- Optional legacy differential updates remain unchanged; public updates continue to use exact architecture-specific GitHub Release assets and published SHA-256 evidence.

## [0.7.3] — 2026-10-15 — Native Windows responsive contracts

### Fixed
- Responsive layout tests now exercise 1000px and 1920px policy widths directly instead of asking the native Windows host to create a window larger than its physical CI desktop. The previous test observed the correctly capped 1000px native window twice and falsely reported that the layout did not adapt.

### Changed
- Native Windows Qt rendering, the glyph preflight, and the 90-minute Intel macOS budget remain unchanged after v0.7.2 proved both font rasterization and both macOS jobs green.
- Immutable failed v0.7.0 through v0.7.2 tags remain preserved; this release fixes forward.
- No application runtime behavior changes unless an internal caller explicitly supplies a responsive-layout target width.

## [0.7.2] — Unpublished tag — Native Windows validation

### Fixed
- Windows UI automation now uses Qt's native Windows backend and verifies ASCII glyph support before the suite starts. The offscreen backend produced placeholder glyphs and invalid text metrics on the hosted Windows runner even when a valid system font was selected.
- Intel macOS Tests now have a 90-minute budget to complete the same full suite; the previous 30-minute limit cancelled a healthy run at 53%.
- Automated startup probes are suppressed through an explicit test-mode contract rather than by depending on the offscreen backend.

### Changed
- The immutable failed v0.7.0 and v0.7.1 tags are preserved. This release fixes forward from conditions proven by their first three-platform runs.
- No scan-engine, report, updater selection, preset, sorting, filtering, or export semantics changed.

## [0.7.1] — Unpublished tag — Cross-platform UI release

### Fixed
- FrameGuard now installs its resolved UI font before building controls, preventing unusable fallback glyphs and mismatched text metrics on clean Windows profiles and headless validation systems.
- Windows caption controls, Results summary metrics, and Checks headings reserve space from realized font metrics; the audit explicitly recognizes the intentional native size-grip overlay.
- The locked-file backlog regression test now drives an already-proven queued batch directly instead of depending on a 10 ms Qt timer under a saturated runner.
- Background update checks safely stop emitting if their Qt owner is deleted during shutdown.

### Changed
- The immutable failed v0.7.0 tag is preserved. This release fixes forward after its first three-platform run exposed CI-only font and timer conditions.
- No scan-engine, report, updater selection, preset, sorting, filtering, or export semantics changed.

## [0.7.0] — Unpublished tag — Quiet Pro interface

### Improved
- FrameGuard now opens into a complete cross-platform desktop design system with intentional styling for every application-owned control, interaction state, dialog, menu, table, and all 15 themes.
- Clean first launch follows the operating system light or dark appearance, restores screen-safe geometry, and no longer creates an output folder before an export needs it.
- Scan and Watch Folder are responsive and scroll safely on short screens; Settings uses a metric-driven layout instead of a rigid control column.
- Results and Checks use realized font metrics for table rows, metadata columns, and one- or two-line headers, keeping long filenames, summaries, check names, and failure counts readable.
- Native macOS title bars remain fully native, while the Windows title bar preserves DWM, resize, Aero Snap, Snap Layouts, and system-menu behavior with explicit accessibility contracts.
- CI now audits populated UI states at three window sizes and four DPI scales, preserving diagnostic screenshots and JSON on failure.

### Fixed
- Removed unstyled native-button fallthrough, overlapping controls, nested summary-card backgrounds, stale light-theme text in dark Watch Folder, tiny effective hit targets, and clipped Results/Checks headers.
- Keyboard focus, label buddies, icon-button names, disabled states, contrast, scrollbar/splitter hit areas, and first-open theme persistence are now verified.

### Changed
- No scan-engine, report, updater, preset, sorting, filtering, or export semantics changed.

## [0.6.3] — 2026-10-15 — Reliable public release

### Fixed
- Public releases no longer try to commit oversized application payloads to a Git branch; the Windows FFmpeg tools exceed GitHub object limits and the macOS tree is over 600 MB.
- Fresh installs now check complete, architecture-specific GitHub Release assets directly. Optional differential updates remain available for explicitly configured filesystem or static-HTTP hosts.
- Windows watch-folder backlog tests now use explicit settled timestamps instead of depending on runner filesystem timing.
- macOS disk-image creation now retries transient DiskImages failures and preserves full diagnostics for every failed attempt.

### Changed
- The immutable unpublished v0.6.1 and v0.6.2 tags are preserved; this release fixes forward without rewriting history.
- No scanning or report behaviour changed.

## [0.6.2] — Unpublished tag — First-release publication

### Fixed
- Publishing now treats the expected GitHub “no latest release” response as an empty initial state, so the first public release can pass the monotonic update-channel guard.
- The abandoned v0.6.1 tag remains immutable; this release fixes forward without rewriting published history.

### Changed
- No scanning or report behaviour changed from v0.6.1.

## [0.6.1] — Unpublished tag — Release reliability

### Fixed
- macOS disk-image verification now retries transient DiskImages service failures instead of discarding a valid arm64 build.
- Windows and both native macOS architectures now run the full constrained test suite before a release can be tagged.
- Official macOS signing, notarization and stapling fail closed when Apple credentials are configured, while ad-hoc internal builds remain supported.
- Release drafts can resume safely, and the delta channel is updated before an immutable release is published.

### Changed
- Python, packaging and test dependencies are pinned to the versions proven by the Windows, Apple Silicon and Intel builders.
- GitHub Actions use reviewed Node 24-compatible revisions and preserve build diagnostics on failure.
- Manual releases must run from the matching tag ref; published releases and tags are never rewritten.

## [0.6.0] — Unpublished tag — Release pipeline repair

### Fixed
- The macOS build could not finish: its FFmpeg and FFprobe downloads pointed at a retired source, so the build failed at the final step. The tools are now downloaded per architecture (Apple Silicon and Intel separately), pinned to a specific build, and verified against the publisher's SHA-256 checksums before bundling.
- MediaInfo silently went missing from macOS builds: unpacking its installer package failed without a trace in the log. The package is now unpacked reliably, with a fallback method, and any failure is reported instead of swallowed.
- A missing required tool was reported as a warning and the build failed later with an unrelated message. Both builders now stop immediately, with the real cause.
- The Windows build's test gate could stall forever: with FFmpeg missing, a startup check opened a dialog that automated runs could never dismiss. The check no longer opens dialogs when the app runs without a display, every test has a time limit, and the suite also runs in its own CI workflow on every push.
- Running from a source checkout, the app did not search the per-architecture tool folder the downloader now uses, so freshly downloaded tools could be overlooked. The search order now matches the downloader.

### Changed
- App identity moved from legacy identifiers to the publisher's own: the macOS bundle ID is now com.caprilaria.frameguard, the organization domain is caprilaria.com, and the updater's organization is Capri la Ria. On macOS, preferences and preset-file ownership start fresh under the new identity.
- Bundled MediaInfo updated to 26.10.
- No changes to scanning behaviour.

## [0.5.5] — Hardening

### Fixed
- Scan page verdict could stick after Clear Results; it now clears with the results, and transient notes auto-dismiss instead of lingering.
- Strict Mode setting had no effect; warnings now escalate to FAIL with it on.
- Open in VLC could silently fail: installs are re-detected each use, a dead launch falls back with a message, and the frozen bundle dir no longer leaks into the player environment.
- Startup splash bar froze at 0% while the window built; it now tracks progress live.
- Startup no longer blocks on the FFmpeg probe; the window paints first.
- Default-player fallback now reports when nothing could be launched.

## [0.5.4] — Appearance

- Redesigned startup splash: it now shows the real FrameGuard mark instead of a placeholder box, with a soft shadow and a scan sweep.
- The splash now opens in whichever theme you last selected, rather than always the default.
- Two new dark themes: Carbon (neutral true-dark) and Abyss Teal. The theme picker is now a complete 5x3 grid of 15 themes.
- Switched the interface to Segoe UI Variable on Windows 11, which is noticeably cleaner at small sizes.
- Resolution and duration columns now use tabular figures so the digits line up down the column.

## [0.5.3] — UI corrections

### Fixed
- Text was cut off in the Results table: Status, Resolution and Duration columns were too narrow for their own contents. Columns are now measured from the actual text.
- Check names were cut off in the Checks matrix headers, making it impossible to tell which check a red cell belonged to.
- The Results sidebar row showed two icons after a scan finished.
- "Audio Loudness" still appeared in results as N/A. That check was removed in v0.4.4 and now leaves no trace in reports or the Checks grid.
- An in-place update could delete the uninstaller and the app icon, leaving FrameGuard impossible to remove through Add/Remove Programs. Files owned by the installer are now protected.

## [0.5.2] — Correctness Release

FIXED - Scan engine
- Deep scan detected nothing: black frame, freeze frame and silence detection were suppressed by an FFmpeg verbosity flag, so every file reported "no issues found". These checks now work.
- Files 10-25% under the bitrate target crashed the scan and were reported as "Internal Error" / MISMATCH.
- Black frame and silence timestamps were reported relative to the internal scan segment, so a defect at 05:12 was reported at 00:12. Timestamps are now absolute.
- Failed or timed-out scan segments were reported as clean. They now raise a warning.
- Non-ASCII filenames could abort a scan.

FIXED - Settings and presets
- Every Delivery Profile and Deep Scan setting was ignored by the scan engine. Resolution, frame rate, tolerance, bitrate, hardware acceleration and segmentation now actually apply.
- Exported .fg presets always contained hardcoded defaults instead of your settings.
- Imported presets had no effect on scans.
- "Reset to defaults" restored values that did not match the engine defaults.

FIXED - PDF export
- Reports spanning more than one page failed to generate (any multi-file batch).
- Success and failure dialogs could silently never appear.
- Auto-export froze the UI. It now runs in the background.
- The export dialog now offers Open PDF and Show in Folder.

FIXED - Drag and drop
- Dropping a .fg preset was rejected by Windows and could be ignored.
- Drops and clicks stopped working once files were queued.
- Presets can now be dropped anywhere in the window, and .fg files can be opened by double-click.

FIXED - Stability
- Startup crash on machines without a system tray.
- "Check for Updates" crashed and left the button stuck on "Checking...".
- Watch folder auto-scan stopped permanently after the first scan.
- Watch folder errors crashed the handler.
- Results from earlier scans disappeared when a new scan finished.
- "Start with Windows" never worked.
- The selected theme was lost from the Settings page after a restart.

### Improved
- Results now appear one by one as each file finishes instead of all at once at the end.
- Bundled presets are now shipped with the installer.
- FFmpeg detection is more reliable and no longer re-probed for every scan segment.

### Ui Changes From Leadership Review
- Failed files now read FAIL instead of MISMATCH, sort to the top of the results list, are tinted red, and are summarised in a banner above the table.
- New Checks page: a grid of every file against every check, so a problem affecting the whole batch is obvious at a glance.
- Right-click a result (or use the toolbar button) to open it in VLC.
- Import Preset moved out of Settings and onto the sidebar.
- New Darkula colour theme.
- The active theme now also recolours the app icon, title bar and tray icon.
- The window now supports Aero Snap: drag to the top to maximise, drag to a side to tile, Win+Arrow works.
- .fg preset files now show the FrameGuard icon in Explorer.
- PDF reports redesigned: proper column alignment, pill status badges, accurate row heights and clean page breaks.
- Installer wording reviewed across every page.
