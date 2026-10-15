# Pre-push evidence gate

`src/tools_dev/pre_push.py` is FrameGuard's local, fail-closed push-readiness
orchestrator. It does not commit, push, tag, dispatch, or release anything. It
binds every result to the exact Git `HEAD`, tracked dirty binary diff, untracked
file names and contents, commands, platform, and tool versions that were tested.
A zero exit code is the only authorized result.

## One command

From `src`, run the complete internal gate:

```bash
python3 tools_dev/pre_push.py --profile full
```

The first run is expected to exit nonzero with `INCOMPLETE` after all internal
gates pass. Its final line names a fresh evidence directory under
`.git/frameguard-prepush/`. Give that directory's `summary.txt`, `summary.json`,
logs, artifacts, and `review-request.json` to an independent reviewer. The
reviewer writes the completed JSON object from `review-request.json` to (for
example) `review-evidence.json` in the same evidence directory.

Resume without rerunning valid stages:

```bash
python3 tools_dev/pre_push.py --profile full \
  --resume ../.git/frameguard-prepush/RUN_ID \
  --review-evidence ../.git/frameguard-prepush/RUN_ID/review-evidence.json
```

Resume is refused if the full tree or tool/platform evidence changed. Before
indexing any prior result, the summary must contain exactly one record for every
freshly planned stage: duplicate, missing, unexpected, reordered-plan, command,
timeout, validator, signature, evidence-directory, or metadata mismatches fail
closed. A passed stage is reused only when its log and artifact hashes still
match; recorded nonpassing or blocked stages rerun.

## Profiles

| Profile | Intended use | Gates | Can authorize a push? |
|---|---|---|---|
| `fast` | Local iteration | Universal startup/version/contracts/static gates plus impacted tests | **No.** It exits nonzero even with review evidence. |
| `full` | Normal pre-push proof | Universal and impacted gates, complete pytest suite, repeated timing stress, rendered DPI audit, independent review | Yes, only when every result is fresh and passed. |
| `package` | Native Apple Silicon release-candidate proof | Everything in `full`, then a clean `build_mac.sh --skip-delta` and a second mounted/copied arm64 DMG startup smoke | Yes, on native macOS arm64 only, with independent review. |

The package build deliberately does not pass `--fast`, `--skip-tests`, or
`--allow-unsigned`. Existing signing/notarization environment variables retain
their strict behavior in `build_mac.sh`. `--skip-delta` omits only the optional
external differential payload; it does not bypass source, frozen-app, DMG,
signature, or startup gates.

## Required gates

Every profile runs these non-optional stages:

- Real source startup through `tools_dev/startup_smoke.py`, with its internal Qt watchdog and an independent outer process timeout.
- Version consistency through `validate_version.py`.
- Workflow, packaging, platform, and startup contract tests.
- Python bytecode compilation, `pyflakes`, and `git diff --check HEAD`.
- Focused pytest suites selected from every classified changed path.

`full` and `package` additionally run the complete suite, repeat timing-sensitive
startup/engine/watch-folder suites, and execute the rendered audit at every
supported scale. Rendered warnings are failures, not soft passes. Every command
has an external stage timeout; pytest and startup retain their own internal
timeouts.

## Evidence and decisions

Each run writes both `summary.json` and `summary.txt`, one log per stage,
per-command timestamps/return codes/durations, impact classification, exact
commands, tool versions, fingerprints, and rendered/startup/package artifacts.
It also computes a canonical authorization-evidence digest over the ordered full
passing-stage records (excluding only the resume-only `reused` marker), commands
and executions, log paths and hashes, complete sorted artifact manifests,
validator results, and package outputs. For the package profile this explicitly
includes the ignored `Output/FrameGuard-arm64.dmg` bytes.
The tool fingerprint records both the exact Python executable path used to
invoke the orchestrator and its resolved base-interpreter path. Stage commands
preserve the invoked path because resolving a symlink-based virtualenv's Python
would bypass that environment's `pyvenv.cfg` and installed packages. Changing
either identity invalidates resume evidence.
Evidence defaults inside `.git` so producing it cannot dirty the source tree.
Use `--evidence-root /absolute/path` when evidence must live elsewhere; every new
run still requires a non-existing directory.

The final state is one of:

- `AUTHORIZED`: a `full` or `package` run has every expected stage passed and fresh, no unknown paths/statuses/warnings/skips, and fresh matching independent review evidence.
- `INCOMPLETE`: internal gates passed but review evidence is missing, or the profile is intentionally non-authorizing (`fast`).
- `FAILED`: a command, timeout, evidence validator, classification, resume check, or independent-review contract failed.

Missing, duplicate, stale, skipped, warning, timed-out, unknown, or failed stage
results block authorization. Review evidence must exactly match the run, tree,
tool, and canonical authorization-evidence digests; attest `independent: true`;
identify the reviewer with a non-template identity; provide a normalized,
substantive scope/findings summary; be written after internal gates complete;
and be no more than 24 hours old by default. Copying the request template and
changing only its timestamp is rejected. Change the age ceiling only with the
explicit `--review-max-age-hours` option.

The authorization digest deliberately excludes `summary.json`, `summary.txt`,
`review-request.json`, and the external review-evidence file. Those files cannot
circularly approve their own bytes; only stage outputs and package artifacts are
inside the reviewed digest.

## Useful options

```text
--stage-timeout SECONDS       outer ceiling for long non-package stages (default 5400)
--package-timeout SECONDS     outer ceiling for the clean macOS build (default 14400)
--review-max-age-hours HOURS  freshness ceiling for independent review (default 24)
--evidence-root PATH          parent for a new evidence directory
--resume PATH                 exact prior evidence directory; cannot combine with --evidence-root
```

If the tree changes during any command, that stage becomes `STALE` and the run
fails. The complete `HEAD` and dirty-tree snapshot is recaptured again after all
stages and review input immediately before authorization, including all-reused
resumes, and once more before a zero exit. At both boundaries every passing stage
is also revalidated through its command/signature/log/artifact contract and
semantic validator, then the authorization digest is recomputed. A changed log,
manifest, rendered/startup evidence file, or ignored package DMG rewrites the
result to `FAILED`. Each final check is recorded in both summaries. Start a fresh run for the
new fingerprint; never edit `summary.json` or a stage log to force reuse.
