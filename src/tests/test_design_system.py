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

    def test_gutter_and_sidebar_do_not_jump_while_resizing(self):
        """0.9.1: stepped gutters (16/24/32 px) and a sidebar that narrowed at
        1100 px made the page jump sideways mid-drag. Both are now constant."""
        widths = range(760, 2561, 20)
        assert len({D.page_margins(w) for w in widths}) == 1
        assert len({D.sidebar_width(w) for w in widths}) == 1
        assert D.page_margins(1000) % 8 == 0


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
        original = settings.value("theme", "Blue Hour")
        try:
            settings.setValue("theme", "Carbon")
            set_current_theme("Blue Hour")      # live theme deliberately differs
            assert get_saved_theme_name() == "Carbon"

            splash = SplashScreen()
            assert splash._theme.name == "Carbon", (
                f"splash painted in {splash._theme.name}, not the saved Carbon")
            splash.close()
        finally:
            settings.setValue("theme", original)

    def test_unknown_saved_theme_heals_to_os_appearance(self, qapp, monkeypatch):
        from PySide6.QtCore import QSettings
        from frameguard import gui as G

        settings = QSettings(G._SETTINGS_ORG, G._SETTINGS_APP)
        settings.clear()
        settings.setValue(G._THEME_KEY, "A Theme That Was Deleted")
        monkeypatch.setattr(G, "_os_prefers_dark", lambda app=None: False)

        assert G.get_saved_theme_name() == "Clean Light"
        assert not settings.contains(G._THEME_KEY)
        assert not settings.contains(G._LEGACY_THEME_KEY)

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
        splash.set_theme("Blue Hour")
        blue = splash._logo.toImage()
        splash.set_theme("Monokai Pro")
        amber = splash._logo.toImage()
        splash.close()

        assert blue != amber, "the logo did not recolour with the theme"

    def test_mark_is_two_tone_in_the_theme_colours(self, qapp):
        """The splash mark keeps the brand structure in every theme: stem in
        the theme's text colour, tick in its accent (not one flat tint)."""
        from PySide6.QtGui import QColor
        from frameguard.gui import SplashScreen
        from frameguard.theme import get_theme

        splash = SplashScreen()
        splash.set_theme("Monokai Pro")
        image = splash._logo.toImage()
        splash.close()
        theme = get_theme("Monokai Pro")

        def near(c, ref, tol=40):
            r = QColor(ref)
            return (abs(c.red() - r.red()) + abs(c.green() - r.green())
                    + abs(c.blue() - r.blue())) <= tol

        stem = tick = 0
        for y in range(0, image.height(), 2):
            for x in range(0, image.width(), 2):
                c = image.pixelColor(x, y)
                if c.alpha() < 250:
                    continue
                stem += near(c, theme.text_primary)
                tick += near(c, theme.accent)
        assert stem > 40 and tick > 40, (stem, tick)

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

    def test_application_appearance_installs_the_resolved_ui_font(self, qapp):
        from PySide6.QtGui import QFontInfo
        from frameguard import design as D
        from frameguard.gui import _apply_application_palette
        from frameguard.theme import get_theme

        _apply_application_palette(get_theme("Clean Light"), qapp)
        assert QFontInfo(qapp.font()).family() == QFontInfo(D.ui_font()).family()
        assert qapp.font().pointSize() == D.FONT_BODY


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

    def test_sidebar_width_is_stable_across_window_widths(self, window, qapp):
        """The sidebar keeps one width while the window is resized (0.9.1)."""
        window._apply_responsive_layout(1000)
        narrow = window._sidebar.maximumWidth()
        window._apply_responsive_layout(1920)
        wide = window._sidebar.maximumWidth()
        assert narrow == wide == D.sidebar_width(1280)

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
        window._apply_theme("Blue Hour")
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


