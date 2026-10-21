#!/usr/bin/env python3
"""Fail-closed, evidence-producing pre-push verification for FrameGuard.

This tool never pushes. A zero exit code means that a full/package profile is
locally ready for one exact source, host, and toolchain fingerprint. It never
authorizes a cross-platform release; GitHub's exact-SHA native candidate gate does.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import subprocess
import sys
import time
from typing import Iterable


SRC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SRC_ROOT.parent
SCHEMA_VERSION = 2
FINAL_STATUSES = {"local-ready", "failed", "incomplete"}
PASSING_STAGE_STATUS = "passed"
NON_PASSING_STAGE_STATUSES = {
    "blocked", "failed", "incomplete", "missing", "skipped", "stale",
    "timed_out", "unclassified", "warning",
}
LOCAL_READY_PROFILES = {"full", "package"}
DEFAULT_STAGE_TIMEOUT = 90 * 60
DEFAULT_PACKAGE_TIMEOUT = 4 * 60 * 60


@dataclass(frozen=True)
class Stage:
    name: str
    commands: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    validator: str = "exit-code"


@dataclass(frozen=True)
class Snapshot:
    head: str
    dirty_diff_sha256: str
    tree_fingerprint: str
    changed_paths: tuple[str, ...]
    tracked_diff_bytes: int
    untracked_files: tuple[str, ...]


class PreflightError(RuntimeError):
    """A fail-closed preflight contract violation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def git_bytes(*args: str, check: bool = True) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, check=False,
        timeout=60,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PreflightError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def snapshot_payload(snapshot: Snapshot) -> dict:
    return {
        "head": snapshot.head,
        "dirty_diff_sha256": snapshot.dirty_diff_sha256,
        "tree_fingerprint": snapshot.tree_fingerprint,
        "changed_paths": list(snapshot.changed_paths),
        "tracked_diff_bytes": snapshot.tracked_diff_bytes,
        "untracked_files": list(snapshot.untracked_files),
    }


def capture_snapshot() -> Snapshot:
    """Fingerprint HEAD plus every tracked and untracked dirty byte."""
    head = git_bytes("rev-parse", "HEAD").decode("ascii").strip()
    tracked_diff = git_bytes(
        "diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--",
    )
    tracked_names = {
        item for item in git_bytes("diff", "--name-only", "-z", "HEAD", "--")
        .decode("utf-8", errors="surrogateescape").split("\0") if item
    }
    untracked = tuple(sorted(
        item for item in git_bytes("ls-files", "--others", "--exclude-standard", "-z")
        .decode("utf-8", errors="surrogateescape").split("\0") if item
    ))

    dirty = hashlib.sha256()
    dirty.update(b"frameguard-dirty-v1\0tracked\0")
    dirty.update(tracked_diff)
    for relative in untracked:
        encoded = relative.encode("utf-8", errors="surrogateescape")
        path = REPO_ROOT / relative
        dirty.update(b"\0untracked\0")
        dirty.update(len(encoded).to_bytes(8, "big"))
        dirty.update(encoded)
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
            dirty.update(b"\0mode=120000\0")
        else:
            try:
                payload = path.read_bytes()
                executable = bool(path.stat().st_mode & 0o111)
            except OSError as exc:
                raise PreflightError(f"cannot fingerprint untracked file {relative}: {exc}") from exc
            dirty.update(b"\0mode=100755\0" if executable else b"\0mode=100644\0")
        dirty.update(len(payload).to_bytes(8, "big"))
        dirty.update(payload)
    dirty_digest = dirty.hexdigest()
    tree_fingerprint = hashlib.sha256(
        f"frameguard-tree-v1\0{head}\0{dirty_digest}".encode("ascii")
    ).hexdigest()
    return Snapshot(
        head=head,
        dirty_diff_sha256=dirty_digest,
        tree_fingerprint=tree_fingerprint,
        changed_paths=tuple(sorted(tracked_names | set(untracked))),
        tracked_diff_bytes=len(tracked_diff),
        untracked_files=untracked,
    )


