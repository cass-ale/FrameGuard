"""Packaging, tool discovery, and release-metadata consistency checks.

These catch the class of problem that only shows up in the built installer:
a version string that disagrees between files, a data folder that never gets
bundled, or a bundled binary the app cannot find at runtime.
"""

import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


class TestVersionConsistency:
    def test_all_release_files_agree(self):
        from frameguard import __version__

        spec = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _read("frameguard.spec")).group(1)
        iss = re.search(r"^AppVersion=(.+)$", _read("installer.iss"), re.M).group(1).strip()
        vjson = json.loads(_read("version.json"))["version"]

        assert spec == __version__, f"frameguard.spec={spec} != package {__version__}"
        assert iss == __version__, f"installer.iss={iss} != package {__version__}"
        assert vjson == __version__, f"version.json={vjson} != package {__version__}"

    def test_windows_file_version_matches(self):
        from frameguard import __version__

        spec = _read("frameguard.spec")
        expected = tuple(int(p) for p in __version__.split(".")) + (0,)
        assert f"filevers={expected}".replace(" ", "") in spec.replace(" ", "")

    def test_core_reports_the_package_version(self):
        import frameguard
        import frameguard.core

        assert frameguard.core.__version__ == frameguard.__version__

    def test_installer_information_matches_the_package_version(self):
        from frameguard import __version__

        info = _read("installer_info.txt")
        assert info.startswith(f"FrameGuard Desktop v{__version__}\n")
        assert f"WHAT'S NEW IN v{__version__}" in info

    def test_no_hardcoded_versions_in_source(self):
        """Splash screen and PDF footer used to hard-code an old version."""
        # Sentinels and floors are legitimate constants, not the app version.
        allowed = {"0.0.0", "0.4.0"}
        offenders = []
        for mod in ("gui.py", "report.py", "core.py", "updater.py"):
            text = _read(os.path.join("frameguard", mod))
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("#", '"""', "*")) or "changelog" in stripped.lower():
                    continue
                if "__version__" in stripped:
                    continue
                for match in re.findall(r'["\']v?(\d+\.\d+\.\d+)["\']', stripped):
                    if match not in allowed:
                        offenders.append(f"frameguard/{mod}:{lineno}: {stripped[:80]}")
        assert not offenders, "hardcoded version strings:\n" + "\n".join(offenders)


