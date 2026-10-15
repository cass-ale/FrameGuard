"""Contracts for the security- and correctness-critical release workflows."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_WORKFLOW = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
RELEASE_WORKFLOW = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

ACTION_PINS = {
    "actions/checkout": "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "actions/attest-build-provenance": "8beda2b7ed98355c0e97c0a63bec38ae472e66c4",
}


def _job(workflow, name):
    start_marker = f"  {name}:\n"
    start = workflow.index(start_marker) + len(start_marker)
    next_job = re.search(r"^  [a-zA-Z0-9_-]+:\n", workflow[start:], re.MULTILINE)
    end = start + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def test_all_actions_use_the_reviewed_node24_compatible_pins():
    uses = re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)",
                      TESTS_WORKFLOW + RELEASE_WORKFLOW, re.MULTILINE)
    assert uses, "no workflow actions found"
    for action, revision in uses:
        assert action in ACTION_PINS, f"unreviewed action added: {action}"
        assert revision == ACTION_PINS[action], (
            f"{action} must use reviewed revision {ACTION_PINS[action]}, got {revision}"
        )
        assert re.fullmatch(r"[0-9a-f]{40}", revision), revision


def test_push_and_pull_requests_run_the_full_supported_runner_matrix():
    assert re.search(
        r"^on:\n\s+push:\n\s+branches: \[main\]\n\s+pull_request:\s*$",
        TESTS_WORKFLOW,
        re.MULTILINE,
    )
    runners = re.findall(r"^\s{10}- (windows-2025|macos-15|macos-15-intel)$",
                         TESTS_WORKFLOW, re.MULTILINE)
    assert runners == ["windows-2025", "macos-15", "macos-15-intel"]
    assert "runs-on: ${{ matrix.runner }}" in TESTS_WORKFLOW
    assert "python-version-file: src/.python-version" in TESTS_WORKFLOW
    assert "src/constraints-3.12.txt" in TESTS_WORKFLOW
    assert "--constraint constraints-3.12.txt" in TESTS_WORKFLOW
    assert "QT_QPA_PLATFORM: offscreen" in TESTS_WORKFLOW
    assert "python -m pytest --verbose" in TESTS_WORKFLOW


def test_release_checks_out_only_the_requested_tag_ref():
    verify = _job(RELEASE_WORKFLOW, "verify")
    assert "if: github.event_name == 'workflow_dispatch'" in verify
    assert '"$GITHUB_REF" != "refs/tags/${REQUESTED_TAG}"' in verify
    assert "git/ref/tags/${REQUESTED_TAG}" in verify
    assert "ref: refs/tags/${{ env.RELEASE_TAG }}" in verify
    assert 'git cat-file -t "refs/tags/${RELEASE_TAG}"' in verify
    assert "Releases require an annotated tag" in verify
    assert RELEASE_WORKFLOW.count("ref: refs/tags/${{ env.RELEASE_TAG }}") == 3


def test_release_installs_are_constrained_and_jobs_are_bounded():
    verify = _job(RELEASE_WORKFLOW, "verify")
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "timeout-minutes: 15" in verify
    assert "timeout-minutes: 30" in publish
    assert "PIP_CONSTRAINT: ${{ github.workspace }}/src/constraints-3.12.txt" in RELEASE_WORKFLOW
    assert RELEASE_WORKFLOW.count("--constraint") >= 2
    assert 'cyclonedx-bom==7.5.0' not in RELEASE_WORKFLOW


def test_macos_uses_one_deterministic_build_root_and_preserves_failure_logs():
    mac = _job(RELEASE_WORKFLOW, "build-macos")
    root = "${{ github.workspace }}/.frameguard-build/${{ matrix.label }}"
    assert f"FRAMEGUARD_MAC_BUILD_ROOT: {root}" in mac
    assert "target_arch: arm64" in mac
    assert "target_arch: x86_64" in mac
    assert "FRAMEGUARD_TARGET_ARCH: ${{ matrix.target_arch }}" in mac
    for suffix in (
        'tee "$FRAMEGUARD_MAC_BUILD_ROOT/build.log"',
        'BUNDLE="$FRAMEGUARD_MAC_BUILD_ROOT/dist/FrameGuard.app"',
        'VENV_PY="$FRAMEGUARD_MAC_BUILD_ROOT/venv/bin/python"',
        "${{ env.FRAMEGUARD_MAC_BUILD_ROOT }}/build.log",
    ):
        assert suffix in mac
    assert "if: failure()" in mac
    assert "include-hidden-files: true" in mac

    windows = _job(RELEASE_WORKFLOW, "build-windows")
    assert "if: failure()" in windows
    assert "src/Output/build-logs/" in windows

    upload_pin = "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"]
    assert RELEASE_WORKFLOW.count("overwrite: true") == RELEASE_WORKFLOW.count(upload_pin)


def test_publish_reconciles_only_drafts_and_verifies_asset_names_and_sizes():
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "already published and immutable" in publish
    assert publish.index("already published and immutable") < publish.index(
        "actions/attest-build-provenance"
    )
    assert "--verify-tag" in publish
    assert "gh release delete-asset" in publish
    assert "gh release upload \"$RELEASE_TAG\" stage/* --clobber" in publish
    assert "path.stat().st_size" in publish
    assert "expected-assets.tsv" in publish
    assert "uploaded-assets.tsv" in publish
    assert "diff -u expected-assets.tsv uploaded-assets.tsv" in publish
    assert "isDraft" in publish


def test_channel_is_pushed_before_undraft_with_a_bounded_non_force_retry():
    publish = _job(RELEASE_WORKFLOW, "publish")
    checkout = publish.index("Check out the releases branch")
    rollback_guard = publish.index("Refuse to roll back the published update channel")
    mirror = publish.index("Mirror the delta channel")
    commit = publish.index("git commit -m")
    push = publish.index("git push origin HEAD:refs/heads/releases")
    undraft = publish.index('gh release edit "$RELEASE_TAG" --draft=false')
    assert checkout < rollback_guard < mirror < commit < push < undraft
    assert "Refusing channel rollback" in publish
    assert "latest GitHub release" in publish
    assert 'data.get("app_version", "")' in publish
    assert "for attempt in 1 2 3" in publish
    assert "git fetch --no-tags origin" in publish
    assert "git rebase refs/remotes/origin/releases" in publish
    assert "--force" not in publish


def test_publish_keeps_least_privilege_environment_and_release_options():
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "GH_REPO: ${{ github.repository }}" in publish
    assert "group: release-publish" in publish
    assert "cancel-in-progress: false" in publish
    assert "environment: release" in publish
    assert "contents: write" in publish
    assert "id-token: write" in publish
    assert "attestations: write" in publish
    assert "contents: read" in RELEASE_WORKFLOW.split("jobs:", 1)[0]

    mac = _job(RELEASE_WORKFLOW, "build-macos")
    job_environment = mac.split("    steps:\n", 1)[0]
    for secret_name in (
        "MACOS_SIGN_IDENTITY", "MACOS_CERTIFICATE_P12_BASE64",
        "MACOS_CERTIFICATE_PASSWORD", "APPLE_ID", "APPLE_TEAM_ID",
        "APPLE_APP_PASSWORD",
    ):
        assert secret_name not in job_environment
    assert "security create-keychain" in mac
    assert "security import" in mac
    assert "security set-key-partition-list" in mac
    assert "security delete-keychain" in mac
    assert "Official macOS releases require the certificate" in mac
    assert "SIGN_IDENTITY: ${{ secrets.MACOS_SIGN_IDENTITY }}" in mac
    assert "delta: true" in mac
    assert "delta: false" in mac
    assert "if: matrix.delta" in mac
    assert "continue-on-error: true" in mac
    assert "if: matrix.label == 'arm64'" in mac
    assert '|| echo "no windows SBOM"' in publish
    assert '|| echo "no macos SBOM"' in publish
