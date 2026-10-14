# Code Signing Policy

**Project:** FrameGuard Desktop
**Publisher:** Capri la Ria
**Author, Reviewer, and Approver:** Cassian Aléria (sole maintainer — all
three roles are held by the same person; every release and any future
signing request is reviewed and approved by him personally before it
proceeds)

## Current signing status

FrameGuard is currently distributed **unsigned** on Windows and
**ad-hoc signed** on macOS. This is a deliberate, disclosed state — see
`RELEASE_PLAN.md` for the audited reasoning and the signing roadmap.

- **Windows:** the installer and app carry no Authenticode signature, so
  SmartScreen shows "Windows protected your PC" on first run. Choose
  **More info → Run anyway**. Publisher metadata inside the files
  (Installed Apps, file properties) reads **Capri la Ria**; that metadata
  is informational only and is *not* a signature.
- **macOS:** the app is ad-hoc signed (a valid seal, required for Apple
  Silicon to run it at all) but not notarized, so Gatekeeper blocks the
  first launch of a downloaded copy. On macOS 15 or later: double-click,
  choose **Done**, then System Settings → Privacy & Security →
  **Open Anyway**. On macOS 14 or earlier: right-click the app →
  **Open**.

## How to verify a download

Every release publishes:

- `SHA256SUMS.txt` — check your download against it
  (`Get-FileHash` on Windows, `shasum -a 256 -c SHA256SUMS.txt` on macOS).
- Build-provenance attestations — with the GitHub CLI:
  `gh attestation verify <file> --owner cass-ale`
- One CycloneDX SBOM per platform, listing the Python components in the
  build.

## Future signing

When the project applies for free open-source signing (SignPath
Foundation), the signing certificate's publisher will read
**SignPath Foundation** — that is the signing authority's identity, not
a change of publisher. FrameGuard remains published by Capri la Ria and
developed by Cassian Aléria; this page will be updated when that happens,
and every signing request will continue to be manually approved by the
Approver named above.

## Privacy statement

FrameGuard processes all video locally on the user's machine. Nothing is
uploaded, transmitted, or shared, and the app collects no telemetry.
(Update checks fetch only a version manifest from this repository.)

## Contact

Issues and support: https://github.com/cass-ale/FrameGuard/issues
