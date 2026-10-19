"""End-to-end contracts for the real FrameGuard startup path."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "main.py"


def _run_startup(tmp_path, *extra, timeout_ms=4000, cli_paths=(), qpa="offscreen"):
    run_root = tmp_path / f"run-{len(list(tmp_path.glob('run-*')))}"
    profile = run_root / "profile"
    report = run_root / "startup.json"
    run_root.mkdir(parents=True)
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": qpa,
        "FRAMEGUARD_TEST_MODE": "1",
        "HOME": str(profile),
        "USERPROFILE": str(profile),
        "XDG_CONFIG_HOME": str(profile / "config"),
        "XDG_DATA_HOME": str(profile / "data"),
        "XDG_CACHE_HOME": str(profile / "cache"),
        "APPDATA": str(profile / "appdata"),
        "LOCALAPPDATA": str(profile / "localappdata"),
    })
    challenge = secrets.token_urlsafe(24)
    command = [
        sys.executable,
        str(MAIN),
        "--startup-preflight",
        str(report),
        "--startup-preflight-profile",
        str(profile),
        "--startup-timeout-ms",
        str(timeout_ms),
        "--startup-challenge",
        challenge,
        *map(str, extra),
        *map(str, cli_paths),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=max(8, timeout_ms / 1000 + 5),
        )
    except subprocess.TimeoutExpired as exc:
        completed = SimpleNamespace(
            returncode=-999,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + "\nEXTERNAL TEST TIMEOUT",
        )
    completed.startup_challenge = challenge
    evidence = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    return completed, evidence, report


def _assert_passed(completed, evidence):
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert evidence is not None
    assert evidence["status"] == "passed", evidence
    assert evidence["event_loop"]["turn_observed"] is True
    assert evidence["event_loop"]["deferred_show_confirmed"] is True


def test_evidence_accepts_authenticated_child_of_spawned_launcher():
    from tools_dev.startup_smoke import REQUIRED_CHECKPOINTS, _validate

    launched_ns = 100
    evidence = {
        "challenge": "current-run",
        "process_id": 222,
        "parent_process_id": 111,
        "started_wall_time_ns": launched_ns,
        "status": "passed",
        "frozen": False,
        "event_loop": {
            "turn_observed": True,
            "deferred_show_confirmed": True,
        },
        "checkpoints": [{"name": name} for name in REQUIRED_CHECKPOINTS],
        "executable": "python.exe",
        "os": "Windows",
        "architecture": "AMD64",
        "python": "3.12",
        "qt_version": "6.9",
        "qpa": "windows",
        "profile_path": "profile",
        "theme": "default",
        "duration_ms": 1,
    }
    assert _validate(evidence, False, "current-run", 111, launched_ns) == []


def test_real_source_startup_preflight_is_bounded_and_machine_readable(tmp_path):
    completed, evidence, report = _run_startup(tmp_path)
    _assert_passed(completed, evidence)

    assert evidence["schema_version"] == 2
    assert evidence["challenge"] == completed.startup_challenge
    assert evidence["process_id"] > 0
    assert evidence["process_id"] != os.getpid()
    assert evidence["parent_process_id"] > 0
    assert evidence["started_wall_time_ns"] > 0
    assert evidence["frozen"] is False
    assert evidence["executable"] == sys.executable
    assert evidence["profile_path"].endswith("profile")
    assert evidence["python"]
    assert evidence["qt_version"]
    assert evidence["qpa"] == "offscreen"
    assert evidence["architecture"]
    assert evidence["duration_ms"] >= 0
    assert evidence["theme"]
    names = [item["name"] for item in evidence["checkpoints"]]
    for required in (
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
    ):
        assert required in names
    assert names == list(dict.fromkeys(names)), "startup checkpoints must be unique"
    assert not list(report.parent.glob(f".{report.name}.*.tmp"))


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (("--startup-preflight-theme", "Clean Light"), "Clean Light"),
        (("--startup-preflight-legacy-theme", "Meta Dark"), "Blue Hour"),
    ],
)
def test_saved_and_legacy_themes_use_the_real_startup_resolver(tmp_path, options, expected):
    completed, evidence, _ = _run_startup(tmp_path, *options)
    _assert_passed(completed, evidence)
    assert evidence["theme"] == expected


def test_invalid_saved_theme_falls_back_without_startup_failure(tmp_path):
    completed, evidence, _ = _run_startup(
        tmp_path, "--startup-preflight-theme", "Removed Theme")
    _assert_passed(completed, evidence)
    assert evidence["theme"] != "Removed Theme"


def test_cli_and_cold_warm_file_open_paths_are_observed(tmp_path):
    preset = tmp_path / "team.fg"
    preset.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.fg"

    completed, evidence, _ = _run_startup(
        tmp_path,
        "--startup-file-open-cold", str(preset),
        "--startup-file-open-cold", str(preset),
        "--startup-file-open-cold", str(missing),
        "--startup-file-open-warm", str(preset),
        cli_paths=(preset, preset, missing),
    )
    _assert_passed(completed, evidence)
    paths = evidence["paths"]
    assert paths["cli_requested"] == [str(preset), str(preset)]
    assert paths["cli_delivered"] == [str(preset), str(preset)]
    assert paths["file_open_cold_requested"] == [str(preset), str(preset), str(missing)]
    assert paths["file_open_warm_requested"] == [str(preset)]
    assert paths["file_open_delivered"] == [str(preset), str(preset)]


@pytest.mark.parametrize("checkpoint", ["theme_resolved", "comboboxes_styled"])
def test_injected_startup_fault_never_produces_a_passing_report(tmp_path, checkpoint):
    completed, evidence, _ = _run_startup(
        tmp_path, "--startup-fail-at", checkpoint)
    assert completed.returncode != 0
    assert evidence is not None
    assert evidence["status"] == "failed"
    assert evidence["exception"]["type"] == "RuntimeError"
    assert checkpoint in evidence["exception"]["message"]


@pytest.mark.skipif(sys.platform != "darwin", reason="native Cocoa requires macOS")
def test_real_source_startup_preflight_runs_on_native_cocoa(tmp_path):
    completed, evidence, _ = _run_startup(
        tmp_path, qpa="cocoa", timeout_ms=15000)
    _assert_passed(completed, evidence)
    assert evidence["qpa"] == "cocoa"


def test_external_smoke_watchdog_terminates_an_unresponsive_process(tmp_path):
    sleeper = tmp_path / "sleep_forever.py"
    sleeper.write_text(
        "import time\nwhile True:\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "tools_dev" / "startup_smoke.py"),
            "--executable", sys.executable,
            "--entrypoint", str(sleeper),
            "--artifacts-dir", str(artifacts),
            "--timeout", "0.2",
            "--internal-timeout-ms", "500",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "external timeout" in result.stderr
    assert (artifacts / "startup.stderr.log").is_file()


def test_smoke_rejects_stale_passing_evidence_from_zero_exit(tmp_path):
    fake = tmp_path / "zero_exit.py"
    fake.write_text("raise SystemExit(0)\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    report = artifacts / "startup.json"
    required = (
        "application_created", "file_open_filter_installed", "theme_resolved",
        "splash_shown", "window_constructed", "comboboxes_styled",
        "event_loop_entered", "event_loop_turn", "deferred_show_confirmed",
        "preflight_passed",
    )
    report.write_text(json.dumps({
        "status": "passed",
        "frozen": False,
        "event_loop": {
            "turn_observed": True,
            "deferred_show_confirmed": True,
        },
        "checkpoints": [{"name": name} for name in required],
        "executable": sys.executable,
        "os": "stale",
        "architecture": "stale",
        "python": "stale",
        "qt_version": "stale",
        "qpa": "stale",
        "profile_path": "stale",
        "theme": "stale",
        "duration_ms": 1,
    }), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "tools_dev" / "startup_smoke.py"),
            "--executable", sys.executable,
            "--entrypoint", str(fake),
            "--artifacts-dir", str(artifacts),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "evidence" in result.stderr.lower()


def test_smoke_recreates_tool_owned_profile_for_reused_artifacts(tmp_path):
    artifacts = tmp_path / "artifacts"
    command = [
        sys.executable,
        str(REPO / "tools_dev" / "startup_smoke.py"),
        "--executable", sys.executable,
        "--entrypoint", str(MAIN),
        "--artifacts-dir", str(artifacts),
        "--qpa", "offscreen",
    ]
    first = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    evidence = json.loads((artifacts / "startup.json").read_text(encoding="utf-8"))
    profile = Path(evidence["profile_path"])
    stale_settings = profile / "settings" / "stale.ini"
    stale_cache = profile / "cache" / "stale.bin"
    stale_settings.parent.mkdir(parents=True, exist_ok=True)
    stale_cache.parent.mkdir(parents=True, exist_ok=True)
    stale_settings.write_text("old=true", encoding="utf-8")
    stale_cache.write_bytes(b"old-cache")

    second = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True, timeout=30)

    assert second.returncode == 0, second.stderr
    assert not stale_settings.exists()
    assert not stale_cache.exists()


def test_smoke_never_deletes_nonempty_explicit_profile(tmp_path):
    artifacts = tmp_path / "artifacts"
    profile = tmp_path / "user-profile"
    profile.mkdir()
    sentinel = profile / "keep.txt"
    sentinel.write_text("user-owned", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "tools_dev" / "startup_smoke.py"),
            "--executable", sys.executable,
            "--entrypoint", str(MAIN),
            "--artifacts-dir", str(artifacts),
            "--profile", str(profile),
            "--qpa", "offscreen",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "explicit profile" in result.stderr.lower()
    assert sentinel.read_text(encoding="utf-8") == "user-owned"


def test_internal_watchdog_reports_timeout_and_exits(tmp_path):
    completed, evidence, _ = _run_startup(
        tmp_path,
        "--startup-timeout-at", "event_loop_turn",
        timeout_ms=600,
    )
    assert completed.returncode != 0
    assert completed.returncode != -999, completed.stderr
    assert evidence is not None
    assert evidence["status"] == "timeout"
    assert evidence["timeout"]["checkpoint"] == "event_loop_turn"
    assert evidence["duration_ms"] >= 500
