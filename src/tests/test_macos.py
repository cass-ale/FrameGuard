"""macOS build and runtime checks.

These run on any host: they exercise the darwin code paths by simulation, and
statically validate the .app bundle configuration. The point is to catch a
broken macOS build from the Windows dev machine, instead of discovering it on
a Mac at release time.
"""

import ast
import json
import os
import posixpath
import struct
import subprocess
import sys
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


def _spec_tree():
    return ast.parse(_read("frameguard.spec"))


def _load_download_tools(name="dl_tools_test"):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name, os.path.join(REPO, "download_tools.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAppBundle:
    def test_spec_contains_a_bundle_call(self):
        """Without BUNDLE() PyInstaller emits a bare Unix binary on macOS and
        build_mac.sh can never find FrameGuard.app."""
        calls = [n for n in ast.walk(_spec_tree())
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "BUNDLE"]
        assert calls, "frameguard.spec has no BUNDLE() call"

    def test_bundle_declares_the_fg_document_type(self):
        """macOS associates .fg via Info.plist, not the registry."""
        spec = _read("frameguard.spec")
        assert "CFBundleDocumentTypes" in spec
        assert "UTExportedTypeDeclarations" in spec
        assert "com.caprilaria.frameguard.preset" in spec

    def test_bundle_sets_retina_and_minimum_os(self):
        spec = _read("frameguard.spec")
        assert "NSHighResolutionCapable" in spec
        assert "LSMinimumSystemVersion" in spec

    def test_icns_exists_and_is_valid(self):
        """macOS rejects a .ico; the bundle needs a real .icns."""
        path = os.path.join(REPO, "assets", "frameguard.icns")
        if not os.path.isfile(path):
            import pytest
            pytest.skip("assets/frameguard.icns is a build product "
                        "(assets/make_icns.py); not generated in this checkout")
        assert os.path.isfile(path), "assets/frameguard.icns missing (run assets/make_icns.py)"

        data = open(path, "rb").read()
        assert data[:4] == b"icns"
        declared = struct.unpack(">I", data[4:8])[0]
        assert declared == len(data), "icns length header does not match file size"

        sizes, offset = set(), 8
        while offset < len(data):
            length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            payload = data[offset + 8:offset + length]
            if payload[:8] == b"\x89PNG\r\n\x1a\n":
                sizes.add(struct.unpack(">I", payload[16:20])[0])
            offset += length

        # Finder, the Dock and Get Info all pull different sizes.
        for required in (16, 32, 128, 256, 512):
            assert required in sizes, f"icns missing {required}x{required} (has {sorted(sizes)})"

    def test_icns_generator_is_runnable(self):
        assert os.path.isfile(os.path.join(REPO, "assets", "make_icns.py"))
        ast.parse(_read(os.path.join("assets", "make_icns.py")))

    def test_spec_does_not_bundle_qt_developer_applications(self):
        """collect_all(PySide6) pulls Assistant.app into the bundle, whose
        framework symlinks make recursive macOS signing fail."""
        assert "collect_all('PySide6')" not in _read("frameguard.spec")


class TestMacToolDiscovery:
    def test_resources_tools_is_searched_first(self):
        """Tools are staged into Contents/Resources/tools by build_mac.sh."""
        from frameguard import tools

        app = "/Applications/FrameGuard.app"
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "_MEIPASS", f"{app}/Contents/Frameworks", create=True), \
             mock.patch.object(sys, "executable", f"{app}/Contents/MacOS/FrameGuard"), \
             mock.patch.object(os, "name", "posix"), \
             mock.patch.object(tools.os.path, "join", posixpath.join), \
             mock.patch.object(tools.os.path, "abspath", posixpath.normpath), \
             mock.patch.object(tools.os.path, "isdir", lambda p: True):
            dirs = tools._candidate_tools_dirs()

        assert dirs[0] == f"{app}/Contents/Resources/tools"
        for expected in (f"{app}/Contents/MacOS/tools",
                         f"{app}/Contents/Frameworks/tools"):
            assert expected in dirs, f"{expected} not searched"

    def test_platform_specific_cache_is_searched(self):
        """download_tools.py caches under tools_cache/<platform>/."""
        from frameguard import tools

        if os.name == "nt":
            key = os.path.join("tools_cache", "windows")
        elif sys.platform == "darwin":
            import platform as _platform
            arch = "arm64" if _platform.machine() == "arm64" else "x86_64"
            key = os.path.join("tools_cache", f"darwin-{arch}")
        else:
            import pytest
            pytest.skip("the tool cache layout is only exercised on macOS/Windows")
        # _candidate_tools_dirs() only lists directories that exist, and
        # tools_cache/ is a gitignored build product: a clean checkout (CI)
        # has none until download_tools.py runs. Arrange it for this test
        # and remove it again only if this test created it.
        cache_dir = os.path.join(REPO, key)
        created = not os.path.isdir(cache_dir)
        if created:
            os.makedirs(cache_dir)
        try:
            dirs = tools._candidate_tools_dirs()
            assert any(d.endswith(key) for d in dirs), dirs
        finally:
            if created:
                os.rmdir(cache_dir)
                try:
                    os.rmdir(os.path.dirname(cache_dir))
                except OSError:
                    pass

    def test_unrunnable_binary_is_not_reported_usable(self):
        """A missing +x bit used to be reported as 'found and OK', turning every
        scan into an Internal Error."""
        from frameguard import tools

        with mock.patch.object(tools, "_find_executable", lambda name: f"/fake/{name}"), \
             mock.patch.object(tools, "_get_tool_version", lambda p, n: (None, ())):
            info = tools._detect_tool("ffmpeg")

        assert info.found
        assert not info.meets_minimum
        assert "could not be executed" in (info.error or "")