def _command_version(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command, cwd=SRC_ROOT, capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {type(exc).__name__}: {exc}"
    output = (completed.stdout or completed.stderr).strip().splitlines()
    rendered = output[0] if output else "no version output"
    return f"exit={completed.returncode}: {rendered}"


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def capture_tools() -> dict:
    versions = {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        # A venv's interpreter is commonly a symlink to the base Python. The
        # invoked path selects pyvenv.cfg and site-packages, while the resolved
        # target does not. Record both identities, but execute only the former.
        "python_executable_invoked": sys.executable,
        "python_executable_resolved": str(Path(sys.executable).resolve()),
        "git": _command_version(["git", "--version"]),
        "pytest": _distribution_version("pytest"),
        "pytest_timeout": _distribution_version("pytest-timeout"),
        "pyflakes": _distribution_version("pyflakes"),
        "pyside6": _distribution_version("PySide6"),
        "pyinstaller": _distribution_version("pyinstaller"),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }
    return {"versions": versions, "fingerprint": sha256_json(versions)}


def classify_path(path: str) -> set[str]:
    """Return conservative impact areas, or an empty set for unknown paths."""
    if path.startswith(".github/workflows/"):
        return {"workflow_packaging"}
    if path.startswith(("docs/", "assets/")) or path in {
            "README.md", "CHANGELOG.md", "CODE_SIGNING_POLICY.md",
            "SECURITY.md", "LICENSE"}:
        # Top-level assets/ holds README screenshots and demo media only;
        # the app's bundled assets live under src/assets/.
        return {"documentation"}
    if path == "src/LICENSE":
        # installer.iss shows this file as the Setup licence page.
        return {"documentation", "workflow_packaging"}
    if path in {".gitignore", ".gitattributes", "src/.gitignore"} \
            or path.startswith(".github/dependabot"):
        return {"repository_config"}
    if path.startswith("src/tests/"):
        return {"tests"}
    if path.startswith("src/tools_dev/"):
        return {"developer_tools", "workflow_packaging"}
    if path.startswith("src/presets/"):
        return {"ingestion"}
    if path.startswith(("src/assets/", "src/branding/")):
        # Brand sources feed the generated app icons that the spec bundles,
        # so a change there affects both the rendered UI and the packages.
        return {"ui", "workflow_packaging"}
    if path.startswith("src/frameguard/"):
        name = Path(path).name
        if name in {"core.py", "worker.py", "tools.py"}:
            return {"engine"}
        if name in {"presets.py", "watchfolder.py", "manifest.py"}:
            return {"ingestion"}
        if name == "report.py":
            return {"reports"}
        if name in {"updater.py", "updatesource.py", "deltaupdate.py"}:
            return {"updater"}
        if name in {"gui.py", "design.py", "icons.py", "theme.py", "brand.py"}:
            return {"ui"}
        if name == "__init__.py":
            return {"runtime", "workflow_packaging"}
        return {"runtime"}
    if path == "src/main.py":
        return {"startup", "ui"}
    if path.startswith("src/Output/"):
        return {"workflow_packaging"}
    if path.startswith("src/"):
        name = Path(path).name
        if name.endswith((".md", ".txt")) and name not in {
                "requirements.txt", "requirements-dev.txt", "constraints-3.12.txt"}:
            return {"documentation"}
        packaging_names = {
            ".python-version", "build_installer.bat", "build_mac.sh",
            "constraints-3.12.txt", "diagnostic.bat", "diagnostic.sh",
            "download_tools.py", "entitlements.plist", "frameguard.spec",
            "installer.iss", "installer_info.txt", "publish_release.py",
            "pytest.ini", "requirements-dev.txt", "requirements.txt",
            "validate_version.py", "version.json", "write_build_stamp.py",
        }
        if name in packaging_names:
            return {"workflow_packaging"}
    return set()


def classify_changes(paths: Iterable[str]) -> dict:
    by_area: dict[str, list[str]] = {}
    unclassified: list[str] = []
    for path in sorted(paths):
        areas = classify_path(path)
        if not areas:
            unclassified.append(path)
        for area in sorted(areas):
            by_area.setdefault(area, []).append(path)
    return {
        "areas": sorted(by_area),
        "paths_by_area": by_area,
        "unclassified_paths": unclassified,
    }


def focused_tests(classification: dict, changed_paths: Iterable[str]) -> tuple[str, ...]:
    suites: set[str] = set()
    mapping = {
        "engine": {"tests/test_engine.py", "tests/test_hwaccel_probe.py", "tests/test_bug_sweep.py"},
        "ingestion": {"tests/test_config_and_presets.py", "tests/test_watchfolder.py"},
        "reports": {"tests/test_report.py"},
        "startup": {"tests/test_startup.py"},
        "ui": {
            "tests/test_design_system.py", "tests/test_feedback_regressions.py",
            "tests/test_gui.py", "tests/test_rendered_ui_contracts.py",
            "tests/test_usability.py",
        },
        "updater": {
            "tests/test_deltaupdate.py", "tests/test_release_channel.py",
            "tests/test_updater.py",
        },
        "workflow_packaging": {
            "tests/test_macos.py", "tests/test_packaging.py", "tests/test_startup.py",
            "tests/test_windows.py", "tests/test_workflow_contracts.py",
        },
        "developer_tools": {"tests/test_pre_push.py"},
        "runtime": {"tests/test_bug_sweep.py"},
    }
    for area in classification["areas"]:
        suites.update(mapping.get(area, set()))
    for path in changed_paths:
        if path.startswith("src/tests/test_") and path.endswith(".py"):
            suites.add(path.removeprefix("src/"))
    return tuple(sorted(path for path in suites if (SRC_ROOT / path).is_file()))


def stage_signature(stage: Stage, tree_fingerprint: str, tool_fingerprint: str) -> str:
    return sha256_json({
        "name": stage.name,
        "commands": [list(command) for command in stage.commands],
        "timeout_seconds": stage.timeout_seconds,
        "validator": stage.validator,
        "tree_fingerprint": tree_fingerprint,
        "tool_fingerprint": tool_fingerprint,
    })


def build_stages(profile: str, classification: dict, changed_paths: Iterable[str],
                 evidence_dir: Path, stage_timeout: int,
                 package_timeout: int) -> tuple[Stage, ...]:
    # Do not resolve this path: for symlink-based virtualenvs, sys.executable
    # is the environment identity that selects pyvenv.cfg and site-packages.
    py = sys.executable
    pytest = (py, "-m", "pytest", "-q", "--timeout=300")
    startup_dir = evidence_dir / "artifacts" / "source-startup"
    audit_dir = evidence_dir / "artifacts" / "rendered-audit"
    stages = [
        Stage("startup-preflight", ((
            py, "tools_dev/startup_smoke.py", "--executable", py,
            "--entrypoint", "main.py", "--artifacts-dir", str(startup_dir),
            "--label", "source-startup", "--qpa", "offscreen",
            "--timeout", "45", "--internal-timeout-ms", "15000",
        ),), 90, "startup-evidence"),
        Stage("version-consistency", ((py, "validate_version.py"),), 60),
        Stage("workflow-packaging-contracts", (pytest + (
            "tests/test_packaging.py", "tests/test_workflow_contracts.py",
            "tests/test_macos.py", "tests/test_windows.py", "tests/test_startup.py",
        ),), stage_timeout),
        Stage("compile-lint-diff", (
            (py, "-m", "compileall", "-q", "frameguard", "main.py",
             "validate_version.py", "write_build_stamp.py", "publish_release.py",
             "download_tools.py", "tools_dev"),
            (py, "-m", "pyflakes", "frameguard", "main.py", "validate_version.py",
             "write_build_stamp.py", "publish_release.py", "download_tools.py",
             "tools_dev"),
            ("git", "diff", "--check", "HEAD", "--"),
        ), stage_timeout),
    ]
    impacted = focused_tests(classification, changed_paths)
    if impacted:
        stages.append(Stage("impacted-suites", (pytest + impacted,), stage_timeout))
    else:
        stages.append(Stage("impacted-suites", (pytest + (
            "tests/test_packaging.py", "tests/test_workflow_contracts.py",
        ),), stage_timeout))

    if profile in LOCAL_READY_PROFILES:
        stages.extend([
            Stage("full-suite", (pytest + ("--durations=25",),), stage_timeout),
            Stage("timing-stress", (
                pytest + ("--durations=20", "tests/test_startup.py",
                          "tests/test_engine.py", "tests/test_watchfolder.py"),
                pytest + ("--durations=20", "tests/test_startup.py",
                          "tests/test_engine.py", "tests/test_watchfolder.py"),
            ), stage_timeout),
            Stage("rendered-audit", ((
                py, "tools_dev/gui_audit.py", "--matrix",
                "--json-dir", str(audit_dir),
                "--artifacts-dir", str(audit_dir / "screenshots"),
            ),), stage_timeout, "rendered-audit-evidence"),
        ])
    if profile == "package":
        stages.extend([
            Stage("official-macos-arm64-build", ((
                "bash", "build_mac.sh", "--skip-delta",
            ),), package_timeout, "native-arm64-build"),
            Stage("final-dmg-smoke", ((
                py, "tools_dev/macos_package_smoke.py",
                "--dmg", "Output/FrameGuard-arm64.dmg",
                "--expected-arch", "arm64",
                "--artifacts-dir", str(evidence_dir / "artifacts" / "final-dmg-smoke"),
                "--qpa", "cocoa",
            ),), 10 * 60, "macos-package-evidence"),
        ])
    return tuple(stages)


def validate_profile_environment(profile: str) -> None:
    if profile == "package" and (sys.platform != "darwin" or platform.machine() != "arm64"):
        raise PreflightError("package profile requires a native macOS arm64 host")


def _terminate(process: subprocess.Popen) -> None:
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
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_evidence_paths(stage: Stage, evidence_dir: Path) -> list[Path]:
    roots: list[Path] = []
    if stage.validator == "startup-evidence":
        roots.append(evidence_dir / "artifacts" / "source-startup")
    elif stage.validator == "rendered-audit-evidence":
        roots.append(evidence_dir / "artifacts" / "rendered-audit")
    elif stage.validator == "macos-package-evidence":
        roots.append(evidence_dir / "artifacts" / "final-dmg-smoke")
    elif stage.validator == "native-arm64-build":
        roots.append(SRC_ROOT / "Output" / "FrameGuard-arm64.dmg")
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(files)


def evidence_manifest(stage: Stage, evidence_dir: Path) -> list[dict]:
    return [
        {"path": str(path), "size_bytes": path.stat().st_size,
         "sha256": file_sha256(path)}
        for path in _stage_evidence_paths(stage, evidence_dir)
    ]


def _validate_artifacts(stage: Stage, evidence_dir: Path) -> list[str]:
    errors: list[str] = []
    if stage.validator == "startup-evidence":
        path = evidence_dir / "artifacts" / "source-startup" / "source-startup.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"missing or invalid startup evidence: {exc}"]
        if payload.get("status") != "passed":
            errors.append(f"startup evidence status is {payload.get('status')!r}")
    elif stage.validator == "rendered-audit-evidence":
        path = evidence_dir / "artifacts" / "rendered-audit" / "gui-audit-matrix.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"missing or invalid rendered audit evidence: {exc}"]
        summary = payload.get("summary") or {}
        if summary.get("completed_reports") != len(payload.get("scales") or []):
            errors.append("rendered audit did not write every scale report")
        if summary.get("errors"):
            errors.append(f"rendered audit reports {summary['errors']} errors")
        if summary.get("warnings"):
            errors.append(f"rendered audit reports {summary['warnings']} warnings")
        if any(code != 0 for code in summary.get("worker_return_codes", [])):
            errors.append("rendered audit has a nonzero worker return code")
    elif stage.validator == "macos-package-evidence":
        path = evidence_dir / "artifacts" / "final-dmg-smoke" / "macos-package-smoke.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"missing or invalid DMG smoke evidence: {exc}"]
        if payload.get("status") != "passed":
            errors.append(f"DMG smoke status is {payload.get('status')!r}")
        if payload.get("mounted_preflight") is not True:
            errors.append("mounted DMG startup preflight is missing")
        if payload.get("copied_preflight") is not True:
            errors.append("copied DMG startup preflight is missing")
    elif stage.validator == "native-arm64-build":
        dmg = SRC_ROOT / "Output" / "FrameGuard-arm64.dmg"
        if not dmg.is_file() or dmg.stat().st_size == 0:
            errors.append(f"official build did not produce {dmg}")
    return errors


def run_stage(stage: Stage, evidence_dir: Path, snapshot: Snapshot,
              tools: dict) -> dict:
    logs_dir = evidence_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{stage.name}.log"
    started = utc_now()
    started_mono = time.monotonic()
    deadline = started_mono + stage.timeout_seconds
    executions = []
    status = PASSING_STAGE_STATUS
    failure = None
    validation_errors: list[str] = []

    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("FRAMEGUARD_TEST_MODE", "1")
    env["PYTHONUNBUFFERED"] = "1"

    with log_path.open("w", encoding="utf-8", errors="backslashreplace") as log:
        log.write(f"stage: {stage.name}\nstarted_at: {started}\n")
        log.write(f"tree_fingerprint: {snapshot.tree_fingerprint}\n")
        log.write(f"tool_fingerprint: {tools['fingerprint']}\n")
        for index, command in enumerate(stage.commands, 1):
            command_started = utc_now()
            remaining = deadline - time.monotonic()
            rendered = shlex.join(command)
            log.write(f"\n--- command {index}/{len(stage.commands)} ---\n$ {rendered}\n")
            log.flush()
            if remaining <= 0:
                status = "timed_out"
                failure = f"stage external timeout after {stage.timeout_seconds}s"
                break
            creationflags = 0
            popen_kwargs = {}
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            command_start_mono = time.monotonic()
            try:
                process = subprocess.Popen(
                    command, cwd=SRC_ROOT, env=env, stdout=log,
                    stderr=subprocess.STDOUT, text=True,
                    creationflags=creationflags, **popen_kwargs,
                )
            except OSError as exc:
                status = "failed"
                failure = f"could not start command: {exc}"
                executions.append({
                    "command": list(command), "command_text": rendered,
                    "started_at": command_started, "ended_at": utc_now(),
                    "duration_seconds": round(time.monotonic() - command_start_mono, 3),
                    "status": status, "returncode": None,
                })
                break
            timed_out = False
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate(process)
                returncode = process.returncode
            command_status = "timed_out" if timed_out else (
                "passed" if returncode == 0 else "failed")
            executions.append({
                "command": list(command), "command_text": rendered,
                "started_at": command_started, "ended_at": utc_now(),
                "duration_seconds": round(time.monotonic() - command_start_mono, 3),
                "status": command_status, "returncode": returncode,
            })
            if timed_out:
                status = "timed_out"
                failure = f"stage external timeout after {stage.timeout_seconds}s"
                break
            if returncode != 0:
                status = "failed"
                failure = f"command {index} exited with {returncode}"
                break
        if status == PASSING_STAGE_STATUS:
            validation_errors = _validate_artifacts(stage, evidence_dir)
            if validation_errors:
                status = "failed"
                failure = "; ".join(validation_errors)
                log.write("\n--- evidence validation failed ---\n")
                for error in validation_errors:
                    log.write(f"- {error}\n")

    validator_result_status = (
        "failed" if validation_errors else
        "passed" if status == PASSING_STAGE_STATUS else
        "not_run"
    )
    current = capture_snapshot()
    if current != snapshot:
        status = "stale"
        failure = "source tree changed while the stage was running"
    ended = utc_now()
    manifest = evidence_manifest(stage, evidence_dir)
    return {
        "name": stage.name,
        "commands": [list(command) for command in stage.commands],
        "timeout_seconds": stage.timeout_seconds,
        "validator": stage.validator,
        "signature": stage_signature(
            stage, snapshot.tree_fingerprint, tools["fingerprint"]),
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": round(time.monotonic() - started_mono, 3),
        "log": str(log_path),
        "log_sha256": file_sha256(log_path),
        "evidence_files": manifest,
        "validator_result": {
            "validator": stage.validator,
            "status": validator_result_status,
            "errors": list(validation_errors),
        },
        "status": status,
        "failure": failure,
        "executions": executions,
    }


def reusable_stage(previous: dict, stage: Stage, snapshot: Snapshot,
                   tools: dict, evidence_dir: Path) -> tuple[bool, str | None]:
    if previous.get("status") != PASSING_STAGE_STATUS:
        return False, None
    expected = stage_signature(stage, snapshot.tree_fingerprint, tools["fingerprint"])
    if previous.get("signature") != expected:
        return False, "stage signature is stale (tree, tools, timeout, validator, or command changed)"
    if previous.get("commands") != [list(command) for command in stage.commands]:
        return False, "stage command evidence does not exactly match"
    log = previous.get("log")
    expected_log = evidence_dir / "logs" / f"{stage.name}.log"
    if not isinstance(log, str) or Path(log) != expected_log:
        return False, "stage log path is stale or unexpected"
    if expected_log.is_symlink() or not expected_log.is_file():
        return False, "stage log is missing or symlinked"
    if previous.get("log_sha256") != file_sha256(expected_log):
        return False, "stage log hash is stale"
    recorded_files = previous.get("evidence_files")
    if not isinstance(recorded_files, list):
        return False, "stage evidence manifest is missing"
    for record in recorded_files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            return False, "stage evidence manifest contains a malformed record"
        path = Path(record["path"])
        if path.is_symlink() or not path.is_file():
            return False, f"stage evidence file is missing or symlinked: {path}"
        if record.get("size_bytes") != path.stat().st_size:
            return False, f"stage evidence size is stale: {path}"
        if record.get("sha256") != file_sha256(path):
            return False, f"stage evidence hash is stale: {path}"
    if evidence_manifest(stage, evidence_dir) != recorded_files:
        return False, "stage evidence manifest has missing or unexpected files"
    expected_validator_result = {
        "validator": stage.validator,
        "status": "passed",
        "errors": [],
    }
    if previous.get("validator_result") != expected_validator_result:
        return False, "stage validator result is missing, failed, or tampered"
    validation_errors = _validate_artifacts(stage, evidence_dir)
    if validation_errors:
        return False, "stage evidence no longer validates: " + "; ".join(validation_errors)
    return True, None


AUTHORIZATION_STAGE_FIELDS = (
    "name", "commands", "timeout_seconds", "validator", "signature",
    "started_at", "ended_at", "duration_seconds", "log", "log_sha256",
    "evidence_files", "validator_result", "status", "failure", "executions",
)


def authorization_evidence_payload(stages: Iterable[Stage],
                                   results: Iterable[dict]) -> dict:
    stage_list = tuple(stages)
    result_list = tuple(results)
    expected_names = [stage.name for stage in stage_list]
    blockers = stage_blockers(expected_names, result_list)
    if blockers:
        raise PreflightError(
            "cannot build authorization evidence: " + "; ".join(blockers))
    by_name = {record["name"]: record for record in result_list}
    canonical_stages = []
    for stage in stage_list:
        record = by_name[stage.name]
        missing = [field for field in AUTHORIZATION_STAGE_FIELDS if field not in record]
        if missing:
            raise PreflightError(
                f"stage {stage.name} lacks authorization fields: {', '.join(missing)}")
        canonical = {
            key: value for key, value in record.items() if key != "reused"
        }
        manifest = canonical["evidence_files"]
        if (not isinstance(manifest, list) or
                any(not isinstance(item, dict) or
                    not isinstance(item.get("path"), str) for item in manifest)):
            raise PreflightError(
                f"stage {stage.name} has a malformed evidence manifest")
        canonical["evidence_files"] = sorted(
            manifest, key=lambda item: item["path"])
        canonical_stages.append(canonical)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage_order": expected_names,
        "stages": canonical_stages,
    }


