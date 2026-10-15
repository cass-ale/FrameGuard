"""Full-installer updater safety tests.

The optional differential updater has its own regression suite. These tests cover
only the shipped GitHub/local full-installer flow.
"""

import hashlib
import io
import json
import time

import pytest

from frameguard import updater as U


def _capture_error(errors, callback, *args):
    try:
        callback(*args)
    except Exception as error:
        errors.append(error)


def _stalled_local_copy_worker(_source, destination, _size, _digest,
                               connection):
    with open(destination, "xb") as output:
        output.write(b"partial")
        output.flush()
        connection.send(("progress", len(b"partial")))
        while True:
            time.sleep(60)


def _slow_local_copy_worker(source, destination, expected_size, expected_digest,
                            connection):
    copied = 0
    hasher = hashlib.sha256()
    try:
        with open(source, "rb") as input_file, open(destination, "xb") as output:
            while True:
                chunk = input_file.read(64 * 1024)
                if not chunk:
                    break
                time.sleep(0.02)
                output.write(chunk)
                hasher.update(chunk)
                copied += len(chunk)
                connection.send(("progress", copied))
        if copied != expected_size or hasher.hexdigest() != expected_digest:
            raise RuntimeError("slow worker integrity mismatch")
        connection.send(("done", copied))
    except Exception as error:
        connection.send(("error", str(error)))
    finally:
        connection.close()


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _Response:
    def __init__(self, body=b"", headers=None, status=200):
        self._body = io.BytesIO(body)
        self.headers = headers or {}
        self.status = status

    def read(self, size=-1):
        return self._body.read(size)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestExactReleaseAssets:
    def test_windows_requires_one_exact_canonical_installer(self):
        assets = [
            {"name": "FrameGuardSetup.exe"},
            {"name": "FrameGuardInstaller-old.exe"},
        ]
        assert U._pick_release_asset(assets, platform="win32") is None

    def test_duplicate_windows_canonical_assets_are_ambiguous(self):
        assets = [
            {"name": "FrameGuardInstaller.exe", "id": 1},
            {"name": "frameguardinstaller.EXE", "id": 2},
        ]
        assert U._pick_release_asset(assets, platform="win32") is None

    @pytest.mark.parametrize("machine, expected", [
        ("arm64", "FrameGuard-arm64.dmg"),
        ("aarch64", "FrameGuard-arm64.dmg"),
        ("x86_64", "FrameGuard-intel.dmg"),
        ("amd64", "FrameGuard-intel.dmg"),
    ])
    def test_macos_requires_the_exact_architecture_asset(self, machine, expected):
        assets = [
            {"name": "FrameGuard.dmg"},
            {"name": "FrameGuard-arm64.dmg"},
            {"name": "FrameGuard-intel.dmg"},
        ]
        picked = U._pick_release_asset(
            assets, platform="darwin", machine=machine)
        assert picked["name"] == expected

    def test_duplicate_macos_architecture_assets_are_ambiguous(self):
        assets = [
            {"name": "FrameGuard-arm64.dmg", "id": 1},
            {"name": "frameguard-ARM64.dmg", "id": 2},
        ]
        assert U._pick_release_asset(
            assets, platform="darwin", machine="arm64") is None


