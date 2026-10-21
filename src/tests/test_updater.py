"""Full-installer updater safety tests.

The optional differential updater has its own regression suite. These tests cover
only the shipped GitHub Releases full-installer flow.
"""

import hashlib
import io
import json

import pytest

from frameguard import updater as U


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


class TestNoLocalInstallerFallback:
    """The local-folder fallback trusted the SHA-256 in the version.json
    sitting next to the installer (the folder vouched for itself), joined
    unsanitised installer names, and still searched ~/Downloads/FGv0.2.2.
    It is gone: only HTTPS GitHub Releases installers are ever applied."""

    def test_fallback_api_is_removed(self):
        for name in ("get_default_search_paths", "_check_path_for_update",
                     "create_version_json", "_copy_local_installer_worker",
                     "_LOCAL_COPY_WORKER_TARGET"):
            assert not hasattr(U, name), name
        for name in ("test_path", "get_update_source", "set_update_source",
                     "_stage_local_installer", "_stop_local_copy_process"):
            assert not hasattr(U.UpdateManager, name), name
        source = open(U.__file__, encoding="utf-8").read()
        assert "FGv0.2.2" not in source
        assert "multiprocessing" not in source

    def test_dropped_installer_and_version_json_are_never_offered(
            self, qapp, monkeypatch, tmp_path):
        """A folder that looks like an old local release must be ignored:
        with GitHub reporting nothing newer, the check is 'up to date'."""
        home = tmp_path / "home"
        folder = home / "Downloads" / "FGv0.2.2"
        folder.mkdir(parents=True)
        installer = folder / "FrameGuardInstaller.exe"
        installer.write_bytes(b"installer")
        (folder / "version.json").write_text(json.dumps({
            "version": "99.0.0", "installer_windows": installer.name,
            "sha256": hashlib.sha256(b"installer").hexdigest()}))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setattr(U, "check_github_for_update", lambda: None)
        manager = U.UpdateManager()
        monkeypatch.setattr(manager, "check_channel_for_delta", lambda: False)
        found, none = [], []
        manager.update_available.connect(found.append)
        manager.update_not_found.connect(lambda: none.append(True))

        manager._do_check()
        qapp.processEvents()

        assert found == []
        assert none == [True]

    @pytest.mark.parametrize("location", [
        "FrameGuardInstaller.exe",
        "../FrameGuardInstaller.exe",
        "/tmp/FrameGuardInstaller.exe",
        "C:\\Users\\x\\Downloads\\FrameGuardInstaller.exe",
        "\\\\server\\share\\FrameGuardInstaller.exe",
        "http://example.test/FrameGuardInstaller.exe",
        "file:///tmp/FrameGuardInstaller.exe",
        "",
    ])
    def test_non_https_installer_location_is_refused_before_staging(
            self, qapp, monkeypatch, location):
        monkeypatch.setattr(
            U.tempfile, "mkdtemp",
            lambda **_k: pytest.fail("a refused location must not stage"))
        monkeypatch.setattr(
            U.subprocess, "Popen",
            lambda *_a, **_k: pytest.fail("a refused location must not launch"))
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: pytest.fail("a refused location must not download"))
        manager = U.UpdateManager()
        failures = []
        manager.apply_failed.connect(failures.append)
        info = U.UpdateInfo(
            version="9.9.9", installer_path=location,
            sha256=hashlib.sha256(b"installer").hexdigest(), size_bytes=9)

        manager._do_apply(info)

        assert failures and "not an HTTPS URL" in failures[-1]
        assert manager._applying is False


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


class TestStagingOwnership:
    def test_cancelled_download_removes_owned_staging_tree(
            self, qapp, monkeypatch, tmp_path):
        stage = tmp_path / "stage"
        body = b"a" * (512 * 1024)
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_kwargs: str(stage))
        monkeypatch.setattr(
            U.subprocess, "Popen",
            lambda *_a, **_k: pytest.fail("cancelled staging must not launch"))
        manager = U.UpdateManager()

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
        statuses = []
        manager.status_changed.connect(statuses.append)
        info = U.UpdateInfo(
            version="9.9.9",
            installer_path="https://example.test/FrameGuard-arm64.dmg",
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            asset_name="FrameGuard-arm64.dmg",
        )

        manager._do_apply(info)
        qapp.processEvents()

        assert not stage.exists()
        assert "Update cancelled." in statuses

    def test_launch_failure_cleans_the_single_staging_tree(
            self, qapp, monkeypatch, tmp_path):
        body = b"installer"
        digest = hashlib.sha256(body).hexdigest()
        stage = tmp_path / "stage"
        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_kwargs: str(stage))
        monkeypatch.setattr(U.sys, "platform", "darwin")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_args, **_kwargs: _Response(
                body, {"Content-Length": str(len(body))}))
        monkeypatch.setattr(
            U.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(
                OSError("launch failed")))
        manager = U.UpdateManager()
        failures = []
        manager.check_failed.connect(failures.append)
        info = U.UpdateInfo(
            version="9.9.9",
            installer_path="https://example.test/FrameGuard-arm64.dmg",
            sha256=digest,
            size_bytes=len(body),
            asset_name="FrameGuard-arm64.dmg",
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