def authorization_evidence_digest(stages: Iterable[Stage],
                                  results: Iterable[dict]) -> str:
    return sha256_json(authorization_evidence_payload(stages, results))


def revalidate_authorization_evidence(
        stages: Iterable[Stage], results: Iterable[dict], snapshot: Snapshot,
        tools: dict, evidence_dir: Path, expected_digest: str,
        phase: str) -> tuple[bool, dict]:
    stage_list = tuple(stages)
    result_list = tuple(results)
    failures = stage_blockers(
        [stage.name for stage in stage_list], result_list)
    by_name = {
        record.get("name"): record for record in result_list
        if isinstance(record, dict) and isinstance(record.get("name"), str)
    }
    if not failures:
        for stage in stage_list:
            reusable, reason = reusable_stage(
                by_name[stage.name], stage, snapshot, tools, evidence_dir)
            if not reusable:
                failures.append(
                    f"stage {stage.name} failed final evidence validation: "
                    f"{reason or 'not reusable'}")
    observed_digest = None
    if not failures:
        try:
            observed_digest = authorization_evidence_digest(stage_list, result_list)
        except PreflightError as exc:
            failures.append(str(exc))
    if observed_digest is not None and observed_digest != expected_digest:
        failures.append(
            "authorization evidence digest changed: "
            f"expected {expected_digest}, observed {observed_digest}")
    return not failures, {
        "phase": phase,
        "checked_at": utc_now(),
        "status": "passed" if not failures else "stale",
        "expected_digest": expected_digest,
        "observed_digest": observed_digest,
        "failures": failures,
    }