class TestReleaseMetadata:
    @pytest.mark.parametrize("remote, local, expected", [
        ("1.2.4", "1.2.3", True),
        ("v1.2.3", "1.2.3", False),
        ("1.2", "1.1.9", False),
        ("1.two.3", "1.2.2", False),
        ("1.2.3-beta", "1.2.2", False),
        (None, "1.2.3", False),
    ])
    def test_only_complete_stable_versions_compare(self, remote, local, expected):
        assert U._is_newer(remote, local) is expected

    def test_release_without_a_trusted_digest_is_not_offered(
            self, monkeypatch):
        payload = {
            "tag_name": "v9.9.9",
            "body": "update",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "FrameGuardInstaller.exe",
                "browser_download_url": (
                    "https://example.test/FrameGuardInstaller.exe"),
                "size": 100,
            }],
        }
        monkeypatch.setattr(U.sys, "platform", "win32")
        monkeypatch.setattr(U, "get_current_version", lambda: "1.0.0")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(json.dumps(payload).encode()))

        assert U.check_github_for_update() is None

    def test_sha256sums_asset_supplies_installer_digest(
            self, monkeypatch):
        installer = b"verified installer"
        digest = hashlib.sha256(installer).hexdigest()
        payload = {
            "tag_name": "v9.9.9",
            "body": "update",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "FrameGuardInstaller.exe",
                    "browser_download_url": (
                        "https://example.test/FrameGuardInstaller.exe"),
                    "size": len(installer),
                },
                {
                    "name": "SHA256SUMS.txt",
                    "browser_download_url": (
                        "https://example.test/SHA256SUMS.txt"),
                    "size": 100,
                },
            ],
        }

        def _urlopen(request, **_kwargs):
            if request.full_url.endswith("SHA256SUMS.txt"):
                return _Response(
                    f"{digest}  FrameGuardInstaller.exe\n".encode())
            return _Response(json.dumps(payload).encode())

        monkeypatch.setattr(U.sys, "platform", "win32")
        monkeypatch.setattr(U, "get_current_version", lambda: "1.0.0")
        monkeypatch.setattr("urllib.request.urlopen", _urlopen)

        info = U.check_github_for_update()

        assert info is not None
        assert info.sha256 == digest
        assert info.size_bytes == len(installer)
        assert info.asset_name == "FrameGuardInstaller.exe"

    @pytest.mark.parametrize("response", [
        _Response(b"not json"),
        _Response(b"{}", status=403),
        _Response(json.dumps([]).encode()),
    ])
    def test_malformed_or_non_success_api_response_is_rejected(
            self, monkeypatch, response):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: response)
        assert U.check_github_for_update() is None

    @pytest.mark.parametrize("extra", [
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "not-a-version"},
    ])
    def test_uninstallable_release_metadata_is_rejected(
            self, monkeypatch, extra):
        digest = hashlib.sha256(b"installer").hexdigest()
        payload = {
            "tag_name": "v9.9.9",
            "body": "update",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "FrameGuardInstaller.exe",
                "browser_download_url": (
                    "https://example.test/FrameGuardInstaller.exe"),
                "size": len(b"installer"),
                "digest": f"sha256:{digest}",
            }],
        }
        payload.update(extra)
        monkeypatch.setattr(U.sys, "platform", "win32")
        monkeypatch.setattr(U, "get_current_version", lambda: "1.0.0")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(json.dumps(payload).encode()))

        assert U.check_github_for_update() is None


class TestLocalInstallerSource:
    def test_path_requires_a_trusted_digest(
            self, qapp, tmp_path, monkeypatch):
        installer = tmp_path / "FrameGuardInstaller.exe"
        installer.write_bytes(b"installer")
        manifest = {
            "version": "9.9.9",
            "installer_windows": installer.name,
        }
        (tmp_path / "version.json").write_text(json.dumps(manifest))
        monkeypatch.setattr(U.sys, "platform", "win32")
        manager = U.UpdateManager()

        assert "SHA-256 digest is missing" in manager.test_path(str(tmp_path))

        manifest["sha256"] = hashlib.sha256(installer.read_bytes()).hexdigest()
        (tmp_path / "version.json").write_text(json.dumps(manifest))
        assert manager.test_path(str(tmp_path)).startswith("Valid!")