class TestSemanticThemeContract:
    ACTION_ROLES = {
        "primary", "secondary", "quiet", "destructive", "toolbar", "icon",
    }
    SURFACE_ROLES = {
        "page", "toolbar", "sidebar", "statistic", "framed-tool", "status",
    }
    TEXT_ROLES = {"heading", "muted", "metric", "status"}

    def test_fg_role_is_the_single_semantic_property_contract(self):
        from frameguard import theme

        assert theme.FG_ROLE_PROPERTY == "fgRole"
        assert set(theme.ACTION_ROLES) == self.ACTION_ROLES
        assert set(theme.SURFACE_ROLES) == self.SURFACE_ROLES
        assert set(theme.TEXT_ROLES) == self.TEXT_ROLES
        assert theme.FG_ROLES == (
            self.ACTION_ROLES | self.SURFACE_ROLES | self.TEXT_ROLES)

    def test_all_semantic_roles_have_type_specific_selectors(self):
        from frameguard.theme import generate_stylesheet

        css = generate_stylesheet()
        for role in self.ACTION_ROLES:
            assert f'QPushButton[fgRole="{role}"]' in css
        for role in self.SURFACE_ROLES:
            assert f'QWidget[fgRole="{role}"]' in css
        for role in self.TEXT_ROLES:
            assert f'QLabel[fgRole="{role}"]' in css

    def test_generic_and_semantic_buttons_own_every_state(self):
        from frameguard.theme import generate_stylesheet

        css = generate_stylesheet()
        states = ("hover", "pressed", "checked", "focus", "disabled", "default")
        for state in states:
            assert f"QPushButton:{state}" in css
        for role in self.ACTION_ROLES:
            for state in states:
                selector = f'QPushButton[fgRole="{role}"]:{state}'
                assert selector in css, f"missing state selector: {selector}"

    def test_stylesheet_has_no_accidental_bare_selector_lines(self):
        from frameguard.theme import generate_stylesheet

        bare = []
        for line in generate_stylesheet().splitlines():
            stripped = line.strip()
            if (stripped.startswith(("Q", "#", "*")) and "{" not in stripped
                    and not stripped.endswith(",")):
                bare.append(stripped)
        assert not bare, f"selectors without declaration blocks: {bare}"

    def test_stylesheet_uses_supported_point_sized_typography(self):
        import re
        from frameguard.theme import generate_stylesheet

        css = generate_stylesheet()
        assert "text-transform" not in css
        assert "font-family" not in css
        assert not re.search(r"letter-spacing\s*:\s*-", css)
        assert not re.search(r"font-size\s*:\s*\d+(?:\.\d+)?px", css)
        assert re.search(r"font-size\s*:\s*\d+(?:\.\d+)?pt", css)

    def test_every_theme_renders_without_unresolved_tokens(self):
        from frameguard.theme import THEMES, generate_stylesheet

        assert tuple(THEMES) == (
            "Blue Hour", "Midnight Charcoal", "Slate Professional", "Nord Frost",
            "Monokai Pro", "Solarized Dark", "Darkula", "Carbon", "Abyss Teal",
            "Clean Light", "Warm Cream", "Arctic Blue", "Sage", "Pearl", "Glacier",
        )
        for name, theme in THEMES.items():
            css = generate_stylesheet(theme)
            assert "%%" not in css, f"unresolved token in {name}"
            assert theme.accent in css
            assert theme.accent_foreground in css
            assert theme.disabled_text in css

    def test_all_interactive_state_contrast(self):
        from frameguard.theme import THEMES, contrast_ratio

        failures = []
        for name, theme in THEMES.items():
            checks = {
                "accent foreground": (
                    min(contrast_ratio(theme.accent_foreground, fill) for fill in (
                        theme.accent, theme.accent_hover, theme.accent_pressed)), 4.5),
                "selection foreground": (
                    contrast_ratio(theme.selection_foreground, theme.selection), 4.5),
                "focus boundary": (
                    min(contrast_ratio(theme.focus, fill) for fill in (
                        theme.bg_primary, theme.bg_secondary, theme.bg_input)), 3.0),
                "disabled content": (
                    contrast_ratio(theme.disabled_text, theme.disabled_bg), 3.0),
                "destructive foreground": (
                    min(contrast_ratio(theme.destructive_foreground, fill)
                        for fill in (theme.destructive, theme.destructive_hover,
                                     theme.destructive_pressed)), 4.5),
            }
            for label, (ratio, minimum) in checks.items():
                if ratio < minimum:
                    failures.append(f"{name}/{label}: {ratio:.2f}:1")
        assert not failures, "; ".join(failures)

    def test_all_status_surface_contrast(self):
        from frameguard.theme import THEMES, contrast_ratio

        failures = []
        for name, theme in THEMES.items():
            for status in ("pass", "fail", "warn", "skip"):
                text = getattr(theme, f"status_{status}")
                surface = getattr(theme, f"status_{status}_surface")
                border = getattr(theme, f"status_{status}_border")
                text_ratio = contrast_ratio(text, surface)
                border_ratio = contrast_ratio(border, surface)
                if text_ratio < 4.5:
                    failures.append(
                        f"{name}/{status} text: {text_ratio:.2f}:1")
                if border_ratio < 3.0:
                    failures.append(
                        f"{name}/{status} border: {border_ratio:.2f}:1")
        assert not failures, "; ".join(failures)

    def test_scrollbars_splitters_and_group_sections_follow_contract(self):
        from frameguard.theme import generate_stylesheet

        css = generate_stylesheet()
        assert f"width: {D.SCROLLBAR_HIT_AREA}px" in css
        assert f"height: {D.SCROLLBAR_HIT_AREA}px" in css
        assert f"width: {D.SPLITTER_HIT_AREA}px" in css
        assert f"height: {D.SPLITTER_HIT_AREA}px" in css
        generic_group = css[css.index("QGroupBox {"):css.index("QGroupBox::title")]
        assert "border: none" in generic_group
        assert 'QGroupBox[fgRole="framed-tool"]' in css

    def test_icon_role_meets_hit_area_in_every_theme(self, qapp):
        from PySide6.QtWidgets import QPushButton
        from frameguard.theme import THEMES, generate_stylesheet

        original = qapp.styleSheet()
        button = QPushButton()
        button.setProperty("fgRole", "icon")
        try:
            for name, theme in THEMES.items():
                qapp.setStyleSheet(generate_stylesheet(theme))
                button.style().unpolish(button)
                button.style().polish(button)
                hint = button.sizeHint()
                assert hint.width() >= D.CONTROL_SM, f"{name}: icon width {hint.width()}"
                assert hint.height() >= D.CONTROL_SM, f"{name}: icon height {hint.height()}"
        finally:
            button.deleteLater()
            qapp.setStyleSheet(original)