class TestPackaging:
    @staticmethod
    def _spec_data_destinations():
        """Destination folder names from the spec's `datas = [...]` literal.

        Parsed rather than string-matched so refactoring the spec (e.g. wrapping
        paths in a helper) doesn't break these tests for the wrong reason.
        """
        import ast

        tree = ast.parse(_read("frameguard.spec"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", None) == "datas" for t in node.targets)
                    and isinstance(node.value, ast.List)):
                dests = []
                for element in node.value.elts:
                    if isinstance(element, ast.Tuple) and len(element.elts) == 2:
                        dest = element.elts[1]
                        if isinstance(dest, ast.Constant):
                            dests.append(dest.value)
                return dests
        return []

    def test_presets_folder_is_bundled(self):
        """Without this the installed app ships with zero built-in presets."""
        assert "presets" in self._spec_data_destinations()

    def test_assets_folder_is_bundled(self):
        assert "assets" in self._spec_data_destinations()

    def test_build_stamp_is_bundled_when_present(self):
        """The About dialog reads it; it used to be written but never shipped."""
        spec = _read("frameguard.spec")
        assert "_build_stamp.json" in spec

    def test_every_package_module_is_a_hidden_import(self):
        spec = _read("frameguard.spec")
        pkg_dir = os.path.join(REPO, "frameguard")
        for fn in os.listdir(pkg_dir):
            if fn.endswith(".py") and fn != "__init__.py":
                mod = f"frameguard.{fn[:-3]}"
                assert f"'{mod}'" in spec, f"{mod} missing from spec hiddenimports"

    def test_installer_does_not_use_external_flag_for_tools(self):
        """`external` reads the source at install time from a build-machine
        path, so those entries silently do nothing."""
        iss = _read("installer.iss")
        for line in iss.splitlines():
            if line.strip().startswith("Source:") and "external" in line:
                pytest.fail(f"external flag in installer: {line.strip()}")

    def test_fg_file_association_is_registered(self):
        iss = _read("installer.iss")
        assert "Software\\Classes\\.fg" in iss
        assert "FrameGuard.Preset" in iss
        assert '""%1""' in iss or '"%1"' in iss

    def test_bundled_presets_exist_on_disk(self):
        presets = os.path.join(REPO, "presets")
        files = [f for f in os.listdir(presets) if f.endswith((".json", ".fg"))]
        assert files, "presets/ is empty"

    def test_requirements_lists_runtime_deps(self):
        reqs = _read("requirements.txt").lower()
        for pkg in ("pyside6", "fpdf2", "psutil"):
            assert pkg in reqs

    def test_release_builders_use_the_shared_constraints(self):
        constraints = _read("constraints-3.12.txt").lower()
        for pkg in ("pyside6==", "fpdf2==", "psutil==", "pyinstaller==",
                    "pytest==", "pillow=="):
            assert pkg in constraints

        for script in ("build_installer.bat", "build_mac.sh"):
            body = _read(script)
            assert "constraints-3.12.txt" in body
            assert "-c" in body
            assert "pip check" in body
            assert "--skip-delta" in body

    def test_pyside_pin_preserves_the_advertised_macos_floor(self):
        constraints = _read("constraints-3.12.txt")
        spec = _read("frameguard.spec")
        assert "PySide6==6.9.3" in constraints
        assert "PySide6_Essentials==6.9.3" in constraints
        assert '"LSMinimumSystemVersion": "12.0"' in spec
        assert "6.12 requires macOS 14.4" in constraints


class TestToolDiscovery:
    def test_candidate_dirs_include_the_source_tree(self):
        from frameguard.tools import _candidate_tools_dirs

        dirs = _candidate_tools_dirs()
        assert dirs, "no tool directories resolved"
        assert any(os.path.isdir(d) for d in dirs)

    def test_frozen_layouts_are_probed(self, monkeypatch, tmp_path):
        """PyInstaller >=6 puts data under _internal/; one-file uses _MEIPASS."""
        import sys
        from frameguard import tools

        exe_dir = tmp_path / "app"
        (exe_dir / "_internal" / "tools").mkdir(parents=True)
        (exe_dir / "tools").mkdir()
        meipass = tmp_path / "meipass"
        (meipass / "tools").mkdir(parents=True)

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
        monkeypatch.setattr(sys, "executable", str(exe_dir / "FrameGuard.exe"))

        dirs = tools._candidate_tools_dirs()
        assert str(meipass / "tools") in dirs
        assert str(exe_dir / "_internal" / "tools") in dirs
        assert str(exe_dir / "tools") in dirs

    def test_capabilities_are_cached(self, monkeypatch):
        """Uncached, this ran two extra FFmpeg processes per scan segment."""
        from frameguard import tools

        class Result:
            def __init__(self, stdout):
                self.stdout = stdout

        calls = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            if "-filters" in args:
                return Result("libvmaf signalstats ebur128")
            return Result("Hardware acceleration methods:\nvideotoolbox\n")

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        tools._caps_cache.clear()
        first = tools.detect_ffmpeg_capabilities("/fake/ffmpeg")
        assert tools._caps_cache, "capabilities were not cached"
        assert tools.detect_ffmpeg_capabilities("/fake/ffmpeg") == first
        assert len(calls) == 2

    def test_unparseable_version_is_still_usable(self, monkeypatch):
        """Nightly builds report 'N-114523-g4a1b2c3' with no dotted version;
        returning None for those crashed the app at startup."""
        from frameguard import tools

        monkeypatch.setattr(tools, "_find_executable", lambda name: f"/fake/{name}")
        monkeypatch.setattr(tools, "_get_tool_version",
                            lambda path, name: ("ffmpeg version N-114523-g4a1b2c3", ()))
        info = tools._detect_tool("ffmpeg")
        assert info.found
        assert info.meets_minimum
        assert info.path == "/fake/ffmpeg"


class TestUpdaterApi:
    def test_signals_used_by_the_gui_exist(self):
        """gui.py connected to a 'no_update' signal that never existed."""
        from frameguard.updater import UpdateManager

        for sig in ("update_available", "update_not_found", "check_failed",
                    "download_progress", "download_complete", "status_changed"):
            assert hasattr(UpdateManager, sig), f"UpdateManager.{sig} missing"

    def test_no_stale_signal_names_referenced(self):
        gui = _read(os.path.join("frameguard", "gui.py"))
        assert ".no_update" not in gui

    def test_check_alias_exists(self):
        """A shipped build crashed with 'no attribute check'."""
        from frameguard.updater import UpdateManager

        assert UpdateManager.check is UpdateManager.check_now

    def test_public_methods_exist(self):
        from frameguard.updater import UpdateManager

        for m in ("start_auto_check", "check_now", "apply_update",
                  "get_update_source", "set_update_source"):
            assert callable(getattr(UpdateManager, m, None)), m


class TestWatchFolderApi:
    def test_paused_monitor_is_still_active(self):
        """is_active used to be timer-based, so a paused monitor reported
        inactive and could never be resumed — auto-scan died after one scan."""
        from frameguard.watchfolder import WatchFolderMonitor

        m = WatchFolderMonitor()
        m._enabled = True
        m._timer.stop()
        assert m.is_active
        assert m.is_paused
        assert not m.is_polling

    def test_stopped_monitor_is_inactive(self):
        from frameguard.watchfolder import WatchFolderMonitor

        m = WatchFolderMonitor()
        m.stop()
        assert not m.is_active
        assert not m.is_paused

    def test_invalid_path_stops_polling(self, tmp_path):
        from frameguard.watchfolder import WatchFolderMonitor

        m = WatchFolderMonitor()
        m.set_watch_path(str(tmp_path / "does-not-exist"))
        assert m.watch_path is None
        assert not m.is_polling