def blocked_stage(stage: Stage, snapshot: Snapshot, tools: dict,
                  reason: str) -> dict:
    now = utc_now()
    return {
        "name": stage.name,
        "commands": [list(command) for command in stage.commands],
        "timeout_seconds": stage.timeout_seconds,
        "validator": stage.validator,
        "signature": stage_signature(
            stage, snapshot.tree_fingerprint, tools["fingerprint"]),
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0.0,
        "log": None,
        "log_sha256": None,
        "evidence_files": [],
        "validator_result": {
            "validator": stage.validator,
            "status": "not_run",
            "errors": [reason],
        },
        "status": "blocked",
        "failure": reason,
        "executions": [],
    }


def validate_resume_summary(previous: dict, evidence_dir: Path, profile: str,
                            snapshot: Snapshot, tools: dict,
                            stages: Iterable[Stage]) -> dict[str, dict]:
    """Validate the complete resume record set before indexing any stage."""
    stage_list = tuple(stages)
    expected_names = [stage.name for stage in stage_list]
    errors: list[str] = []
    if not isinstance(previous.get("run_id"), str) or not previous["run_id"].strip():
        errors.append("run_id is missing")
    elif previous["run_id"] != evidence_dir.name:
        errors.append("run_id does not match the evidence directory")
    if previous.get("profile") != profile:
        errors.append("profile does not match")
    if previous.get("evidence_dir") != str(evidence_dir):
        errors.append("evidence directory identity does not match")
    if previous.get("snapshot") != snapshot_payload(snapshot):
        errors.append("full source snapshot is stale or tampered")
    if previous.get("tools") != tools:
        errors.append("full tool/platform evidence is stale or tampered")
    if previous.get("expected_stages") != expected_names:
        errors.append("expected stage plan does not exactly match")
    if previous.get("final_status") not in FINAL_STATUSES:
        errors.append("final_status is missing or unclassified")

    records = previous.get("stages")
    if not isinstance(records, list):
        errors.append("stage records are missing or not a list")
        records = []
    names: list[str] = []
    record_by_unique_name: dict[str, dict] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"stage record {index} is not an object")
            continue
        name = record.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"stage record {index} has no valid name")
            continue
        names.append(name)
        if name in record_by_unique_name:
            errors.append(f"duplicate stage record: {name}")
        else:
            record_by_unique_name[name] = record

    for name in expected_names:
        if names.count(name) == 0:
            errors.append(f"missing stage record: {name}")
    for name in sorted(set(names) - set(expected_names)):
        errors.append(f"unexpected stage record: {name}")

    stages_by_name = {stage.name: stage for stage in stage_list}
    for name, record in record_by_unique_name.items():
        stage = stages_by_name.get(name)
        if stage is None:
            continue
        if record.get("commands") != [list(command) for command in stage.commands]:
            errors.append(f"stage command evidence does not match: {name}")
        if record.get("timeout_seconds") != stage.timeout_seconds:
            errors.append(f"stage timeout evidence does not match: {name}")
        if record.get("validator") != stage.validator:
            errors.append(f"stage validator evidence does not match: {name}")
        expected_signature = stage_signature(
            stage, snapshot.tree_fingerprint, tools["fingerprint"])
        if record.get("signature") != expected_signature:
            errors.append(f"stage signature is stale or tampered: {name}")
        status = record.get("status")
        if status != PASSING_STAGE_STATUS and status not in NON_PASSING_STAGE_STATUSES:
            errors.append(f"stage status is unclassified: {name}={status!r}")
        if status == PASSING_STAGE_STATUS:
            expected_log = evidence_dir / "logs" / f"{name}.log"
            if record.get("log") != str(expected_log):
                errors.append(f"stage log path does not match: {name}")
            expected_validator_result = {
                "validator": stage.validator,
                "status": "passed",
                "errors": [],
            }
            if record.get("validator_result") != expected_validator_result:
                errors.append(f"stage validator result does not match: {name}")

    try:
        computed_digest = authorization_evidence_digest(stage_list, records)
    except PreflightError:
        computed_digest = None
    if previous.get("authorization_evidence_digest") != computed_digest:
        errors.append("authorization evidence digest is stale or tampered")

    if errors:
        raise PreflightError("invalid resume summary: " + "; ".join(errors))
    return {name: record_by_unique_name[name] for name in expected_names}


