# FrameGuard — Release Plan (audited)

**Version:** 1.8 · **Date:** 2026-10-15 · **Status:** v0.8.0 shipped-path
reliability release; the exact source, toolchain, evidence, and arm64 package
fingerprints must pass the fail-closed package preflight before `main` and
`v0.8.0` are pushed.
**Publisher:** Capri la Ria · **Developer / copyright holder:** Cassian Aléria
**Research basis:** `frameguard-github-release-best-practices` deep-research
report, 2026-10-14 (sources cited there; headline claims re-verified live
during the run). Zero-budget constraint set by the publisher: **no purchased
licences or certificates** — no Apple Developer Program, no commercial
Windows code-signing certificate.

---

## 1. Decisions (with the evidence that forced them)

| # | Decision | Why |
|---|----------|-----|
| D1 | Ship unsigned on Windows; ad-hoc signed on macOS | Self-signing is treated by Microsoft exactly like no signature. There is no free notarization path at all (Developer ID requires the paid program). |
| D2 | The only free Windows signing route is **SignPath Foundation**, applied for *later* | Free for OSI-licensed projects, but: cert publisher reads "SignPath Foundation", every signing request is manually approved, and there is an unpublished reputation gate — several 2026 projects were declined until they had more releases/adoption. Plan: ship unsigned first, publish the code-signing policy from day one (§5), apply after a few releases. |
| D3 | Microsoft Artifact Signing and Certum are out of scope | Artifact Signing is $9.99/mo Basic / $99.99/mo Premium and geo/identity-gated; Certum OSS is ~€25–49/yr. Both break the zero-budget rule. "Buy EV for instant SmartScreen trust" is outdated — EV no longer bypasses SmartScreen. |
| D4 | macOS ships **two per-arch DMGs**, not universal2 | universal2 needs a universal2 Python *and* universal2 wheels for every binary dep; current Qt-ecosystem wheels are increasingly per-arch, so a universal2 build can silently come out single-arch. Per-arch builds on `macos-15` (arm64) and `macos-15-intel` (x86_64) are native and testable. (`macos-13` was retired 2025-12-04.) |
| D5 | Gatekeeper instructions are split by macOS version | Apple removed right-click → Open in macOS 15. 15/26: double-click → **Done** → System Settings → Privacy & Security → **Open Anyway** → authenticate → relaunch. ≤14: right-click → Open still works. Fallback on all versions: `xattr -dr com.apple.quarantine` on the .app (or on the DMG first if the app is reported "damaged"). All repo text that said only "right-click → Open" was wrong for current macOS and is corrected. |
| D6 | Draft → attach everything → verify → publish, with **immutable releases** enabled | Immutable releases are GA (2025-10-28). Once enabled, a published release's tag and assets can never be changed or deleted, and the tag name can never be reused. A forgotten asset means a new version — hence draft-first with a verification gate before publishing. |
| D7 | Provenance is free, so it is mandatory | `actions/attest-build-provenance` on the final EXE + both DMGs (Sigstore keyless via OIDC; **SLSA Build L2** — L3 is not claimed), plus `SHA256SUMS.txt` covering every asset. Users verify with `gh attestation verify`. |
| D8 | SBOM ships, generated from the **constrained frozen Python environment** | Windows and both macOS architectures install through `constraints-3.12.txt`; one CycloneDX JSON per platform is generated from `pip freeze` of the build venv. Never scan the PyInstaller bundle/installer — the frozen archive under-reports exactly the Python deps an SBOM exists to list. SBOM step failures remain non-gating. |
| D9 | version.json is the single source of truth | SemVer, annotated `vX.Y.Z` tags, never moved. CI fails the release unless tag == version.json == Inno AppVersion == spec version. The curated changelog in version.json becomes the release-notes body (plus a compare link). release-please / git-cliff are rejected for this project shape — the curated changelog already exists and beats commit-log prose. |
| D10 | Public updates use **GitHub Release installers**, not a Git branch payload mirror | The proposed channel is physically incompatible with GitHub: `ffmpeg.exe` and `ffprobe.exe` each exceed the hard 100 MB Git-object limit, and the macOS payload is over 600 MB. Omitting them is unsafe because the manifest would tell clients to delete required tools; Git LFS adds quota, authentication and pointer-file failure modes. The existing architecture-aware GitHub Releases updater is therefore the zero-configuration path. Differential updates remain available only as an explicit local/static-host override for teams with suitable infrastructure. |
| D11 | Publisher ≠ copyright holder, deliberately | Publisher/brand fields say **Capri la Ria**: Inno `AppPublisher`, `VersionInfoCompany`; spec `CompanyName`; About dialog "Publisher"; README and release-notes voice. Legal fields carry his legal name, **Cassian Aléria**: `LegalCopyright`, `VersionInfoCopyright`, `NSHumanReadableCopyright`, and both LICENSE files — copyright sits with him personally, while Capri la Ria (his label, founded 2022) is the publishing imprint credited wherever a publisher is named. This model was applied repo-wide on 2026-10-14 in commits `35dd479`–`df35054` (installer, spec, app metadata, About dialog, both LICENSE files). If SignPath is ever adopted, the *certificate* publisher becomes "SignPath Foundation" while these fields stay the project's — the policy page discloses that. |
| D12 | The three Inno URLs are distinct | `AppPublisherURL` = caprilaria.com, `AppSupportURL` = GitHub Issues, and `AppUpdatesURL` = GitHub Releases. |
| D13 | A release tag is created only from a green three-platform push commit | The full constrained suite runs on `windows-2025`, `macos-15`, and `macos-15-intel`. Release-only validation discovered too many defects after tagging; `vX.Y.Z` now points only at a commit already proven on every supported build architecture. |
| D14 | Drafts resume; published releases never mutate | A rerun may reconcile assets only while the release is a draft. An existing published release is a hard failure requiring a new version. The workflow verifies exact draft asset names and sizes immediately before undrafting; no mutable branch publication sits between those checks. |

