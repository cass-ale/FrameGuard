# Local pre-push evidence gate

`src/tools_dev/pre_push.py` is FrameGuard's local, fail-closed readiness
orchestrator. It does not commit, push, tag, dispatch, or release anything. It
binds results to the exact Git `HEAD`, dirty binary diff, untracked files,
commands, host platform, and tool versions that were tested.

A zero exit code means `LOCAL-READY` for that host and fingerprint. It does not
authorize a cross-platform release. Only a successful GitHub Release Candidate
run whose `head_sha` exactly matches the release commit can make that commit
eligible for a tag-triggered release.

## One command

From `src`, run the complete local gate:

```bash
python3 tools_dev/pre_push.py --profile full
```

The first run normally exits nonzero with `INCOMPLETE` after internal gates
pass. Its evidence directory contains `summary.txt`, `summary.json`, logs,
artifacts, and `review-request.json`. A reviewer may record their assessment in
a matching `review-evidence.json`, then the run can be resumed:

```bash
python3 tools_dev/pre_push.py --profile full \
  --resume ../.git/frameguard-prepush/RUN_ID \
  --review-evidence ../.git/frameguard-prepush/RUN_ID/review-evidence.json
```

Review metadata is an integrity-bound local checklist, not authenticated proof
of reviewer identity or independence. GitHub environment protection and the
exact-SHA native candidate workflow are the release controls.

Resume is refused if the source snapshot, tool/platform fingerprint, planned
stages, commands, timeouts, validators, logs, or artifact hashes changed. Old
evidence schemas are rejected rather than interpreted using current semantics.

## Profiles

| Profile | Intended use | Gates | Successful state |
|---|---|---|---|
| `fast` | Local iteration | Universal startup/version/contracts/static gates plus impacted tests | Always `INCOMPLETE`; diagnostic only. |
| `full` | Normal local push readiness | Universal and impacted gates, complete pytest, repeated timing stress, rendered DPI audit, review metadata | `LOCAL-READY` on this host only. |
| `package` | Local Apple Silicon package readiness | Everything in `full`, clean `build_mac.sh --skip-delta`, mounted/copied arm64 DMG startup | `LOCAL-READY` on native macOS arm64 only. |

The package profile deliberately omits `--fast`, `--skip-tests`, and
`--allow-unsigned`. `--skip-delta` omits only the retired external differential
payload; it does not bypass source, frozen-app, DMG, signature, or startup
gates.

The local package profile does not build the Windows installer or Intel DMG.
Its ignored `Output/FrameGuard-arm64.dmg` is evidence from this host, not the
byte sequence later rebuilt by the release workflow.

## Required local gates

Every profile runs:

- Real source startup through `tools_dev/startup_smoke.py`, with internal and external timeouts.
- Version consistency through `validate_version.py`.
- Workflow, packaging, platform, and startup contract tests.
- Bytecode compilation, `pyflakes`, and `git diff --check HEAD`.
- Focused pytest suites selected from every classified changed path.

`full` and `package` also run the complete suite, repeat timing-sensitive
startup/engine/watch-folder suites, and execute the rendered audit at every
supported scale. Warnings and unknown paths fail closed.

## Native release candidate gate

`.github/workflows/release-candidate.yml` runs on the final `main` commit before
a release tag is moved or created. It must succeed on the exact commit SHA and
prove all of these native paths:

- Windows 2025: full builder, frozen executable, silent installer, installed startup, uninstall, and cleanup.
- macOS Apple Silicon: full arm64 build plus mounted and copied DMG startup.
- macOS Intel: full x86_64 build plus mounted and copied DMG startup.

Each job records package hashes and uploads logs and smoke JSON evidence. The
release workflow queries GitHub for a completed successful candidate run with
an exact matching `head_sha`; absent, failed, stale, or unreadable evidence
blocks the release before native release builds start.

## Evidence states

- `LOCAL-READY`: all expected local stages passed and remained fresh, and matching review metadata was supplied. `release_authorized` is always `false`.
- `INCOMPLETE`: internal gates passed but review metadata is missing, or the profile is diagnostic.
- `FAILED`: a command, timeout, validator, classification, resume check, snapshot check, evidence check, or review-metadata contract failed.

Every summary records `scope: local` and `release_authorized: false`. The
canonical local evidence digest covers ordered stage records, commands,
executions, logs and hashes, artifact manifests, validators, and package
outputs. It excludes summaries and review metadata to prevent those files from
approving their own bytes.

Evidence defaults inside `.git/frameguard-prepush`. Use
`--evidence-root /absolute/path` when it must live elsewhere. New runs require
a fresh directory.

## Useful options

```text
--stage-timeout SECONDS       outer ceiling for non-package stages (default 5400)
--package-timeout SECONDS     outer ceiling for the local macOS build (default 14400)
--review-max-age-hours HOURS  local review-metadata freshness ceiling (default 24)
--evidence-root PATH          parent for a new evidence directory
--resume PATH                 exact prior evidence directory
```

The snapshot and every passing stage are revalidated before local readiness and
again before zero exit. If the tree, logs, manifests, rendered evidence,
startup evidence, or ignored arm64 DMG changes, the result becomes `FAILED`.
Start a fresh run for the new fingerprint; never edit evidence to force reuse.