def check_snapshot(snapshot: Snapshot, phase: str) -> tuple[bool, dict]:
    checked_at = utc_now()
    try:
        observed = capture_snapshot()
    except PreflightError as exc:
        return False, {
            "phase": phase,
            "checked_at": checked_at,
            "status": "failed",
            "expected": snapshot_payload(snapshot),
            "observed": None,
            "failure": f"could not recapture source snapshot: {exc}",
        }
    matches = observed == snapshot
    return matches, {
        "phase": phase,
        "checked_at": checked_at,
        "status": "passed" if matches else "stale",
        "expected": snapshot_payload(snapshot),
        "observed": snapshot_payload(observed),
        "failure": None if matches else f"source snapshot changed at {phase}",
    }


def stage_blockers(expected_names: Iterable[str], results: Iterable[dict]) -> list[str]:
    """Detect missing, stale, warning, skipped, unclassified, and failed stages."""
    expected = tuple(expected_names)
    by_name: dict[str, dict] = {}
    blockers: list[str] = []
    for result in results:
        name = result.get("name")
        if not isinstance(name, str) or not name:
            blockers.append("unclassified stage result")
            continue
        if name in by_name:
            blockers.append(f"duplicate stage result: {name}")
            continue
        by_name[name] = result
        status = result.get("status")
        if status != PASSING_STAGE_STATUS:
            rendered = status if status in NON_PASSING_STAGE_STATUSES else f"unclassified status {status!r}"
            blockers.append(f"stage {name}: {rendered}")
    for name in expected:
        if name not in by_name:
            blockers.append(f"missing stage: {name}")
    for name in by_name:
        if name not in expected:
            blockers.append(f"unclassified stage: {name}")
    return blockers


