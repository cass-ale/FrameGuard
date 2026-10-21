"""0.9.0 release hardening: pinned build tools and the release contracts.

Each class names the audit finding it pins. Updater, delta and crash-handler
regressions live beside their existing suites; this file holds the
build-time and release-pipeline contracts that have no other home.
"""

import hashlib
import importlib.util
import os
import re
from pathlib import Path
from unittest import mock

import pytest

SRC = Path(__file__).resolve().parent.parent
REPO_ROOT = SRC.parent


def _load_download_tools(name):
    spec = importlib.util.spec_from_file_location(
        name, str(SRC / "download_tools.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_tool_entries(m):
    """(label, entry) for every tool on every platform and architecture."""
    specs = [("windows", m.sources_for("windows"))]
    for arch in ("arm64", "x86_64"):
        specs.append((f"darwin-{arch}", m.sources_for("darwin", arch)))
    for label, spec in specs:
        for name, entry in spec.items():
            if isinstance(entry, dict):
                yield f"{label}/{name}", entry


# Words that mark a URL whose content can change under the same address.
_MOVING_URL_MARKERS = ("latest", "nightly", "master", "redirect", "snapshot",
                       "release-essentials", "/current/", "daily")


class TestPinnedBuildTools:
    """C-H3: every bundled third-party tool is pinned to an immutable URL and
    a SHA-256 committed in the repository, and a mismatch fails closed."""

    def test_every_source_has_a_pinned_sha256_and_versioned_urls(self):
        m = _load_download_tools("dl_pins_contract")
        entries = list(_all_tool_entries(m))
        # Windows: ffmpeg (+ffprobe) zip and MediaInfo; macOS: ffmpeg,
        # ffprobe and MediaInfo for each of the two architectures.
        assert len(entries) == 2 + 3 * 2
        for label, entry in entries:
            digest = entry.get("sha256")
            assert isinstance(digest, str) and re.fullmatch(
                r"[0-9a-f]{64}", digest), f"{label} has no pinned SHA-256"
            assert "sha256_from_url" not in entry, \
                f"{label} still trusts a checksum served next to the download"
            assert entry["urls"], f"{label} has no source"
            for url in entry["urls"]:
                assert url.startswith("https://"), f"{label}: {url}"
                lowered = url.lower()
                for marker in _MOVING_URL_MARKERS:
                    assert marker not in lowered, \
                        f"{label} uses a moving URL ({marker!r}): {url}"
                version = (m.MEDIAINFO_VERSION if "mediainfo" in label
                           else m.FFMPEG_VERSION)
                assert version in url, f"{label} URL is not versioned: {url}"
        # Distinct architectures must never share an FFmpeg archive.
        mac_ffmpeg = {entry["sha256"] for label, entry in entries
                      if label.startswith("darwin-") and label.endswith("/ffmpeg")}
        assert len(mac_ffmpeg) == 2

    def test_mac_archives_are_pinned_per_architecture(self):
        m = _load_download_tools("dl_pins_arch")
        arm = m.sources_for("darwin", "arm64")
        intel = m.sources_for("darwin", "x86_64")
        for tool in ("ffmpeg", "ffprobe"):
            assert "/macos/arm64/" in arm[tool]["urls"][0]
            assert "/macos/amd64/" in intel[tool]["urls"][0]
            assert arm[tool]["sha256"] != intel[tool]["sha256"]
        # MediaInfo's Mac CLI is one universal DMG for both architectures.
        assert arm["mediainfo"] == intel["mediainfo"]

    def test_windows_ffmpeg_comes_from_the_official_github_release(self):
        m = _load_download_tools("dl_pins_windows")
        urls = m.sources_for("windows")["ffmpeg"]["urls"]
        assert urls[0] == (
            "https://github.com/GyanD/codexffmpeg/releases/download/"
            f"{m.FFMPEG_VERSION}/ffmpeg-{m.FFMPEG_VERSION}-essentials_build.zip")
        assert not any("BtbN" in url for url in urls)

    def _entry(self, payload: bytes, *, digest=None, urls=None):
        return {
            "required": True,
            "members": ["ffmpeg"],
            "urls": urls or ["https://example.test/ffmpeg-1.0.zip"],
            "sha256": digest or hashlib.sha256(payload).hexdigest(),
        }

    @staticmethod
    def _fake_download(payloads):
        """download_file stand-in writing the next payload for each call."""
        calls = []

        def _download(url, dest, label=""):
            calls.append(url)
            Path(dest).write_bytes(payloads[len(calls) - 1])
            return True, url
        return _download, calls

    def test_fetch_rejects_a_download_whose_hash_differs(self, tmp_path):
        m = _load_download_tools("dl_pins_mismatch")
        entry = self._entry(b"the pinned archive")
        download, calls = self._fake_download([b"something else entirely"])
        with mock.patch.object(m, "download_file", side_effect=download), \
             mock.patch.object(m, "extract_members") as extract:
            assert m.fetch("darwin", "ffmpeg", entry, tmp_path, force=False) is False
        assert calls == entry["urls"]
        extract.assert_not_called()
        assert not m.is_cached("ffmpeg", entry, tmp_path)

    def test_fetch_tries_the_next_mirror_after_a_mismatch(self, tmp_path):
        m = _load_download_tools("dl_pins_mirror")
        good = b"the pinned archive"
        entry = self._entry(good, urls=["https://a.test/x-1.0.zip",
                                        "https://b.test/x-1.0.zip"])
        download, calls = self._fake_download([b"tampered", good])

        def _extract(_archive, members, dest):
            for member in members:
                (dest / member).write_bytes(b"binary")
            return {member.lower() for member in members}

        with mock.patch.object(m, "download_file", side_effect=download), \
             mock.patch.object(m, "extract_members", side_effect=_extract):
            assert m.fetch("darwin", "ffmpeg", entry, tmp_path, force=False) is True
        assert calls == entry["urls"]
        assert m.is_cached("ffmpeg", entry, tmp_path)

    def test_entry_without_a_pinned_hash_is_never_downloaded(self, tmp_path):
        m = _load_download_tools("dl_pins_missing")
        entry = self._entry(b"x")
        del entry["sha256"]
        with mock.patch.object(m, "download_file") as download:
            assert m.fetch("windows", "ffmpeg", entry, tmp_path, force=False) is False
        download.assert_not_called()

    def test_cache_from_an_unpinned_source_is_refetched_not_shipped(self, tmp_path):
        m = _load_download_tools("dl_pins_cache")
        good = b"the pinned archive"
        entry = self._entry(good)
        # A binary cached by an older build that had no pins.
        (tmp_path / "ffmpeg").write_bytes(b"old unverified build")
        assert not m.is_cached("ffmpeg", entry, tmp_path)

        download, calls = self._fake_download([good])

        def _extract(_archive, members, dest):
            for member in members:
                (dest / member).write_bytes(b"pinned binary")
            return {member.lower() for member in members}

        with mock.patch.object(m, "download_file", side_effect=download), \
             mock.patch.object(m, "extract_members", side_effect=_extract):
            assert m.fetch("darwin", "ffmpeg", entry, tmp_path, force=False)
        assert len(calls) == 1
        assert (tmp_path / "ffmpeg").read_bytes() == b"pinned binary"
        # The second build-script invocation stages from the stamped cache.
        with mock.patch.object(m, "download_file") as again:
            assert m.fetch("darwin", "ffmpeg", entry, tmp_path, force=False)
        again.assert_not_called()

    def test_main_never_stages_tools_that_are_not_from_a_pinned_archive(
            self, tmp_path, monkeypatch):
        m = _load_download_tools("dl_pins_stage")
        cache = tmp_path / "cache"
        cache.mkdir()
        for name in ("ffmpeg.exe", "ffprobe.exe", "mediainfo.exe"):
            (cache / name).write_bytes(b"stale")
        dest = tmp_path / "dist" / "tools"
        dest.parent.mkdir(parents=True)
        monkeypatch.setattr(m, "cache_dir", lambda *_a, **_k: cache)
        monkeypatch.setattr(m, "fetch", lambda *_a, **_k: False)
        monkeypatch.setattr(m.sys, "argv", [
            "download_tools.py", "--platform", "windows", "--dest", str(dest)])
        assert m.main() == 1
        assert not dest.exists() or list(dest.iterdir()) == []


def _pascal_calls(source: str, name: str):
    """Top-level argument lists of every `name(...)` call in Pascal source.

    Understands single-quoted strings (with '' escapes) so commas and
    parentheses inside message text do not split arguments.
    """
    calls = []
    for match in re.finditer(rf"\b{name}\s*\(", source):
        index = match.end()
        depth, args, current, in_string = 1, [], [], False
        while depth:
            char = source[index]
            if in_string:
                current.append(char)
                if char == "'":
                    in_string = False
            elif char == "'":
                in_string = True
                current.append(char)
            elif char == "(":
                depth += 1
                current.append(char)
            elif char == ")":
                depth -= 1
                if depth:
                    current.append(char)
            elif char == "," and depth == 1:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(char)
            index += 1
        args.append("".join(current).strip())
        calls.append((match.start(), args))
    return calls


class TestSilentWindowsUpdate:
    """C-M1: the in-app Windows update never stops at a message box after
    FrameGuard has quit."""

    @staticmethod
    def _iss():
        return (SRC / "installer.iss").read_text(encoding="utf-8-sig")

    @staticmethod
    def _code_section(iss):
        return iss[iss.index("[Code]"):]

    def test_updater_launches_setup_silently_without_message_boxes(
            self, qapp, monkeypatch, tmp_path):
        from frameguard import updater as U

        body = b"installer"
        stage = tmp_path / "stage"
        launched = []

        class _Response:
            status = 200
            headers = {"Content-Length": str(len(body))}

            def __init__(self):
                import io
                self._io = io.BytesIO(body)

            def read(self, size=-1):
                return self._io.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(U.tempfile, "mkdtemp", lambda **_k: str(stage))
        monkeypatch.setattr(U.sys, "platform", "win32")
        monkeypatch.setattr(U.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Response())
        monkeypatch.setattr(
            U.subprocess, "Popen",
            lambda command, **kwargs: launched.append((command, kwargs)))
        manager = U.UpdateManager()
        info = U.UpdateInfo(
            version="9.9.9",
            installer_path="https://example.test/FrameGuardInstaller.exe",
            sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body),
            asset_name="FrameGuardInstaller.exe")

        manager._do_apply(info)

        assert len(launched) == 1
        command, kwargs = launched[0]
        assert command == [str(stage / "FrameGuardInstaller.exe"), "/SILENT",
                           "/SUPPRESSMSGBOXES", "/NORESTART",
                           "/RESTARTAPPLICATIONS"]
        assert kwargs["creationflags"] == 0x8

    def test_relaunch_check_matches_the_updater_arguments(self):
        from frameguard.updater import WINDOWS_INSTALLER_ARGS

        code = self._code_section(self._iss())
        assert "WizardSilent and" in code
        assert "Pos('/RESTARTAPPLICATIONS', Uppercase(GetCmdTail))" in code
        assert "/RESTARTAPPLICATIONS" in WINDOWS_INSTALLER_ARGS
        assert {"/SILENT", "/SUPPRESSMSGBOXES"} <= set(WINDOWS_INSTALLER_ARGS)

    def test_every_installer_message_box_is_suppressible_with_a_default(self):
        code = self._code_section(self._iss())
        bare = [m.start() for m in re.finditer(r"(?<!Suppressible)\bMsgBox\s*\(", code)]
        assert bare == [], "MsgBox ignores /SUPPRESSMSGBOXES; use SuppressibleMsgBox"
        calls = _pascal_calls(code, "SuppressibleMsgBox")
        assert len(calls) >= 4
        for _offset, args in calls:
            assert len(args) == 4, args
            assert args[1].startswith("mb"), args
            assert re.fullmatch(r"ID[A-Z]+", args[3]), args

    def test_upgrade_and_repair_prompts_are_skipped_when_silent(self):
        code = self._code_section(self._iss())
        init = code[code.index("function InitializeSetup"):
                    code.index("procedure CurStepChanged")]
        guard = init.index("(not WizardSilent) and")
        registry = init.index("RegQueryStringValue(HKEY_CURRENT_USER")
        repair = init.index("is already installed.")
        upgrade = init.index("will be updated from v")
        assert guard < registry < repair < upgrade
        # The repair question defaults to Yes so an unattended same-version
        # install still repairs instead of silently aborting.
        repair_call = next(args for offset, args in
                           _pascal_calls(init, "SuppressibleMsgBox")
                           if "is already installed." in args[0])
        assert repair_call[3] == "IDYES"

    def test_post_install_and_uninstall_prompts_are_skipped_when_silent(self):
        code = self._code_section(self._iss())
        post = code[code.index("procedure CurStepChanged"):
                    code.index("procedure CurUninstallStepChanged")]
        assert "(not WizardSilent) and (not FileExists(FFmpegPath))" in post
        uninstall = code[code.index("procedure CurUninstallStepChanged"):]
        assert "(not UninstallSilent) and DirExists(ExportPath)" in uninstall
        delete_call = _pascal_calls(uninstall, "SuppressibleMsgBox")[0][1]
        assert delete_call[3] == "IDNO", "a suppressed prompt must keep reports"
