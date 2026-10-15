"""Differential update tests.

The update path is the one feature that can destroy a working installation, so
these cover the safety properties rather than just the happy path: nothing is
touched until every staged file verifies, a tampered payload is rejected, and a
failed apply rolls back.
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from frameguard import manifest as M
from frameguard import deltaupdate as D
from frameguard.updatesource import make_source


def make_tree(root, spec):
    for rel, data in spec.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
    return root


@pytest.fixture
def install(tmp_path):
    return make_tree(str(tmp_path / "install"), {
        "FrameGuard.exe": "VERSION 1",
        "_internal/runtime.dll": "B" * 50_000,     # big, unchanging
        "_internal/obsolete.dat": "remove me",
    })


@pytest.fixture
def release(tmp_path):
    payload = make_tree(str(tmp_path / "release" / M.PAYLOAD_DIRNAME), {
        "FrameGuard.exe": "VERSION 2",
        "_internal/runtime.dll": "B" * 50_000,     # identical to the install
        "_internal/added.dll": "NEW",
    })
    manifest = M.build_manifest(payload, app_version="0.5.3",
                                entry_point="FrameGuard.exe")
    M.write_manifest(manifest, os.path.join(str(tmp_path / "release"),
                                            M.MANIFEST_FILENAME))
    return str(tmp_path / "release"), manifest


class TestManifest:
    def test_paths_are_posix_style(self, install):
        m = M.build_manifest(install)
        assert all("\\" not in key for key in m["files"])

    def test_logs_are_excluded(self, tmp_path):
        root = make_tree(str(tmp_path / "app"), {
            "app.exe": "x",
            "frameguard_crash.log": "boom",
            "sub/thing.log": "noise",
            "__pycache__/mod.pyc": "cached",
        })
        files = M.build_manifest(root)["files"]
        assert "app.exe" in files
        assert not any("log" in k or "pyc" in k for k in files)

    def test_identical_trees_produce_an_empty_plan(self, install):
        m = M.build_manifest(install)
        plan = M.diff_manifests(m, m)
        assert plan.is_empty
        assert plan.total_bytes == 0
        assert "up to date" in plan.summary().lower()

    def test_unchanged_files_are_not_transferred(self, install, release):
        """The whole point: the 50KB runtime must not be re-downloaded."""
        _, manifest = release
        plan = M.diff_against_install(install, manifest)
        assert "_internal/runtime.dll" not in plan.transfer
        assert "FrameGuard.exe" in plan.replace
        assert "_internal/added.dll" in plan.add
        assert "_internal/obsolete.dat" in plan.delete

    def test_transfer_is_far_smaller_than_the_build(self, install, release):
        _, manifest = release
        full = sum(f["size"] for f in manifest["files"].values())
        plan = M.diff_against_install(install, manifest)
        assert plan.total_bytes < full * 0.2, (
            f"delta {plan.total_bytes} is not much smaller than full {full}")

    def test_corrupted_local_file_is_repaired(self, install, release):
        """Diffing against the real tree, not a stored manifest, means a file
        damaged after install gets replaced."""
        _, manifest = release
        with open(os.path.join(install, "_internal", "runtime.dll"), "wb") as f:
            f.write(b"corrupted")
        plan = M.diff_against_install(install, manifest)
        assert "_internal/runtime.dll" in plan.replace

    def test_missing_manifest_returns_none(self, tmp_path):
        assert M.load_manifest(str(tmp_path / "nope.json")) is None

    def test_malformed_manifest_returns_none(self, tmp_path):
        bad = tmp_path / "manifest.json"
        bad.write_text("{not json", encoding="utf-8")
        assert M.load_manifest(str(bad)) is None

    def test_future_manifest_version_is_refused(self, tmp_path):
        """Better to refuse than to half-understand a format that might make
        us delete the wrong files."""
        import json
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({"manifest_version": 999, "files": {}}),
                     encoding="utf-8")
        assert M.load_manifest(str(p)) is None

    def test_verify_tree_detects_tampering(self, install):
        m = M.build_manifest(install)
        assert M.verify_tree(install, m) == []
        with open(os.path.join(install, "FrameGuard.exe"), "wb") as f:
            f.write(b"tampered")
        assert "FrameGuard.exe" in M.verify_tree(install, m)


class TestUpdateSources:
    """One channel setting must work for a local folder, a UNC share or a URL."""

    def test_path_yields_a_local_source(self, tmp_path):
        from frameguard.updatesource import LocalSource, make_source
        assert isinstance(make_source(str(tmp_path)), LocalSource)

    def test_unc_path_yields_a_local_source(self):
        from frameguard.updatesource import LocalSource, make_source
        assert isinstance(make_source(r"\\share\FrameGuard\release"), LocalSource)

    def test_url_yields_an_http_source(self):
        from frameguard.updatesource import HttpSource, make_source
        for url in ("http://host/rel", "https://host/rel", "HTTPS://Host/Rel"):
            assert isinstance(make_source(url), HttpSource), url

    def test_empty_channel_is_rejected(self):
        from frameguard.updatesource import UpdateSourceError, make_source
        with pytest.raises(UpdateSourceError):
            make_source("   ")

    def test_local_source_reads_and_fetches(self, tmp_path):
        from frameguard.updatesource import make_source

        payload = tmp_path / M.PAYLOAD_DIRNAME
        payload.mkdir()
        (payload / "FrameGuard.exe").write_bytes(b"BUILD")
        manifest = M.build_manifest(str(payload), app_version="9.9.9")
        M.write_manifest(manifest, str(tmp_path / M.MANIFEST_FILENAME))

        source = make_source(str(tmp_path))
        assert source.read_manifest()["app_version"] == "9.9.9"

        dest = tmp_path / "out.bin"
        source.fetch("FrameGuard.exe", str(dest))
        assert dest.read_bytes() == b"BUILD"

    def test_missing_manifest_raises(self, tmp_path):
        from frameguard.updatesource import UpdateSourceError, make_source
        with pytest.raises(UpdateSourceError):
            make_source(str(tmp_path)).read_manifest()

    def test_unreachable_channel_is_not_an_exception_for_callers(self):
        """A laptop off the VPN must not produce an error dialog on a
        background check."""
        from frameguard.updatesource import make_source
        assert make_source("http://127.0.0.1:1/nope").is_reachable() is False

    def test_missing_payload_file_raises(self, tmp_path):
        from frameguard.updatesource import UpdateSourceError, make_source

        (tmp_path / M.PAYLOAD_DIRNAME).mkdir()
        M.write_manifest(M.build_manifest(str(tmp_path / M.PAYLOAD_DIRNAME)),
                         str(tmp_path / M.MANIFEST_FILENAME))
        with pytest.raises(UpdateSourceError):
            make_source(str(tmp_path)).fetch("ghost.dll", str(tmp_path / "x"))


class TestUpdateChannel:
    def test_a_default_channel_ships_with_the_app(self):
        """A fresh install must already point at the project's release
        location — the repository."""
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL
        assert DEFAULT_UPDATE_CHANNEL.strip()

    def test_channel_round_trips(self, qapp, tmp_path):
        from frameguard.updater import get_update_channel, set_update_channel

        original = get_update_channel()
        try:
            set_update_channel(str(tmp_path))
            assert get_update_channel() == str(tmp_path)
        finally:
            set_update_channel(original)

    def test_blank_channel_falls_back_to_the_default(self, qapp):
        from frameguard.updater import (get_update_channel,
                                        get_update_channel_for,
                                        set_update_channel)
        original = get_update_channel()
        try:
            set_update_channel("")
            assert get_update_channel() == get_update_channel_for(sys.platform)
        finally:
            set_update_channel(original)

    def test_auto_check_interval_is_24_hours(self):
        from frameguard import updater
        assert updater._CHECK_INTERVAL_HOURS == 24
class TestInstallRootDetection:
    def test_source_checkout_is_refused(self, monkeypatch):
        """Overwriting a developer's working tree with a release payload would
        be destructive."""
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert D.find_install_root() is None

    def test_frozen_windows_uses_the_exe_directory(self, monkeypatch):
        import ntpath
        from types import SimpleNamespace

        windows_sys = SimpleNamespace(
            frozen=True,
            executable=r"C:\Apps\FrameGuard\FrameGuard.exe",
            platform="win32",
        )
        windows_os = SimpleNamespace(
            name="nt", path=ntpath, pathsep=";", sep="\\")
        monkeypatch.setattr(D, "sys", windows_sys)
        monkeypatch.setattr(D, "os", windows_os)

        assert D.find_install_root() == r"C:\Apps\FrameGuard"


class TestSwapScript:
    def _plan(self):
        return M.UpdatePlan(add=["_internal/added.dll"], replace=["FrameGuard.exe"],
                            delete=["_internal/obsolete.dat"], total_bytes=10,
                            app_version="0.5.3", entry_point="FrameGuard.exe")

    def test_script_waits_for_the_process_to_exit(self, tmp_path):
        """A running executable is locked on Windows and cannot replace itself."""
        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"),
                                     self._plan(), pid=4242)
        body = open(script, encoding="utf-8").read()
        assert "4242" in body
        assert ("waitloop" in body) or ("kill -0" in body)

    def test_script_backs_up_before_overwriting(self, tmp_path):
        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"),
                                     self._plan(), pid=1)
        body = open(script, encoding="utf-8").read()
        assert "backup" in body.lower()

    def test_script_can_roll_back(self, tmp_path):
        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"),
                                     self._plan(), pid=1)
        body = open(script, encoding="utf-8").read()
        assert "rollback" in body.lower() or "Restoring" in body

    def test_script_removes_deleted_files(self, tmp_path):
        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"),
                                     self._plan(), pid=1)
        assert "obsolete.dat" in open(script, encoding="utf-8").read()

    def test_script_relaunches_the_app(self, tmp_path):
        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"),
                                     self._plan(), pid=1)
        body = open(script, encoding="utf-8").read()
        assert "FrameGuard" in body

    @pytest.mark.skipif(os.name != "nt", reason="batch helper is Windows-only")
    def test_swap_actually_applies_the_update(self, tmp_path):
        """Executes the real helper against throwaway directories."""
        install = make_tree(str(tmp_path / "install"), {
            "FrameGuard.exe": "VERSION 1",
            "_internal/keep.dll": "UNCHANGED",
            "_internal/obsolete.dat": "gone soon",
        })
        staging = make_tree(str(tmp_path / "staging"), {
            "FrameGuard.exe": "VERSION 2",
            "_internal/added.dll": "NEW",
        })

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()

        script = D.write_swap_script(install, staging, self._plan(), pid=dead.pid)
        body = open(script, encoding="utf-8").read().replace('start ""', "echo skip")
        with open(script, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(body)

        result = subprocess.run(["cmd", "/c", script], capture_output=True,
                                text=True, timeout=180)
        assert result.returncode == 0, result.stdout

        assert open(os.path.join(install, "FrameGuard.exe")).read() == "VERSION 2"
        assert os.path.exists(os.path.join(install, "_internal", "added.dll"))
        assert not os.path.exists(os.path.join(install, "_internal", "obsolete.dat"))
        assert open(os.path.join(install, "_internal", "keep.dll")).read() == "UNCHANGED"

        shutil.rmtree(os.path.join(tempfile.gettempdir(),
                                   "frameguard_update_backup"), ignore_errors=True)


class TestStaging:
    def test_staging_verifies_before_touching_the_install(self, install, release, qapp):
        """A bad payload must cost nothing: the installation is only modified
        after every staged file matches its hash."""
        release_dir, manifest = release
        plan = M.diff_against_install(install, manifest)

        # Corrupt the payload the client would pull.
        payload = os.path.join(release_dir, M.PAYLOAD_DIRNAME)
        with open(os.path.join(payload, "FrameGuard.exe"), "wb") as f:
            f.write(b"TAMPERED")

        worker = D.StageWorker(make_source(release_dir), plan, manifest)
        failures = []
        worker.failed.connect(failures.append)
        worker.run()

        assert failures, "tampered payload was accepted"
        assert "integrity" in failures[0].lower()
        # Installation untouched.
        assert open(os.path.join(install, "FrameGuard.exe")).read() == "VERSION 1"

    def test_missing_payload_file_is_reported(self, install, release, qapp):
        release_dir, manifest = release
        plan = M.diff_against_install(install, manifest)
        os.remove(os.path.join(release_dir, M.PAYLOAD_DIRNAME, "FrameGuard.exe"))

        worker = D.StageWorker(make_source(release_dir), plan, manifest)
        failures = []
        worker.failed.connect(failures.append)
        worker.run()

        assert failures and "missing" in failures[0].lower()

    def test_successful_staging_copies_only_the_delta(self, install, release, qapp):
        release_dir, manifest = release
        plan = M.diff_against_install(install, manifest)

        worker = D.StageWorker(make_source(release_dir), plan, manifest)
        staged = []
        worker.finished.connect(staged.append)
        worker.failed.connect(lambda m: pytest.fail(m))
        worker.run()

        assert staged
        staging = staged[0]
        assert os.path.exists(os.path.join(staging, "FrameGuard.exe"))
        assert os.path.exists(os.path.join(staging, "_internal", "added.dll"))
        # The unchanged runtime must NOT have been copied.
        assert not os.path.exists(os.path.join(staging, "_internal", "runtime.dll"))
        shutil.rmtree(staging, ignore_errors=True)