class TestMacRuntimePaths:
    def test_logs_go_outside_the_app_bundle(self):
        """Writing inside /Applications/FrameGuard.app fails for a standard
        user and invalidates the code signature."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "fgmain_test", os.path.join(REPO, "main.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        with mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.os.path, "join", posixpath.join), \
             mock.patch("os.makedirs", lambda *a, **k: None):
            path = m._log_dir()

        assert path.endswith("Library/Logs/FrameGuard")
        assert ".app/" not in path

    def test_default_watch_path_prefers_movies_on_mac(self):
        from pathlib import Path
        from frameguard import watchfolder as W

        with mock.patch.object(W.sys, "platform", "darwin"), \
             mock.patch.object(W.Path, "home", staticmethod(lambda: Path("/Users/jane"))), \
             mock.patch.object(W.Path, "is_dir", lambda self: self.name == "Movies"):
            assert W.get_default_watch_path().endswith("Movies")

    def test_updater_targets_a_dmg_on_mac(self):
        from frameguard import updater

        with mock.patch.object(updater.sys, "platform", "darwin"):
            assert updater.installer_filename().endswith(".dmg")
        with mock.patch.object(updater.sys, "platform", "win32"):
            assert updater.installer_filename().endswith(".exe")

    def test_updater_has_no_filesystem_installer_search(self):
        """Retired Drive mounts, team shares and UNC paths can no longer
        leak into any platform's update search: the filesystem installer
        fallback is gone and GitHub Releases is the only source."""
        from frameguard import updater

        assert not hasattr(updater, "get_default_search_paths")


class TestNativeChrome:
    def test_every_platform_uses_the_native_frame(self):
        """No frameless window anywhere: on macOS it removes the traffic
        lights, shadow, edge resize and fullscreen; on Windows the hand-drawn
        title bar clipped its own close button and the page edges (0.9.1)."""
        import inspect
        from frameguard.gui import MainWindow

        gui_src = _read(os.path.join("frameguard", "gui.py"))
        assert "FramelessWindowHint" not in inspect.getsource(MainWindow.__init__)
        assert "USE_CUSTOM_CHROME" not in gui_src
        assert "SetWindowLongW" not in gui_src  # the old WS_THICKFRAME patch

    def test_no_hardcoded_segoe_ui_fonts(self):
        """Segoe UI does not exist on macOS; QFont silently substitutes and the
        metrics drift."""
        gui_src = _read(os.path.join("frameguard", "gui.py"))
        offenders = [
            f"{i}: {line.strip()[:80]}"
            for i, line in enumerate(gui_src.splitlines(), 1)
            if 'QFont("Segoe UI"' in line
        ]
        assert not offenders, "use ui_font() instead:\n" + "\n".join(offenders)

    def test_ui_font_resolves_on_this_platform(self, qapp):
        from frameguard.gui import ui_font

        assert ui_font(10).family()

    def test_main_window_is_not_frameless(self, qapp):
        from frameguard import gui

        window = gui.MainWindow()
        try:
            assert not (window.windowFlags() & gui.Qt.FramelessWindowHint)
            assert not hasattr(window, "_title_bar")
        finally:
            window._cleanup_worker()
            window._watch_monitor.stop()
            window.hide()

    def test_os_mapping_does_not_become_an_explicit_choice(self):
        from PySide6.QtCore import QSettings
        from frameguard.gui import (
            _SETTINGS_APP, _SETTINGS_ORG, _THEME_KEY, resolve_startup_theme,
        )

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.clear()
        assert resolve_startup_theme(settings, prefers_dark=False) == ("Clean Light", False)
        assert not settings.contains(_THEME_KEY)


class TestFileOpenEvent:
    def test_file_open_filter_exists(self):
        """Finder sends an Apple Event, not argv. Without this, double-clicking
        a .fg launches FrameGuard with an empty queue."""
        from frameguard import gui

        assert hasattr(gui, "_FileOpenFilter")

    def test_filter_routes_to_open_paths(self, qapp, tmp_path):
        from PySide6.QtCore import QEvent
        from frameguard.gui import _FileOpenFilter

        target = tmp_path / "team.fg"
        target.write_text("{}", encoding="utf-8")

        opened = []

        class _Window:
            def open_paths(self, paths):
                opened.extend(paths)

            show = raise_ = activateWindow = staticmethod(lambda: None)

        filt = _FileOpenFilter(_Window())

        class _FileOpenEvent(QEvent):
            def __init__(self, path):
                super().__init__(QEvent.FileOpen)
                self._path = path

            def file(self):
                return self._path

        assert filt.eventFilter(None, _FileOpenEvent(str(target))) is True
        assert opened == [str(target)]

    def test_cold_launch_event_waits_for_the_main_window(self, qapp, tmp_path):
        from PySide6.QtCore import QEvent
        from frameguard.gui import _FileOpenFilter

        target = tmp_path / "cold-launch.fg"
        target.write_text("{}", encoding="utf-8")
        opened = []

        class _FileOpenEvent(QEvent):
            def __init__(self, path):
                super().__init__(QEvent.FileOpen)
                self._path = path

            def file(self):
                return self._path

        class _Window:
            def open_paths(self, paths):
                opened.extend(paths)

            show = raise_ = activateWindow = staticmethod(lambda: None)

        filt = _FileOpenFilter()
        assert filt.eventFilter(None, _FileOpenEvent(str(target))) is True
        assert opened == []
        assert filt._pending_paths == [str(target)]

        filt.set_window(_Window())
        qapp.processEvents()
        assert opened == [str(target)]
        assert filt._pending_paths == []

    def test_nonexistent_path_is_ignored(self, qapp):
        from PySide6.QtCore import QEvent
        from frameguard.gui import _FileOpenFilter

        class _Window:
            def open_paths(self, paths):
                raise AssertionError("should not be called")

        filt = _FileOpenFilter(_Window())

        class _FileOpenEvent(QEvent):
            def __init__(self):
                super().__init__(QEvent.FileOpen)

            def file(self):
                return "/nope/missing.fg"

        assert filt.eventFilter(None, _FileOpenEvent()) is not True


class TestBuildScripts:
    def test_mac_script_uses_lf_line_endings(self):
        """CRLF in a shell script makes bash fail with a confusing
        'bad interpreter' error."""
        raw = open(os.path.join(REPO, "build_mac.sh"), "rb").read()
        assert b"\r\n" not in raw, "build_mac.sh has CRLF line endings"
        assert raw.startswith(b"#!/usr/bin/env bash")

    def test_mac_script_covers_the_required_steps(self):
        sh = _read("build_mac.sh")
        for token, why in [
            ("validate_version.py", "version consistency gate"),
            ("make_icns.py", "icon generation"),
            ("write_build_stamp.py", "build stamp"),
            ("pytest", "test gate"),
            ("codesign", "Gatekeeper signing"),
            ("hdiutil create", "DMG packaging"),
            ("--selftest", "smoke test"),
            ("chmod +x", "tool executable bit"),
            ("quarantine", "Gatekeeper guidance"),
        ]:
            assert token in sh, f"build_mac.sh is missing the {why} step ({token})"

    def test_final_dmg_smoke_runs_before_artifact_copy(self):
        sh = _read("build_mac.sh")
        harness = sh.index("tools_dev/macos_package_smoke.py")
        final_verify = sh.index('verify_dmg "$LOCAL_DMG_PATH" "final"')
        publish_copy = sh.index('cp -f "$LOCAL_DMG_PATH" "$DMG_PATH"')
        assert final_verify < harness < publish_copy
        for contract in (
            "--expected-arch", "--artifacts-dir", "--qpa cocoa",
            "mounted and copied bundle", "Detach is unconditional",
        ):
            assert contract in sh

    def test_package_smoke_fails_when_both_detach_attempts_fail(self, tmp_path):
        import importlib.util
        import plistlib

        path = os.path.join(REPO, "tools_dev", "macos_package_smoke.py")
        spec = importlib.util.spec_from_file_location("macos_smoke_detach_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        dmg = tmp_path / "FrameGuard.dmg"
        dmg.write_bytes(b"test-image")
        artifacts = tmp_path / "artifacts"
        root = tmp_path / "smoke-root"
        root.mkdir()
        mountpoint = root / "mount"
        detach_calls = []

        def fake_run(command, **_kwargs):
            if command[:2] == ["hdiutil", "attach"]:
                payload = {"system-entities": [{
                    "mount-point": str(mountpoint),
                    "dev-entry": "/dev/disk-test",
                }]}
                return subprocess.CompletedProcess(
                    command, 0, stdout=plistlib.dumps(payload))
            if command[:2] == ["hdiutil", "detach"]:
                detach_calls.append(command)
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout=b"")

        with mock.patch.object(module.sys, "platform", "darwin"), \
             mock.patch.object(module.tempfile, "mkdtemp", return_value=str(root)), \
             mock.patch.object(module, "_run", side_effect=fake_run), \
             mock.patch.object(module, "_require_layout", return_value=tmp_path / "FrameGuard"), \
             mock.patch.object(module, "_startup"), \
             mock.patch.object(module.subprocess, "run", return_value=None):
            result = module.main([
                "--dmg", str(dmg),
                "--expected-arch", "arm64",
                "--artifacts-dir", str(artifacts),
            ])

        evidence = json.loads(
            (artifacts / "macos-package-smoke.json").read_text(encoding="utf-8"))
        assert result == 1
        assert evidence["status"] == "failed"
        assert evidence["detach_attempted"] is True
        assert evidence["detach_completed"] is False
        assert evidence["temp_root_removed"] is True
        assert len(detach_calls) == 2
        assert not root.exists()

    def test_mac_build_state_is_isolated_from_windows(self):
        """A Mac build must coexist with the tested Windows tree."""
        sh = _read("build_mac.sh")

        for declaration in (
            'Library/Caches/FrameGuard/${PROJECT_KEY}',
            'VENV="${LOCAL_BUILD_ROOT}/venv"',
            'WORK_DIR="${LOCAL_BUILD_ROOT}/build"',
            'DIST_DIR="${LOCAL_BUILD_ROOT}/dist"',
            'BUNDLE="${DIST_DIR}/${APP_NAME}.app"',
            'LOCAL_DMG_PATH="${LOCAL_BUILD_ROOT}/FrameGuard.dmg"',
        ):
            assert declaration in sh

        assert 'VENV=".buildenv"' not in sh
        assert 'VENV=".buildenv-macos"' not in sh
        assert 'rm -rf build dist' not in sh
        assert 'rm -rf "$WORK_DIR"' in sh
        assert 'rm -rf "$DIST_DIR"' in sh
        assert '--workpath "$WORK_DIR"' in sh
        assert '--distpath "$DIST_DIR"' in sh
        assert 'DYLD_LIBRARY_PATH="$PYINSTALLER_DYLD_PATH"' in sh
        assert '--build-dir "$BUNDLE"' in sh
        assert 'hdiutil create' in sh and '"$LOCAL_DMG_PATH"' in sh
        assert 'cp -f "$LOCAL_DMG_PATH" "$DMG_PATH"' in sh
        assert 'write_build_stamp.py --output "$BUILD_STAMP"' in sh
        assert 'FRAMEGUARD_BUILD_STAMP_PATH' in sh

    def test_mac_signing_visits_all_nested_macho_code_inside_out(self):
        sh = _read("build_mac.sh")
        assert 'find "${BUNDLE}/Contents" -type f -print0' in sh
        assert '== *"Mach-O"*' in sh
        for suffix in ("*.framework", "*.app", "*.appex", "*.bundle", "*.xpc", "*.plugin"):
            assert f'-name "{suffix}"' in sh
        assert 'find "${BUNDLE}/Contents" -depth -type d' in sh
        assert "Entitlements belong on the main application" in sh
        nested = sh.index('find "${BUNDLE}/Contents" -type f -print0')
        bundles = sh.index('find "${BUNDLE}/Contents" -depth -type d')
        outer = sh.index("if _sign_outer_app; then")
        assert nested < bundles < outer

    def test_mac_target_arch_is_explicit_and_build_paths_are_deterministic(self):
        sh = _read("build_mac.sh")
        assert 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"' in sh
        assert 'REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"' in sh
        assert 'PROJECT_PATH_ID="$(printf' in sh
        assert "cksum" in sh
        assert 'PROJECT_KEY="$(basename "$REPO_ROOT")-${PROJECT_PATH_ID}"' in sh
        assert 'arm64|x86_64)' in sh
        assert 'if [[ "$TARGET_ARCH" != "$ARCH" ]]' in sh
        assert "Cross-architecture macOS builds are unsupported" in sh
        assert 'export FRAMEGUARD_TARGET_ARCH="$TARGET_ARCH"' in sh
        assert ('Library/Caches/FrameGuard/${PROJECT_KEY}/${TARGET_ARCH}' in sh)
        assert 'PROJECT_KEY="$(basename "$PWD")"' not in sh

    def test_pyinstaller_diagnostics_are_preserved_and_internal_modules_gate(self):
        sh = _read("build_mac.sh")
        assert 'PYINSTALLER_LOG="${LOG_DIR}/pyinstaller.log"' in sh
        assert 'warn-frameguard.txt' in sh
        assert '| tee "$PYINSTALLER_LOG"' in sh
        assert 'PYINSTALLER_PIPESTATUS=("${PIPESTATUS[@]}")' in sh
        assert 'PYINSTALLER_STATUS="${PYINSTALLER_PIPESTATUS[0]}"' in sh
        assert 'TEE_STATUS="${PYINSTALLER_PIPESTATUS[1]}"' in sh
        assert 'missing module named' in sh
        assert 'frameguard([.]' in sh
        assert 'PyInstaller reported missing internal FrameGuard modules' in sh

    def test_dmg_creation_and_verification_are_bounded_and_diagnostic(self):
        sh = _read("build_mac.sh")
        create_helper = sh[sh.index("create_dmg()"):
                           sh.index("verify_dmg()")]
        assert "for attempt in 1 2 3" in create_helper
        assert 'rm -f "$image"' in create_helper
        assert 'sleep "$delay"' in create_helper
        assert "hdiutil-create.log" in create_helper
        assert "hdiutil info" in create_helper

        helper = sh[sh.index("verify_dmg()"):
                    sh.index("print_notary_log()")]
        assert '[[ -s "$image" ]]' in helper
        assert "sync" in helper
        assert 'while [[ $attempt -le 3 ]]' in helper
        assert 'sleep "$delay"' in helper
        assert 'cat "$attempt_log" >&2' in helper
        assert 'hdiutil-verify-${label}.log' in helper
        assert 'hdiutil imageinfo "$image"' in helper

        create = sh.index('create_dmg "$DMG_STAGE" "$LOCAL_DMG_PATH"')
        created = sh.index('verify_dmg "$LOCAL_DMG_PATH" "created"')
        dmg_sign = sh.index('codesign --force --timestamp --sign')
        staple = sh.index('xcrun stapler staple')
        final = sh.index('verify_dmg "$LOCAL_DMG_PATH" "final"')
        publish = sh.index('cp -f "$LOCAL_DMG_PATH" "$DMG_PATH"')
        assert create < created < dmg_sign < staple < final < publish

    def test_mac_builder_requires_the_release_python_and_apple_tools(self):
        sh = _read("build_mac.sh")
        assert "sys.version_info[:2] == (3, 12)" in sh
        assert "for required_tool in python3 hdiutil codesign xcrun file xattr" in sh
        assert "Python 3.12 is required for reproducible release builds" in sh

    def test_ad_hoc_signing_failure_needs_an_explicit_internal_override(self):
        sh = _read("build_mac.sh")
        assert "Ad-hoc signing failed" in sh
        assert "Ad-hoc app signature validation failed" in sh
        assert "if [[ $ALLOW_UNSIGNED -ne 1 ]]" in sh
        assert "--allow-unsigned only for an internal diagnostic build" in sh

    def test_configured_apple_release_operations_fail_closed(self):
        sh = _read("build_mac.sh")
        assert "Notarization is partially configured" in sh
        assert "Notarization credentials require a configured Developer ID" in sh
        assert "--allow-unsigned cannot bypass configured Developer ID" in sh
        assert "Configured Developer ID signing failed" in sh
        assert "Configured Developer ID app signature validation failed" in sh
        assert "Configured Developer ID DMG signing failed" in sh
        assert "Configured Developer ID DMG signature validation failed" in sh
        assert "Configured notarization failed" in sh
        assert 'xcrun stapler staple "$LOCAL_DMG_PATH" \\\n        || die' in sh
        assert sh.count('xcrun stapler validate "$LOCAL_DMG_PATH"') >= 2
        assert "tail -3" not in sh

    def test_notarization_diagnostics_redact_credentials(self):
        sh = _read("build_mac.sh")
        assert "set -x" not in sh
        assert 'text.replace(value, "<redacted>")' in sh
        assert '>"$NOTARY_LOG" 2>&1' in sh
        for secret in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD"):
            assert f'echo "${secret}' not in sh

    def test_spec_supports_an_isolated_build_stamp(self):
        spec = _read("frameguard.spec")
        assert 'os.environ.get("FRAMEGUARD_BUILD_STAMP_PATH")' in spec
        assert '_asset("frameguard", "_build_stamp.json")' in spec

    def test_build_stamp_accepts_an_isolated_output(self, tmp_path):
        output = tmp_path / "mac-build" / "_build_stamp.json"
        default = os.path.join(REPO, "frameguard", "_build_stamp.json")
        before = open(default, "rb").read() if os.path.isfile(default) else None

        result = subprocess.run(
            [sys.executable, os.path.join(REPO, "write_build_stamp.py"),
             "--output", str(output)],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        stamp = json.loads(output.read_text(encoding="utf-8"))
        assert stamp["platform"]
        assert stamp["machine"]
        assert stamp["version"]
        after = open(default, "rb").read() if os.path.isfile(default) else None
        assert after == before

    def test_windows_script_covers_the_required_steps(self):
        bat = _read("build_installer.bat")
        for token, why in [
            ("validate_version.py", "version consistency gate"),
            ("write_build_stamp.py", "build stamp"),
            ("pytest", "test gate"),
            ("--selftest", "smoke test"),
            ("download_tools.py", "tool staging"),
        ]:
            assert token in bat, f"build_installer.bat is missing the {why} step ({token})"

    def test_windows_script_verifies_installer_output(self):
        bat = _read("build_installer.bat")
        assert bat.count("Output\\FrameGuardInstaller.exe") >= 2, \
            "the script never verifies the installer was actually produced"

    def test_no_stale_google_drive_instructions(self):
        """Source lives in Git Enterprise now, not a Drive folder."""
        bat = _read("build_installer.bat")
        assert "Re-download the FGv0.2.2 folder from Google Drive" not in bat

    def test_build_stamp_script_is_runnable(self):
        ast.parse(_read("write_build_stamp.py"))


class TestDownloadTools:
    def test_platform_flag_is_actually_parsed(self):
        """--platform was documented but never parsed, so build_mac.sh silently
        downloaded Windows binaries."""
        src = _read("download_tools.py")
        assert "argparse" in src
        assert '"--platform"' in src

    def test_both_platforms_are_configured(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "dl_tools_test", os.path.join(REPO, "download_tools.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        assert m.SOURCES["windows"]["suffix"] == ".exe"
        # darwin sources are per-architecture (the arm64 and x86_64 ffmpeg
        # builds live at different URLs), so they are built on demand by
        # sources_for() instead of sitting in the static SOURCES table.
        for arch in ("arm64", "x86_64"):
            darwin = m.sources_for("darwin", arch)
            assert darwin["suffix"] == ""
            # Riedl ships ffmpeg and ffprobe as separate macOS downloads
            assert "ffprobe" in darwin
            assert darwin["ffmpeg"]["members"] == ["ffmpeg"]

    def test_macos_sources_are_not_windows_binaries(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "dl_tools_test2", os.path.join(REPO, "download_tools.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        for arch in ("arm64", "x86_64"):
            for name, entry in m.sources_for("darwin", arch).items():
                if not isinstance(entry, dict):
                    continue
                for member in entry["members"]:
                    assert not member.endswith(".exe"), f"{name} expects a .exe on macOS"
                for url in entry["urls"]:
                    assert "Windows" not in url, f"{name} points at a Windows build: {url}"

    def test_sets_executable_bit_on_posix(self):
        src = _read("download_tools.py")
        assert "make_executable" in src
        assert "S_IXUSR" in src

    def test_macos_mediainfo_url_is_not_a_dead_tarball(self):
        """The old mediaarea .tar.bz2 URL 404s; the Mac CLI ships as a .dmg."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "dl_tools_test3", os.path.join(REPO, "download_tools.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)

        urls = m.sources_for("darwin", "arm64")["mediainfo"]["urls"]
        assert urls, "no macOS mediainfo source configured"
        assert not any(u.endswith(".tar.bz2") for u in urls), \
            f"dead tarball URL still configured: {urls}"
        assert any(u.endswith(".dmg") for u in urls), \
            "mediaarea ships the Mac CLI as a .dmg"

    def test_dmg_extraction_is_supported(self):
        src = _read("download_tools.py")
        assert "_extract_dmg_members" in src
        assert "hdiutil" in src

    def test_hdiutil_retries_with_bounded_backoff_and_full_diagnostics(self, capsys):
        m = _load_download_tools("dl_tools_hdiutil_retry")
        long_error = "attach failure " + ("diagnostic-" * 40)
        failed = subprocess.CompletedProcess(
            ["hdiutil"], 1, stdout="complete stdout", stderr=long_error)
        succeeded = subprocess.CompletedProcess(
            ["hdiutil"], 0, stdout="attached", stderr="")

        with mock.patch.object(m.subprocess, "run",
                               side_effect=[failed, failed, succeeded]) as run, \
             mock.patch.object(m.time, "sleep") as sleep:
            result = m._run_hdiutil(
                ["attach", "image.dmg"], "attach", timeout=5)

        assert result is succeeded
        assert run.call_count == 3
        assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
        output = capsys.readouterr().out
        assert long_error in output
        assert "attempt 1/3" in output
        assert "attempt 2/3" in output

    def test_hdiutil_retries_timeouts_without_losing_output(self, capsys):
        m = _load_download_tools("dl_tools_hdiutil_timeout")
        timed_out = subprocess.TimeoutExpired(
            ["hdiutil"], 5, output=b"partial stdout", stderr=b"timeout stderr")
        succeeded = subprocess.CompletedProcess(["hdiutil"], 0, "ok", "")

        with mock.patch.object(m.subprocess, "run",
                               side_effect=[timed_out, succeeded]), \
             mock.patch.object(m.time, "sleep") as sleep:
            result = m._run_hdiutil(
                ["attach", "image.dmg"], "attach", timeout=5)

        assert result is succeeded
        sleep.assert_called_once_with(1)
        output = capsys.readouterr().out
        assert "timed out" in output
        assert "partial stdout" in output
        assert "timeout stderr" in output

    def test_non_mac_dmg_extraction_never_invokes_hdiutil(self, tmp_path):
        m = _load_download_tools("dl_tools_non_mac_dmg")
        archive = tmp_path / "MediaInfo.dmg"
        archive.write_bytes(b"not a real image")

        with mock.patch.object(m._platform, "system", return_value="Linux"), \
             mock.patch.object(m, "_run_hdiutil") as hdiutil:
            found = m._extract_dmg_members(
                archive, ["mediainfo"], tmp_path / "dest")

        assert found == set()
        hdiutil.assert_not_called()

    def test_optional_mediainfo_survives_exhausted_attach_retries(self, tmp_path):
        m = _load_download_tools("dl_tools_optional_mediainfo")
        archive = tmp_path / "MediaInfo.dmg"
        archive.write_bytes(b"not a real image")

        with mock.patch.object(m._platform, "system", return_value="Darwin"), \
             mock.patch.object(m, "_run_hdiutil", return_value=None) as hdiutil:
            found = m._extract_dmg_members(
                archive, ["mediainfo"], tmp_path / "dest")

        assert found == set()
        assert hdiutil.call_count == 1
        assert m.sources_for("darwin", "arm64")["mediainfo"]["required"] is False

    def test_dmg_detach_retries_then_uses_bounded_force(self, tmp_path):
        m = _load_download_tools("dl_tools_dmg_detach")
        archive = tmp_path / "MediaInfo.dmg"
        archive.write_bytes(b"not a real image")
        mount = tmp_path / "mount"
        mount.mkdir()
        succeeded = subprocess.CompletedProcess(["hdiutil"], 0, "", "")

        with mock.patch.object(m._platform, "system", return_value="Darwin"), \
             mock.patch.object(m.tempfile, "mkdtemp", return_value=str(mount)), \
             mock.patch.object(
                 m, "_run_hdiutil",
                 side_effect=[succeeded, None, succeeded]) as hdiutil:
            found = m._extract_dmg_members(
                archive, ["mediainfo"], tmp_path / "dest")

        assert found == set()
        operations = [call.args[1] for call in hdiutil.call_args_list]
        assert operations == ["attach", "detach", "forced detach"]
        forced_args = hdiutil.call_args_list[-1].args[0]
        assert forced_args[:2] == ["detach", "-force"]
        assert hdiutil.call_args_list[-1].kwargs["attempts"] == 1
        assert not mount.exists()