class TestMetricHelpers:
    def test_fonts_are_point_sized_and_platform_resolved(self, qapp):
        assert D.font_css(D.FONT_BODY, D.WEIGHT_MEDIUM) == (
            f"font-size: {D.FONT_BODY}pt; font-weight: {D.WEIGHT_MEDIUM};")
        ui = D.ui_font()
        mono = D.mono_font()
        assert ui.pointSize() == D.FONT_BODY
        assert mono.pointSize() == D.FONT_SMALL
        assert ui.pixelSize() == -1 and mono.pixelSize() == -1
        assert ui.family() and mono.family()

    def test_text_metrics_derive_readable_grid_aligned_heights(self, qapp):
        from PySide6.QtGui import QFontMetrics

        metrics = QFontMetrics(D.ui_font())
        assert D.line_height(metrics) == metrics.lineSpacing()
        assert D.label_height(metrics, lines=2) >= metrics.height() + metrics.lineSpacing()
        assert D.control_height(metrics, qapp.style()) >= D.CONTROL_MD
        assert D.table_row_height(metrics) >= D.ROW_H
        assert D.table_header_height(metrics, lines=2) > D.table_header_height(
            metrics, lines=1)
        for value in (
                D.label_height(metrics, lines=2),
                D.control_height(metrics, qapp.style()),
                D.table_row_height(metrics),
                D.table_header_height(metrics, lines=2)):
            assert value % D.SPACE_1 == 0

    def test_bounded_text_width_uses_realized_metrics(self, qapp):
        from PySide6.QtGui import QFontMetrics

        metrics = QFontMetrics(D.ui_font())
        short = D.bounded_text_width("File", metrics, 80, 240)
        long = D.bounded_text_width("A very long filename that must be bounded", metrics,
                                    80, 240)
        assert 80 <= short <= long <= 240
        assert D.bounded_text_width("x", metrics, 120, 120) == 120

    def test_screen_safe_geometry_centers_and_clamps(self):
        from PySide6.QtCore import QRect, QSize

        available = QRect(100, 50, 1000, 700)
        normal = D.screen_safe_geometry(QSize(800, 600), available)
        oversized = D.screen_safe_geometry(QSize(1600, 1200), available)
        assert normal.center() == available.center()
        assert available.contains(normal)
        assert available.contains(oversized)
        assert oversized.width() == available.width() - 2 * D.SPACE_2
        assert oversized.height() == available.height() - 2 * D.SPACE_2


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

    def test_panel_gutters_hold_still_while_resizing(self, window, qapp):
        """0.9.1: the gutter is one value at every width, so the page no longer
        jumps sideways when a window edge is dragged across a breakpoint."""
        from frameguard import gui as G

        window._switch_panel(G.PANEL_RESULTS)
        window._apply_responsive_layout(1000)
        narrow = window.results_panel.layout().contentsMargins().left()

        window._apply_responsive_layout(1920)
        wide = window.results_panel.layout().contentsMargins().left()

        assert narrow == wide == D.page_margins(1280), f"gutter moved: {narrow} vs {wide}"
