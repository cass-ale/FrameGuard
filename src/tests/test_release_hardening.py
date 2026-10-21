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


def _load_release_notes():
    spec = importlib.util.spec_from_file_location(
        "fg_release_notes", str(SRC / "release_notes.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SAMPLE_CHANGELOG = (
    "v2.1.0 - Faster scans\n\nFIXED\n- Scans start sooner.\n- Reports open.\n\n"
    "v2.0.1 - Patch\n\nFIXED\n- Old fix.\n\n"
    "v2.0.0 - Big release\n\nCHANGED\n- Everything.\n"
)


class TestReleaseNotes:
    """C-M2: the release body carries only this version's changes, and the
    update prompt shows those changes rather than download boilerplate."""

    def test_entry_is_exactly_the_requested_version(self):
        rn = _load_release_notes()
        assert rn.changelog_entry(_SAMPLE_CHANGELOG, "2.0.1") == (
            "v2.0.1 - Patch\n\nFIXED\n- Old fix.")
        assert rn.changelog_entry(_SAMPLE_CHANGELOG, "2.1.0").endswith(
            "- Reports open.")
        assert rn.changelog_entry(_SAMPLE_CHANGELOG, "2.0.0") == (
            "v2.0.0 - Big release\n\nCHANGED\n- Everything.")

    def test_missing_entry_fails_instead_of_publishing_old_notes(self):
        rn = _load_release_notes()
        with pytest.raises(rn.ReleaseNotesError, match="v3.0.0"):
            rn.changelog_entry(_SAMPLE_CHANGELOG, "3.0.0")
        with pytest.raises(rn.ReleaseNotesError):
            rn.render({"version": "2.1.0", "changelog": "v2.1.0 - x\n```\n"})

    def test_real_release_body_contains_only_the_current_entry(self):
        import json

        rn = _load_release_notes()
        data = json.loads((SRC / "version.json").read_text(encoding="utf-8"))
        notes = rn.render(data)
        headers = re.findall(r"^v\d+\.\d+\.\d+ - ", notes, re.MULTILINE)
        assert headers == [f"v{data['version']} - "]
        assert notes.count("## Changes") == 1
        assert rn.changelog_entry(data["changelog"], data["version"]) in notes
        assert len(notes) < len(data["changelog"]) + 2000

    def test_update_prompt_shows_the_changes_from_a_real_release_body(self):
        import json
        from frameguard import updater as U

        rn = _load_release_notes()
        data = json.loads((SRC / "version.json").read_text(encoding="utf-8"))
        entry = rn.changelog_entry(data["changelog"], data["version"])
        summary = U.release_notes_summary(rn.render(data))

        first_lines = [line for line in entry.splitlines() if line.strip()][:2]
        assert summary.startswith(first_lines[0])
        assert first_lines[1] in summary
        assert len(summary) <= U.MAX_CHANGELOG_CHARS
        for boilerplate in ("|", "SmartScreen", "SHA256SUMS", "```",
                            "Published by", "MIT License"):
            assert boilerplate not in summary

    def test_short_body_is_shown_whole_and_long_body_cuts_at_a_line(self):
        from frameguard import updater as U

        rn = _load_release_notes()
        body = rn.render({"version": "2.1.0", "changelog": _SAMPLE_CHANGELOG})
        assert U.release_notes_summary(body) == (
            "v2.1.0 - Faster scans\n\nFIXED\n- Scans start sooner.\n- Reports open.")
        cut = U.release_notes_summary(body, limit=40)
        assert cut == "v2.1.0 - Faster scans\n\nFIXED\u2026"

    def test_body_without_a_changes_heading_drops_tables_and_boilerplate(self):
        from frameguard import updater as U

        body = ("**Published by someone**\n\n## Download\n\n| A | B |\n|---|---|\n"
                "| x | y |\n\nFixed the crash on launch.\n")
        assert U.release_notes_summary(body) == "Fixed the crash on launch."
        assert U.release_notes_summary(None) == ""

    def test_github_check_offers_the_summary_not_the_raw_body(self, monkeypatch):
        import io
        import json
        from frameguard import updater as U

        rn = _load_release_notes()
        body = rn.render({"version": "9.9.9", "changelog":
                          "v9.9.9 - New\n\nFIXED\n- The thing.\n"})
        digest = hashlib.sha256(b"installer").hexdigest()
        payload = {
            "tag_name": "v9.9.9", "body": body, "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "FrameGuardInstaller.exe",
                "browser_download_url": "https://example.test/FrameGuardInstaller.exe",
                "size": 9, "digest": f"sha256:{digest}"}],
        }

        class _Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(U.sys, "platform", "win32")
        monkeypatch.setattr(U, "get_current_version", lambda: "1.0.0")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: _Response(json.dumps(payload).encode()))
        info = U.check_github_for_update()
        assert info is not None
        assert info.changelog == "v9.9.9 - New\n\nFIXED\n- The thing."

    def test_release_workflow_publishes_only_the_rendered_entry(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")
        assert 'python release_notes.py --output "$GITHUB_WORKSPACE/release-notes.md"' \
            in workflow
        assert 'data.get("changelog"' not in workflow
        assert "path: release-notes.md" in workflow


class TestOsNativeTls:
    """C-M3: update traffic is verified by the operating system (truststore),
    never by loading keychain certificates as roots."""

    @staticmethod
    def _assert_verifying(context):
        import ssl
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_truststore_context_is_used_when_available(self):
        truststore = pytest.importorskip("truststore")
        from frameguard import updater as U

        context = U.https_context()
        assert isinstance(context, truststore.SSLContext)
        self._assert_verifying(context)

    def test_updatesource_uses_the_same_os_native_context(self):
        truststore = pytest.importorskip("truststore")
        from frameguard import updatesource

        assert isinstance(updatesource._https_context(), truststore.SSLContext)

    def test_missing_truststore_falls_back_to_a_verifying_default(
            self, monkeypatch):
        import sys
        from frameguard import updater as U

        monkeypatch.setitem(sys.modules, "truststore", None)  # ImportError
        context = U.https_context()
        assert type(context).__module__ == "ssl"
        self._assert_verifying(context)

    def test_unusable_truststore_falls_back_to_a_verifying_default(
            self, monkeypatch):
        import sys
        import types
        from frameguard import updater as U

        broken = types.ModuleType("truststore")

        def _fail(*_a, **_k):
            raise OSError("Security framework unavailable")
        broken.SSLContext = _fail
        monkeypatch.setitem(sys.modules, "truststore", broken)
        context = U.https_context()
        assert type(context).__module__ == "ssl"
        self._assert_verifying(context)

    def test_truststore_backend_failing_at_import_falls_back(self, monkeypatch):
        import builtins
        import sys
        from frameguard import updater as U

        real_import = builtins.__import__

        def _import(name, *args, **kwargs):
            if name == "truststore":
                raise OSError("Security framework failed to load")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "truststore", raising=False)
        monkeypatch.setattr(builtins, "__import__", _import)
        context = U.https_context()
        assert type(context).__module__ == "ssl"
        self._assert_verifying(context)

    @pytest.mark.parametrize("truststore_available", [True, False])
    def test_keychain_certificates_are_never_added_as_roots(
            self, monkeypatch, truststore_available):
        """A certificate an administrator marked never-trust (or merely
        imported) in System.keychain must not become a trusted root: no
        keychain is read and no certificate data is loaded, on any path."""
        import ssl
        import sys
        from frameguard import updater as U

        if not truststore_available:
            monkeypatch.setitem(sys.modules, "truststore", None)
        monkeypatch.setattr(U.sys, "platform", "darwin")
        monkeypatch.setattr(
            U.subprocess, "run",
            mock.Mock(side_effect=AssertionError("keychain was read")))
        loaded = []
        original = ssl.SSLContext.load_verify_locations

        def _record(self, cafile=None, capath=None, cadata=None):
            loaded.append((cafile, capath, cadata))
            return original(self, cafile=cafile, capath=capath, cadata=cadata)

        monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", _record)
        U.https_context()
        assert all(cadata is None for _cafile, _capath, cadata in loaded)
        assert all(capath is None for _cafile, capath, _cadata in loaded)
        assert all(cafile is None or "certifi" in cafile
                   for cafile, _capath, _cadata in loaded)

    def test_keychain_trust_fallback_is_gone(self):
        source = (SRC / "frameguard" / "updater.py").read_text(encoding="utf-8")
        for marker in ("System.keychain", "find-certificate",
                       "include_admin", "cadata="):
            assert marker not in source, marker
        delta = (SRC / "frameguard" / "updatesource.py").read_text(encoding="utf-8")
        assert "include_admin" not in delta

    def test_truststore_is_a_pinned_runtime_dependency(self):
        requirements = (SRC / "requirements.txt").read_text(encoding="utf-8")
        constraints = (SRC / "constraints-3.12.txt").read_text(encoding="utf-8")
        assert re.search(r"^truststore>=0\.10\.5,<1\.0$", requirements, re.M)
        assert re.search(r"^truststore==0\.10\.5$", constraints, re.M)