## 2. What the workflow does (`.github/workflows/release.yml`)

Trigger: push of a `v*` tag, or manual dispatch from that existing tag's
ref. Supplying a tag input while dispatching from `main` is rejected because
the protected `release` environment admits only `v*` tag refs.

1. **verify** (`ubuntu-24.04`) — checkout the tag, Python from
   `src/.python-version` (3.12, pinned everywhere), `validate_version.py`,
   fail unless tag == version.json. Build the release notes from
   version.json (download table, Gatekeeper/SmartScreen instructions,
   verify commands, publisher credit, changelog).
2. **build-windows** (`windows-2025`) — detect Inno Setup's ISCC.exe first
   (the image ships Inno 6.7.1; a pinned Chocolatey install now *fails* as a
   downgrade), Chocolatey unpinned only if missing, fail loudly if still
   absent. Full gated `build_installer.bat` (its `pause` calls are guarded
   by `if not defined CI` at the source — no stdin tricks). SBOM from the
   build venv's freeze (non-gating). Upload installer + SBOM.
3. **build-macos-arm64** (`macos-15`) and **build-macos-intel**
   (`macos-15-intel`) — full gated `build_mac.sh`, ad-hoc signing, SBOM
   (arm job only — the dependency set is identical; the Intel job notes
   this), and one DMG upload per native architecture.
4. **publish** (`ubuntu-24.04`, `environment: release`, the only job with
   `contents: write` + `id-token: write` + `attestations: write`) —
   stage assets under release names (`FrameGuardInstaller.exe`,
   `FrameGuard-arm64.dmg`, `FrameGuard-intel.dmg`), generate
   `SHA256SUMS.txt`, attest the three binaries, create or safely resume a
   **draft** release, verify exact asset names and sizes, then undraft.
   Published releases are immutable and are never reconciled.

Hardening throughout: Node 24-compatible actions pinned to full commit SHAs;
`.github/dependabot.yml` keeps the pins moving; top-level
`permissions: contents: read`; `persist-credentials: false` on build
checkouts; per-tag build concurrency plus globally serialized release
publishing; bounded job and network retries; pinned runner labels (no floating
`*-latest` for release jobs).

## 3. One-time setup the publisher must do in GitHub settings

These live in repo settings, not YAML, so no workflow edit can bypass them:

