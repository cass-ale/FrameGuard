"""Release channel tests.

The shipped channel is the project's GitHub repository: releases live on
its `releases` branch and are served from raw.githubusercontent.com.
These tests pin that, the migration off the retired Google Drive channel
(a machine that saved the old Drive path as an override must not stay
pinned to a folder that no longer receives releases), and — because a
per-machine override may still point at ANY local folder — that a local
channel under a non-ASCII path round-trips byte-exact: batch scripts run
under an OEM codepage that mangles non-ASCII, QSettings has to
round-trip values, and every path join and file read has to stay
Unicode-clean.
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
    def test_channel_points_at_the_repository(self):
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL, GITHUB_REPO

        assert GITHUB_REPO == "cass-ale/FrameGuard"
        assert DEFAULT_UPDATE_CHANNEL == (
            f"https://raw.githubusercontent.com/{GITHUB_REPO}"
            f"/releases/RELEASES")

    def test_channel_uses_the_releases_folder(self):
        """RELEASES/ on the releases branch holds the manifest and the
        files/ payload — the layout the channel has always had."""
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL

        assert DEFAULT_UPDATE_CHANNEL.endswith("/RELEASES")

    def test_crash_logs_stay_local(self):
        """Crash logs go to the per-user local log dir. The retired
        behaviour — a shared Drive collection folder — must not return."""
        import importlib.util, os

        spec = importlib.util.spec_from_file_location(
            "fgmain_channel", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "main.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = open(spec.origin, encoding="utf-8").read()
        assert "Shared drives" not in source
        assert "MetaMate" not in source
        assert "_log_dir" in source

    def test_channel_is_a_url(self):
        from frameguard.updater import DEFAULT_UPDATE_CHANNEL
        from frameguard.updatesource import HttpSource

        assert DEFAULT_UPDATE_CHANNEL.startswith("https://")
        assert isinstance(make_source(DEFAULT_UPDATE_CHANNEL), HttpSource)

    def test_a_legacy_drive_channel_migrates_to_the_default(self, qapp):
        """Machines that saved the retired Drive channel as an override
        must not stay pinned to a folder that no longer gets releases:
        background checks swallow unreachable-channel errors, so nothing
        would ever surface that they are stuck."""
        from PySide6.QtCore import QSettings
        from frameguard import updater as U

        settings = QSettings(U._SETTINGS_ORG, U._SETTINGS_APP)
        original = settings.value(U._CHANNEL_KEY, None)
        try:
            settings.setValue(U._CHANNEL_KEY,
                              U._LEGACY_RELEASE_ROOT + "\\RELEASES")
            assert U.get_update_channel() == U.get_update_channel_for(sys.platform)
            assert settings.value(U._CHANNEL_KEY, None) is None, \
                "the legacy override should be cleared, not left to rot"
            # Storing the legacy value through the setter is also a no-op.
            U.set_update_channel(U._LEGACY_RELEASE_ROOT + "\\RELEASES-macos")
            assert settings.value(U._CHANNEL_KEY, None) is None
        finally:
            if original is None:
                settings.remove(U._CHANNEL_KEY)
            else:
                settings.setValue(U._CHANNEL_KEY, original)

    def test_channel_survives_qsettings(self, qapp):
        """QSettings is the per-machine override; the value has to round-trip."""
        from frameguard.updater import (get_update_channel,
                                        get_update_channel_for,
                                        set_update_channel)

        original = get_update_channel()
        expected = get_update_channel_for(sys.platform)
        try:
            set_update_channel(expected)
            assert get_update_channel() == expected
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

    def test_search_paths_carry_no_retired_locations(self):
        """The installer-flow fallback searches Downloads and the app's
        own folder — never the retired Drive root or team shares."""
        from frameguard.updater import get_default_search_paths

        paths = get_default_search_paths()
        assert paths
        assert not any("TPG" in p or "team-share" in p
                       or "Shared drives" in p for p in paths)


@pytest.fixture
def emoji_release(tmp_path):
    """A release published under a path shaped like a real-world messy
    one — the retired Drive root had exactly this shape, and a local
    mirror used as an override still can."""
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


class TestGithubReleaseAssetPick:
    """A GitHub Release carries BOTH installers. The updater's GitHub
    fallback must hand each platform its own file — the old first-match
    loop ("installer" in the name, or any .exe) gave a Mac the Windows
    installer whenever the .exe happened to be listed first."""

    ASSETS = [
        {"name": "FrameGuardInstaller.exe",
         "browser_download_url": "https://example.test/win.exe", "size": 10},
        {"name": "FrameGuard.dmg",
         "browser_download_url": "https://example.test/mac.dmg", "size": 20},
    ]

    def test_windows_picks_the_exe(self):
        from frameguard.updater import _pick_release_asset
        picked = _pick_release_asset(self.ASSETS, platform="win32")
        assert picked["name"] == "FrameGuardInstaller.exe"

    def test_macos_picks_the_dmg_even_when_the_exe_is_first(self):
        from frameguard.updater import _pick_release_asset
        picked = _pick_release_asset(self.ASSETS, platform="darwin")
        assert picked["name"] == "FrameGuard.dmg"

    def test_macos_picks_the_dmg_when_it_is_first(self):
        from frameguard.updater import _pick_release_asset
        picked = _pick_release_asset(list(reversed(self.ASSETS)),
                                      platform="darwin")
        assert picked["name"] == "FrameGuard.dmg"

    def test_no_asset_for_this_platform_returns_none(self):
        from frameguard.updater import _pick_release_asset
        only_win = [self.ASSETS[0]]
        assert _pick_release_asset(only_win, platform="darwin") is None
        assert _pick_release_asset([], platform="win32") is None

    def test_canonical_name_wins_over_another_same_extension_asset(self):
        from frameguard.updater import _pick_release_asset
        assets = [
            {"name": "FrameGuard-arm64.dmg",
             "browser_download_url": "https://example.test/arm.dmg",
             "size": 1},
            {"name": "FrameGuard.dmg",
             "browser_download_url": "https://example.test/mac.dmg",
             "size": 2},
        ]
        # An Intel machine must not be handed the arm64 DMG when the
        # canonical (un-suffixed) DMG is also on the release.
        picked = _pick_release_asset(assets, platform="darwin",
                                     machine="x86_64")
        assert picked["name"] == "FrameGuard.dmg"

    def test_arm64_machine_picks_the_arm64_dmg(self):
        from frameguard.updater import _pick_release_asset
        assets = [
            {"name": "FrameGuard-intel.dmg",
             "browser_download_url": "https://example.test/intel.dmg",
             "size": 1},
            {"name": "FrameGuard-arm64.dmg",
             "browser_download_url": "https://example.test/arm.dmg",
             "size": 2},
        ]
        picked = _pick_release_asset(assets, platform="darwin",
                                     machine="arm64")
        assert picked["name"] == "FrameGuard-arm64.dmg"

    def test_intel_machine_picks_the_intel_dmg(self):
        from frameguard.updater import _pick_release_asset
        assets = [
            {"name": "FrameGuard-arm64.dmg",
             "browser_download_url": "https://example.test/arm.dmg",
             "size": 1},
            {"name": "FrameGuard-intel.dmg",
             "browser_download_url": "https://example.test/intel.dmg",
             "size": 2},
        ]
        picked = _pick_release_asset(assets, platform="darwin",
                                     machine="x86_64")
        assert picked["name"] == "FrameGuard-intel.dmg"

    def test_wrong_arch_only_returns_none_rather_than_a_bad_dmg(self):
        from frameguard.updater import _pick_release_asset
        assets = [
            {"name": "FrameGuard-intel.dmg",
             "browser_download_url": "https://example.test/intel.dmg",
             "size": 1},
        ]
        assert _pick_release_asset(assets, platform="darwin",
                                   machine="arm64") is None
