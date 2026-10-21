"""Regression tests for v0.9.0: the Monogram mark and theme-aware app icons.

Before 0.9.0 the Dock always showed the old icon: startup set the .ico as the
application icon and theme changes only retinted the window icon, which macOS
never displays. Qt 6.9 also hands AppKit a 32 px rendition of the application
icon, so the Dock upscaled it. The menu-bar icon was accent-tinted artwork
filling about 60% of its slot instead of a template glyph.
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]


def _alpha_bbox(image, threshold=40):
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() > threshold:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


@pytest.fixture
def window(qapp):
    from frameguard.gui import MainWindow
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    for _ in range(4):
        qapp.processEvents()
    yield w
    w._cleanup_worker()
    if getattr(w, "_watch_monitor", None) is not None:
        w._watch_monitor.stop()
    w.hide()
    w.deleteLater()


class TestMarkGeometry:
    def test_both_optical_masters_render(self, qapp):
        from frameguard import brand
        for small in (False, True):
            stem, tick = brand.glyph_paths(small)
            assert not stem.isEmpty() and not tick.isEmpty()
            ink = brand.silhouette(small).boundingRect()
            assert 0 <= ink.left() and ink.right() <= 100 and 0 <= ink.top() and ink.bottom() <= 100

    def test_small_master_keeps_two_pixel_strokes_at_16px(self):
        from frameguard import brand
        thinnest = min(brand._SMALL["stem_width"], brand._SMALL["tick_width"])
        assert thinnest * 16 / 100 >= 2.0

    def test_mac_icon_follows_apples_grid(self, qapp):
        """824 px body on 1024 with transparent margin: macOS 26+ shows
        conforming icons full size, and shrinks others onto a grey tile."""
        from frameguard import brand
        image = brand.app_icon_image(256, brand.BRAND, "mac")
        left, top, right, bottom = _alpha_bbox(image)
        expected = 256 * brand.MAC_BODY / brand.MAC_GRID
        assert abs((right - left + 1) - expected) <= 2
        assert abs((bottom - top + 1) - expected) <= 2
        assert image.pixelColor(2, 2).alpha() == 0
        assert image.pixelColor(128, 128 + 90).alpha() == 255

    def test_full_grid_icon_fills_the_canvas_for_windows(self, qapp):
        from frameguard import brand
        image = brand.app_icon_image(32, brand.BRAND, "full")
        left, top, right, bottom = _alpha_bbox(image)
        assert right - left + 1 >= 29 and bottom - top + 1 >= 29

    def test_every_theme_icon_is_legible(self):
        from frameguard import brand
        from frameguard.theme import THEMES, contrast_ratio
        for name, theme in THEMES.items():
            palette = brand.theme_palette(theme)
            for plate in (palette.plate_top, palette.plate_bottom):
                assert contrast_ratio(palette.stem, plate) >= 4.5, (name, plate)
                assert contrast_ratio(palette.tick, plate) >= 3.0, (name, plate)
            for page in (theme.bg_primary, theme.bg_secondary):
                assert contrast_ratio(theme.accent, page) >= 3.0, (name, page)


class TestMenuBarIcon:
    def test_template_glyph_is_a_mask(self, qapp):
        from frameguard import brand
        assert brand.menubar_template_icon().isMask()

    def test_template_glyph_fills_the_slot(self, qapp):
        from frameguard import brand
        image = brand.menubar_template_image(18, 2)
        left, top, right, bottom = _alpha_bbox(image)
        assert max(right - left + 1, bottom - top + 1) >= 34  # of 36 px

    def test_tray_uses_the_template_on_macos(self, qapp, monkeypatch):
        from frameguard import gui
        from frameguard.theme import get_theme
        monkeypatch.setattr(gui.sys, "platform", "darwin")
        assert gui._tray_icon_for(get_theme("Blue Hour")).isMask()
        monkeypatch.setattr(gui.sys, "platform", "win32")
        assert not gui._tray_icon_for(get_theme("Blue Hour")).isMask()


class TestThemedAppIcon:
    @staticmethod
    def _plate(icon):
        from PySide6.QtCore import QSize
        image = icon.pixmap(QSize(128, 128)).toImage()
        return image.pixelColor(int(image.width() * 0.25), int(image.height() * 0.80))

    def test_theme_change_recolours_application_and_window_icons(self, window, qapp):
        from PySide6.QtGui import QColor
        from frameguard.theme import get_theme
        for name in ("Blue Hour", "Clean Light", "Monokai Pro"):
            window._apply_theme(name)
            qapp.processEvents()
            theme = get_theme(name)
            for icon in (qapp.windowIcon(), window.windowIcon()):
                plate = self._plate(icon)
                bgs = [QColor(theme.bg_primary), QColor(theme.bg_secondary), QColor(theme.bg_tertiary)]
                assert min(abs(plate.lightness() - c.lightness()) for c in bgs) <= 24, (name, plate.name())

    def test_sidebar_mark_is_two_tone_and_retina_ready(self, window, qapp):
        from PySide6.QtGui import QColor
        from frameguard.theme import get_theme
        window._apply_theme("Abyss Teal")
        pixmap = window._sidebar_logo_small.pixmap()
        assert pixmap.deviceIndependentSize().width() == 28
        image = pixmap.toImage()
        theme = get_theme("Abyss Teal")
        colours = {image.pixelColor(x, y).name() for y in range(image.height())
                   for x in range(image.width()) if image.pixelColor(x, y).alpha() == 255}
        def has(ref):
            r = QColor(ref)
            return any(sum(abs(a - b) for a, b in zip(QColor(c).getRgb()[:3], r.getRgb()[:3])) <= 30
                       for c in colours)
        assert has(theme.text_primary) and has(theme.accent)

    def test_dock_setter_is_a_noop_off_cocoa(self, qapp):
        from frameguard import brand, gui
        if qapp.platformName() == "cocoa":
            pytest.skip("exercised natively by the subprocess probe")
        assert gui._set_macos_dock_icon(brand.app_icon_image(64)) is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Dock only")
    def test_dock_shows_the_themed_icon_at_full_resolution(self, tmp_path):
        env = dict(os.environ, QT_QPA_PLATFORM="cocoa", HOME=str(tmp_path))
        env.pop("FRAMEGUARD_TEST_MODE", None)
        completed = subprocess.run([sys.executable, str(SRC / "tests" / "dock_icon_probe.py")],
                                   cwd=str(SRC), env=env, capture_output=True, text=True, timeout=90)
        if completed.returncode == 3:
            pytest.skip("not running on Cocoa: " + completed.stdout.strip()[-200:])
        assert completed.returncode == 0, completed.stdout + completed.stderr[-2000:]
        rows = {parts[1]: parts for parts in (line.split() for line in completed.stdout.splitlines())
                if parts and parts[0] == "ICON"}
        from PySide6.QtGui import QColor
        assert int(rows["Blue_Hour"][2]) >= 1024 and int(rows["Clean_Light"][2]) >= 1024
        assert QColor(rows["Blue_Hour"][3]).lightness() < 80
        assert QColor(rows["Clean_Light"][3]).lightness() > 200


class TestShippedIcons:
    def test_committed_brand_assets_match_the_geometry(self, qapp):
        sys.path.insert(0, str(SRC / "tools_dev"))
        try:
            import make_brand_assets
        finally:
            sys.path.pop(0)
        assert make_brand_assets.main(["--check"]) == 0

    def test_icns_packs_every_size_from_branding(self, tmp_path):
        out = tmp_path / "fg.icns"
        completed = subprocess.run([sys.executable, str(SRC / "assets" / "make_icns.py"), "--out", str(out)],
                                   capture_output=True, text=True, timeout=60)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        data = out.read_bytes()
        assert data[:4] == b"icns" and struct.unpack(">I", data[4:8])[0] == len(data)
        sizes, offset = set(), 8
        while offset < len(data):
            length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            payload = data[offset + 8:offset + length]
            sizes.add(struct.unpack(">I", payload[16:20])[0])
            offset += length
        assert sizes == {16, 32, 64, 128, 256, 512, 1024}

    def test_ico_has_every_windows_size(self):
        data = (SRC / "assets" / "frameguard.ico").read_bytes()
        count = struct.unpack("<H", data[4:6])[0]
        sizes = set()
        for i in range(count):
            width = data[6 + 16 * i] or 256
            offset = struct.unpack("<I", data[18 + 16 * i:22 + 16 * i])[0]
            assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
            sizes.add(width)
        assert {16, 20, 24, 32, 40, 48, 64, 256} <= sizes

    def test_no_reference_to_deleted_artwork(self):
        gone = ("frameguard_logo.png", "icon_64.png", "theme_icons.json", "decode_icons")
        roots = [SRC / "frameguard", SRC / "tools_dev", SRC / "assets"]
        files = [p for root in roots for p in root.rglob("*") if p.suffix in (".py", ".json")]
        files += [SRC / n for n in ("frameguard.spec", "installer.iss", "build_mac.sh",
                                    "BUILD_README.md", "README.md") if (SRC / n).exists()]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in gone:
                assert name not in text, f"{path.relative_to(SRC)} still references {name}"

    def test_brand_module_is_frozen_into_the_app(self):
        assert "'frameguard.brand'" in (SRC / "frameguard.spec").read_text(encoding="utf-8")


def test_installer_shows_the_same_licence_as_the_repository():
    """installer.iss displays src/LICENSE; it must not drift from the
    repository's canonical MIT text (GitHub's licence detection reads the root)."""
    root = (SRC.parent / "LICENSE").read_text(encoding="utf-8")
    assert (SRC / "LICENSE").read_text(encoding="utf-8") == root
