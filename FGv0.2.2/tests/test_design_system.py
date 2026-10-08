"""Design-system regression tests.

An audit found 116 layout defects: 10 distinct control heights, 9 margin
values, 7 font sizes, 23 clipped labels, 3 panels overflowing their container
and 13 WCAG contrast failures. These tests stop that drifting back.

They run headless and are cheap, so they guard every future UI change.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frameguard import design as D  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(relative_path):
    with open(os.path.join(REPO, relative_path), encoding="utf-8") as f:
        return f.read()


def _lum(rgb):
    def channel(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def contrast(a, b):
    l1, l2 = _lum(_hex(a)), _lum(_hex(b))
    lo, hi = sorted((l1, l2))
    return (hi + 0.05) / (lo + 0.05)


class TestTokens:
    def test_spacing_scale_is_on_the_8pt_grid(self):
        for name in dir(D):
            if name.startswith("SPACE_"):
                value = getattr(D, name)
                assert value % 4 == 0, f"{name}={value} is off the 4/8 grid"

    def test_control_heights_are_on_the_grid(self):
        for name in ("CONTROL_SM", "CONTROL_MD", "CONTROL_LG", "CONTROL_XL"):
            assert getattr(D, name) % 4 == 0

    def test_control_heights_meet_minimum_click_target(self):
        """21px buttons shipped in Settings; anything under 28px is too small
        to hit comfortably with a mouse."""
        assert D.CONTROL_SM >= 28

    def test_type_scale_is_ordered_and_distinct(self):
        scale = [D.FONT_CAPTION, D.FONT_SMALL, D.FONT_BODY, D.FONT_SUBHEAD,
                 D.FONT_TITLE, D.FONT_DISPLAY, D.FONT_HERO]
        assert scale == sorted(scale)
        assert len(set(scale)) == len(scale)

    def test_page_margins_shrink_on_narrow_windows(self):
        """A fixed 32px gutter is what made the UI feel cramped until maximized."""
        narrow = D.page_margins(1000)
        wide = D.page_margins(1920)
        assert narrow < wide
        assert narrow % 8 == 0 and wide % 8 == 0

    def test_sidebar_shrinks_on_narrow_windows(self):
        assert D.sidebar_width(1000) < D.sidebar_width(1920)


class TestThemeContrast:
    """Every theme must satisfy WCAG AA against both background layers."""

    @staticmethod
    def _themes():
        from frameguard.theme import THEMES, get_theme
        return [(name, get_theme(name)) for name in THEMES]

    @pytest.mark.parametrize("pair,minimum", [
        (("text_primary", "bg_primary"), 4.5),
        (("text_secondary", "bg_primary"), 4.5),
        (("text_tertiary", "bg_primary"), 4.5),
        (("text_secondary", "bg_secondary"), 4.5),
        (("text_tertiary", "bg_secondary"), 4.5),
        (("accent", "bg_primary"), 3.0),
        (("status_pass", "bg_primary"), 3.0),
        (("status_fail", "bg_primary"), 3.0),
        (("status_warn", "bg_primary"), 3.0),
    ])
    def test_contrast_meets_wcag_aa(self, pair, minimum):
        fg_attr, bg_attr = pair
        failures = []
        for name, theme in self._themes():
            ratio = contrast(getattr(theme, fg_attr), getattr(theme, bg_attr))
            if ratio < minimum:
                failures.append(f"{name}: {ratio:.2f}:1")
        assert not failures, (
            f"{fg_attr} on {bg_attr} needs {minimum}:1 -- " + ", ".join(failures))


class TestThemeCatalogue:
    def test_grid_is_complete_at_three_columns(self):
        """The picker lays out 3 per row; a partial last row looks unfinished."""
        from frameguard.theme import THEMES
        assert len(THEMES) % 3 == 0, (
            f"{len(THEMES)} themes leaves {len(THEMES) % 3} on the last row")

    def test_new_dark_themes_are_present(self):
        from frameguard.theme import THEMES
        for name in ("Carbon", "Abyss Teal"):
            assert name in THEMES

    def test_accent_is_distinguishable_from_that_themes_status_colours(self):
        """Green means PASS and red means FAIL in the results table. An accent
        that is perceptually close to either reads as a verdict.

        Compared against each theme's own status colours rather than against
        abstract "green"/"red", since what matters is whether a user could
        confuse two swatches sitting in the same window.
        """
        from frameguard.theme import THEMES, get_theme

        def rgb(value):
            value = value.lstrip("#")
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

        def distance(a, b):
            # Weighted euclidean, approximating perceived difference.
            ar, ag, ab = rgb(a)
            br, bg, bb = rgb(b)
            rmean = (ar + br) / 2
            dr, dg, db = ar - br, ag - bg, ab - bb
            return (((2 + rmean / 256) * dr * dr)
                    + 4 * dg * dg
                    + ((2 + (255 - rmean) / 256) * db * db)) ** 0.5

        offenders = []
        for name in THEMES:
            t = get_theme(name)
            for label, status in (("PASS", t.status_pass), ("FAIL", t.status_fail)):
                d = distance(t.accent, status)
                if d < 90:
                    offenders.append(
                        f"{name}: accent {t.accent} is only {d:.0f} from "
                        f"{label} {status}")
        assert not offenders, "; ".join(offenders)


class TestSplashTheme:
    def test_splash_uses_the_saved_theme_not_the_default(self, qapp):
        """The splash is built before MainWindow applies the saved theme, so
        reading the live theme meant it always painted in the default."""
        from PySide6.QtCore import QSettings
        from frameguard.gui import (SplashScreen, get_saved_theme_name,
                                    _SETTINGS_APP, _SETTINGS_ORG)
        from frameguard.theme import set_current_theme

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        original = settings.value("theme", "Meta Dark")
        try:
            settings.setValue("theme", "Carbon")
            set_current_theme("Meta Dark")      # live theme deliberately differs
            assert get_saved_theme_name() == "Carbon"

            splash = SplashScreen()
            assert splash._theme.name == "Carbon", (
                f"splash painted in {splash._theme.name}, not the saved Carbon")
            splash.close()
        finally:
            settings.setValue("theme", original)

    def test_unknown_saved_theme_falls_back(self, qapp):
        from PySide6.QtCore import QSettings
        from frameguard.gui import get_saved_theme_name, _SETTINGS_APP, _SETTINGS_ORG

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        original = settings.value("theme", "Meta Dark")
        try:
            settings.setValue("theme", "A Theme That Was Deleted")
            assert get_saved_theme_name() == "Meta Dark"
        finally:
            settings.setValue("theme", original)

    def test_splash_can_be_retinted_live(self, qapp):
        from frameguard.gui import SplashScreen

        splash = SplashScreen()
        splash.set_theme("Abyss Teal")
        assert splash._theme.name == "Abyss Teal"
        splash.close()

    def test_splash_renders_in_every_theme(self, qapp):
        """Each theme must paint without raising - a splash crash would make
        the app unlaunchable."""
        from frameguard.gui import SplashScreen
        from frameguard.theme import THEMES

        splash = SplashScreen()
        for name in THEMES:
            splash.set_theme(name)
            splash.grab()
        splash.close()

    def test_splash_uses_the_real_logo(self, qapp):
        """It used to draw a rounded box with the letters FG while the actual
        2048px artwork shipped unused."""
        from frameguard.gui import SplashScreen

        splash = SplashScreen()
        assert splash._logo is not None and not splash._logo.isNull()
        splash.close()

    def test_logo_is_tinted_to_the_theme(self, qapp):
        """The mark stayed hard blue in every theme, clashing with the teal,
        amber and purple palettes."""
        from frameguard.gui import SplashScreen

        splash = SplashScreen()
        splash.set_theme("Meta Dark")
        blue = splash._logo.toImage()
        splash.set_theme("Monokai Pro")
        amber = splash._logo.toImage()
        splash.close()

        assert blue != amber, "the logo did not recolour with the theme"

    def test_tinting_preserves_the_silhouette(self, qapp):
        """A flat fill would destroy the shield's internal detail, so the
        colorize has to keep both the alpha mask and the shading."""
        from frameguard.gui import SplashScreen

        splash = SplashScreen()
        source = splash._logo_source.toImage()
        tinted = splash._logo.toImage()
        splash.close()

        assert source.size() == tinted.size()

        opaque_source = opaque_tinted = 0
        distinct_values = set()
        for y in range(0, source.height(), 4):
            for x in range(0, source.width(), 4):
                if source.pixelColor(x, y).alpha() > 128:
                    opaque_source += 1
                tone = tinted.pixelColor(x, y)
                if tone.alpha() > 128:
                    opaque_tinted += 1
                    distinct_values.add((tone.red(), tone.green(), tone.blue()))

        # Silhouette retained...
        assert abs(opaque_source - opaque_tinted) <= opaque_source * 0.05
        # ...and it is not a single flat colour.
        assert len(distinct_values) > 8, (
            f"only {len(distinct_values)} distinct colours; detail was flattened")

    def test_no_hardcoded_colours_in_the_splash(self):
        """Everything the splash paints must come from the theme."""
        import re

        source = _read(os.path.join("frameguard", "gui.py"))
        start = source.index("class SplashScreen")
        end = source.index("class _FileOpenFilter")
        body = source[start:end]

        literals = re.findall(r'QColor\(\s*"#[0-9A-Fa-f]{3,8}"', body)
        assert not literals, f"hardcoded colours in the splash: {literals}"


class TestFonts:
    def test_ui_font_resolves_to_a_real_family(self, qapp):
        from frameguard.gui import ui_font
        assert ui_font(12).family()

    def test_mono_font_is_available_for_numeric_columns(self, qapp):
        from frameguard.gui import mono_font
        assert mono_font(10).family()

    def test_font_choice_is_not_hardcoded_to_one_platform(self):
        from frameguard.gui import _UI_FONT_CANDIDATES
        for key in ("win32", "darwin", "default"):
            assert _UI_FONT_CANDIDATES.get(key), f"no UI font chain for {key}"


class TestSidebar:
    """The sidebar had a 0px left / 8px right inset, so nav rows touched one
    edge and stopped short of the other, and its icons were a mix of geometric
    glyphs and a colour emoji."""

    @pytest.fixture
    def window(self, qapp):
        from frameguard import gui as G
        w = G.MainWindow()
        w.resize(1100, 760)
        w.show()
        for _ in range(4):
            qapp.processEvents()
        yield w
        w.close()

    def test_horizontal_insets_are_symmetric(self, window):
        m = window._sidebar.layout().contentsMargins()
        assert m.left() == m.right(), (
            f"asymmetric sidebar gutter: left {m.left()}px, right {m.right()}px")
        assert m.left() % 4 == 0

    def test_nav_rows_have_equal_gaps_on_both_sides(self, window, qapp):
        for _ in range(3):
            qapp.processEvents()
        btn = window.nav_buttons[0]
        left_gap = btn.x()
        right_gap = window._sidebar.width() - (btn.x() + btn.width())
        assert left_gap == right_gap, (
            f"nav row is off-centre: {left_gap}px left, {right_gap}px right")

    def test_banner_is_full_bleed_but_its_text_aligns_with_the_nav(self, window, qapp):
        """The banner box spans the sidebar edge to edge, while the wordmark
        inside it still lines up with the nav labels.

        This used to assert logo.x() == nav.x(), i.e. that the banner itself
        was inset by the nav gutter — which made it read as a floating panel
        rather than a header.
        """
        for _ in range(3):
            qapp.processEvents()
        sidebar = window._sidebar
        logo = sidebar.findChild(type(sidebar), "logo_area")
        assert logo is not None

        assert logo.x() == 0 and logo.width() == sidebar.width(), (
            f"banner is inset: x={logo.x()}, w={logo.width()}, "
            f"sidebar w={sidebar.width()}")

        # Icon inside the banner vs. the nav rows' own text inset.
        nav = window.nav_buttons[0]
        logo_content_x = logo.layout().contentsMargins().left()
        nav_content_x = nav.x() + 12       # stylesheet padding on #sidebar QPushButton
        assert logo_content_x == nav_content_x, (
            f"banner content at x={logo_content_x} but nav text at x={nav_content_x}")

    def test_nav_rows_are_on_the_control_scale(self, window):
        for btn in window.nav_buttons:
            assert btn.height() in (D.CONTROL_MD, D.CONTROL_LG, D.CONTROL_XL), \
                f"nav row height {btn.height()}px is off the scale"

    def test_sidebar_width_is_not_frozen_by_the_stylesheet(self, window, qapp):
        """A hard-coded min-width/max-width in theme.py silently overrode the
        responsive sizing, so the sidebar never actually narrowed."""
        window.resize(1000, 700)
        for _ in range(4):
            qapp.processEvents()
        narrow = window._sidebar.width()

        window.resize(1920, 1080)
        for _ in range(4):
            qapp.processEvents()
        wide = window._sidebar.width()

        assert narrow < wide, f"sidebar did not adapt: {narrow}px vs {wide}px"
        assert narrow == D.sidebar_width(1000)

    def test_every_nav_row_has_a_real_icon(self, window):
        for btn in window.nav_buttons:
            assert not btn.icon().isNull(), f"{btn.text()!r} has no icon"

    def test_no_emoji_or_geometric_glyphs_in_nav_labels(self, window):
        """The old set mixed U+25B6/U+2611/U+25A6/U+2699 with a colour emoji,
        so weight and size were inconsistent and rendering was font-dependent."""
        for btn in window.nav_buttons:
            for ch in btn.text():
                assert ord(ch) < 0x2000, (
                    f"{btn.text()!r} still uses glyph U+{ord(ch):04X}; "
                    f"use frameguard.icons instead")

    def test_icons_recolour_with_the_active_state(self, window, qapp):
        from frameguard import gui as G

        window._switch_panel(G.PANEL_SCAN)
        qapp.processEvents()
        scan_active = window.nav_buttons[0].icon().pixmap(20, 20).toImage()

        window._switch_panel(G.PANEL_CHECKS)
        qapp.processEvents()
        scan_inactive = window.nav_buttons[0].icon().pixmap(20, 20).toImage()

        assert scan_active != scan_inactive, \
            "the nav icon does not change colour when its row becomes active"

    def test_icons_recolour_with_the_theme(self, window, qapp):
        window._apply_theme("Meta Dark")
        qapp.processEvents()
        dark = window.nav_buttons[1].icon().pixmap(20, 20).toImage()

        window._apply_theme("Clean Light")
        qapp.processEvents()
        light = window.nav_buttons[1].icon().pixmap(20, 20).toImage()

        assert dark != light, "nav icons did not re-render for the new theme"


class TestIconSet:
    def test_every_icon_renders(self):
        from frameguard import icons

        for name in icons.BUILDERS:
            pm = icons.icon_pixmap(name, 20, "#FFFFFF")
            assert not pm.isNull(), f"{name} failed to render"
            assert pm.width() == 20

    def test_icons_are_not_blank(self):
        """A path that draws nothing still yields a valid, empty pixmap."""
        from frameguard import icons

        for name in icons.BUILDERS:
            image = icons.icon_pixmap(name, 32, "#FFFFFF").toImage()
            painted = sum(
                1 for y in range(image.height()) for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            )
            assert painted > 20, f"{name} rendered only {painted} pixels"

    def test_unknown_name_degrades_quietly(self):
        from frameguard import icons

        assert icons.icon_pixmap("nope", 20, "#FFFFFF").isNull()

    def test_cache_is_keyed_by_colour(self):
        from frameguard import icons

        icons.clear_cache()
        a = icons.icon_pixmap("scan", 20, "#FF0000")
        b = icons.icon_pixmap("scan", 20, "#00FF00")
        assert a.toImage() != b.toImage(), "cache ignored the colour key"

    def test_nav_icons_all_exist(self):
        from frameguard import icons

        for name in ("scan", "results", "checks", "watch", "settings", "import"):
            assert name in icons.BUILDERS
class TestLayoutIntegrity:
    """Measured against a real window, at the smallest size we allow."""

    @pytest.fixture
    def window(self, qapp):
        from frameguard import gui as G
        from frameguard.core import QAResult

        def mk(name, kind):
            r = QAResult("C:/videos/" + name)
            r.set_metadata("video_codec", "hevc")
            r.set_metadata("resolution", "3840x2160")
            r.add_pass("Container Format", "ok")
            if kind != "pass":
                r.add_warn("Black Frames", "2 sequences", confidence=60)
            if kind == "fail":
                r.add_fail("Bitrate", "below minimum")
            r.finalize()
            return r

        w = G.MainWindow()
        w.resize(1000, 700)
        w.show()
        qapp.processEvents()
        w.results_panel.set_results(
            [mk(f"SHOW_EP{i:02}_4K_30p_MASTER.mov", ["pass", "warn", "fail"][i % 3])
             for i in range(8)], append=True)
        qapp.processEvents()
        yield w
        w.close()

    def _panels(self, window):
        from frameguard import gui as G
        return [(G.PANEL_SCAN, "Scan"), (G.PANEL_RESULTS, "Results"),
                (G.PANEL_CHECKS, "Checks"), (G.PANEL_WATCH, "Watch"),
                (G.PANEL_SETTINGS, "Settings")]

    def test_no_panel_overflows_at_minimum_size(self, window, qapp):
        """Three panels used to demand more width than they were given, which
        is what produced the horizontal scrollbars and squeezed toolbars."""
        offenders = []
        for index, name in self._panels(window):
            window._switch_panel(index)
            for _ in range(3):
                qapp.processEvents()
            panel = window.stack.widget(index)
            need = panel.minimumSizeHint().width()
            have = panel.width()
            if need > have + 1:
                offenders.append(f"{name}: needs {need}px, has {have}px")
        assert not offenders, "panels overflow their container: " + "; ".join(offenders)

    def test_settings_has_no_horizontal_scrollbar(self, window, qapp):
        """A single un-wrapped label used to force the whole page sideways."""
        from frameguard import gui as G

        window._switch_panel(G.PANEL_SETTINGS)
        for _ in range(3):
            qapp.processEvents()
        sp = window.settings_panel
        assert not sp._scroll.horizontalScrollBar().isVisible(), (
            f"content {sp._content.width()}px exceeds viewport "
            f"{sp._scroll.viewport().width()}px")

    def test_results_toolbar_fits(self, window, qapp):
        """The toolbar needed 1447px in a 711px space before the overflow menu."""
        from frameguard import gui as G

        window._switch_panel(G.PANEL_RESULTS)
        for _ in range(3):
            qapp.processEvents()
        rp = window.results_panel
        margins = rp.layout().contentsMargins()
        available = rp.width() - margins.left() - margins.right()

        row = rp.layout().itemAt(0).layout()
        needed = 0
        for i in range(row.count()):
            item = row.itemAt(i)
            widget = item.widget()
            needed += widget.sizeHint().width() if widget else item.sizeHint().width()
        assert needed <= available, f"toolbar needs {needed}px, has {available}px"

    def test_no_visible_control_is_below_the_click_target(self, window, qapp):
        from PySide6.QtWidgets import QPushButton, QComboBox, QWidget

        offenders = []
        for index, name in self._panels(window):
            window._switch_panel(index)
            for _ in range(3):
                qapp.processEvents()
            for child in window.stack.widget(index).findChildren(QWidget):
                if not isinstance(child, (QPushButton, QComboBox)):
                    continue
                if child.isVisible() and 0 < child.height() < 28:
                    label = child.text() if hasattr(child, "text") else ""
                    offenders.append(f"{name}/{label[:20]!r}={child.height()}px")
        assert not offenders, "controls below 28px: " + ", ".join(offenders)

    def test_panel_gutters_respond_to_width(self, window, qapp):
        from frameguard import gui as G

        window._switch_panel(G.PANEL_RESULTS)
        window.resize(1000, 700)
        for _ in range(3):
            qapp.processEvents()
        narrow = window.results_panel.layout().contentsMargins().left()

        window.resize(1920, 1080)
        for _ in range(3):
            qapp.processEvents()
        wide = window.results_panel.layout().contentsMargins().left()

        assert narrow < wide, f"gutter did not adapt: {narrow} vs {wide}"