def review_request(snapshot: Snapshot, tools: dict, run_id: str,
                   internal_completed_at: str,
                   authorization_digest: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "tree_fingerprint": snapshot.tree_fingerprint,
        "tool_fingerprint": tools["fingerprint"],
        "internal_completed_at": internal_completed_at,
        "authorization_evidence_digest": authorization_digest,
        "required_review_evidence": {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "tree_fingerprint": snapshot.tree_fingerprint,
            "tool_fingerprint": tools["fingerprint"],
            "authorization_evidence_digest": authorization_digest,
            "decision": "approved",
            "independent": True,
            "reviewer": "REPLACE_WITH_REVIEWER_IDENTITY",
            "reviewed_at": "REPLACE_WITH_UTC_TIMESTAMP",
            "summary": "REPLACE_WITH_REVIEW_SCOPE_AND_FINDINGS",
        },
    }


def _normalize_review_text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _review_text_errors(payload: dict) -> list[str]:
    errors = []
    reviewer = _normalize_review_text(payload.get("reviewer"))
    summary = _normalize_review_text(payload.get("summary"))
    reviewer_sentinels = {
        "replace_with_reviewer_identity", "reviewer", "reviewer name",
        "your name", "name", "todo", "tbd", "n/a", "unknown",
        "reviewer@example.com", "example@example.com",
    }
    summary_sentinels = {
        "replace_with_review_scope_and_findings", "reviewed", "approved",
        "looks good", "lgtm", "no issues", "todo", "tbd", "n/a",
    }
    reviewer_compact = "".join(
        char for char in reviewer.casefold() if char.isalnum())
    summary_compact = "".join(
        char for char in summary.casefold() if char.isalnum())
    if not reviewer:
        errors.append("review reviewer is missing")
    elif (reviewer.casefold() in reviewer_sentinels or
          reviewer_compact == "replacewithrevieweridentity" or
          "replace_with" in reviewer.casefold() or
          len(reviewer) < 4 or not any(char.isalnum() for char in reviewer)):
        errors.append("review reviewer is a template or non-substantive identity")
    if not summary:
        errors.append("review summary is missing")
    elif (summary.casefold() in summary_sentinels or
          summary_compact == "replacewithreviewscopeandfindings" or
          "replace_with" in summary.casefold() or
          len(summary) < 40 or len(summary.split()) < 6):
        errors.append("review summary is template or non-substantive content")
    return errors


def validate_review(path: Path | None, request: dict,
                    max_age_hours: float, now: datetime | None = None) -> dict:
    if path is None:
        return {"status": "missing", "path": None,
                "failure": "review metadata was not supplied"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "failed", "path": str(path),
                "failure": f"cannot read review evidence: {exc}"}
    errors = []
    for field in (
            "run_id", "tree_fingerprint", "tool_fingerprint",
            "authorization_evidence_digest"):
        if payload.get(field) != request.get(field):
            errors.append(f"review {field} does not match this run")
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("review schema_version is unsupported")
    if payload.get("decision") != "approved":
        errors.append("review decision is not approved")
    if payload.get("independent") is not True:
        errors.append("review must explicitly attest independent=true")
    errors.extend(_review_text_errors(payload))
    now = now or datetime.now(timezone.utc)
    try:
        reviewed_at = parse_utc(payload.get("reviewed_at"))
        completed_at = parse_utc(request["internal_completed_at"])
        if reviewed_at < completed_at:
            errors.append("review is stale: it predates completion of internal gates")
        if reviewed_at > now.replace(microsecond=999999):
            errors.append("review timestamp is in the future")
        if (now - reviewed_at).total_seconds() > max_age_hours * 3600:
            errors.append(f"review is stale: older than {max_age_hours:g} hours")
    except (TypeError, ValueError) as exc:
        errors.append(f"reviewed_at is invalid: {exc}")
    return {
        "status": "passed" if not errors else "failed",
        "path": str(path),
        "failure": "; ".join(errors) if errors else None,
        "evidence": payload,
        "normalized_reviewer": _normalize_review_text(payload.get("reviewer")),
        "normalized_summary": _normalize_review_text(payload.get("summary")),
    }