class TestDeltaHardening:
    """C-O1: the optional delta channel only speaks HTTPS (bearer token or
    not) and refuses manifests that list a file without a SHA-256."""

    @pytest.mark.parametrize("channel", [
        "http://artifacts.example.test/frameguard",
        "HTTP://artifacts.example.test/frameguard",
        "ftp://artifacts.example.test/frameguard",
        "file:///srv/frameguard",
        "https://",
    ])
    @pytest.mark.parametrize("token", [None, "secret-token"])
    def test_non_https_url_channels_are_refused(self, channel, token):
        from frameguard.updatesource import UpdateSourceError, make_source
        with pytest.raises(UpdateSourceError):
            make_source(channel, token=token)

    def test_http_source_itself_refuses_plain_http(self):
        from frameguard.updatesource import HttpSource, UpdateSourceError
        with pytest.raises(UpdateSourceError):
            HttpSource("http://artifacts.example.test/rel", token="secret")
        assert HttpSource("https://artifacts.example.test/rel").base_url == (
            "https://artifacts.example.test/rel")

    def test_folder_channels_still_work(self, tmp_path):
        from frameguard.updatesource import LocalSource, make_source
        assert isinstance(make_source(str(tmp_path)), LocalSource)
        assert isinstance(make_source(r"\\share\FrameGuard\release"), LocalSource)

    @staticmethod
    def _redirect(old_url, new_url, token="secret"):
        import urllib.request
        from frameguard.updatesource import _HttpsOnlyRedirectHandler

        request = urllib.request.Request(
            old_url, headers={"Authorization": f"Bearer {token}",
                              "User-Agent": "test"})
        return _HttpsOnlyRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, new_url)

    def test_redirect_to_plain_http_is_refused(self):
        import urllib.error
        with pytest.raises(urllib.error.HTTPError):
            self._redirect("https://a.example.test/manifest.json",
                           "http://a.example.test/manifest.json")

    def test_token_is_dropped_when_a_redirect_leaves_the_host(self):
        moved = self._redirect("https://a.example.test/manifest.json",
                               "https://cdn.example.test/manifest.json")
        assert not moved.has_header("Authorization")
        same = self._redirect("https://a.example.test/manifest.json",
                              "https://a.example.test/v2/manifest.json")
        assert same.get_header("Authorization") == "Bearer secret"

    def test_requests_go_through_the_https_only_opener(self, monkeypatch):
        import io
        import json
        from frameguard import updatesource as S

        opened = []

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        good = {"manifest_version": 1, "files": {
            "FrameGuard.exe": {"sha256": "a" * 64, "size": 1}}}
        monkeypatch.setattr(
            S, "_open_https",
            lambda request, timeout: opened.append(request.full_url)
            or _Response(json.dumps(good).encode()))
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *_a, **_k: pytest.fail("bypassed the HTTPS-only opener"))
        source = S.make_source("https://artifacts.example.test/rel",
                               token="secret")
        assert source.read_manifest()["files"]
        assert opened == ["https://artifacts.example.test/rel/manifest.json"]

    def test_https_opener_uses_os_trust_and_the_redirect_guard(self, monkeypatch):
        import urllib.request
        from frameguard import updatesource as S

        built = []
        monkeypatch.setattr(
            urllib.request, "build_opener",
            lambda *handlers: built.extend(handlers) or mock.Mock())
        sentinel = object()
        monkeypatch.setattr(S, "_https_context", lambda: sentinel)
        S._open_https(urllib.request.Request("https://a.example.test/x"), 5)
        https = [h for h in built if isinstance(h, urllib.request.HTTPSHandler)]
        assert https and https[0]._context is sentinel
        assert any(isinstance(h, S._HttpsOnlyRedirectHandler) for h in built)

    @pytest.mark.parametrize("entry", [
        {"size": 1},
        {"sha256": "", "size": 1},
        {"sha256": "abc", "size": 1},
        {"sha256": "Z" * 64, "size": 1},
        "not-a-dict",
    ])
    def test_manifest_entry_without_a_valid_sha256_is_refused(
            self, tmp_path, monkeypatch, entry):
        import io
        import json
        from frameguard import manifest as M
        from frameguard import updatesource as S

        data = {"manifest_version": 1, "files": {
            "FrameGuard.exe": {"sha256": "a" * 64, "size": 1},
            "_internal/unverified.dll": entry}}
        path = tmp_path / M.MANIFEST_FILENAME
        path.write_text(json.dumps(data), encoding="utf-8")
        assert M.load_manifest(str(path)) is None
        with pytest.raises(S.UpdateSourceError):
            S.make_source(str(tmp_path)).read_manifest()

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(
            S, "_open_https",
            lambda *_a, **_k: _Response(json.dumps(data).encode()))
        with pytest.raises(S.UpdateSourceError, match="SHA-256"):
            S.make_source("https://artifacts.example.test/rel").read_manifest()

    def test_verify_tree_treats_a_missing_hash_as_a_mismatch(self, tmp_path):
        from frameguard import manifest as M

        (tmp_path / "a.dll").write_bytes(b"payload")
        manifest = {"files": {"a.dll": {"size": 7}}}
        assert M.verify_tree(str(tmp_path), manifest) == ["a.dll"]
        assert M.verify_tree(str(tmp_path), manifest, ["ghost.dll"]) == ["ghost.dll"]
        manifest["files"]["a.dll"]["sha256"] = M.file_sha256(str(tmp_path / "a.dll"))
        assert M.verify_tree(str(tmp_path), manifest) == []

    def test_a_stored_plain_http_channel_heals_to_the_default(self, qapp):
        from PySide6.QtCore import QSettings
        from frameguard import updater as U

        settings = QSettings(U._SETTINGS_ORG, U._SETTINGS_APP)
        original = settings.value(U._CHANNEL_KEY, None)
        try:
            settings.setValue(U._CHANNEL_KEY, "http://artifacts.example.test/fg")
            assert U.get_update_channel() == U.get_update_channel_for(U.sys.platform)
            assert settings.value(U._CHANNEL_KEY, None) is None
            settings.setValue(U._CHANNEL_KEY, "https://artifacts.example.test/fg")
            assert U.get_update_channel() == "https://artifacts.example.test/fg"
        finally:
            if original is None:
                settings.remove(U._CHANNEL_KEY)
            else:
                settings.setValue(U._CHANNEL_KEY, original)


