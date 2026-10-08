"""Release channel tests.

The team's release root contains non-ASCII characters:

    G:\\Shared drives\\TPG XR\\<star>gDrive Reorganization<star>\\...\\MetaMate Follower

That is a real hazard. Batch scripts run under an OEM codepage that mangles
non-ASCII, QSettings has to round-trip it, and every path join and file read
has to stay Unicode-clean. These tests pin that behaviour so a future change
cannot quietly break updates for the whole team.
"""

import os
import subprocess
import sys
import tempfile

import pytest

from frameguard import deltaupdate as D
from frameguard import manifest as M
from frameguard.updatesource import make_source

STAR = "\u2b50"


class TestConfiguredChannel:
    def test_channel_points_at_the_team_release_root(self):
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL, _RELEASE_ROOT

        assert "MetaMate Follower" in _RELEASE_ROOT
        assert "T260340134" in _RELEASE_ROOT
        assert DEFAULT_UPDATE_CHANNEL.startswith(_RELEASE_ROOT)

    def test_channel_uses_the_existing_releases_folder(self):
        """RELEASES/ already holds FrameGuardInstaller.exe; the manifest and
        payload go alongside it rather than in a second folder."""
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL

        assert DEFAULT_UPDATE_CHANNEL.endswith("RELEASES")

    def test_crash_logs_target_the_project_folder(self):
        """MetaMate Follower/CrashLogs is the real shared collection point."""
        import importlib.util, os

        spec = importlib.util.spec_from_file_location(
            "fgmain_channel", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "main.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = open(spec.origin, encoding="utf-8").read()
        assert "MetaMate Follower" in source
        assert "CrashLogs" in source

    def test_channel_is_absolute_not_a_url(self):
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL
        from frameguard.updatesource import LocalSource

        assert os.path.isabs(DEFAULT_UPDATE_CHANNEL)
        assert isinstance(make_source(DEFAULT_UPDATE_CHANNEL), LocalSource)

    def test_channel_survives_qsettings(self, qapp):
        """QSettings is the per-machine override; the emoji has to round-trip."""
        from frameguard.updater import (DEFAULT_UPDATE_CHANNEL,
                                        get_update_channel, set_update_channel)

        original = get_update_channel()
        try:
            set_update_channel(DEFAULT_UPDATE_CHANNEL)
            assert get_update_channel() == DEFAULT_UPDATE_CHANNEL
        finally:
            set_update_channel(original)

    def test_a_dead_stored_channel_heals_to_the_default(self, qapp, tmp_path):
        """A machine that once saved an old path stayed pinned to it forever
        and silently received no updates: background checks swallow
        unreachable-channel errors, so nothing surfaced the problem."""
        from PySide6.QtCore import QSettings
        from frameguard import updater as U

        settings = QSettings(U._SETTINGS_ORG, U._SETTINGS_APP)
        original = settings.value(U._CHANNEL_KEY, None)
        try:
            settings.setValue(U._CHANNEL_KEY, str(tmp_path / "deleted_share"))
            resolved = U.get_update_channel()
            if os.path.isdir(U.DEFAULT_UPDATE_CHANNEL):
                assert resolved == U.DEFAULT_UPDATE_CHANNEL
                assert settings.value(U._CHANNEL_KEY, None) is None, \
                    "the dead override should be cleared, not left to rot"
        finally:
            if original is None:
                settings.remove(U._CHANNEL_KEY)
            else:
                settings.setValue(U._CHANNEL_KEY, original)

    def test_a_live_override_is_respected(self, qapp, tmp_path):
        from PySide6.QtCore import QSettings
        from frameguard import updater as U

        settings = QSettings(U._SETTINGS_ORG, U._SETTINGS_APP)
        original = settings.value(U._CHANNEL_KEY, None)
        try:
            U.set_update_channel(str(tmp_path))
            assert U.get_update_channel() == str(tmp_path)
        finally:
            if original is None:
                settings.remove(U._CHANNEL_KEY)
            else:
                settings.setValue(U._CHANNEL_KEY, original)

    def test_setting_the_default_does_not_pin_the_machine(self, qapp):
        """Storing today's default would freeze this machine on it, so a future
        release that moves the channel could never reach it."""
        from PySide6.QtCore import QSettings
        from frameguard import updater as U

        settings = QSettings(U._SETTINGS_ORG, U._SETTINGS_APP)
        original = settings.value(U._CHANNEL_KEY, None)
        try:
            U.set_update_channel(U.DEFAULT_UPDATE_CHANNEL)
            assert settings.value(U._CHANNEL_KEY, None) is None
        finally:
            if original is None:
                settings.remove(U._CHANNEL_KEY)
            else:
                settings.setValue(U._CHANNEL_KEY, original)

    def test_http_override_is_not_probed_as_a_directory(self, qapp):
        from PySide6.QtCore import QSettings
        from frameguard import updater as U

        settings = QSettings(U._SETTINGS_ORG, U._SETTINGS_APP)
        original = settings.value(U._CHANNEL_KEY, None)
        try:
            U.set_update_channel("https://releases.internal/frameguard")
            assert U.get_update_channel() == "https://releases.internal/frameguard"
        finally:
            if original is None:
                settings.remove(U._CHANNEL_KEY)
            else:
                settings.setValue(U._CHANNEL_KEY, original)

    def test_release_root_is_in_the_update_search_paths(self, qapp):
        from frameguard.updater import _RELEASE_ROOT, get_default_search_paths

        if sys.platform != "win32":
            pytest.skip("Windows-only search paths")
        assert any(_RELEASE_ROOT in p for p in get_default_search_paths())


@pytest.fixture
def emoji_release(tmp_path):
    """A release published under a path shaped like the real one."""
    root = tmp_path / f"{STAR}gDrive Reorganization{STAR}" / "03 Desktop App" \
        / "MetaMate Follower" / "release"
    payload = root / M.PAYLOAD_DIRNAME
    (payload / "_internal").mkdir(parents=True)
    (payload / "FrameGuard.exe").write_bytes(b"BUILD-V2" * 500)
    (payload / "_internal" / "runtime.dll").write_bytes(b"R" * 20_000)

    manifest = M.build_manifest(str(payload), app_version="0.5.3",
                                entry_point="FrameGuard.exe")
    M.write_manifest(manifest, str(root / M.MANIFEST_FILENAME))
    return str(root), manifest


class TestNonAsciiReleasePath:
    def test_manifest_reads_from_an_emoji_path(self, emoji_release):
        root, _ = emoji_release
        assert make_source(root).read_manifest()["app_version"] == "0.5.3"

    def test_file_transfer_from_an_emoji_path_is_byte_exact(self, emoji_release, tmp_path):
        root, manifest = emoji_release
        dest = tmp_path / "staged.exe"
        make_source(root).fetch("FrameGuard.exe", str(dest))
        assert M.file_sha256(str(dest)) == manifest["files"]["FrameGuard.exe"]["sha256"]

    def test_diff_works_across_an_emoji_path(self, emoji_release, tmp_path):
        root, manifest = emoji_release
        install = tmp_path / "install"
        (install / "_internal").mkdir(parents=True)
        (install / "FrameGuard.exe").write_bytes(b"OLD")
        (install / "_internal" / "runtime.dll").write_bytes(b"R" * 20_000)

        plan = M.diff_against_install(str(install), manifest)
        assert plan.transfer == ["FrameGuard.exe"]
        assert "_internal/runtime.dll" not in plan.transfer

    def test_nested_payload_dir_is_found(self, emoji_release):
        """The release keeps its tree in files/ under the emoji folder."""
        root, _ = emoji_release
        assert os.path.isdir(os.path.join(root, M.PAYLOAD_DIRNAME))


class TestSwapScriptEncoding:
    def _plan(self):
        return M.UpdatePlan(add=["_internal/added.dll"], replace=["FrameGuard.exe"],
                            delete=["_internal/obsolete.dat"], total_bytes=10,
                            app_version="0.5.3", entry_point="FrameGuard.exe")

    @pytest.mark.skipif(os.name != "nt", reason="batch helper is Windows-only")
    def test_script_sets_a_utf8_codepage(self, tmp_path):
        """Without chcp 65001 the default OEM codepage mangles non-ASCII paths
        and the copy silently fails."""
        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"), self._plan(), pid=1)
        assert "chcp 65001" in open(script, encoding="utf-8").read()

    def test_release_channel_never_reaches_the_swap_script(self, tmp_path):
        """The helper only touches the install, staging and backup paths."""
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL

        script = D.write_swap_script(str(tmp_path / "install"),
                                     str(tmp_path / "staging"), self._plan(), pid=1)
        assert DEFAULT_UPDATE_CHANNEL not in open(script, encoding="utf-8").read()

    @pytest.mark.skipif(os.name != "nt", reason="batch helper is Windows-only")
    def test_swap_succeeds_with_non_ascii_install_path(self, tmp_path):
        """Worst realistic case: a symbol in the folder and an accent in the
        username. Executes the real helper."""
        install = tmp_path / f"{STAR}App Folder{STAR}" / "Jos\u00e9 FrameGuard"
        staging = tmp_path / "staging"
        (install / "_internal").mkdir(parents=True)
        (staging / "_internal").mkdir(parents=True)
        (install / "FrameGuard.exe").write_text("VERSION 1")
        (install / "_internal" / "keep.dll").write_text("UNCHANGED")
        (install / "_internal" / "obsolete.dat").write_text("bye")
        (staging / "FrameGuard.exe").write_text("VERSION 2")
        (staging / "_internal" / "added.dll").write_text("NEW")

        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()

        script = D.write_swap_script(str(install), str(staging), self._plan(),
                                     pid=dead.pid)
        body = open(script, encoding="utf-8").read().replace('start ""', "echo skip")
        with open(script, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(body)

        result = subprocess.run(["cmd", "/c", script], capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=180)
        assert result.returncode == 0, result.stdout

        assert (install / "FrameGuard.exe").read_text() == "VERSION 2"
        assert (install / "_internal" / "added.dll").exists()
        assert not (install / "_internal" / "obsolete.dat").exists()
        assert (install / "_internal" / "keep.dll").read_text() == "UNCHANGED"

        import shutil
        shutil.rmtree(os.path.join(tempfile.gettempdir(),
                                   "frameguard_update_backup"), ignore_errors=True)
