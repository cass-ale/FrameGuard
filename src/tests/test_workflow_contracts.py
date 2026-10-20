"""Contracts for the security- and correctness-critical release workflows."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_WORKFLOW = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
CANDIDATE_WORKFLOW = (WORKFLOWS / "release-candidate.yml").read_text(
    encoding="utf-8")
RELEASE_WORKFLOW = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

# Reviewed action revisions. A Dependabot bump fails CI until a person has
# checked the new release (tag points at this exact commit, runtime is node24,
# no breaking input or trigger changes for these workflows) and updated the
# pin here in the same commit as the workflows.
#   actions/checkout v7.0.1, actions/setup-python v7.0.0 and
#   actions/attest-build-provenance v4.2.2 were reviewed for v0.8.3.
ACTION_PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "actions/attest-build-provenance": "4d101475d8b20a2381f78447822ac1eab6504dd8",
}


def _job(workflow, name):
    start_marker = f"  {name}:\n"
    start = workflow.index(start_marker) + len(start_marker)
    next_job = re.search(r"^  [a-zA-Z0-9_-]+:\n", workflow[start:], re.MULTILINE)
    end = start + next_job.start() if next_job else len(workflow)
    return workflow[start:end]


def test_all_actions_use_the_reviewed_node24_compatible_pins():
    uses = re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)",
                      TESTS_WORKFLOW + CANDIDATE_WORKFLOW + RELEASE_WORKFLOW,
                      re.MULTILINE)
    assert uses, "no workflow actions found"
    for action, revision in uses:
        assert action in ACTION_PINS, f"unreviewed action added: {action}"
        assert revision == ACTION_PINS[action], (
            f"{action} must use reviewed revision {ACTION_PINS[action]}, got {revision}. "
            "If this is a Dependabot bump, review the new release and update "
            "ACTION_PINS in this file in the same commit."
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
    assert "QT_QPA_PLATFORM: ${{ startsWith(matrix.runner, 'windows') && 'windows' || 'offscreen' }}" in TESTS_WORKFLOW
    assert 'FRAMEGUARD_TEST_MODE: "1"' in TESTS_WORKFLOW
    assert "QFontMetrics(font).inFont('A')" in TESTS_WORKFLOW
    assert "python -m pytest --verbose" in TESTS_WORKFLOW


def test_source_and_final_artifact_startup_gates_precede_uploads():
    test_job = _job(TESTS_WORKFLOW, "test")
    source_gate = test_job.index("tools_dev/startup_smoke.py")
    assert test_job.index("python -m pytest --verbose") < source_gate
    assert source_gate < test_job.index("tools_dev/gui_audit.py --matrix")
    assert "src/startup-evidence/" in test_job

    windows = _job(RELEASE_WORKFLOW, "build-windows")
    installer_smoke = windows.index("tools_dev/windows_installer_smoke.ps1")
    installer_upload = windows.index("name: Upload the installer")
    assert installer_smoke < installer_upload
    assert "Build and installed-smoke the installer" in windows
    assert "run: build_installer.bat --skip-delta" in windows
    assert "--skip-installer-smoke" not in windows
    assert "Output/FrameGuardInstaller.exe" in windows

    mac = _job(RELEASE_WORKFLOW, "build-macos")
    dmg_smoke = mac.index("tools_dev/macos_package_smoke.py")
    dmg_upload = mac.index("name: Upload the DMG")
    assert dmg_smoke < dmg_upload
    assert '--expected-arch "${{ matrix.target_arch }}"' in mac


def test_visual_evidence_uses_fresh_dpi_processes_and_preserves_failures():
    test_job = _job(TESTS_WORKFLOW, "test")
    assert "FRAMEGUARD_VISUAL_ARTIFACTS: visual-test-artifacts" in test_job
    assert "python tools_dev/gui_audit.py --matrix" in test_job
    assert "--json-dir gui-audit-artifacts" in test_job
    assert "--artifacts-dir gui-audit-artifacts/screenshots" in test_job
    assert test_job.index("python -m pytest --verbose") < test_job.index(
        "python tools_dev/gui_audit.py --matrix"
    )

    upload = "actions/upload-artifact@" + ACTION_PINS["actions/upload-artifact"]
    assert upload in test_job
    assert "if: failure()" in test_job
    assert "name: visual-evidence-${{ matrix.runner }}" in test_job
    assert "src/gui-audit-artifacts/" in test_job
    assert "src/visual-test-artifacts/" in test_job
    assert "if-no-files-found: warn" in test_job
    assert "retention-days: 14" in test_job

    # Keep the expensive DPI expansion inside the audit launcher rather than
    # multiplying the three supported OS runners in the Actions matrix.
    strategy = test_job.split("    runs-on:", 1)[0]
    assert "scale:" not in strategy
    # Intel macOS takes roughly an hour for the complete Qt suite; 30 minutes
    # cancelled a healthy run at 53%.
    assert "timeout-minutes: 90" in test_job


def test_release_candidate_proves_all_native_packages_for_exact_sha():
    header = CANDIDATE_WORKFLOW.split("jobs:", 1)[0]
    assert "branches: [main]" in header
    assert "workflow_dispatch:" in header
    assert "candidate_sha:" in header
    assert "permissions:\n  contents: read" in header
    assert "cancel-in-progress: false" in header

    verify = _job(CANDIDATE_WORKFLOW, "verify-candidate")
    assert '"$REQUESTED_SHA" != "$GITHUB_SHA"' in verify
    assert 'CHECKED_OUT_SHA="$(git rev-parse HEAD)"' in verify
    assert "candidate_sha=$CHECKED_OUT_SHA" in verify
    assert "python validate_version.py" in verify

    windows = _job(CANDIDATE_WORKFLOW, "build-windows")
    assert "runs-on: windows-2025" in windows
    assert "ref: ${{ needs.verify-candidate.outputs.candidate_sha }}" in windows
    initialize = windows.index("Initialize guaranteed Windows evidence")
    checkout = windows.index("Check out the verified candidate SHA")
    assert initialize < checkout
    assert "${{ runner.temp }}/frameguard-candidate-logs/" in windows
    assert 'cmd.exe /d /c "build_installer.bat --skip-delta"' in windows
    assert "frameguard-candidate-logs\\builder.log" in windows
    smoke = windows.index("tools_dev/windows_installer_smoke.ps1")
    manifest = windows.index("candidate-manifest.json")
    upload = windows.index("name: Upload the proven Windows installer")
    assert smoke < manifest < upload
    evidence_upload = windows.split(
        "- name: Upload Windows candidate evidence", 1)[1]
    assert "if: always()" in evidence_upload
    assert "src/Output/candidate-logs/" in evidence_upload
    assert "if-no-files-found: error" in evidence_upload
    assert "timeout-minutes: 12" in windows
    assert "retention-days: 90" in windows

    mac = _job(CANDIDATE_WORKFLOW, "build-macos")
    assert "runner: macos-15" in mac
    assert "runner: macos-15-intel" in mac
    assert "target_arch: arm64" in mac
    assert "target_arch: x86_64" in mac
    assert "timeout-minutes: 150" in mac
    assert "bash build_mac.sh --skip-delta" in mac
    smoke = mac.index("tools_dev/macos_package_smoke.py")
    manifest = mac.index("candidate-manifest.json")
    upload = mac.index("name: Upload the proven DMG")
    assert smoke < manifest < upload
    evidence_upload = mac.split(
        "- name: Upload macOS candidate evidence", 1)[1]
    assert "if: always()" in evidence_upload
    assert "retention-days: 90" in evidence_upload


def test_release_requires_successful_candidate_for_exact_tag_commit():
    verify = _job(RELEASE_WORKFLOW, "verify")
    assert "release_sha: ${{ steps.candidate.outputs.release_sha }}" in verify
    assert "tag_object_sha: ${{ steps.candidate.outputs.tag_object_sha }}" in verify
    assert 'TAG_OBJECT_SHA="$(git rev-parse "refs/tags/${RELEASE_TAG}")"' in verify
    assert "release-candidate.yml/runs?head_sha=${RELEASE_SHA}" in verify
    assert "tests.yml/runs?head_sha=${RELEASE_SHA}" in verify
    assert 'run.get("head_sha") == expected_sha' in verify
    assert 'run.get("conclusion") == "success"' in verify
    assert 'run.get("head_branch") == "main"' in verify
    assert "No successful Release Candidate run exists" in verify
    assert "No successful main-push Tests run exists" in verify

    windows = _job(RELEASE_WORKFLOW, "build-windows")
    mac = _job(RELEASE_WORKFLOW, "build-macos")
    for job in (windows, mac):
        assert "ref: ${{ needs.verify.outputs.release_sha }}" in job
    assert "timeout-minutes: 120" in windows
    assert "timeout-minutes: 150" in mac
    assert "timeout-minutes: 12" in windows
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "needs: [verify, build-windows, build-macos]" in publish
    assert "VERIFIED_RELEASE_SHA: ${{ needs.verify.outputs.release_sha }}" in publish
    assert "VERIFIED_TAG_OBJECT_SHA: ${{ needs.verify.outputs.tag_object_sha }}" in publish
    assert publish.count('REF_OBJECT" != "$VERIFIED_TAG_OBJECT_SHA') == 2
    assert publish.count('CURRENT_SHA" != "$VERIFIED_RELEASE_SHA') == 2
    draft = publish.index("Create or reconcile the draft release")
    final = publish.index("Publish the verified release")
    assert draft < publish.index("tag object changed before draft reconciliation")
    assert final < publish.index("tag object changed before publication")
    assert publish.index("tag object changed before publication") < publish.index(
        'gh release edit "$RELEASE_TAG" --draft=false')


def test_release_paths_forbid_all_package_bypass_flags():
    for workflow in (CANDIDATE_WORKFLOW, RELEASE_WORKFLOW):
        for forbidden in (
            "--fast",
            "--skip-tests",
            "--allow-unsigned",
            "--skip-installer-smoke",
        ):
            assert forbidden not in workflow
        assert "--skip-delta" in workflow


def test_release_checks_out_only_the_requested_tag_ref():
    verify = _job(RELEASE_WORKFLOW, "verify")
    assert "if: github.event_name == 'workflow_dispatch'" in verify
    assert "REQUESTED_TAG: ${{ inputs.tag }}" in verify
    assert '[[ ! "$REQUESTED_TAG" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]; then' in verify
    assert '"$GITHUB_REF" != "refs/tags/${REQUESTED_TAG}"' in verify
    assert "git/ref/tags/${REQUESTED_TAG}" in verify
    assert "ref: refs/tags/${{ env.RELEASE_TAG }}" in verify
    assert 'git cat-file -t "refs/tags/${RELEASE_TAG}"' in verify
    assert "Releases require an annotated tag" in verify
    assert RELEASE_WORKFLOW.count("ref: refs/tags/${{ env.RELEASE_TAG }}") == 1
    assert RELEASE_WORKFLOW.count(
        "ref: ${{ needs.verify.outputs.release_sha }}") == 2


def test_release_installs_are_constrained_and_jobs_are_bounded():
    verify = _job(RELEASE_WORKFLOW, "verify")
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "timeout-minutes: 15" in verify
    assert "timeout-minutes: 30" in publish
    assert "PIP_CONSTRAINT: ${{ github.workspace }}/src/constraints-3.12.txt" in RELEASE_WORKFLOW
    assert RELEASE_WORKFLOW.count("--constraint") >= 2
    assert 'cyclonedx-bom==7.5.0' not in RELEASE_WORKFLOW
    assert RELEASE_WORKFLOW.count("--skip-delta") == 2
    assert CANDIDATE_WORKFLOW.count("--skip-delta") == 2
    assert "release-windows\\manifest.json" not in RELEASE_WORKFLOW
    assert "release-macos/manifest.json" not in RELEASE_WORKFLOW


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
    assert CANDIDATE_WORKFLOW.count("overwrite: true") == CANDIDATE_WORKFLOW.count(
        upload_pin)


def test_publish_reconciles_only_drafts_and_verifies_asset_names_and_sizes():
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "already published and immutable" in publish
    attest = publish.index("actions/attest-build-provenance")
    assert publish.index("already published and immutable") < attest
    latest_guard = publish.index("Refuse to supersede a newer published release")
    assert latest_guard < attest
    assert "--retry 3 --retry-all-errors" in publish
    assert '404)' in publish
    assert "Refusing to mark" in publish
    assert "already published" in publish
    assert "--verify-tag" in publish
    assert "gh release delete-asset" in publish
    assert "gh release upload \"$RELEASE_TAG\" stage/* --clobber" in publish
    assert "path.stat().st_size" in publish
    assert "expected-assets.tsv" in publish
    assert "uploaded-assets.tsv" in publish
    assert "diff -u expected-assets.tsv uploaded-assets.tsv" in publish
    assert "isDraft" in publish


def test_publish_uses_verified_release_assets_without_a_git_payload_mirror():
    publish = _job(RELEASE_WORKFLOW, "publish")
    reconcile = publish.index("Create or reconcile the draft release")
    verify_assets = publish.index("diff -u expected-assets.tsv uploaded-assets.tsv")
    undraft = publish.index('gh release edit "$RELEASE_TAG" --draft=false')
    assert reconcile < verify_assets < undraft

    for required_asset in (
        "FrameGuardInstaller.exe",
        "FrameGuard-arm64.dmg",
        "FrameGuard-intel.dmg",
        "SHA256SUMS.txt",
    ):
        assert required_asset in publish

    for retired_channel_fragment in (
        "windows-delta",
        "macos-delta",
        "update-manifest-windows.json",
        "update-manifest-macos.json",
        "refs/heads/releases",
        "Mirror the delta channel",
        "Output/release-windows",
        "Output/release-macos",
    ):
        assert retired_channel_fragment not in RELEASE_WORKFLOW


def test_publish_keeps_least_privilege_environment_and_release_options():
    publish = _job(RELEASE_WORKFLOW, "publish")
    assert "GH_REPO: ${{ github.repository }}" in publish
    assert "group: release-publish" in publish
    assert "cancel-in-progress: false" in publish
    assert "environment: release" in publish
    assert "contents: write" in publish
    assert "id-token: write" in publish
    assert "attestations: write" in publish
    workflow_header = RELEASE_WORKFLOW.split("jobs:", 1)[0]
    assert "contents: read" in workflow_header
    assert "actions: read" in workflow_header

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
    assert "continue-on-error: true" in mac
    assert "if: matrix.label == 'arm64'" in mac
    assert '|| echo "no windows SBOM"' in publish
    assert '|| echo "no macos SBOM"' in publish