class TestBoundedDownload:
    def test_truncated_content_length_fails_and_cleans_owned_staging(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_kwargs: str(stage))
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                b"abc", {"Content-Length": "10"}))
        manager = U.UpdateManager()

        assert manager._download_installer(
            "https://example.test/FrameGuardInstaller.exe") == ""
        assert not stage.exists()

    def test_oversized_declared_body_is_rejected_before_writing(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_kwargs: str(stage))
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                b"x", {"Content-Length": str(2 * 1024 ** 3 + 1)}))
        manager = U.UpdateManager()

        assert manager._download_installer(
            "https://example.test/FrameGuardInstaller.exe") == ""
        assert not stage.exists()

    def test_total_download_deadline_is_enforced(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        body = b"verified"
        ticks = iter([0.0, 0.0, U._MAX_DOWNLOAD_SECONDS + 1.0])
        monkeypatch.setattr(U.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                body, {"Content-Length": str(len(body))}))
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="exceeded 30 minutes"):
            manager._download_installer(
                "https://example.test/FrameGuardInstaller.exe",
                staging_dir=str(stage),
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert list(stage.iterdir()) == []

    def test_hash_mismatch_refuses_the_download_and_cleans_staging(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        body = b"tampered"
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                body, {"Content-Length": str(len(body))}))
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="integrity"):
            manager._download_installer(
                "https://example.test/FrameGuardInstaller.exe",
                staging_dir=str(stage),
                expected_size=len(body),
                expected_sha256="0" * 64,
            )
        assert list(stage.iterdir()) == []

    def test_cancellation_between_chunks_removes_partial_file(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        manager = U.UpdateManager()
        body = b"a" * (512 * 1024)

        class _CancellingResponse(_Response):
            def read(self, size=-1):
                chunk = super().read(size)
                if chunk:
                    manager.cancel_update()
                return chunk

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _CancellingResponse(
                body, {"Content-Length": str(len(body))}))

        with pytest.raises(RuntimeError, match="cancel"):
            manager._download_installer(
                "https://example.test/FrameGuardInstaller.exe",
                staging_dir=str(stage),
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert list(stage.iterdir()) == []


    def test_valid_unknown_length_body_is_still_size_and_hash_checked(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        body = b"verified"
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(body))
        manager = U.UpdateManager()

        path = manager._download_installer(
            "https://example.test/FrameGuardInstaller.exe",
            staging_dir=str(stage),
            expected_size=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )

        assert open(path, "rb").read() == body

    def test_endless_body_is_bounded_by_maximum_size(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        stage.mkdir()
        body = b"abcd"
        monkeypatch.setattr(U, "MAX_INSTALLER_BYTES", 3)
        monkeypatch.setattr(U, "_MIN_FREE_SPACE_BYTES", 0)
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(body))
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="2 GiB limit"):
            manager._download_installer(
                "https://example.test/FrameGuardInstaller.exe",
                staging_dir=str(stage),
                expected_size=3,
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert list(stage.iterdir()) == []

    def test_write_failure_removes_partial_artifact(
            self, qapp, monkeypatch, tmp_path):
        import builtins

        stage = tmp_path / "stage"
        stage.mkdir()
        body = b"verified"
        real_open = builtins.open

        def _open(path, mode="r", *args, **kwargs):
            if mode == "xb":
                raise OSError("disk write failed")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _open)
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                body, {"Content-Length": str(len(body))}))
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="disk write failed"):
            manager._download_installer(
                "https://example.test/FrameGuardInstaller.exe",
                staging_dir=str(stage),
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert list(stage.iterdir()) == []

    def test_low_disk_is_rejected_before_file_creation(
            self, qapp, monkeypatch, tmp_path):
        from types import SimpleNamespace

        stage = tmp_path / "stage"
        stage.mkdir()
        body = b"verified"
        monkeypatch.setattr(
            U.shutil, "disk_usage",
            lambda _path: SimpleNamespace(free=1))
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                body, {"Content-Length": str(len(body))}))
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="free disk space"):
            manager._download_installer(
                "https://example.test/FrameGuardInstaller.exe",
                staging_dir=str(stage),
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )
        assert list(stage.iterdir()) == []