_VERSION_FILES = ("frameguard/__init__.py", "frameguard.spec", "installer.iss",
                  "version.json", "installer_info.txt")


class TestVersionValidatorLocations:
    """C-O2: validate_version.py checks every location a version ships in,
    including the spec's integer prodvers tuple the literal scan cannot see."""

    @staticmethod
    def _tree(tmp_path, mutate=None):
        (tmp_path / "frameguard").mkdir()
        for rel in _VERSION_FILES:
            text = (SRC / rel).read_text(encoding="utf-8-sig")
            if mutate and rel == mutate[0]:
                new = mutate[1](text)
                assert new != text, f"mutation did not apply to {rel}"
                text = new
            encoding = "utf-8-sig" if rel == "installer.iss" else "utf-8"
            (tmp_path / rel).write_text(text, encoding=encoding)
        return tmp_path

    @staticmethod
    def _run(cwd):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, str(SRC / "validate_version.py")],
            cwd=str(cwd), capture_output=True, text=True, timeout=60)

    def test_copied_tree_is_consistent(self, tmp_path):
        result = self._run(self._tree(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr

    @staticmethod
    def _version():
        from frameguard import __version__
        return __version__

    @pytest.mark.parametrize("rel, mutate, message", [
        ("frameguard.spec",
         lambda t, v: re.sub(r"prodvers=\([^)]*\)", "prodvers=(0, 0, 1, 0)", t),
         "prodvers"),
        ("frameguard.spec",
         lambda t, v: re.sub(r"filevers=\([^)]*\)", "filevers=(0, 0, 1, 0)", t),
         "filevers"),
        ("frameguard.spec",
         lambda t, v: re.sub(r"\s*prodvers=\([^)]*\),", "", t),
         "no prodvers"),
        ("frameguard.spec",
         lambda t, v: t.replace(f"'ProductVersion', '{v}.0'",
                                "'ProductVersion', '0.0.1.0'"),
         "0.0.1.0"),
        ("installer_info.txt",
         lambda t, v: t.replace(f"FrameGuard Desktop v{v}\n",
                                "FrameGuard Desktop v0.0.1\n", 1),
         "installer_info.txt title"),
        ("installer_info.txt",
         lambda t, v: t.replace(f"WHAT'S NEW IN v{v}", "WHAT'S NEW IN v0.0.1"),
         "WHAT'S NEW"),
        ("version.json",
         lambda t, v: t.replace(f'"changelog": "v{v} - ', '"changelog": "v0.0.1 - ', 1),
         "changelog must start"),
        ("installer.iss",
         lambda t, v: t.replace(f"AppVersion={v}\n", "AppVersion=0.0.1\n"),
         "installer.iss"),
    ])
    def test_each_version_location_is_checked(self, tmp_path, rel, mutate, message):
        version = self._version()
        tree = self._tree(tmp_path, (rel, lambda t: mutate(t, version)))
        result = self._run(tree)
        assert result.returncode == 1, result.stdout
        assert message in result.stdout, result.stdout

    def test_missing_installer_information_fails_with_a_reason(self, tmp_path):
        tree = self._tree(tmp_path)
        (tree / "installer_info.txt").unlink()
        result = self._run(tree)
        assert result.returncode == 1
        assert "installer_info.txt" in result.stdout


@pytest.fixture
def fg_main(tmp_path, monkeypatch):
    """main.py loaded as a module (not __main__), logging into tmp_path."""
    spec = importlib.util.spec_from_file_location(
        "fgmain_crash_handling", str(SRC / "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_log_dir", lambda: str(tmp_path))
    return module


class TestCrashHandling:
    """C-L1: unraisable/GC errors are logged without a modal dialog, and
    per-crash logs are pruned to the newest few."""

    def test_unraisable_errors_are_logged_without_a_dialog(
            self, fg_main, tmp_path, monkeypatch):
        import types

        monkeypatch.setattr(
            fg_main, "_show_error",
            lambda *_a: pytest.fail("unraisable errors must not show a dialog"))
        try:
            raise RuntimeError("wrapped C/C++ object has been deleted")
        except RuntimeError as error:
            fg_main._unraisable_hook(types.SimpleNamespace(
                exc_type=type(error), exc_value=error,
                exc_traceback=error.__traceback__,
                err_msg="Exception ignored in", object=object()))
        log = (tmp_path / "frameguard_crash.log").read_text(encoding="utf-8")
        assert "Unraisable Exception (logged only)" in log
        assert "wrapped C/C++ object has been deleted" in log
        assert "Context:   Exception ignored in" in log
        assert list(tmp_path.glob("crash_*.log")) == []

    def test_a_real_finalizer_error_reaches_the_log_not_a_dialog(
            self, fg_main, tmp_path, monkeypatch):
        import gc
        import sys

        monkeypatch.setattr(
            fg_main, "_show_error",
            lambda *_a: pytest.fail("unraisable errors must not show a dialog"))
        monkeypatch.setattr(sys, "unraisablehook", fg_main._unraisable_hook)

        class _Noisy:
            def __del__(self):
                raise ValueError("finalizer failed")

        _Noisy()
        gc.collect()
        log = (tmp_path / "frameguard_crash.log").read_text(encoding="utf-8")
        assert "finalizer failed" in log

    def test_a_broken_unraisable_payload_never_raises(self, fg_main, monkeypatch):
        monkeypatch.setattr(fg_main, "_append_crash_log",
                            mock.Mock(side_effect=OSError("disk full")))
        fg_main._unraisable_hook(object())  # no attributes at all

    def test_real_crashes_still_tell_the_user_on_the_main_thread(
            self, fg_main, tmp_path, monkeypatch):
        shown = []
        monkeypatch.setattr(fg_main, "_show_error", shown.append)
        try:
            raise KeyError("boom")
        except KeyError as error:
            fg_main._global_excepthook(type(error), error, error.__traceback__)
        assert shown and "Unexpected Error" in shown[0]
        assert len(list(tmp_path.glob("crash_*.log"))) == 1
        assert "KeyError" in (tmp_path / "frameguard_crash.log").read_text(
            encoding="utf-8")

    def test_crash_logs_are_pruned_to_the_newest(self, fg_main, tmp_path, monkeypatch):
        monkeypatch.setattr(fg_main, "_show_error", lambda *_a: None)
        for index in range(25):
            path = tmp_path / f"crash_20260101_0000{index:02d}_0.9.0.log"
            path.write_text("old", encoding="utf-8")
            stamp = 1_700_000_000 + index
            os.utime(path, (stamp, stamp))
        keep = tmp_path / "frameguard_fault.log"
        keep.write_text("native", encoding="utf-8")

        try:
            raise RuntimeError("new crash")
        except RuntimeError as error:
            fg_main._global_excepthook(type(error), error, error.__traceback__)

        remaining = sorted(p.name for p in tmp_path.glob("crash_*.log"))
        assert len(remaining) == fg_main._MAX_CRASH_LOGS == 20
        assert "crash_20260101_000000_0.9.0.log" not in remaining
        assert "crash_20260101_000005_0.9.0.log" not in remaining
        assert "crash_20260101_000006_0.9.0.log" in remaining
        assert any("new crash" in (tmp_path / name).read_text(encoding="utf-8")
                   for name in remaining)
        assert keep.exists()

    def test_running_crash_log_rotates_instead_of_growing_forever(
            self, fg_main, tmp_path):
        log = tmp_path / "frameguard_crash.log"
        log.write_bytes(b"x" * (fg_main._MAX_CRASH_LOG_BYTES + 1))
        fg_main._append_crash_log(str(tmp_path), "fresh entry\n")
        assert log.read_text(encoding="utf-8") == "fresh entry\n"
        assert (tmp_path / "frameguard_crash.log.1").stat().st_size > \
            fg_main._MAX_CRASH_LOG_BYTES

    def test_entry_point_installs_all_three_hooks(self):
        source = (SRC / "main.py").read_text(encoding="utf-8")
        entry = source[source.index('if __name__ == "__main__":'):]
        for line in ("sys.excepthook = _global_excepthook",
                     "threading.excepthook = _thread_excepthook",
                     "sys.unraisablehook = _unraisable_hook"):
            assert line in entry


class TestDependabot:
    """C-L4: Dependabot watches the Python pins as well as the actions."""

    @staticmethod
    def _config():
        return (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    def test_pip_updates_are_weekly_and_grouped(self):
        config = self._config()
        pip = config[config.index('package-ecosystem: "pip"'):]
        assert 'directory: "/src"' in pip
        assert 'interval: "weekly"' in pip
        assert re.search(r"groups:\n\s+python-dependencies:\n\s+patterns:\n\s+- \"\*\"", pip)

    def test_action_updates_are_still_grouped(self):
        config = self._config()
        actions = config[config.index('package-ecosystem: "github-actions"'):
                         config.index('package-ecosystem: "pip"')]
        assert 'directory: "/"' in actions
        assert re.search(r"groups:\n\s+github-actions:\n\s+patterns:\n\s+- \"\*\"", actions)

    def test_pyside_floor_hold_is_respected(self):
        config = self._config()
        assert 'dependency-name: "PySide6*"' in config
        assert 'dependency-name: "shiboken6"' in config
        assert config.count('"version-update:semver-minor"') == 2

    def test_constraints_file_is_parseable_as_plain_requirements(self):
        """Dependabot only updates a .txt whose every line is a comment or a
        requirement; a pip option line would silently stop pin updates."""
        lines = (SRC / "constraints-3.12.txt").read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.]*", stripped), line
