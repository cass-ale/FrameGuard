Operator 1:
Is there an easier way of inputting custom profile parameters, rather than importing preset?
How do you create an .fg file to import new preset?
I got different results on same files. First scan found issues that 2nd scan didn't. 2nd scan found different issues
Tried clearing files to reset results page. Didn't work and files add to previous file set
Can you make scroll bar easier to move up and down? in Scan Files>Queue
Can you make more files visible in Queue? Lots of empty space and only 3/6 files visible. Have to use tiny scroll bar to check all 6 are there.
Can results page reset with new batch of files or have a reset results page button? Found Clear History in ... menu.
"Open selected in VLC" feature did not open VLC
"8 of 24 files passed with warnings" dialog still there after clear history
6/7 files wrongly passed QA. input files res: 2000x2140 - Passed on a 4k check (Quick Mode). Correctly flagged res on Deep Scan mode though
App unexpectedly quit when switching windows
Would prefer more visual feedback for scan completion

Operator 2:
I was unsure if it defaults to "Deep mode" unless "Quick mode" is selected. Would it be more intuitive to have two separate checkboxes?
For black frames or frame drops, it would be helpful to know exactly when they happen. I rewatched the footage flagged for black frames but couldn’t find them; perhaps they aren’t noticeable to the human eye, or the tool is misidentifying black transitions as black frames.

---

## Resolution log (bug-sweep pass, working tree @ v0.5.4)

Operator 1:
- Custom profile input / .fg authoring → already shipped (Scan page "Edit values…",
  Settings → Export Current). README "Presets" section documents the flow.
- Non-repeatable scan results → deterministic overlapping segments +
  `_merge_event_intervals` already shipped; this pass additionally bounds stderr
  (previously truncated implicitly by memory pressure, now explicitly with a
  `deep_scan_stderr_truncated_lines` metadata count).
- Clearing files didn't reset results / files appended to previous set →
  `_set_files` now clears the verdict banner; Results page reset behaviour
  unchanged (per-scan reset is opt-in via Settings).
- Queue scrollbar / visible rows → already fixed (uncapped list, wheel events).
- Results reset button → already shipped ("Clear Results" + overflow "Clear
  History").
- "Open selected in VLC" did nothing → `_play_selected` exists; requires VLC
  on PATH. Failure is now surfaced where visible instead of a hidden label.
- "8 of 24 files passed with warnings" persisted after Clear History → fixed:
  `_clear_all_results` clears the attention banner with the table.
- 2000x2140 wrongly passing 4K Quick check → already fixed (floor rule, 1%
  rounding tolerance).
- App quit when switching windows → already fixed
  (`setQuitOnLastWindowClosed(False)`), pinned by
  `TestApplicationLifetime`.
- More visual scan-completion feedback → verdict banner reworked: PASS
  auto-dismisses (10s) and clears on new files / navigation; FAIL stays until
  acted on; errors set an ERROR state instead of leaving a stale verdict.
- Deep-scan crashes (Windows) → bounded: segment workers capped at 4,
  stderr capped (512KB / 5000 lines), cooperative cancel threaded through
  segment scans, no `QThread.terminate()` on hot paths, single terminal
  signal, missing-tool fail-fast, oversized-file skip gate.

Operator 2:
- Deep vs Quick ambiguity → already shipped (explicit radio buttons).
- Black-frame timestamps unfindable → absolute `HH:MM:SS.mmm` timecodes
  already shipped; merges duplicates across segment overlaps.

New in this pass (operator-visible):
- Severity-weighted results: corrupted/unreadable files rank above
  framerate-only files; attention banner reports "N critical, M other";
  critical rows carry stronger tint + bolder filenames. PDF reports mirror
  the same order.
- Async file discovery: dropping/browsing a network or Drive folder no
  longer freezes the UI ("Not Responding").
- Disk-space preflight before scans (256MB floor; unattended watch batches
  log instead of popping a modal).
- Accessible names on primary controls (screen-reader support).
- macOS: inside-out code signing, signed DMG + arch-tagged copy, opt-in
  Apple notarization, arch-aware release manifest, POSIX update channels,
  `diagnostic.sh` parity with `diagnostic.bat`.