1. **Settings → General → Releases:** enable release immutability.
   **DONE 2026-10-14** (confirmed by screenshot).
2. **Settings → Environments → `release`:** deployment branches/tags =
   only `v*` tags; required reviewer = yourself. Do **not** enable
   "prevent self-review" — as a solo maintainer it locks you out.
   **DONE 2026-10-14** (reviewer = cass-ale, `v*` tag rule, self-review
   prevention off; confirmed by screenshot and his word).
3. Confirm Dependabot alerts/updates are on (the config file is in the repo).
4. For a future official macOS release, configure all-or-none secrets:
   `MACOS_SIGN_IDENTITY`, `MACOS_CERTIFICATE_P12_BASE64`,
   `MACOS_CERTIFICATE_PASSWORD`, `APPLE_ID`, `APPLE_TEAM_ID`, and
   `APPLE_APP_PASSWORD`. The workflow imports the P12 into a temporary
   keychain and fails closed on any partial configuration.

## 4. Release-day checklist (against the draft, before publishing)

From the research §6 — with immutable releases there is no fix-up after
publishing, so this list is the gate. (The workflow automates items 1–2
partially; the rest are human, on real machines.)

- [ ] Full constrained push CI is green on Windows, macOS arm64, and macOS Intel for the exact `main` commit.
- [ ] Annotated tag target ↔ version.json ↔ installer ↔ spec versions all equal (CI asserts).
- [ ] Draft assets downloaded *from GitHub*; `SHA256SUMS.txt` verifies;
      `gh attestation verify` passes on each binary.
- [ ] Clean-machine installs: Windows with no Python (install, launch,
      uninstall; check Installed Apps shows Publisher "Capri la Ria");
      macOS arm64 and Intel — follow the *published* Gatekeeper
      instructions literally.
- [ ] Installed-app smoke test: run a scan, export a multi-file PDF,
      check the report footer and About dialog credits.
- [ ] Clean-profile first open follows OS light/dark appearance, stays within
      the active screen, creates no Documents/FrameGuard folder, and preserves
      an explicit theme choice on restart.
- [ ] macOS arm64 and Intel: inspect Retina rendering and native traffic lights,
      shadow, drag, resize, green-button fullscreen, menu bar and file pickers.
- [ ] Windows 11 at 100%, 125%, 150% and 200%: inspect title-bar active/inactive
      states, edge resize, maximize/restore, Aero Snap, Snap Layouts, Alt+Space,
      right-click system menu and per-monitor DPI transitions.
- [ ] VirusTotal check on the installer; if flagged, start the Microsoft
      file-submission false-positive process before announcing.
      false positives are structural: onedir + Inno, full version resource,
      UPX off are already the posture; never tell users to disable AV.)
- [ ] Only then publish the draft (the workflow's publish job does this
      after its own asset verification; a manual release follows this list).

## 5. Signing roadmap (zero budget)

- **Now:** unsigned Windows / ad-hoc macOS, honest instructions everywhere,
  `CODE_SIGNING_POLICY.md` published in the repo (roles: Author, Reviewer
  and Approver are all Cassian Aléria; privacy statement = the app's local-only
  processing, per the README Privacy section). This policy page is a
  SignPath eligibility requirement, so it exists from day one.
- **After a few releases with real downloads:** apply to SignPath
  Foundation. Expect manual approval per signing request once accepted,
  and the certificate publisher to read "SignPath Foundation".
- **Never:** self-signing for distribution (no effect), EV certificates
  (money for no SmartScreen benefit), hardened-runtime entitlements on the
  ad-hoc macOS build (can break Qt/dylib loading without a Team ID; the
  existing `entitlements.plist` stays as a draft for a future Developer ID
  build).

## 6. Publisher / developer credit map (implemented)

| Surface | Publisher (Capri la Ria) | Developer / legal (Cassian Aléria) |
|---|---|---|
| Inno Setup | `AppPublisher`, `VersionInfoCompany`, distinct Support/Updates URLs | `VersionInfoCopyright` stays Cassian Aléria |
| PyInstaller spec | `CompanyName` (Windows version resource) | `LegalCopyright`, `NSHumanReadableCopyright` stay |
| App package | `__publisher__` added | `__author__` / `__maintainer__` / LICENSE unchanged |
| GUI About | "Publisher: Capri la Ria" | "Lead Developer: Cassian Aléria" stays |
| PDF reports | footer gains "Published by Capri la Ria" | "Developed by Cassian Aléria" stays |
| README | credit line under the title + License section | LICENSE file unchanged |
| Release notes | "Published by Capri la Ria · Developed by Cassian Aléria" | © line in notes |

