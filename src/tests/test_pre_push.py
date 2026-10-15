"""Fail-closed contracts for the staged pre-push evidence orchestrator."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest

from tools_dev import pre_push


def _snapshot(fingerprint="tree-a"):
    return pre_push.Snapshot(
        head="a" * 40,
        dirty_diff_sha256="b" * 64,
        tree_fingerprint=fingerprint,
        changed_paths=("src/frameguard/core.py",),
        tracked_diff_bytes=1,
        untracked_files=(),
    )


def _passed(name):
    return {"name": name, "status": "passed"}


def _materialize_stage_record(stage, evidence_dir, snapshot, tools):
    log = evidence_dir / "logs" / f"{stage.name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{stage.name} passed\n", encoding="utf-8")
    record = _stage_record(stage, snapshot, tools)
    record.update({
        "log": str(log),
        "log_sha256": pre_push.file_sha256(log),
        "evidence_files": pre_push.evidence_manifest(stage, evidence_dir),
    })
    return record


def _stage_record(stage, snapshot, tools, status="passed"):
    now = pre_push.utc_now()
    return {
        "name": stage.name,
        "commands": [list(command) for command in stage.commands],
        "timeout_seconds": stage.timeout_seconds,
        "validator": stage.validator,
        "signature": pre_push.stage_signature(
            stage, snapshot.tree_fingerprint, tools["fingerprint"]),
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0.01,
        "log": None,
        "log_sha256": None,
        "evidence_files": [],
        "validator_result": {
            "validator": stage.validator,
            "status": "passed" if status == "passed" else "not_run",
            "errors": [],
        },
        "status": status,
        "failure": None,
        "executions": [],
    }


@pytest.mark.skipif(os.name != "posix", reason="symlink virtualenv regression is POSIX-specific")
def test_symlinked_venv_path_is_preserved_and_fingerprinted(tmp_path, monkeypatch):
    venv_root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_root)
    venv_python = venv_root / "bin" / "python"
    assert venv_python.is_symlink(), "regression requires a symlink-based virtualenv"
    resolved_python = venv_python.resolve()
    assert resolved_python != venv_python

    site_packages = Path(subprocess.check_output(
        [str(venv_python), "-c", "import site; print(site.getsitepackages()[0])"],
        text=True,
    ).strip())
    marker_name = "frameguard_venv_only_marker"
    (site_packages / f"{marker_name}.py").write_text("VALUE = 'venv-only'\n", encoding="utf-8")
    import_marker = f"import {marker_name}; print({marker_name}.VALUE)"

    invoked = subprocess.run(
        [str(venv_python), "-c", import_marker], capture_output=True, text=True,
        check=False,
    )
    resolved = subprocess.run(
        [str(resolved_python), "-c", import_marker], capture_output=True, text=True,
        check=False,
    )
    assert invoked.returncode == 0
    assert invoked.stdout.strip() == "venv-only"
    assert resolved.returncode != 0

    monkeypatch.setattr(pre_push.sys, "executable", str(venv_python))
    tools = pre_push.capture_tools()
    assert tools["versions"]["python_executable_invoked"] == str(venv_python)
    assert tools["versions"]["python_executable_resolved"] == str(resolved_python)

    stages = pre_push.build_stages(
        "package", {"areas": ["workflow_packaging"]},
        ["src/build_mac.sh"], tmp_path / "evidence", 120, 600,
    )
    for stage in stages:
        for command in stage.commands:
            if command[0] not in {"bash", "git"}:
                assert command[0] == str(venv_python)

    alias = venv_python.with_name("python-alias")
    alias.symlink_to(resolved_python)
    monkeypatch.setattr(pre_push.sys, "executable", str(alias))
    alias_tools = pre_push.capture_tools()
    assert alias_tools["versions"]["python_executable_resolved"] == str(resolved_python)
    assert alias_tools["fingerprint"] != tools["fingerprint"]

    gate = pre_push.Stage("gate", ((str(venv_python), "--version"),), 30)
    prior = {
        "name": "gate",
        "status": "passed",
        "commands": [[str(venv_python), "--version"]],
        "signature": pre_push.stage_signature(
            gate, "tree-a", tools["fingerprint"]),
    }
    reusable, reason = pre_push.reusable_stage(
        prior, gate, _snapshot(), alias_tools, tmp_path)
    assert reusable is False
    assert "signature is stale" in reason


def test_snapshot_fingerprints_head_tracked_diff_and_untracked_bytes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    monkeypatch.setattr(pre_push, "REPO_ROOT", repo)

    clean = pre_push.capture_snapshot()
    tracked.write_text("changed\n", encoding="utf-8")
    dirty = pre_push.capture_snapshot()
    untracked = repo / "new.txt"
    untracked.write_text("one\n", encoding="utf-8")
    with_untracked = pre_push.capture_snapshot()
    untracked.write_text("two\n", encoding="utf-8")
    changed_untracked = pre_push.capture_snapshot()
    if os.name == "posix":
        untracked.chmod(0o755)
        executable_untracked = pre_push.capture_snapshot()
        assert executable_untracked.tree_fingerprint != changed_untracked.tree_fingerprint

    assert clean.head == dirty.head == with_untracked.head
    assert len({clean.tree_fingerprint, dirty.tree_fingerprint,
                with_untracked.tree_fingerprint, changed_untracked.tree_fingerprint}) == 4
    assert dirty.changed_paths == ("tracked.txt",)
    assert with_untracked.untracked_files == ("new.txt",)


@pytest.mark.parametrize("status", [
    "failed", "missing", "stale", "warning", "skipped", "unclassified", "timed_out",
])
def test_every_nonpassing_stage_status_blocks_authorization(status):
    blockers = pre_push.stage_blockers(["gate"], [{"name": "gate", "status": status}])
    assert blockers
    assert "gate" in blockers[0]
    assert status in blockers[0]


def test_missing_and_unknown_stages_block_authorization():
    blockers = pre_push.stage_blockers(
        ["required-a", "required-b"],
        [_passed("required-a"), _passed("unexpected")],
    )
    assert "missing stage: required-b" in blockers
    assert "unclassified stage: unexpected" in blockers


def test_failed_command_writes_a_failed_stage_and_log(tmp_path, monkeypatch):
    snapshot = _snapshot()
    monkeypatch.setattr(pre_push, "capture_snapshot", lambda: snapshot)
    stage = pre_push.Stage(
        "negative-control",
        ((sys.executable, "-c", "import sys; print('deliberate failure'); sys.exit(7)"),),
        10,
    )
    result = pre_push.run_stage(
        stage, tmp_path, snapshot, {"fingerprint": "tools-a"},
    )

    assert result["status"] == "failed"
    assert result["executions"][0]["returncode"] == 7
    assert "deliberate failure" in Path(result["log"]).read_text(encoding="utf-8")
    status, blockers = pre_push.authorization_status(
        "full", ["negative-control"], [result],
        {"unclassified_paths": []}, {"status": "passed"},
    )
    assert status == "failed"
    assert blockers


def test_resume_rejects_stale_command_and_missing_log(tmp_path):
    snapshot = _snapshot()
    tools = {"fingerprint": "tools-a"}
    original = pre_push.Stage("gate", (("tool", "first"),), 30)
    changed = pre_push.Stage("gate", (("tool", "second"),), 30)
    log = tmp_path / "logs" / "gate.log"
    log.parent.mkdir()
    log.write_text("passed\n", encoding="utf-8")
    evidence = {
        "name": "gate",
        "status": "passed",
        "commands": [["tool", "first"]],
        "signature": pre_push.stage_signature(original, "tree-a", "tools-a"),
        "log": str(log),
        "log_sha256": pre_push.file_sha256(log),
        "evidence_files": [],
    }

    reusable, stale = pre_push.reusable_stage(
        evidence, changed, snapshot, tools, tmp_path)
    assert reusable is False
    assert "stale" in stale

    log.write_text("tampered\n", encoding="utf-8")
    reusable, stale = pre_push.reusable_stage(
        evidence, original, snapshot, tools, tmp_path)
    assert reusable is False
    assert stale == "stage log hash is stale"

    log.unlink()
    reusable, stale = pre_push.reusable_stage(
        evidence, original, snapshot, tools, tmp_path)
    assert reusable is False
    assert stale == "stage log is missing or symlinked"


@pytest.mark.parametrize(
    ("fingerprints", "failed_phase"),
    [
        (("tree-a", "tree-b"), "pre-authorization"),
        (("tree-a", "tree-a", "tree-b"), "pre-exit"),
    ],
)
def test_snapshot_mutation_after_last_stage_fails_closed(
        tmp_path, monkeypatch, fingerprints, failed_phase):
    snapshots = iter(_snapshot(value) for value in fingerprints)
    initial = _snapshot("tree-a")
    tools = {"versions": {}, "fingerprint": "tools-a"}
    stage = pre_push.Stage("gate", ((sys.executable, "--version"),), 30)
    monkeypatch.setattr(pre_push, "capture_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(pre_push, "capture_tools", lambda: tools)
    monkeypatch.setattr(pre_push, "build_stages", lambda *args, **kwargs: (stage,))
    monkeypatch.setattr(
        pre_push, "run_stage",
        lambda *args, **kwargs: _stage_record(stage, initial, tools),
    )
    monkeypatch.setattr(
        pre_push, "validate_review",
        lambda *args, **kwargs: {"status": "passed", "path": "review.json"},
    )
    monkeypatch.setattr(
        pre_push, "revalidate_authorization_evidence",
        lambda *args, **kwargs: (
            True, {"phase": args[-1], "status": "passed", "failures": [],
                   "observed_digest": args[-2]}),
    )
    args = SimpleNamespace(
        profile="full",
        evidence_root=tmp_path / "evidence",
        resume=None,
        stage_timeout=30,
        package_timeout=30,
        review_evidence=tmp_path / "review.json",
        review_max_age_hours=24.0,
    )

    assert pre_push.run(args) == 1
    run_dir = next((tmp_path / "evidence").iterdir())
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["final_status"] == "failed"
    failed_check = next(
        check for check in summary["snapshot_checks"]
        if check["phase"] == failed_phase
    )
    assert failed_check["status"] == "stale"
    assert failed_check["expected"]["tree_fingerprint"] == "tree-a"
    assert failed_check["observed"]["tree_fingerprint"] == "tree-b"
    assert failed_check["failure"] in summary["blockers"]


def test_fully_reused_resume_recaptures_snapshot_before_authorization(
        tmp_path, monkeypatch):
    evidence_dir = tmp_path / "run-a"
    logs_dir = evidence_dir / "logs"
    logs_dir.mkdir(parents=True)
    snapshot = _snapshot("tree-a")
    mutated = _snapshot("tree-b")
    tools = {"versions": {}, "fingerprint": "tools-a"}
    stage = pre_push.Stage("gate", ((sys.executable, "--version"),), 30)
    log = logs_dir / "gate.log"
    log.write_text("passed\n", encoding="utf-8")
    record = _stage_record(stage, snapshot, tools)
    record.update({
        "log": str(log),
        "log_sha256": pre_push.file_sha256(log),
    })
    previous = {
        "schema_version": pre_push.SCHEMA_VERSION,
        "run_id": evidence_dir.name,
        "profile": "full",
        "created_at": pre_push.utc_now(),
        "evidence_dir": str(evidence_dir),
        "snapshot": pre_push.snapshot_payload(snapshot),
        "tools": tools,
        "classification": pre_push.classify_changes(snapshot.changed_paths),
        "expected_stages": ["gate"],
        "stages": [record],
        "review": {"status": "missing", "path": None},
        "authorization_evidence_digest": pre_push.authorization_evidence_digest(
            (stage,), (record,)),
        "final_status": "incomplete",
        "blockers": ["review missing"],
    }
    (evidence_dir / "summary.json").write_text(
        json.dumps(previous), encoding="utf-8")
    snapshots = iter((snapshot, mutated))
    monkeypatch.setattr(pre_push, "capture_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(pre_push, "capture_tools", lambda: tools)
    monkeypatch.setattr(pre_push, "build_stages", lambda *args, **kwargs: (stage,))
    monkeypatch.setattr(
        pre_push, "validate_review",
        lambda *args, **kwargs: {"status": "passed", "path": "review.json"},
    )
    args = SimpleNamespace(
        profile="full",
        evidence_root=None,
        resume=evidence_dir,
        stage_timeout=30,
        package_timeout=30,
        review_evidence=tmp_path / "review.json",
        review_max_age_hours=24.0,
    )

    assert pre_push.run(args) == 1
    summary = json.loads(
        (evidence_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["stages"][0]["reused"] is True
    assert summary["final_status"] == "failed"
    assert summary["snapshot_checks"][0]["phase"] == "pre-authorization"
    assert summary["snapshot_checks"][0]["status"] == "stale"


@pytest.mark.parametrize(
    ("record_mode", "message"),
    [
        ("duplicate", "duplicate stage record: gate"),
        ("missing", "missing stage record: gate"),
        ("unexpected", "unexpected stage record: surprise"),
    ],
)
def test_resume_rejects_duplicate_missing_and_unexpected_stage_records(
        tmp_path, record_mode, message):
    snapshot = _snapshot()
    tools = {"versions": {}, "fingerprint": "tools-a"}
    stage = pre_push.Stage("gate", (("tool", "check"),), 30)
    record = _stage_record(stage, snapshot, tools)
    record["log"] = str(tmp_path / "logs" / "gate.log")
    if record_mode == "duplicate":
        records = [record, dict(record)]
    elif record_mode == "missing":
        records = []
    else:
        records = [record, {**record, "name": "surprise"}]
    previous = {
        "run_id": tmp_path.name,
        "profile": "full",
        "evidence_dir": str(tmp_path),
        "snapshot": pre_push.snapshot_payload(snapshot),
        "tools": tools,
        "expected_stages": ["gate"],
        "stages": records,
        "authorization_evidence_digest": None,
        "final_status": "incomplete",
    }

    with pytest.raises(pre_push.PreflightError, match=message):
        pre_push.validate_resume_summary(
            previous, tmp_path, "full", snapshot, tools, (stage,))


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("snapshot", "full source snapshot is stale or tampered"),
        ("tools", "full tool/platform evidence is stale or tampered"),
        ("expected_stages", "expected stage plan does not exactly match"),
        ("evidence_dir", "evidence directory identity does not match"),
    ],
)
def test_resume_rejects_adjacent_metadata_tampering(tmp_path, field, message):
    snapshot = _snapshot()
    tools = {"versions": {}, "fingerprint": "tools-a"}
    stage = pre_push.Stage("gate", (("tool", "check"),), 30)
    record = _stage_record(stage, snapshot, tools)
    record["log"] = str(tmp_path / "logs" / "gate.log")
    previous = {
        "run_id": tmp_path.name,
        "profile": "full",
        "evidence_dir": str(tmp_path),
        "snapshot": pre_push.snapshot_payload(snapshot),
        "tools": tools,
        "expected_stages": ["gate"],
        "stages": [record],
        "authorization_evidence_digest": pre_push.authorization_evidence_digest(
            (stage,), (record,)),
        "final_status": "incomplete",
    }
    previous[field] = {"tampered": True} if field in {"snapshot", "tools"} else ["tampered"]

    with pytest.raises(pre_push.PreflightError, match=message):
        pre_push.validate_resume_summary(
            previous, tmp_path, "full", snapshot, tools, (stage,))


def test_missing_review_is_incomplete_and_fast_never_becomes_local_ready():
    classification = {"unclassified_paths": []}
    passed = [_passed("gate")]
    full_status, full_blockers = pre_push.authorization_status(
        "full", ["gate"], passed, classification,
        {"status": "missing", "failure": "review absent"},
    )
    fast_status, fast_blockers = pre_push.authorization_status(
        "fast", ["gate"], passed, classification, {"status": "passed"},
    )

    assert full_status == "incomplete"
    assert full_blockers == ["review absent"]
    assert fast_status == "incomplete"
    assert "cannot establish local readiness" in fast_blockers[0]


def test_stale_review_is_rejected(tmp_path):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    completed = now - timedelta(hours=1)
    request = {
        "run_id": "run-a",
        "tree_fingerprint": "tree-a",
        "tool_fingerprint": "tools-a",
        "internal_completed_at": completed.isoformat(),
        "authorization_evidence_digest": "evidence-a",
    }
    review = {
        "schema_version": pre_push.SCHEMA_VERSION,
        "run_id": "run-a",
        "tree_fingerprint": "tree-a",
        "tool_fingerprint": "tools-a",
        "authorization_evidence_digest": "evidence-a",
        "decision": "approved",
        "independent": True,
        "reviewer": "Alex Rivera <alex.rivera@studio.test>",
        "reviewed_at": (completed - timedelta(seconds=1)).isoformat(),
        "summary": "Reviewed the complete source diff, logs, artifacts, and negative controls.",
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")

    result = pre_push.validate_review(path, request, 24, now=now)
    assert result["status"] == "failed"
    assert "predates completion" in result["failure"]


def test_matching_fresh_review_can_establish_local_readiness(tmp_path):
    now = datetime.now(timezone.utc)
    completed = now - timedelta(minutes=2)
    request = {
        "run_id": "run-a",
        "tree_fingerprint": "tree-a",
        "tool_fingerprint": "tools-a",
        "internal_completed_at": completed.isoformat(),
        "authorization_evidence_digest": "evidence-a",
    }
    review = {
        "schema_version": pre_push.SCHEMA_VERSION,
        "run_id": "run-a",
        "tree_fingerprint": "tree-a",
        "tool_fingerprint": "tools-a",
        "authorization_evidence_digest": "evidence-a",
        "decision": "approved",
        "independent": True,
        "reviewer": "Alex Rivera <alex.rivera@studio.test>",
        "reviewed_at": (now - timedelta(minutes=1)).isoformat(),
        "summary": "Reviewed source diff, stage logs, artifact hashes, and test evidence in full.",
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    validated = pre_push.validate_review(path, request, 24, now=now)

    status, blockers = pre_push.authorization_status(
        "full", ["gate"], [_passed("gate")],
        {"unclassified_paths": []}, validated,
    )
    assert validated["status"] == "passed"
    assert status == "local-ready"
    assert blockers == []


def test_local_ready_summary_explicitly_refuses_release_authorization():
    summary = {
        "run_id": "run-a",
        "profile": "full",
        "scope": "local",
        "release_authorized": False,
        "final_status": "local-ready",
        "snapshot": {
            "head": "a" * 40,
            "dirty_diff_sha256": "b" * 64,
            "tree_fingerprint": "tree-a",
        },
        "tools": {"fingerprint": "tools-a"},
        "authorization_evidence_digest": "evidence-a",
        "evidence_dir": "/evidence/run-a",
        "classification": {"areas": [], "unclassified_paths": []},
        "stages": [],
        "review": {"status": "passed"},
        "snapshot_checks": [],
        "evidence_checks": [],
        "blockers": [],
    }

    rendered = pre_push.render_summary(summary)
    assert "LOCAL-READY" in rendered
    assert "RELEASE NOT AUTHORIZED" in rendered
    assert "AUTHORIZED: all required gates" not in rendered


def test_old_schema_resume_evidence_fails_closed(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps({"schema_version": pre_push.SCHEMA_VERSION - 1}),
        encoding="utf-8",
    )
    with pytest.raises(pre_push.PreflightError, match="unsupported schema"):
        pre_push._load_resume(tmp_path)


def test_review_template_with_only_timestamp_changed_is_rejected(tmp_path):
    now = datetime.now(timezone.utc)
    request = pre_push.review_request(
        _snapshot(), {"fingerprint": "tools-a"}, "run-a",
        (now - timedelta(minutes=2)).isoformat(), "evidence-a",
    )
    copied_template = dict(request["required_review_evidence"])
    copied_template["reviewed_at"] = (now - timedelta(minutes=1)).isoformat()
    path = tmp_path / "review.json"
    path.write_text(json.dumps(copied_template), encoding="utf-8")

    result = pre_push.validate_review(path, request, 24, now=now)
    assert result["status"] == "failed"
    assert "template or non-substantive identity" in result["failure"]
    assert "template or non-substantive content" in result["failure"]


def test_review_evidence_files_are_excluded_from_authorization_digest(tmp_path):
    snapshot = _snapshot()
    tools = {"fingerprint": "tools-a"}
    stage = pre_push.Stage("gate", (("tool", "check"),), 30)
    record = _materialize_stage_record(stage, tmp_path, snapshot, tools)
    before = pre_push.authorization_evidence_digest((stage,), (record,))

    (tmp_path / "review-request.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "review-evidence.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "summary.json").write_text("{}\n", encoding="utf-8")
    after = pre_push.authorization_evidence_digest((stage,), (record,))

    assert after == before
    assert record["evidence_files"] == []


def test_log_tampering_after_successful_stage_reuse_fails_final_validation(tmp_path):
    snapshot = _snapshot()
    tools = {"fingerprint": "tools-a"}
    stage = pre_push.Stage("gate", (("tool", "check"),), 30)
    record = _materialize_stage_record(stage, tmp_path, snapshot, tools)
    digest = pre_push.authorization_evidence_digest((stage,), (record,))
    reusable, reason = pre_push.reusable_stage(
        record, stage, snapshot, tools, tmp_path)
    assert reusable is True and reason is None

    Path(record["log"]).write_text("tampered after reuse\n", encoding="utf-8")
    valid, check = pre_push.revalidate_authorization_evidence(
        (stage,), (record,), snapshot, tools, tmp_path, digest,
        "pre-authorization",
    )
    assert valid is False
    assert "stage log hash is stale" in "; ".join(check["failures"])


def test_manifest_tampering_after_review_validation_fails_closed(tmp_path):
    snapshot = _snapshot()
    tools = {"fingerprint": "tools-a"}
    stage = pre_push.Stage(
        "startup-preflight", (("tool", "check"),), 30, "startup-evidence")
    artifacts = tmp_path / "artifacts" / "source-startup"
    artifacts.mkdir(parents=True)
    report = artifacts / "source-startup.json"
    report.write_text('{"status": "passed"}\n', encoding="utf-8")
    record = _materialize_stage_record(stage, tmp_path, snapshot, tools)
    digest = pre_push.authorization_evidence_digest((stage,), (record,))
    now = datetime.now(timezone.utc)
    request = pre_push.review_request(
        snapshot, tools, "run-a", (now - timedelta(minutes=2)).isoformat(), digest)
    review = dict(request["required_review_evidence"])
    review.update({
        "reviewer": "Alex Rivera <alex.rivera@studio.test>",
        "reviewed_at": (now - timedelta(minutes=1)).isoformat(),
        "summary": "Reviewed all source changes, stage logs, manifests, and startup evidence.",
    })
    review_path = tmp_path / "review-evidence.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    assert pre_push.validate_review(
        review_path, request, 24, now=now)["status"] == "passed"

    report.write_text('{"status": "failed"}\n', encoding="utf-8")
    valid, check = pre_push.revalidate_authorization_evidence(
        (stage,), (record,), snapshot, tools, tmp_path, digest,
        "pre-authorization",
    )
    assert valid is False
    assert any("evidence" in failure for failure in check["failures"])


def test_ignored_dmg_tampering_before_return_is_in_digest_and_fails(
        tmp_path, monkeypatch):
    source_root = tmp_path / "src"
    dmg = source_root / "Output" / "FrameGuard-arm64.dmg"
    dmg.parent.mkdir(parents=True)
    dmg.write_bytes(b"original dmg bytes")
    monkeypatch.setattr(pre_push, "SRC_ROOT", source_root)
    snapshot = _snapshot()
    tools = {"fingerprint": "tools-a"}
    stage = pre_push.Stage(
        "official-macos-arm64-build", (("tool", "build"),), 30,
        "native-arm64-build")
    evidence_dir = tmp_path / "evidence"
    record = _materialize_stage_record(stage, evidence_dir, snapshot, tools)
    digest = pre_push.authorization_evidence_digest((stage,), (record,))
    assert any(
        Path(item["path"]).name == "FrameGuard-arm64.dmg"
        for item in record["evidence_files"])
    pre_authorization_ok, _ = pre_push.revalidate_authorization_evidence(
        (stage,), (record,), snapshot, tools, evidence_dir, digest,
        "pre-authorization",
    )
    assert pre_authorization_ok is True

    dmg.write_bytes(b"changed dmg bytes")
    pre_exit_ok, check = pre_push.revalidate_authorization_evidence(
        (stage,), (record,), snapshot, tools, evidence_dir, digest, "pre-exit")
    assert pre_exit_ok is False
    assert any("evidence" in failure for failure in check["failures"])


def test_impact_classification_is_conservative_and_unknown_paths_fail_closed():
    classified = pre_push.classify_changes([
        ".github/workflows/tests.yml",
        "src/frameguard/core.py",
        "src/frameguard/gui.py",
        "mystery.bin",
    ])
    assert {"engine", "ui", "workflow_packaging"}.issubset(classified["areas"])
    assert classified["unclassified_paths"] == ["mystery.bin"]


def test_package_profile_uses_clean_builder_and_explicit_final_dmg_smoke(tmp_path):
    classification = {"areas": ["workflow_packaging"]}
    stages = pre_push.build_stages(
        "package", classification, ["src/build_mac.sh"], tmp_path, 120, 600,
    )
    by_name = {stage.name: stage for stage in stages}
    build = by_name["official-macos-arm64-build"].commands[0]
    smoke = by_name["final-dmg-smoke"].commands[0]

    assert build == ("bash", "build_mac.sh", "--skip-delta")
    assert "--fast" not in build
    assert "--skip-tests" not in build
    assert "--allow-unsigned" not in build
    assert "tools_dev/macos_package_smoke.py" in smoke
    assert "Output/FrameGuard-arm64.dmg" in smoke