class TestBoundedLocalStaging:
    def test_copy_worker_is_spawn_picklable_and_frozen_entrypoint_is_ready(self):
        from pathlib import Path

        target = U._LOCAL_COPY_WORKER_TARGET
        assert target.__module__ == "frameguard.updater"
        assert "<locals>" not in target.__qualname__
        main_source = (Path(U.__file__).resolve().parent.parent / "main.py").read_text()
        assert "multiprocessing.freeze_support()" in main_source

    def _paths(self, tmp_path, body=b"installer"):
        source = tmp_path / "FrameGuardInstaller.exe"
        source.write_bytes(body)
        stage = tmp_path / "stage"
        stage.mkdir()
        return source, stage, hashlib.sha256(body).hexdigest()

    def test_blocked_local_read_is_cancellable_without_leaking_process(
            self, qapp, monkeypatch, tmp_path):
        import threading

        source, stage, digest = self._paths(tmp_path)
        manager = U.UpdateManager()
        monkeypatch.setattr(
            U, "_LOCAL_COPY_WORKER_TARGET", _stalled_local_copy_worker)
        errors = []
        thread = threading.Thread(
            target=lambda: _capture_error(
                errors,
                manager._stage_local_installer,
                str(source),
                str(stage),
                source.stat().st_size,
                digest,
            ))
        thread.start()
        dest = stage / source.name
        assert _wait_until(lambda: dest.exists())
        process = manager._local_copy_process
        assert process is not None and process.is_alive()
        child_pid = process.pid

        manager.cancel_update()
        thread.join(3)

        assert not thread.is_alive()
        assert errors and "cancel" in str(errors[0]).lower()
        assert not dest.exists()
        assert manager._local_copy_process is None
        assert child_pid not in {child.pid
                                 for child in U.multiprocessing.active_children()}

    def test_blocked_local_read_obeys_deadline_and_is_reaped(
            self, qapp, monkeypatch, tmp_path):
        source, stage, digest = self._paths(tmp_path)
        manager = U.UpdateManager()
        monkeypatch.setattr(
            U, "_LOCAL_COPY_WORKER_TARGET", _stalled_local_copy_worker)
        monkeypatch.setattr(U, "_MAX_LOCAL_COPY_SECONDS", 0.15)
        monkeypatch.setattr(U, "_LOCAL_COPY_STOP_SECONDS", 0.5)
        started = time.monotonic()

        with pytest.raises(RuntimeError, match="deadline"):
            manager._stage_local_installer(
                str(source), str(stage), source.stat().st_size, digest)

        assert time.monotonic() - started < 3
        assert not (stage / source.name).exists()
        assert manager._local_copy_process is None
        assert not any(child.name == "FrameGuardLocalInstallerCopy"
                       for child in U.multiprocessing.active_children())

    def test_slow_local_copy_remains_bounded_and_verified(
            self, qapp, monkeypatch, tmp_path):
        body = b"slow but valid" * 40_000
        source, stage, digest = self._paths(tmp_path, body)
        manager = U.UpdateManager()
        monkeypatch.setattr(
            U, "_LOCAL_COPY_WORKER_TARGET", _slow_local_copy_worker)
        monkeypatch.setattr(U, "_MAX_LOCAL_COPY_SECONDS", 5)

        dest = manager._stage_local_installer(
            str(source), str(stage), len(body), digest)

        assert open(dest, "rb").read() == body
        assert manager._local_copy_process is None

    def test_normal_local_copy_preserves_bytes_and_hash_contract(
            self, qapp, tmp_path):
        body = b"ordinary local installer" * 20_000
        source, stage, digest = self._paths(tmp_path, body)
        manager = U.UpdateManager()

        dest = manager._stage_local_installer(
            str(source), str(stage), len(body), digest)

        assert open(dest, "rb").read() == body
        assert manager._local_copy_process is None

    def test_local_size_mismatch_cleans_partial_staging(
            self, qapp, tmp_path):
        source, stage, digest = self._paths(tmp_path)
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="truncated"):
            manager._stage_local_installer(
                str(source), str(stage), source.stat().st_size + 1, digest)

        assert list(stage.iterdir()) == []
        assert manager._local_copy_process is None

    def test_local_hash_mismatch_cleans_partial_staging(
            self, qapp, tmp_path):
        source, stage, _digest = self._paths(tmp_path)
        manager = U.UpdateManager()

        with pytest.raises(RuntimeError, match="integrity"):
            manager._stage_local_installer(
                str(source), str(stage), source.stat().st_size, "0" * 64)

        assert list(stage.iterdir()) == []
        assert manager._local_copy_process is None


