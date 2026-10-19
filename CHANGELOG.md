# Changelog

All notable changes to FrameGuard are documented in this file.

Generated from the changelog text in `src/version.json` at main head
`fd53dc4a36ee434bc8c16cd00be3cccde3560cb5` (verified via commit-pinned raw URL,
cross-checked with `gh api`). Version entries and text are reproduced as recorded
there — nothing backdated or invented. Dates are given only where a published
GitHub Release proves them; tagged-but-unpublished versions are marked as such.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.8.2] — Open With, reliable quitting and a polished interface and report

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