def authorization_status(profile: str, expected_names: Iterable[str],
                         results: Iterable[dict], classification: dict,
                         review: dict) -> tuple[str, list[str]]:
    blockers = stage_blockers(expected_names, results)
    for path in classification.get("unclassified_paths", []):
        blockers.append(f"unclassified changed path: {path}")
    if blockers:
        return "failed", blockers
    if profile not in LOCAL_READY_PROFILES:
        return "incomplete", ["fast profile is diagnostic and cannot establish local readiness"]
    if review.get("status") == "missing":
        return "incomplete", [review.get("failure") or "review metadata is missing"]
    if review.get("status") != "passed":
        return "failed", [review.get("failure") or "review metadata failed"]
    return "local-ready", []


def render_summary(summary: dict) -> str:
    snapshot = summary["snapshot"]
    lines = [
        "FrameGuard pre-push evidence",
        "=============================",
        f"Run:          {summary['run_id']}",
        f"Profile:      {summary['profile']}",
        f"Final status: {summary['final_status'].upper()}",
        f"HEAD:         {snapshot['head']}",
        f"Dirty diff:   {snapshot['dirty_diff_sha256']}",
        f"Tree:         {snapshot['tree_fingerprint']}",
        f"Tools:        {summary['tools']['fingerprint']}",
        f"Local evidence digest: {summary.get('authorization_evidence_digest') or '<unavailable>'}",
        f"Evidence:     {summary['evidence_dir']}",
        "",
        "Impacts: " + (", ".join(summary["classification"]["areas"]) or "none"),
    ]
    unclassified = summary["classification"]["unclassified_paths"]
    if unclassified:
        lines.append("Unclassified paths: " + ", ".join(unclassified))
    lines.extend(["", "Stages:"])
    for stage in summary.get("stages", []):
        duration = stage.get("duration_seconds", 0)
        reused = " [reused]" if stage.get("reused") else ""
        lines.append(
            f"  {stage.get('status', 'missing').upper():12} "
            f"{stage.get('name', '<unnamed>'):34} {duration:9.3f}s{reused}"
        )
        lines.append(f"               log: {stage.get('log', '<missing>')}")
        for command in stage.get("commands", []):
            lines.append(f"               cmd: {shlex.join(command)}")
    review = summary.get("review", {})
    lines.extend([
        "",
        f"Review metadata:    {review.get('status', 'missing').upper()}",
    ])
    if review.get("path"):
        lines.append(f"Review evidence:    {review['path']}")
    if summary.get("snapshot_checks"):
        lines.append("Snapshot checks:")
        for check in summary["snapshot_checks"]:
            lines.append(
                f"  {check.get('status', 'missing').upper():12} "
                f"{check.get('phase', '<unnamed>')} at {check.get('checked_at', '<missing>')}"
            )
    if summary.get("evidence_checks"):
        lines.append("Local evidence checks:")
        for check in summary["evidence_checks"]:
            lines.append(
                f"  {check.get('status', 'missing').upper():12} "
                f"{check.get('phase', '<unnamed>')} "
                f"digest={check.get('observed_digest') or '<unavailable>'}"
            )
    if summary.get("blockers"):
        lines.append("Blockers:")
        lines.extend(f"  - {item}" for item in summary["blockers"])
    lines.append("")
    if summary["final_status"] == "local-ready":
        lines.append("LOCAL-READY: local gates and review metadata match this host fingerprint.")
        lines.append("RELEASE NOT AUTHORIZED: require a successful exact-SHA native candidate run.")
    else:
        lines.append("NOT LOCAL-READY: fail-closed; do not push from this evidence.")
    return "\n".join(lines) + "\n"


def _new_evidence_dir(root: Path, snapshot: Snapshot) -> tuple[str, Path]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + snapshot.tree_fingerprint[:12]
    evidence_dir = root / run_id
    try:
        evidence_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise PreflightError(f"fresh evidence directory already exists: {evidence_dir}") from exc
    return run_id, evidence_dir


def _load_resume(path: Path) -> dict:
    summary_path = path / "summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot resume without valid {summary_path}: {exc}") from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise PreflightError("resume evidence uses an unsupported schema")
    return payload