class TestStagingOwnership:
    def test_cancelled_blocked_local_apply_removes_owned_staging_tree(
            self, qapp, monkeypatch, tmp_path):
        import threading

        installer = tmp_path / "FrameGuard-arm64.dmg"
        installer.write_bytes(b"installer")
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        stage = tmp_path / "stage"
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_kwargs: str(stage))
        monkeypatch.setattr(
            U, "_LOCAL_COPY_WORKER_TARGET", _stalled_local_copy_worker)
        monkeypatch.setattr(
            U.subprocess, "Popen",
            lambda *_a, **_k: pytest.fail("cancelled staging must not launch"))
        manager = U.UpdateManager()
        statuses = []
        manager.status_changed.connect(statuses.append)
        info = U.UpdateInfo(
            version="9.9.9",
            installer_path=str(installer),
            sha256=digest,
            size_bytes=installer.stat().st_size,
        )
        thread = threading.Thread(target=manager._do_apply, args=(info,))
        thread.start()
        assert _wait_until(lambda: (stage / installer.name).exists())

        manager.cancel_update()
        thread.join(3)
        qapp.processEvents()

        assert not thread.is_alive()
        assert not stage.exists()
        assert manager._local_copy_process is None
        assert "Update cancelled." in statuses

    def test_launch_failure_cleans_the_single_staging_tree(
            self, qapp, monkeypatch, tmp_path):
        installer = tmp_path / "FrameGuard-arm64.dmg"
        installer.write_bytes(b"installer")
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        stage = tmp_path / "stage"
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_kwargs: str(stage))
        monkeypatch.setattr(U.sys, "platform", "darwin")
        monkeypatch.setattr(
            U.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(
                OSError("launch failed")))
        manager = U.UpdateManager()
        failures = []
        manager.check_failed.connect(failures.append)
        info = U.UpdateInfo(
            version="9.9.9",
            installer_path=str(installer),
            sha256=digest,
            size_bytes=installer.stat().st_size,
        )

        manager._do_apply(info)

        assert failures and "launch failed" in failures[-1]
        assert not stage.exists()

    def test_successful_remote_launch_uses_and_retains_one_staging_tree(
            self, qapp, monkeypatch, tmp_path):
        body = b"installer"
        digest = hashlib.sha256(body).hexdigest()
        stage = tmp_path / "stage"
        made = []

        def _mkdtemp(**_kwargs):
            made.append(str(stage))
            return str(stage)

        launched = []
        monkeypatch.setattr(U.tempfile, "mkdtemp", _mkdtemp)
        monkeypatch.setattr(U.sys, "platform", "darwin")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                body, {"Content-Length": str(len(body))}))
        monkeypatch.setattr(
            U.subprocess, "Popen", lambda command: launched.append(command))
        manager = U.UpdateManager()
        info = U.UpdateInfo(
            version="9.9.9",
            installer_path="https://example.test/FrameGuard-arm64.dmg",
            sha256=digest,
            size_bytes=len(body),
            asset_name="FrameGuard-arm64.dmg",
        )

        manager._do_apply(info)

        assert made == [str(stage)]
        assert launched == [["/usr/bin/open", str(stage / info.asset_name)]]
        assert stage in {type(stage)(path)
                         for path in manager._retained_staging_dirs}
        assert (stage / info.asset_name).read_bytes() == body