Cassian owns the installer *wizard label text* (`installer.iss` labels and
`installer_info.txt`) in a parallel edit; this plan's installer.iss changes
are metadata lines only, to be merged on top of his text.

## 7. Open items / not-checked

- macOS delta patching vs. the .app code seal: not device-verified (D10).
- THIRD_PARTY_NOTICES: not yet written. Needed before a public push for
  LGPL hygiene (PySide6/Qt) — and the bundled FFmpeg builds' exact licence
  terms (gyan.dev / Martin Riedl builds) must be verified first, not guessed.
- SignPath's reputation threshold is unpublished; acceptance is not
  promised by this plan, only prepared for.

## 8. Revision log

- **1.7 - 2026-10-15.** Fixes forward from immutable v0.7.2 after its native Windows run proved font rasterization and both macOS platforms green, then exposed two responsive tests that relied on resizing beyond the hosted desktop. v0.7.3 verifies the responsive policy directly at target widths without weakening production behavior.

- **1.6 - 2026-10-15.** Fixes forward from immutable v0.7.1 after its Windows run proved Qt's offscreen backend could not rasterize system-font glyphs, making its visual metrics invalid, while Intel macOS was cancelled by the 30-minute job limit at 53%. v0.7.2 uses the native Windows backend with a glyph preflight and gives the complete Intel suite a 90-minute budget.

- **1.5 - 2026-10-15.** Fixes forward from the immutable failed v0.7.0 tag after its first three-OS run exposed a Windows offscreen default-font failure and a saturated-runner timer race in a watch-folder test. v0.7.1 installs the resolved application font, tightens metric-derived control sizing, makes updater shutdown emissions safe, and removes the test scheduler dependency without weakening its state assertions.

- **1.4 - 2026-10-15.** Adds the v0.7.0 packaged GUI acceptance gate: clean-profile OS theme selection, screen-safe first open, macOS native chrome, and Windows title-bar/DPI/Snap behavior are explicit release evidence rather than assumptions from offscreen tests.

- **1.3 — 2026-10-15.** Retires the impossible Git-backed delta mirror after live release runs proved the Windows payload violates GitHub's hard per-object limit and the macOS tree is over 600 MB. Public updates use architecture-aware GitHub Release installers; optional differential channels require an external filesystem/static host. v0.6.3 fixes forward without rewriting the immutable v0.6.1 or v0.6.2 tags.
- **1.2 — 2026-10-15.** Added constrained cross-platform push CI, a macOS-12-compatible Qt pin, bounded DMG verification, complete nested-code signing, temporary CI keychain provisioning, Node 24 action pins, overwrite-safe workflow artifacts, rerun-safe drafts, and the green-commit-before-tag rule.
- **1.3 recovery addendum — 2026-10-15.** The exact release-mode build exposed a transient `hdiutil create` failure before push; image creation now has the same bounded retry, backoff and durable diagnostics as verification.
- **1.1 — 2026-10-14.** Credit model corrected to the one Cassian applied
  himself in commits `35dd479`–`df35054`: developer and copyright holder is
  **Cassian Aléria** (legal name, accent included) across the installer, the
  app, and both LICENSE files; Capri la Ria is publisher only. Earlier drafts
  of this plan and the workflow named "Jace Aleria" as developer/copyright
  holder — superseded everywhere, including the release-notes template and
  `version.json`. The whole change set was re-based onto `df35054` so his
  wizard-text overhaul is the base, not a casualty of the merge.
- **1.0 — 2026-10-14.** First audited plan. Supersedes the 2026-10-14
  draft workflow v1 (unpinned actions, `windows-latest`/`macos-14`,
  Chocolatey-installed Inno, no Intel build, no provenance) and corrects
  the Gatekeeper instructions it carried.