def run(args: argparse.Namespace) -> int:
    validate_profile_environment(args.profile)
    snapshot = capture_snapshot()
    tools = capture_tools()
    classification = classify_changes(snapshot.changed_paths)

    if args.resume:
        evidence_dir = args.resume.resolve()
        previous_summary = _load_resume(evidence_dir)
        run_id = previous_summary.get("run_id")
    else:
        root = args.evidence_root.resolve() if args.evidence_root else (
            REPO_ROOT / ".git" / "frameguard-prepush")
        run_id, evidence_dir = _new_evidence_dir(root, snapshot)
        previous_summary = None

    stages = build_stages(
        args.profile, classification, snapshot.changed_paths, evidence_dir,
        args.stage_timeout, args.package_timeout,
    )
    expected_names = [stage.name for stage in stages]
    previous_by_name = validate_resume_summary(
        previous_summary, evidence_dir, args.profile, snapshot, tools, stages,
    ) if previous_summary else {}
    results = []
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "profile": args.profile,
        "scope": "local",
        "release_authorized": False,
        "created_at": (previous_summary or {}).get("created_at", utc_now()),
        "updated_at": utc_now(),
        "evidence_dir": str(evidence_dir),
        "snapshot": snapshot_payload(snapshot),
        "tools": tools,
        "classification": classification,
        "expected_stages": expected_names,
        "stages": results,
        "review": {"status": "missing", "path": None},
        "authorization_evidence_digest": None,
        "snapshot_checks": [],
        "evidence_checks": [],
        "final_status": "incomplete",
        "blockers": ["preflight is still running"],
    }
    summary_path = evidence_dir / "summary.json"
    readable_path = evidence_dir / "summary.txt"
    atomic_write_json(summary_path, summary)
    atomic_write_text(readable_path, render_summary(summary))

    if classification["unclassified_paths"]:
        summary["final_status"] = "failed"
        summary["blockers"] = [
            f"unclassified changed path: {path}"
            for path in classification["unclassified_paths"]
        ]
    else:
        for stage_index, stage in enumerate(stages):
            previous = previous_by_name.get(stage.name)
            if previous:
                reusable, stale_reason = reusable_stage(
                    previous, stage, snapshot, tools, evidence_dir)
                if stale_reason:
                    raise PreflightError(
                        f"cannot resume {stage.name}: {stale_reason}")
                if reusable:
                    reused = dict(previous)
                    reused["reused"] = True
                    results.append(reused)
                    continue
            result = run_stage(stage, evidence_dir, snapshot, tools)
            results.append(result)
            summary["updated_at"] = utc_now()
            summary["stages"] = results
            summary["blockers"] = stage_blockers(expected_names, results)
            atomic_write_json(summary_path, summary)
            atomic_write_text(readable_path, render_summary(summary))
            if result["status"] != PASSING_STAGE_STATUS:
                reason = f"blocked by nonpassing stage {stage.name}"
                results.extend(
                    blocked_stage(remaining, snapshot, tools, reason)
                    for remaining in stages[stage_index + 1:]
                )
                summary["stages"] = results
                summary["blockers"] = stage_blockers(expected_names, results)
                atomic_write_json(summary_path, summary)
                atomic_write_text(readable_path, render_summary(summary))
                break

    passed_end_times = [
        stage["ended_at"] for stage in results
        if stage.get("status") == PASSING_STAGE_STATUS and stage.get("ended_at")
    ]
    internal_completed_at = max(passed_end_times) if passed_end_times else utc_now()
    try:
        authorization_digest = authorization_evidence_digest(stages, results)
    except PreflightError:
        authorization_digest = None
    summary["authorization_evidence_digest"] = authorization_digest
    request = review_request(
        snapshot, tools, run_id, internal_completed_at, authorization_digest)
    request_path = evidence_dir / "review-request.json"
    atomic_write_json(request_path, request)
    review_path = args.review_evidence.resolve() if args.review_evidence else None
    review = validate_review(review_path, request, args.review_max_age_hours)

    pre_authorization_ok, pre_authorization_check = check_snapshot(
        snapshot, "pre-authorization")
    summary["snapshot_checks"].append(pre_authorization_check)
    pre_authorization_evidence_ok, pre_authorization_evidence_check = (
        revalidate_authorization_evidence(
            stages, results, snapshot, tools, evidence_dir,
            authorization_digest or "<unavailable>", "pre-authorization"))
    summary["evidence_checks"].append(pre_authorization_evidence_check)
    final_status, blockers = authorization_status(
        args.profile, expected_names, results, classification, review)
    if not pre_authorization_ok:
        final_status = "failed"
        blockers.append(pre_authorization_check["failure"])
    if not pre_authorization_evidence_ok:
        final_status = "failed"
        blockers.extend(pre_authorization_evidence_check["failures"])
    if final_status not in FINAL_STATUSES:
        raise PreflightError(f"internal error: unknown final status {final_status}")
    summary.update({
        "updated_at": utc_now(),
        "internal_completed_at": internal_completed_at,
        "review_request": str(request_path),
        "review": review,
        "final_status": final_status,
        "blockers": blockers,
        "stages": results,
    })

    if final_status == "local-ready":
        pre_exit_ok, pre_exit_check = check_snapshot(snapshot, "pre-exit")
        summary["snapshot_checks"].append(pre_exit_check)
        pre_exit_evidence_ok, pre_exit_evidence_check = (
            revalidate_authorization_evidence(
                stages, results, snapshot, tools, evidence_dir,
                authorization_digest, "pre-exit"))
        summary["evidence_checks"].append(pre_exit_evidence_check)
        if not pre_exit_ok:
            final_status = "failed"
            blockers.append(pre_exit_check["failure"])
        if not pre_exit_evidence_ok:
            final_status = "failed"
            blockers.extend(pre_exit_evidence_check["failures"])
        if final_status == "failed":
            summary.update({
                "updated_at": utc_now(),
                "final_status": final_status,
                "blockers": blockers,
            })

    atomic_write_json(summary_path, summary)
    rendered = render_summary(summary)
    atomic_write_text(readable_path, rendered)
    print(rendered, end="")
    return 0 if final_status == "local-ready" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("fast", "full", "package"), default="full")
    parser.add_argument("--evidence-root", type=Path,
                        help="new-run evidence root (default: .git/frameguard-prepush)")
    parser.add_argument("--resume", type=Path,
                        help="resume one evidence directory after exact fingerprint checks")
    parser.add_argument("--review-evidence", type=Path,
                        help="local review metadata JSON matching review-request.json")
    parser.add_argument("--review-max-age-hours", type=float, default=24.0)
    parser.add_argument("--stage-timeout", type=int, default=DEFAULT_STAGE_TIMEOUT,
                        help="external timeout in seconds for long non-package stages")
    parser.add_argument("--package-timeout", type=int, default=DEFAULT_PACKAGE_TIMEOUT,
                        help="external timeout in seconds for the clean macOS build")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stage_timeout <= 0 or args.package_timeout <= 0:
        raise SystemExit("timeouts must be positive")
    if args.review_max_age_hours <= 0:
        raise SystemExit("--review-max-age-hours must be positive")
    if args.resume and args.evidence_root:
        raise SystemExit("--resume and --evidence-root are mutually exclusive")
    try:
        return run(args)
    except PreflightError as exc:
        print(f"PRE-PUSH PREFLIGHT FAILED CLOSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
