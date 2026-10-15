#!/usr/bin/env python3
"""Run FrameGuard's real startup preflight with an independent timeout."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time


REQUIRED_CHECKPOINTS = (
    "application_created",
    "file_open_filter_installed",
    "theme_resolved",
    "splash_shown",
    "window_constructed",
    "comboboxes_styled",
    "event_loop_entered",
    "event_loop_turn",
    "deferred_show_confirmed",
    "preflight_passed",
)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--entrypoint")
    parser.add_argument("--artifacts-dir")
    parser.add_argument(
        "--profile",
        help=("Explicit empty profile directory. Existing nonempty directories "
              "are refused and never deleted by this harness."),
    )
    parser.add_argument("--qpa")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--internal-timeout-ms", type=int, default=15000)
    parser.add_argument("--expect-frozen", action="store_true")
    parser.add_argument("--label", default="startup")
    return parser


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _validate(evidence, expect_frozen, challenge, process_id, launched_ns):
    problems = []
    if evidence.get("challenge") != challenge:
        problems.append("startup evidence challenge does not match this run")
    if evidence.get("process_id") != process_id:
        problems.append(
            f"evidence process_id is {evidence.get('process_id')!r}, "
            f"expected spawned PID {process_id}")
    started_ns = evidence.get("started_wall_time_ns")
    if not isinstance(started_ns, int) or started_ns < launched_ns:
        problems.append("startup evidence predates this process launch")
    if evidence.get("status") != "passed":
        problems.append(f"status is {evidence.get('status')!r}, expected 'passed'")
    if bool(evidence.get("frozen")) != expect_frozen:
        problems.append(
            f"frozen is {evidence.get('frozen')!r}, expected {expect_frozen!r}")
    event_loop = evidence.get("event_loop") or {}
    if event_loop.get("turn_observed") is not True:
        problems.append("Qt event-loop turn was not observed")
    if event_loop.get("deferred_show_confirmed") is not True:
        problems.append("deferred show callback was not confirmed")
    names = [item.get("name") for item in evidence.get("checkpoints", [])]
    missing = [name for name in REQUIRED_CHECKPOINTS if name not in names]
    if missing:
        problems.append("missing checkpoints: " + ", ".join(missing))
    positions = [names.index(name) for name in REQUIRED_CHECKPOINTS if name in names]
    if positions != sorted(positions):
        problems.append("required checkpoints are out of order")
    for field in ("executable", "os", "architecture", "python", "qt_version",
                  "qpa", "profile_path", "theme", "duration_ms"):
        if evidence.get(field) in (None, ""):
            problems.append(f"missing evidence field: {field}")
    return problems


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")
    if args.internal_timeout_ms < 100:
        raise SystemExit("--internal-timeout-ms must be at least 100")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.label):
        raise SystemExit("--label must be a simple filename component")

    owned_root = None
    if args.artifacts_dir:
        root = Path(args.artifacts_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
    else:
        owned_root = tempfile.mkdtemp(prefix="frameguard-startup-smoke-")
        root = Path(owned_root)
    if args.profile:
        profile = Path(args.profile).resolve()
        if profile.is_symlink() or (profile.exists() and not profile.is_dir()):
            print("STARTUP PREFLIGHT FAILED: explicit profile must be a directory",
                  file=sys.stderr)
            return 1
        if profile.exists() and any(profile.iterdir()):
            print("STARTUP PREFLIGHT FAILED: explicit profile must be empty; "
                  "user-owned profiles are never deleted", file=sys.stderr)
            return 1
        profile.mkdir(parents=True, exist_ok=True)
    else:
        # This deterministic child is owned by the harness, so every reuse of
        # an artifact directory still starts from genuinely cold state.
        profile = root / f"{args.label}.profile"
        try:
            if profile.is_symlink() or profile.is_file():
                profile.unlink()
            elif profile.exists():
                shutil.rmtree(profile)
            profile.mkdir(parents=True)
        except OSError as exc:
            print(f"STARTUP PREFLIGHT FAILED: cannot reset tool-owned profile: {exc}",
                  file=sys.stderr)
            return 1
    report = root / f"{args.label}.json"
    stdout_path = root / f"{args.label}.stdout.log"
    stderr_path = root / f"{args.label}.stderr.log"
    try:
        report.unlink(missing_ok=True)
    except OSError as exc:
        print(f"STARTUP PREFLIGHT FAILED: cannot remove stale evidence: {exc}",
              file=sys.stderr)
        return 1
    if report.exists():
        print(f"STARTUP PREFLIGHT FAILED: stale evidence remains at {report}",
              file=sys.stderr)
        return 1
    challenge = secrets.token_urlsafe(32)

    executable = shutil.which(args.executable) or str(Path(args.executable).resolve())
    command = [executable]
    if args.entrypoint:
        command.append(str(Path(args.entrypoint).resolve()))
    command.extend([
        "--startup-preflight", str(report),
        "--startup-preflight-profile", str(profile),
        "--startup-timeout-ms", str(args.internal_timeout_ms),
        "--startup-challenge", challenge,
    ])
    env = os.environ.copy()
    if args.qpa:
        env["QT_QPA_PLATFORM"] = args.qpa
    env.update({
        "HOME": str(profile),
        "USERPROFILE": str(profile),
        "XDG_CONFIG_HOME": str(profile / "config"),
        "XDG_DATA_HOME": str(profile / "data"),
        "XDG_CACHE_HOME": str(profile / "cache"),
        "APPDATA": str(profile / "appdata"),
        "LOCALAPPDATA": str(profile / "localappdata"),
    })

    creationflags = 0
    popen_kwargs = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    launched_ns = time.time_ns()
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
        **popen_kwargs,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        stdout, stderr = process.communicate()
    stdout_path.write_text(stdout or "", encoding="utf-8")
    stderr_path.write_text(stderr or "", encoding="utf-8")

    errors = []
    if timed_out:
        errors.append(f"external timeout after {args.timeout:g}s")
    if process.returncode != 0:
        errors.append(f"process exited with {process.returncode}")
    evidence = None
    if not report.is_file():
        errors.append(f"startup evidence was not written: {report}")
    else:
        try:
            evidence = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid startup evidence: {exc}")
    if evidence is not None:
        errors.extend(_validate(
            evidence,
            args.expect_frozen,
            challenge,
            process.pid,
            launched_ns,
        ))

    if errors:
        print("STARTUP PREFLIGHT FAILED", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(f"  evidence: {report}", file=sys.stderr)
        print(f"  stdout:   {stdout_path}", file=sys.stderr)
        print(f"  stderr:   {stderr_path}", file=sys.stderr)
        return 1

    print(f"STARTUP PREFLIGHT PASSED: {report}")
    if owned_root:
        shutil.rmtree(owned_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