class TestMacInstallerGaps:
    """Pins the audit fixes: each test names a real failure mode, not a style."""

    def test_spec_has_no_hardcoded_minimum_os_drift(self):
        """README promises macOS 12+; the bundle must not claim older."""
        spec = _read("frameguard.spec")
        assert '"LSMinimumSystemVersion": "12.0"' in spec

    def test_entitlements_exist_for_hardened_builds(self):
        """Developer ID signing uses --options runtime; without the Qt/JIT
        allowance a future QML surface becomes a launch crash."""
        path = os.path.join(REPO, "entitlements.plist")
        assert os.path.isfile(path), "entitlements.plist missing"
        body = _read("entitlements.plist")
        assert "com.apple.security.cs.allow-jit" in body
        assert "com.apple.security.cs.allow-unsigned-executable-memory" in body

    def test_mac_signing_timestamps_developer_id_builds(self):
        """A Developer ID signature without --timestamp dies with the cert."""
        sh = _read("build_mac.sh")
        assert "--timestamp" in sh
        assert "--options runtime" in sh
        assert "entitlements.plist" in sh

    def test_mac_dmg_name_matches_documented_artifact(self):
        """updater.installer_filename(), version.json and the docs all promise
        FrameGuard.dmg; the script must produce exactly that."""
        sh = _read("build_mac.sh")
        assert 'DMG_PATH="${DMG_DIR}/FrameGuard.dmg"' in sh

    def test_mac_dmg_volume_names_the_arch(self):
        """One DMG name, but an arm64 build won't run on Intel and vice versa.
        The volume name must say which arch is inside."""
        sh = _read("build_mac.sh")
        assert "VOLNAME" in sh
        assert "TARGET_ARCH" in sh

    def test_mac_release_folder_is_platform_separated(self):
        """Windows and macOS payloads overlap by filename; sharing one release
        folder lets one publish silently overwrite the other."""
        sh = _read("build_mac.sh")
        assert "release-macos" in sh
        assert "Output/release\"" not in sh and "Output/release " not in sh

    def test_windows_release_folder_is_platform_separated(self):
        bat = _read("build_installer.bat")
        assert "release-windows" in bat

    def test_version_json_names_both_installers(self):
        """One shared version.json serves both platforms (updater reads
        installer_darwin/installer_windows)."""
        import json
        data = json.loads(_read("version.json"))
        assert data.get("installer_darwin", "").endswith(".dmg")
        assert data.get("installer_windows", "").endswith(".exe")

    def test_manifest_records_build_arch(self, tmp_path):
        """The differential updater refuses cross-arch updates; it needs the
        arch recorded to compare against."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fgmanifest_test", os.path.join(REPO, "frameguard", "manifest.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        (tmp_path / "FrameGuard").write_bytes(b"fake")
        assert "arch" in m.build_manifest(str(tmp_path))

    def test_posix_swap_resigns_the_bundle(self):
        """Replacing files inside a signed .app invalidates the signature;
        without a re-sign Gatekeeper refuses the NEXT launch."""
        src = _read(os.path.join("frameguard", "deltaupdate.py"))
        assert "codesign" in src
        assert "--sign -" in src

    def test_updater_refuses_cross_arch_releases(self):
        src = _read(os.path.join("frameguard", "deltaupdate.py"))
        assert "remote_arch" in src or 'remote.get("arch")' in src

    def test_mac_uses_github_releases_without_a_delta_default(self):
        from frameguard import updater
        assert updater.get_update_channel_for("darwin") == ""
        assert updater.DEFAULT_UPDATE_CHANNEL == ""
        assert updater.GITHUB_RELEASES_URL.endswith("/releases/latest")

    def test_diagnostic_sh_exists_and_is_lf(self):
        """Windows has diagnostic.bat; Mac needs the same triage path."""
        path = os.path.join(REPO, "diagnostic.sh")
        assert os.path.isfile(path), "diagnostic.sh missing"
        raw = open(path, "rb").read()
        assert b"\r\n" not in raw, "diagnostic.sh has CRLF line endings"
        assert raw.startswith(b"#!/usr/bin/env bash")
        body = raw.decode("utf-8")
        assert "--selftest" in body
        assert "codesign" in body
