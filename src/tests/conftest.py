"""Shared pytest fixtures for the FrameGuard test suite.

Qt runs on the "offscreen" platform plugin so the whole suite is headless and
safe to run in CI or over SSH.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FRAMEGUARD_TEST_MODE", "1")

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from frameguard.core import QAResult  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session (Qt forbids more than one)."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_user_dirs(tmp_path, monkeypatch):
    """Redirect ~ so tests never touch the developer's real presets or reports."""
    home = tmp_path / "home"
    (home / "Documents" / "FrameGuard").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home), 1))
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))
    return home


@pytest.fixture(autouse=True)
def isolated_qsettings(tmp_path, monkeypatch):
    """Give every test its own QSettings store.

    QSettings does not live under $HOME: on Windows it writes to
    HKCU\\Software. Redirecting the home directory therefore did nothing for
    it, and the suite was both writing to the developer's real registry and
    leaking saved state between tests — a test that persisted a setting
    silently changed the starting conditions of every test after it, and of
    the next run.

    QSettings.setDefaultFormat() is not sufficient: the (organization,
    application) constructor still resolves to the native backend. The class
    is replaced in each importing module instead, which is unambiguous.
    """
    from PySide6.QtCore import QSettings

    ini = str(tmp_path / "qsettings.ini")

    class IsolatedQSettings(QSettings):
        def __init__(self, *args, **kwargs):
            super().__init__(ini, QSettings.IniFormat)

    # Both the modules that hold a module-level reference, and QtCore itself
    # for tests that do `from PySide6.QtCore import QSettings` inside the test
    # body — otherwise a test writes to one store and the code under test
    # reads another.
    for target in ("PySide6.QtCore.QSettings",
                   "frameguard.gui.QSettings",
                   "frameguard.updater.QSettings"):
        try:
            monkeypatch.setattr(target, IsolatedQSettings)
        except (AttributeError, ImportError):
            pass

    return ini


def make_result(name="SHOW_EP01_4K_30p.mov", outcome="fail", **metadata):
    """Build a realistic finalized QAResult without touching the filesystem."""
    r = QAResult(os.path.join("C:\\videos", name))
    defaults = {
        "video_codec": "hevc",
        "resolution": "3840x2160",
        "frame_rate": 29.97,
        "duration_display": "00:01:30",
        "file_size_mb": 1234.5,
    }
    defaults.update(metadata)
    for k, v in defaults.items():
        r.set_metadata(k, v)

    r.add_pass("Container Format", "mov accepted")
    if outcome in ("warn", "fail"):
        # Must be a name in core.ADVISORY_CHECKS, otherwise overall_status()
        # ignores it and the result stays a plain PASS.
        r.add_warn("Black Frames", "2 black sequence(s), total 1.4s", confidence=60)
    if outcome == "fail":
        r.add_fail("Bitrate", "12.5 Mbps is 38% below minimum 20 Mbps")
    r.finalize()
    return r


@pytest.fixture
def result_factory():
    return make_result
