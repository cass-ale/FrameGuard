"""
FrameGuard Desktop -- PDF Report Generator.
Generates styled handoff reports from QAResult objects.
Uses fpdf2 for PDF generation. Theme-aware: matches the active UI theme.

v0.5.0 changes:
  - Theme-aware: report colors dynamically match active theme
  - Modern card-based layout with generous whitespace
  - Pill-shaped status badges
  - Improved typography hierarchy
  - Updated design language to match UI overhaul
"""

import os
import sys
from datetime import datetime
from typing import List
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from frameguard import __version__, audio_checks
from frameguard.core import ESSENTIAL_CHECKS, QAResult
from frameguard.theme import get_theme, ThemeColors


def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert a six/eight-digit CSS hex color to an RGB tuple safely."""
    if not isinstance(hex_color, str):
        return (128, 128, 128)
    h = hex_color.removeprefix("#")
    # Report PDFs do not support alpha; ignore a valid CSS alpha channel.
    if len(h) == 8:
        h = h[:6]
    if len(h) != 6:
        return (128, 128, 128)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (128, 128, 128)


def _get_report_colors(theme: ThemeColors = None):
    """Get report color palette from the active theme."""
    if theme is None:
        theme = get_theme()
    t = theme
    return {
        "bg_primary": _hex_to_rgb(t.bg_primary),
        "bg_secondary": _hex_to_rgb(t.bg_secondary),
        "bg_tertiary": _hex_to_rgb(t.bg_tertiary),
        "surface": _hex_to_rgb(t.surface),
        "border": _hex_to_rgb(t.border),
        "text_primary": _hex_to_rgb(t.text_primary),
        "text_secondary": _hex_to_rgb(t.text_secondary),
        "text_tertiary": _hex_to_rgb(t.text_tertiary),
        "accent": _hex_to_rgb(t.accent),
        "accent_hover": _hex_to_rgb(t.accent_hover),
        "status_pass": _hex_to_rgb(t.status_pass),
        "status_fail": _hex_to_rgb(t.status_fail),
        "status_warn": _hex_to_rgb(t.status_warn),
        "status_skip": _hex_to_rgb(t.status_skip),
    }


# Status pill fills. White 7-8pt bold labels need at least 4.5:1 contrast;
# the old bright fills (white on #22C55E is 2.3:1, on #F59E0B 2.1:1) were
# hard to read on screen and washed out when printed.
STATUS_COLORS = {
    "PASS": (21, 128, 61),            # #15803D  5.0:1 with white
    "PASS_WITH_WARNINGS": (180, 83, 9),  # #B45309  5.0:1
    "FAIL": (185, 28, 28),            # #B91C1C  6.5:1
    "WARN": (180, 83, 9),
    "SKIP": (75, 85, 99),             # #4B5563  7.6:1
}

STATUS_LABELS = {
    "PASS": "PASS",
    "PASS_WITH_WARNINGS": "WARNING",
    "FAIL": "FAIL",
    "MISMATCH": "FAIL",
    "WARN": "WARN",
    "SKIP": "SKIP",
}

STATUS_COLORS["MISMATCH"] = STATUS_COLORS["FAIL"]


def _result_status(r: QAResult) -> str:
    """Overall status key for a result, tolerating an un-finalized QAResult."""
    status = getattr(r, "status", None)
    if status:
        return status
    if not r.passed:
        return "MISMATCH"
    if r.warnings:
        if getattr(r, "strict", False):
            return "MISMATCH"
        return "PASS_WITH_WARNINGS"
    return "PASS"


def _result_severity_score(r: QAResult) -> float:
    """Severity for report ordering; mirrors the GUI hierarchy."""
    score = getattr(r, "severity_score", None)
    if isinstance(score, (int, float)):
        return float(score)
    try:
        from frameguard.core import result_severity as _rs
        return float(_rs(r)[0])
    except Exception:
        return 0.0


def _status_label(status: str) -> str:
    """Operator-facing wording. Mirrors the GUI so the PDF and the screen agree."""
    return STATUS_LABELS.get(status, str(status))

# Page dimensions (A4 portrait)
# Page geometry (A4 portrait). All table widths are derived from USABLE_W so
# nothing can silently overrun the right margin when a column is retuned.
PAGE_W = 210
MARGIN_L = 14
MARGIN_R = 14
MARGIN_T = 14
MARGIN_B = 18
USABLE_W = PAGE_W - MARGIN_L - MARGIN_R

CELL_PAD = 2.0      # horizontal breathing room inside every table cell
ROW_MIN_H = 7.0     # minimum row height, so single-line rows aren't cramped
LINE_H = 4.0        # leading for wrapped body text


# Unicode fonts already installed with the operating system. File names are
# often not plain Latin ("Café_日本語_proxy.mov"); fpdf2's built-in Helvetica
# is Latin-1 only, so those names printed as "???". Arial is metrically the
# Helvetica the layout was designed around; the fallbacks cover CJK and other
# scripts. Nothing is bundled: when none is found, Helvetica plus the Latin-1
# transliteration in _safe_text is used exactly as before.
def _font_candidates():
    if sys.platform == "darwin":
        sup = "/System/Library/Fonts/Supplemental"
        main = {"": f"{sup}/Arial.ttf", "B": f"{sup}/Arial Bold.ttf",
                "I": f"{sup}/Arial Italic.ttf"}
        fallbacks = [(f"{sup}/Arial Unicode.ttf", 0)]
    elif sys.platform == "win32":
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        main = {"": os.path.join(fonts, "arial.ttf"), "B": os.path.join(fonts, "arialbd.ttf"),
                "I": os.path.join(fonts, "ariali.ttf")}
        fallbacks = [(os.path.join(fonts, name), 0) for name in
                     ("msyh.ttc", "YuGothM.ttc", "malgun.ttf", "Nirmala.ttc", "seguisym.ttf")]
    else:
        base = "/usr/share/fonts/truetype/dejavu"
        main = {"": f"{base}/DejaVuSans.ttf", "B": f"{base}/DejaVuSans-Bold.ttf",
                "I": f"{base}/DejaVuSans-Oblique.ttf"}
        fallbacks = []
    return main, fallbacks


class HandoffReport(FPDF):
    """Styled PDF report with modern design language."""

    def __init__(self, preset_label: str = "", editor: str = "", colors: dict = None,
                 unicode_fonts: bool = True, fallback_fonts: bool = True):
        super().__init__()
        self.ff = (self._register_fonts(fallback_fonts) if unicode_fonts else "Helvetica")
        # Raw text: cell()/multi_cell() adapt it to the font at draw time.
        # Converting here, before any font was set, always took the Latin-1
        # path and turned "日本語 preset" into "??? preset".
        self.preset_label = "" if preset_label is None else str(preset_label)
        self.editor = "" if editor is None else str(editor)
        self.c = colors or _get_report_colors()
        self.set_auto_page_break(auto=False)  # tables manage their own breaks
        self.set_margins(MARGIN_L, MARGIN_T, MARGIN_R)
        self.continuation_label = ""  # set while a file's checks span pages

    def _register_fonts(self, fallback_fonts: bool = True) -> str:
        """Register the Unicode report family; return the family to use.

        Fallback fonts (CJK and other scripts) are only registered when the
        report needs them: each is embedded in the PDF, and opening a large
        font collection costs about half a second.
        """
        main, fallbacks = _font_candidates()
        if not all(os.path.isfile(path) for path in main.values()):
            return "Helvetica"
        try:
            for style, path in main.items():
                self.add_font("FGSans", style, path)
            names = []
            for index, (path, number) in enumerate(fallbacks if fallback_fonts else ()):
                if os.path.isfile(path):
                    name = f"FGFallback{index}"
                    self.add_font(name, "", path, collection_font_number=number)
                    names.append(name)
            if names:
                self.set_fallback_fonts(names, exact_match=False)
        except Exception:
            return "Helvetica"
        return "FGSans"

    @property
    def unicode_fonts(self) -> bool:
        return self.ff != "Helvetica"

    def _text(self, text) -> str:
        """Text the current font can render."""
        if text is None:
            return ""
        text = str(text)
        if self.unicode_fonts and getattr(self, "is_ttf_font", False):
            import unicodedata
            return unicodedata.normalize("NFC", text)
        return _safe_text(text)

    # fpdf2's built-in fonts are latin-1 only and raise on anything outside it.
    # Sanitizing centrally means a stray Unicode character in a filename, codec
    # name, preset label or editor name can never abort a report mid-render.
    def cell(self, *args, **kwargs):
        if "text" in kwargs:
            kwargs["text"] = self._text(kwargs["text"])
        elif len(args) >= 3:
            args = args[:2] + (self._text(args[2]),) + args[3:]
        return super().cell(*args, **kwargs)

    def multi_cell(self, *args, **kwargs):
        if "text" in kwargs:
            kwargs["text"] = self._text(kwargs["text"])
        elif len(args) >= 3:
            args = args[:2] + (self._text(args[2]),) + args[3:]
        return super().multi_cell(*args, **kwargs)

    def header(self):
        # Page background
        self.set_fill_color(*self.c["bg_primary"])
        self.rect(0, 0, self.w, self.h, 'F')
        if self.page_no() == 1:
            return
        # Minimal header on continuation pages. This runs on every page after
        # the first, so an un-encodable character here breaks exactly the
        # multi-file batch reports and nothing else.
        self.set_font(self.ff, "", 8)
        self.set_text_color(*self.c["text_tertiary"])
        self.set_xy(MARGIN_L, MARGIN_T - 4)
        self.cell(0, 6, f"FrameGuard Report   |   {self.preset_label}", align="L")
        self.set_draw_color(*self.c["border"])
        self.set_line_width(0.2)
        y = MARGIN_T + 3
        self.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)
        self.set_xy(MARGIN_L, y + 5)

    def footer(self):
        self.set_y(-13)
        self.set_font(self.ff, "", 8)
        self.set_text_color(*self.c["text_tertiary"])
        self.cell(0, 8, f"Page {self.page_no()} of {{nb}}", align="C")

    # -- measurement --------------------------------------------------------

    @property
    def content_bottom(self) -> float:
        """Last y a row may occupy before it must move to the next page."""
        return self.h - MARGIN_B

    def text_height(self, width: float, text: str, line_h: float = LINE_H) -> float:
        """Exact rendered height of `text` wrapped to `width`.

        Uses fpdf2's own line breaker rather than approximating it, so the
        painted row background always matches the text that lands in it.
        """
        text = self._text(text)
        if not text:
            return line_h
        lines = self.multi_cell(
            width - 2 * CELL_PAD, line_h, text,
            dry_run=True, output="LINES", align="L",
        )
        return max(1, len(lines)) * line_h

    def room_for(self, height: float) -> bool:
        return self.get_y() + height <= self.content_bottom

    # -- drawing primitives -------------------------------------------------

    def pill(self, x, y, w, h, text, fill_rgb, text_rgb=(255, 255, 255), size=8):
        """Rounded status badge with vertically centred label."""
        self.set_fill_color(*fill_rgb)
        self.rect(x, y, w, h, style="F", round_corners=True, corner_radius=min(h / 2, 2.6))
        self.set_text_color(*text_rgb)
        self.set_font(self.ff, "B", size)
        self.set_xy(x, y)
        self.cell(w, h, text, align="C")

    def row_band(self, y, height, fill_rgb):
        """Paint a full-width row background."""
        self.set_fill_color(*fill_rgb)
        self.rect(MARGIN_L, y, USABLE_W, height, style="F")

    def cell_text(self, x, y, w, h, text, *, align="L", pad=CELL_PAD):
        """Single-line cell text, vertically centred, with consistent padding."""
        self.set_xy(x + pad, y)
        self.cell(w - 2 * pad, h, text, align=align)

    def wrapped_text(self, x, y, w, text, line_h=LINE_H, align="L"):
        """Multi-line cell text with the same padding as cell_text."""
        self.set_xy(x + CELL_PAD, y)
        self.multi_cell(w - 2 * CELL_PAD, line_h, text, border=0, align=align)


def _safe_text(text: str) -> str:
    """Sanitize text for fpdf2 - replace unsupported Unicode characters with ASCII equivalents.
    
    fpdf2's built-in fonts (Helvetica, etc.) only support Latin-1.
    This function ensures any Unicode in filenames, codec names, or scan results
    won't crash the PDF generation.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    if not text:
        return ""
    
    # Common Unicode → ASCII replacements
    replacements = {
        "\u2014": "--",   # em-dash
        "\u2013": "-",    # en-dash
        "\u2018": "'",    # left single quote
        "\u2019": "'",    # right single quote
        "\u201C": '"',    # left double quote
        "\u201D": '"',    # right double quote
        "\u2026": "...",  # ellipsis
        "\u2022": "*",    # bullet
        "\u00D7": "x",   # multiplication sign
        "\u2192": "->",   # right arrow
        "\u2190": "<-",   # left arrow
        "\u2713": "[OK]", # checkmark
        "\u2717": "[X]",  # ballot X
        "\u00B0": "deg",  # degree
        "\u00B5": "u",    # micro
        "\u2265": ">=",   # greater than or equal
        "\u2264": "<=",   # less than or equal
        "\u00AE": "(R)",  # registered
        "\u00A9": "(C)",  # copyright
        "\u2122": "(TM)", # trademark
    }
    
    # macOS hands over filenames in decomposed (NFD) form, so "é" arrived as
    # "e" + U+0301 and printed as "e?" in reports. Compose first; then
    # transliterate whatever Latin-1 still cannot hold to its base letters
    # (e.g. "ő" -> "o") before falling back to "?".
    import unicodedata
    text = unicodedata.normalize("NFC", text)
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    if any(ord(ch) > 0xFF for ch in text):
        text = "".join(
            ch if ord(ch) <= 0xFF else (
                "".join(c for c in unicodedata.normalize("NFKD", ch)
                        if not unicodedata.combining(c) and ord(c) <= 0xFF)
                or "?")
            for ch in text)

    # Final safety: encode to latin-1, replacing any remaining unknowns
    try:
        text = text.encode("latin-1", errors="replace").decode("latin-1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        text = text.encode("ascii", errors="replace").decode("ascii")

    return text


class _OutputError(RuntimeError):
    """The finished PDF could not be written to disk."""


_main_cmap_cache = {}


def _main_font_codepoints() -> frozenset:
    """Code points the main report font covers (empty when unavailable)."""
    main, _ = _font_candidates()
    path = main.get("")
    if path in _main_cmap_cache:
        return _main_cmap_cache[path]
    codepoints = frozenset()
    try:
        if path and os.path.isfile(path):
            from fontTools.ttLib import TTFont
            with TTFont(path, lazy=True) as font:
                codepoints = frozenset(font.getBestCmap() or {})
    except Exception:
        codepoints = frozenset()
    _main_cmap_cache[path] = codepoints
    return codepoints


def _needs_fallback_fonts(results, *extra) -> bool:
    """True when some text has characters the main report font lacks.

    Checked against the font's own character map, so FrameGuard's own em
    dashes and arrows do not pull every CJK fallback font into the PDF.
    """
    covered = _main_font_codepoints()

    def _beyond(text) -> bool:
        for ch in str(text or ""):
            code = ord(ch)
            if code > 0x7E and (code not in covered if covered else code > 0x052F):
                if ch.isspace():
                    continue
                return True
        return False
    if any(_beyond(t) for t in extra):
        return True
    for r in results:
        if _beyond(getattr(r, "filename", "")):
            return True
        for check in getattr(r, "checks", ()) or ():
            if isinstance(check, (list, tuple)) and len(check) >= 3 and (
                    _beyond(check[0]) or _beyond(check[2])):
                return True
    return False


def _is_dark(rgb) -> bool:
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128


def _status_text_palette(colors: dict) -> dict:
    """Status colours for text on the page background (not on a pill).

    Dark pill fills are unreadable as text on a dark theme's page, and the
    theme's bright status colours are too light for text on white paper.
    """
    if _is_dark(colors["bg_primary"]):
        fail, warn, ok = colors["status_fail"], colors["status_warn"], colors["status_pass"]
    else:
        fail, warn, ok = STATUS_COLORS["FAIL"], STATUS_COLORS["WARN"], STATUS_COLORS["PASS"]
    return {"MISMATCH": fail, "FAIL": fail, "PASS_WITH_WARNINGS": warn,
            "WARN": warn, "PASS": ok}


# A file's check table never leaves fewer than this many rows at the foot of
# a page (orphans) or carries fewer than this many onto the next (widows).
MIN_ORPHAN_ROWS = 4
MIN_WIDOW_ROWS = 3


def _detail_layout(pdf, r, chk_cols):
    """``(head_height, [row_heights])`` for one file's detailed block."""
    total = _detail_block_height(pdf, r, chk_cols)
    rows = []
    pdf.set_font(pdf.ff, "", 8)
    for check in r.checks:
        if isinstance(check, (list, tuple)) and len(check) >= 3:
            rows.append(max(ROW_MIN_H, pdf.text_height(chk_cols[2], pdf._text(check[2]), 4.0) + 3))
    return total - sum(rows) - 7, rows


def _first_break(available: float, head_h: float, rows) -> object:
    """Where to break a block that starts with ``available`` mm left.

    Returns ``None`` (fits), ``0`` (start it on a new page) or the index of
    the first row to carry onto the next page.
    """
    if head_h + sum(rows) <= available:
        return None
    room = available - head_h
    fit = 0
    for height in rows:
        if room < height:
            break
        room -= height
        fit += 1
    if fit < MIN_ORPHAN_ROWS:
        return 0
    if len(rows) - fit < MIN_WIDOW_ROWS:
        fit = len(rows) - MIN_WIDOW_ROWS
        if fit < MIN_ORPHAN_ROWS:
            return 0
    return fit


def _detail_block_height(pdf, r, chk_cols) -> float:
    """Height of one file's detailed block, measured with the real fonts."""
    height = 5 + 2
    pdf.set_font(pdf.ff, "B", 8)
    pill_w = pdf.get_string_width(_status_label(_result_status(r))) + 8
    pdf.set_font(pdf.ff, "B", 11)
    height += max(6, pdf.text_height(USABLE_W - pill_w - 4 + 2 * CELL_PAD,
                                     pdf._text(r.filename), 5.2))
    height += 6  # metadata line
    summary = r.plain_summary() if hasattr(r, "plain_summary") else ""
    if summary and summary != "All checks passed.":
        pdf.set_font(pdf.ff, "I", 9)
        height += pdf.text_height(USABLE_W, summary, 4.4) + 2
    height += 1 + 6.5  # table header
    pdf.set_font(pdf.ff, "", 8)
    for check in r.checks:
        if isinstance(check, (list, tuple)) and len(check) >= 3:
            height += max(ROW_MIN_H, pdf.text_height(chk_cols[2], pdf._text(check[2]), 4.0) + 3)
    return height + 7


def generate_report(results: List[QAResult], output_path: str,
                    preset_label: str = "Custom", editor: str = "",
                    colors: dict = None) -> str:
    """Generate a styled PDF handoff report.

    Returns the output file path. Rendered with the system's Unicode fonts
    when available; if anything in that path fails (a damaged font file, a
    font feature fpdf2 cannot subset) the report is rebuilt with the built-in
    Latin-1 font rather than not being produced at all.
    """
    try:
        return _generate_report(results, output_path, preset_label, editor, colors, True)
    except _OutputError:
        raise  # the file could not be written; another font will not help
    except ValueError as error:
        if "no scan results" in str(error):
            raise
        return _generate_report(results, output_path, preset_label, editor, colors, False)
    except Exception:
        return _generate_report(results, output_path, preset_label, editor, colors, False)


def _generate_report(results, output_path, preset_label, editor, colors, unicode_fonts):
    if not results:
        raise ValueError("Cannot generate a report with no scan results.")
    preset_label = preset_label or "Custom"
    # Mirror the GUI hierarchy: failures first, then severity, so the most
    # urgent file (corruption) leads the report, not just any failure.
    _order = {"MISMATCH": 0, "PASS_WITH_WARNINGS": 1, "PASS": 2}
    try:
        results = sorted(
            results,
            key=lambda r: (_order.get(_result_status(r), 3),
                           -_result_severity_score(r),
                           (getattr(r, "filename", "") or "").lower()))
    except Exception:
        pass
    # Background runners pass an immutable-at-creation palette so a theme
    # switch cannot change colors halfway through a long report.
    colors = dict(colors) if colors is not None else _get_report_colors()
    pdf = HandoffReport(preset_label=preset_label, editor=editor, colors=colors,
                        unicode_fonts=unicode_fonts,
                        fallback_fonts=_needs_fallback_fonts(results, preset_label, editor))
    pdf.alias_nb_pages()
    # Document properties: what a PDF viewer, search index or DMS shows.
    pdf.set_title(f"FrameGuard Report - {pdf.preset_label}")
    pdf.set_author(pdf.editor or "FrameGuard Desktop")
    pdf.set_subject(f"Video QA results for {len(results)} file(s)")
    pdf.set_keywords("FrameGuard, video QA, delivery, report")
    pdf.set_creator(f"FrameGuard Desktop v{__version__}")
    pdf.set_lang("en")
    pdf.add_page()
    status_text = _status_text_palette(colors)

    c = colors  # shorthand

    # ---- Title block ----
    pdf.set_xy(MARGIN_L, MARGIN_T)
    pdf.set_font(pdf.ff, "B", 24)
    pdf.set_text_color(*c["accent"])
    pdf.cell(0, 11, "FrameGuard Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font(pdf.ff, "", 10)
    pdf.set_text_color(*c["text_secondary"])
    now = datetime.now().astimezone().strftime("%B %d, %Y at %H:%M %Z").strip()
    pdf.cell(0, 5.5, f"{now}   |   Profile: {preset_label}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if editor:
        pdf.cell(0, 5.5, f"Editor: {editor}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(4)
    pdf.set_draw_color(*c["accent"])
    pdf.set_line_width(0.6)
    pdf.line(MARGIN_L, pdf.get_y(), PAGE_W - MARGIN_R, pdf.get_y())
    pdf.ln(7)

    # ---- Summary badges ----
    total = len(results)
    passed = sum(1 for r in results if _result_status(r) == "PASS")
    mismatched = sum(1 for r in results if _result_status(r) == "MISMATCH")
    warned = sum(1 for r in results if _result_status(r) == "PASS_WITH_WARNINGS")

    badges = [
        ("Total", total, c["accent"]),
        ("Passed", passed, STATUS_COLORS["PASS"]),
        ("Failed", mismatched, STATUS_COLORS["FAIL"]),
        ("Warnings", warned, STATUS_COLORS["WARN"]),
    ]
    badge_gap = 4.0
    badge_w = (USABLE_W - badge_gap * (len(badges) - 1)) / len(badges)
    badge_h = 11.0
    badge_y = pdf.get_y()
    for i, (label, count, color) in enumerate(badges):
        pdf.pill(MARGIN_L + i * (badge_w + badge_gap), badge_y, badge_w, badge_h,
                 f"{count}  {label}", color, size=10)
    pdf.set_y(badge_y + badge_h + 10)

    # ---- Batch summary table ----
    # Widths are proportions of the usable width, so the table always ends
    # exactly on the right margin no matter how the page geometry changes.
    # Filename gets the most room: production names are long and truncating
    # them is what makes a report hard to act on.
    sum_cols = [w * USABLE_W for w in (0.36, 0.09, 0.11, 0.12, 0.32)]
    sum_headers = ["Filename", "Codec", "Resolution", "Result", "Summary"]

    def _summary_header():
        pdf.set_font(pdf.ff, "B", 8)
        pdf.set_fill_color(*c["accent"])
        pdf.rect(MARGIN_L, pdf.get_y(), USABLE_W, 7.5, style="F",
                 round_corners=("TOP_LEFT", "TOP_RIGHT"), corner_radius=2)
        pdf.set_text_color(255, 255, 255)
        x, y = MARGIN_L, pdf.get_y()
        for i, h in enumerate(sum_headers):
            align = "C" if sum_headers[i] == "Result" else "L"
            pdf.cell_text(x, y, sum_cols[i], 7.5, h, align=align)
            x += sum_cols[i]
        pdf.set_xy(MARGIN_L, y + 7.5)

    pdf.set_font(pdf.ff, "B", 13)
    pdf.set_text_color(*c["text_primary"])
    pdf.cell(0, 7, "Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    _summary_header()

    for idx, r in enumerate(results):
        summary = pdf._text(r.plain_summary() if hasattr(r, "plain_summary") else "")

        pdf.set_font(pdf.ff, "", 7.5)
        summary_h = pdf.text_height(sum_cols[4], summary, 3.6)
        # File names wrap rather than being cut to "Extremely_Long_...":
        # the full production name is what an operator searches for.
        fname = pdf._text(r.filename)
        pdf.set_font(pdf.ff, "", 8)
        name_h = pdf.text_height(sum_cols[0], fname, 3.8)
        row_h = max(ROW_MIN_H, summary_h + 3, name_h + 3)

        if not pdf.room_for(row_h):
            pdf.add_page()
            _summary_header()

        y = pdf.get_y()
        pdf.row_band(y, row_h, c["bg_secondary"] if idx % 2 == 0 else c["bg_primary"])

        metadata = r.metadata or {}
        x = MARGIN_L
        pdf.set_font(pdf.ff, "", 8)
        pdf.set_text_color(*c["text_primary"])
        pdf.wrapped_text(x, y + (row_h - name_h) / 2, sum_cols[0], fname, line_h=3.8)
        x += sum_cols[0]

        pdf.set_text_color(*c["text_secondary"])
        pdf.cell_text(x, y, sum_cols[1], row_h, pdf._text(metadata.get("video_codec", "-")))
        x += sum_cols[1]
        pdf.cell_text(x, y, sum_cols[2], row_h, pdf._text(metadata.get("resolution", "-")))
        x += sum_cols[2]

        status = _result_status(r)
        pill_h = 5.6
        pdf.pill(x + CELL_PAD, y + (row_h - pill_h) / 2, sum_cols[3] - 2 * CELL_PAD, pill_h,
                 _status_label(status), STATUS_COLORS.get(status, c["text_secondary"]), size=7)
        x += sum_cols[3]

        pdf.set_font(pdf.ff, "", 7.5)
        pdf.set_text_color(*c["text_secondary"])
        pdf.wrapped_text(x, y + (row_h - summary_h) / 2, sum_cols[4], summary, line_h=3.6)
        pdf.set_xy(MARGIN_L, y + row_h)

    pdf.ln(10)

    # ---- Effective spec and tool versions (audit trail) ----
    _draw_spec_table(pdf, results, c)

    # ---- Detailed results ----
    chk_cols = [w * USABLE_W for w in (0.13, 0.24, 0.63)]
    chk_headers = ["Status", "Check", "Detail"]

    def _check_header(continued: str = ""):
        if continued:
            # A file's checks resumed on a new page: say whose they are.
            pdf.set_font(pdf.ff, "B", 9)
            pdf.set_text_color(*c["text_secondary"])
            pdf.set_x(MARGIN_L)
            pdf.multi_cell(USABLE_W, 4.6, f"{continued} (continued)",
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1.5)
        pdf.set_font(pdf.ff, "B", 8)
        pdf.set_fill_color(*c["accent"])
        pdf.rect(MARGIN_L, pdf.get_y(), USABLE_W, 6.5, style="F",
                 round_corners=("TOP_LEFT", "TOP_RIGHT"), corner_radius=2)
        pdf.set_text_color(255, 255, 255)
        x, y = MARGIN_L, pdf.get_y()
        for i, h in enumerate(chk_headers):
            pdf.cell_text(x, y, chk_cols[i], 6.5, h, align="C" if i == 0 else "L")
            x += chk_cols[i]
        pdf.set_xy(MARGIN_L, y + 6.5)

    # The section heading always lands with its first file block.
    first_head, first_rows = _detail_layout(pdf, results[0], chk_cols)
    if _first_break(pdf.content_bottom - pdf.get_y() - 10, first_head, first_rows) == 0:
        pdf.add_page()
    pdf.set_font(pdf.ff, "B", 13)
    pdf.set_text_color(*c["text_primary"])
    pdf.cell(0, 7, "Detailed Results", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    for r in results:
        overall = _result_status(r)
        overall_color = STATUS_COLORS.get(overall, c["text_secondary"])
        overall_text = status_text.get(overall, c["text_secondary"])

        # Widow/orphan control: a block that has to split leaves at least
        # MIN_ORPHAN_ROWS check rows on this page and carries at least
        # MIN_WIDOW_ROWS to the next, under a "(continued)" heading. A page
        # used to open with one or two stray rows of the previous file.
        head_h, row_heights = _detail_layout(pdf, r, chk_cols)
        break_at = _first_break(pdf.content_bottom - pdf.get_y(), head_h, row_heights)
        if break_at == 0:
            pdf.add_page()
            break_at = _first_break(pdf.content_bottom - pdf.get_y(), head_h, row_heights)
            if break_at == 0:
                break_at = None  # taller than a page: split wherever rows run out

        y = pdf.get_y()
        pdf.set_draw_color(*c["border"])
        pdf.set_line_width(0.2)
        pdf.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)
        y += 5

        label_text = _status_label(overall)
        pdf.set_font(pdf.ff, "B", 8)
        pill_w = pdf.get_string_width(label_text) + 8
        pdf.pill(MARGIN_L, y, pill_w, 6, label_text, overall_color, size=8)

        pdf.set_font(pdf.ff, "B", 11)
        pdf.set_text_color(*c["text_primary"])
        name_w = USABLE_W - pill_w - 4
        name = pdf._text(r.filename)
        # Wrapped: a long name used to run straight off the right edge.
        name_h = max(6, pdf.text_height(name_w + 2 * CELL_PAD, name, 5.2))
        pdf.set_xy(MARGIN_L + pill_w + 4, y + (0.4 if name_h > 6 else 0))
        pdf.multi_cell(name_w, 5.2 if name_h > 6 else 6, name)
        y += name_h + 2

        metadata = r.metadata or {}
        meta_parts = []
        if metadata.get("resolution"):
            meta_parts.append(str(metadata["resolution"]))
        if metadata.get("video_codec"):
            meta_parts.append(str(metadata["video_codec"]).upper())
        if isinstance(metadata.get("frame_rate"), (int, float)):
            meta_parts.append(f"{metadata['frame_rate']:.2f} fps")
        if metadata.get("duration_display"):
            meta_parts.append(str(metadata["duration_display"]))
        if isinstance(metadata.get("file_size_mb"), (int, float)):
            from frameguard.core import _format_size
            meta_parts.append(_format_size(float(metadata["file_size_mb"])))
        if meta_parts:
            pdf.set_font(pdf.ff, "", 8)
            pdf.set_text_color(*c["text_tertiary"])
            pdf.set_xy(MARGIN_L, y)
            pdf.cell(USABLE_W, 4.5, pdf._text("   |   ".join(meta_parts)))
            y += 6

        summary = pdf._text(r.plain_summary()) if hasattr(r, "plain_summary") else ""
        if summary and summary != "All checks passed.":
            pdf.set_font(pdf.ff, "I", 9)
            pdf.set_text_color(*overall_text)
            h = pdf.text_height(USABLE_W, summary, 4.4)
            # Same left edge as the metadata line above it.
            pdf.set_xy(MARGIN_L, y)
            pdf.multi_cell(USABLE_W, 4.4, summary)
            y += h + 2

        pdf.set_xy(MARGIN_L, y + 1)
        _check_header()

        row_index = -1
        for check in r.checks:
            if not isinstance(check, (list, tuple)) or len(check) < 3:
                continue
            row_index += 1
            check_name, status, detail = pdf._text(check[0]), check[1], pdf._text(check[2])

            pdf.set_font(pdf.ff, "", 8)
            row_h = max(ROW_MIN_H, pdf.text_height(chk_cols[2], detail, 4.0) + 3)

            if row_index == break_at or not pdf.room_for(row_h):
                pdf.add_page()
                _check_header(continued=pdf._text(r.filename))

            y = pdf.get_y()
            pdf.row_band(y, row_h, c["bg_secondary"])

            pill_h = 5.2
            pdf.pill(MARGIN_L + CELL_PAD, y + (row_h - pill_h) / 2,
                     chk_cols[0] - 2 * CELL_PAD, pill_h, _status_label(status),
                     STATUS_COLORS.get(status, c["text_secondary"]), size=7)

            x = MARGIN_L + chk_cols[0]
            pdf.set_font(pdf.ff, "B", 8)
            pdf.set_text_color(*c["text_primary"])
            pdf.cell_text(x, y, chk_cols[1], row_h, check_name)
            x += chk_cols[1]

            pdf.set_font(pdf.ff, "", 8)
            pdf.set_text_color(*c["text_secondary"])
            pdf.wrapped_text(x, y + 2, chk_cols[2], detail, line_h=4.0)
            pdf.set_xy(MARGIN_L, y + row_h)

        pdf.ln(7)

    # ---- Closing note ----
    if not pdf.room_for(14):
        pdf.add_page()
    pdf.ln(4)
    pdf.set_draw_color(*c["border"])
    pdf.set_line_width(0.2)
    pdf.line(MARGIN_L, pdf.get_y(), PAGE_W - MARGIN_R, pdf.get_y())
    pdf.ln(3)
    pdf.set_font(pdf.ff, "I", 8)
    pdf.set_text_color(*c["text_tertiary"])
    pdf.set_x(MARGIN_L)
    pdf.cell(
        0, 5,
        f"FrameGuard Desktop v{__version__}   |   Published by Capri la Ria   |   Developed by Cassian Aléria   |   "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}   |   Processed locally",
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
    )

    # Save
    try:
        output_path = _resolve_output_path(output_path)
    except OSError as e:
        raise _OutputError(f"Failed to write PDF to '{output_path}': {e}") from e
    # Render first (font subsetting happens here), then write atomically: a
    # report interrupted mid-write never replaces a good one, and a reader
    # never sees a half-written file.
    data = bytes(pdf.output())
    try:
        _atomic_write_bytes(output_path, data)
    except OSError as e:
        raise _OutputError(f"Failed to write PDF to '{output_path}': {e}") from e
    return output_path


def _resolve_output_path(output_path: str) -> str:
    """Where a report file really goes: a bare file name lands in
    ~/Documents/FrameGuard. Creates the directory; raises OSError."""
    if not os.path.dirname(output_path):
        docs_dir = os.path.join(os.path.expanduser("~"), "Documents", "FrameGuard")
        os.makedirs(docs_dir, exist_ok=True)
        return os.path.join(docs_dir, os.path.basename(output_path))
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    return output_path


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write via ``<path>.part`` + rename: a reader never sees a half file and
    an interrupted export never replaces a good one. Raises OSError."""
    partial = f"{path}.part"
    try:
        with open(partial, "wb") as handle:
            handle.write(data)
        os.replace(partial, path)
    except OSError:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def get_default_output_dir() -> str:
    """Get the default output directory for PDF reports.
    Creates it if it doesn't exist.
    Returns: Path to ~/Documents/FrameGuard/
    """
    docs_dir = os.path.join(os.path.expanduser("~"), "Documents", "FrameGuard")
    os.makedirs(docs_dir, exist_ok=True)
    return docs_dir


# ============================================================================
# MACHINE-READABLE EXPORTS (JSON / CSV)
# ============================================================================
# The PDF is for people; these are for audit and automation. Each records
# every check, the metadata, the exact settings and the tool versions that
# produced the verdict, so a result can be reproduced or challenged later.

EXPORT_SCHEMA = "frameguard.results/1"


def _json_safe(value):
    """Recursively convert sets/tuples/odd scalars into JSON-native values."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, float) and value != value:  # NaN
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _tool_versions_only(tools: dict) -> dict:
    """``{"ffmpeg": "ffmpeg version 9.0.2 ..."}`` without install paths.

    An install path names the machine and, on Windows, the user
    (C:\\Users\\<name>\\AppData\\...). It adds nothing to reproducing a verdict
    that the version string does not already say. Media paths are kept:
    operators need them to find the file.
    """
    versions = {}
    for name, info in tools.items():
        version = info.get("version") if isinstance(info, dict) else info
        versions[str(name)] = version if isinstance(version, str) else None
    return versions


def _result_record(r: QAResult) -> dict:
    checks = []
    for check in getattr(r, "checks", ()) or ():
        if not isinstance(check, (list, tuple)) or len(check) < 2:
            continue
        checks.append({
            "name": check[0],
            "status": check[1],
            "detail": check[2] if len(check) > 2 else "",
            "confidence": check[3] if len(check) > 3 else 100,
        })
    status = _result_status(r)
    return _json_safe({
        "file": os.path.abspath(r.filepath) if getattr(r, "filepath", "") else "",
        "filename": getattr(r, "filename", ""),
        "status": status,
        "status_label": _status_label(status),
        "strict": bool(getattr(r, "strict", False)),
        "severity_score": _result_severity_score(r),
        "summary": r.plain_summary() if hasattr(r, "plain_summary") else "",
        "scan_started": getattr(r, "started_at", None),
        "scan_finished": getattr(r, "finished_at", None),
        "frameguard_version": getattr(r, "engine_version", __version__),
        "tool_versions": _tool_versions_only(getattr(r, "tool_versions", {}) or {}),
        "config": getattr(r, "config", {}) or {},
        "checks": checks,
        "metadata": getattr(r, "metadata", {}) or {},
    })


def build_export_document(results: List[QAResult], preset_label: str = "") -> dict:
    """The JSON export as a dict (also the source for the CSV rows)."""
    if not results:
        raise ValueError("Cannot export with no scan results.")
    return {
        "schema": EXPORT_SCHEMA,
        "frameguard_version": __version__,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "profile": preset_label or "Custom",
        "file_count": len(results),
        "results": [_result_record(r) for r in results],
    }


def export_results_json(results: List[QAResult], output_path: str,
                        preset_label: str = "") -> str:
    """Write the JSON export atomically; returns the path."""
    import json
    document = build_export_document(results, preset_label)
    data = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write_bytes(output_path, data)
    return output_path


CSV_COLUMNS = (
    "generated_at", "frameguard_version", "profile", "file", "filename",
    "overall_status", "scan_started", "scan_finished", "ffmpeg_version",
    "ffprobe_version", "record_type", "name", "status", "confidence", "value",
)


def _csv_rows(document: dict):
    """Long format: one row per check, per metadata item, per config key."""
    for record in document["results"]:
        tools = record.get("tool_versions") or {}
        base = {
            "generated_at": document["generated_at"],
            "frameguard_version": record.get("frameguard_version") or document["frameguard_version"],
            "profile": document["profile"],
            "file": record["file"],
            "filename": record["filename"],
            "overall_status": record["status_label"],
            "scan_started": record.get("scan_started") or "",
            "scan_finished": record.get("scan_finished") or "",
            "ffmpeg_version": tools.get("ffmpeg") or "",
            "ffprobe_version": tools.get("ffprobe") or "",
        }
        for check in record["checks"]:
            yield {**base, "record_type": "check", "name": check["name"],
                   "status": check["status"], "confidence": check["confidence"],
                   "value": check["detail"]}
        for key, value in record["metadata"].items():
            yield {**base, "record_type": "metadata", "name": key, "status": "",
                   "confidence": "", "value": _csv_value(value)}
        for key, value in record["config"].items():
            yield {**base, "record_type": "config", "name": key, "status": "",
                   "confidence": "", "value": _csv_value(value)}


def _csv_value(value) -> str:
    import json
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def export_results_csv(results: List[QAResult], output_path: str,
                       preset_label: str = "") -> str:
    """Write the CSV export atomically (UTF-8 with BOM so Excel reads it); returns the path."""
    import csv
    import io
    document = build_export_document(results, preset_label)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _csv_rows(document):
        writer.writerow(row)
    _atomic_write_bytes(output_path, buffer.getvalue().encode("utf-8-sig"))
    return output_path


def write_data_exports(results: List[QAResult], pdf_path: str,
                       preset_label: str = "") -> tuple:
    """Write ``<report>.json`` and ``<report>.csv`` next to a PDF report.

    Resolves the path exactly as generate_report does, so the data files
    always sit beside the PDF (a bare name means ~/Documents/FrameGuard).
    """
    stem = os.path.splitext(_resolve_output_path(pdf_path))[0]
    json_path = export_results_json(results, stem + ".json", preset_label)
    csv_path = export_results_csv(results, stem + ".csv", preset_label)
    return json_path, csv_path


# ============================================================================
# PDF: SPEC / THRESHOLDS TABLE
# ============================================================================

def _fmt_choice(value) -> str:
    return "not enforced" if value in (None, "any") else str(value)


def _loudness_spec(c: dict) -> str:
    mode = c.get("loudness_mode", "report")
    if mode == "off":
        return "not measured"
    if mode == "report":
        return "measured and reported, not enforced"
    profile = audio_checks.resolve_profile(c)
    text = (f"{mode}: {profile.label} {profile.target:g} ± {profile.tolerance:g} LUFS, "
            f"TP ≤ {profile.true_peak_max:g} dBTP")
    if profile.dialogue_gated:
        judged = ("enforced on programme loudness" if c.get("dialogue_gated_loudness") == "enforce"
                  else "programme loudness, warn only")
        text += f" (dialogue-gated target: {judged})"
    return text


def _audio_signal_spec(c: dict) -> str:
    """The audio signal checks and their spec decisions (audio_checks)."""
    def on(key):
        return c.get(key, True)
    parts = []
    if on("audio_phase_check"):
        parts.append(f"phase below {float(c.get('phase_correlation_min', -0.2)):g} correlation")
    if on("audio_dual_mono_check"):
        parts.append("dual mono allowed" if c.get("dual_mono_allowed") else "dual mono warns")
    off = [label for key, label in (("audio_clipping_check", "clipping"), ("audio_dropout_check", "dropouts"),
                                    ("audio_phase_check", "phase"), ("audio_dual_mono_check", "dual mono"),
                                    ("audio_dc_offset_check", "DC offset"), ("audio_hum_check", "hum"))
           if not on(key)]
    if off:
        parts.append("off: " + ", ".join(off))
    return "; ".join(parts)


def _conformance_spec_rows(c: dict) -> List[tuple]:
    """Spec rows for the 0.9.1 container / XR / conformance keys (container_checks.py)."""
    def listed(key):
        values = c.get(key) or ["any"]
        return "any" if "any" in [str(v).lower() for v in values] else ", ".join(map(str, values))
    rows = [
        ("A/V start offset", f"warn beyond ±{c.get('av_offset_warn_ms', 5.0):g} ms; fail beyond "
                             f"{c.get('av_offset_max_early_ms', 40.0):g} ms early / {c.get('av_offset_max_late_ms', 60.0):g} ms late; "
                             + ("unsignalled AAC priming judged over 0-2112 samples"
                                if str(c.get('aac_priming', 'unknown')) == 'unknown'
                                else f"unsignalled AAC priming {c.get('aac_priming')} samples")),
        ("A/V end", f"audio ≤{c.get('av_short_tolerance_frames', 2)} frames short, ≤{c.get('av_long_tolerance_s', 2.0):g} s long"),
        ("Profiles / pixel formats", f"{listed('allowed_video_profiles')} / {listed('allowed_pix_fmts')}"),
    ]
    if c.get("max_frame_pixels") or c.get("max_pixel_rate"):
        rows.append(("Decoder ceiling", f"{int(c.get('max_frame_pixels') or 0):,} px/frame, "
                                        f"{int(c.get('max_pixel_rate') or 0):,} px/s (0 = none)"))
    flags = [label for key, label in (("require_square_pixels", "square pixels"),
                                      ("require_faststart", "fast-start"),
                                      ("allow_ntsc_rate_match", "NTSC rates match integer rates"))
             if c.get(key)]
    if str(c.get("spatial_audio", "any")) == "required":
        flags.append("spatial audio required")
    if flags:
        rows.append(("Also required", ", ".join(flags)))
    if c.get("disabled_checks"):
        rows.append(("Checks switched off", ", ".join(
            f"{name} (essential: not verified)" if name in ESSENTIAL_CHECKS else name
            for name in c["disabled_checks"])))
    return rows


def _black_freeze_spec(c: dict) -> str:
    black = (f"black ≥{c.get('black_detect_duration')} s at ≤{c.get('black_detect_pix_threshold')} "
             f"level on ≥{float(c.get('black_detect_picture_threshold') or 0) * 100:g}% of the picture"
             if c.get("black_frames_enabled", True) else "black off")
    freeze = (f"freeze ≥{c.get('freeze_detect_duration')} s"
              if c.get("freeze_frames_enabled", True) else "freeze off")
    return f"{black}; {freeze}"


def _picture_signal_spec(c: dict) -> str:
    mode = c.get("signal_levels_mode", "advisory")
    levels = "EBU R 103 levels " + ("off" if mode == "off" else
                                    "measured, not enforced" if mode == "report" else mode)
    flash = "BT.1702 flash screening " + ("on" if c.get("flash_check_enabled", True) else "off")
    interlace = "interlace detection " + ("on" if c.get("interlace_check_enabled", True) else "off")
    return f"{levels}; {flash}; {interlace}"


def spec_rows(config: dict) -> List[tuple]:
    """(label, value) pairs summarising the effective spec for the PDF."""
    if not config:
        return []
    c = config
    rows = [
        ("Resolution", f"{c.get('target_width')}x{c.get('target_height')}"
                       + (" or larger (same shape)" if c.get("allow_larger_resolution") else " exactly")),
        ("Frame rate", f"{c.get('target_fps')} ± {c.get('fps_tolerance')} fps"
                       + (", multiples allowed" if c.get("allow_fps_multiples") else "")),
        ("Min video bitrate", f"{c.get('min_bitrate_mbps')} Mbps"),
        ("Video codecs", ", ".join(c.get("accepted_codecs") or [])),
        ("Containers", ", ".join(c.get("accepted_containers") or [])),
        ("Audio codecs", ", ".join(c.get("accepted_audio_codecs") or [])),
        ("Audio", ("required" if c.get("audio_required") else "optional")
                  + f", min {c.get('min_audio_sample_rate')} Hz"),
        ("Frame timing", ("constant required" if c.get("require_cfr") else "variable allowed")
                         + f" (>{float(c.get('vfr_threshold') or 0) * 100:g}% irregular = VFR)"),
        ("Scan type", "progressive required" if c.get("require_progressive") else "interlaced allowed"),
        ("Bit depth / transfer", f"{_fmt_choice(c.get('min_bit_depth'))} / {_fmt_choice(c.get('required_transfer'))}"),
        ("Projection / stereo", f"{_fmt_choice(c.get('required_projection'))} / {_fmt_choice(c.get('required_stereo_mode'))}"),
        *_conformance_spec_rows(c),
        ("Decode errors allowed", str(c.get("max_decode_errors"))),
        ("Black / freeze", _black_freeze_spec(c)),
        ("Picture signal", _picture_signal_spec(c)),
        ("Silence", f"≤{c.get('silence_noise_db')} dB for ≥{c.get('silence_duration')} s; "
                    f"dead at {float(c.get('dead_channel_ratio') or 0) * 100:g}%"),
        ("Loudness", _loudness_spec(c)),
        ("Audio checks", _audio_signal_spec(c)),
    ]
    return [(label, value) for label, value in rows if value not in (None, "")]


def _tools_line(results) -> str:
    for r in results:
        tools = getattr(r, "tool_versions", None) or {}
        if tools:
            parts = [f"FrameGuard {getattr(r, 'engine_version', __version__)}"]
            for name in ("ffmpeg", "ffprobe"):
                version = (tools.get(name) or {}).get("version")
                if version:
                    parts.append(version.split(" Copyright")[0])
            if any((getattr(x, "metadata", None) or {}).get("ffmpeg_below_minimum") for x in results):
                from frameguard.tools import FFMPEG_MIN_VERSION, version_text
                parts.append(f"below the {version_text(FFMPEG_MIN_VERSION)} minimum: frame-level checks "
                             f"not verified")
            return "   |   ".join(parts)
    return f"FrameGuard {__version__}"


def _draw_spec_table(pdf, results, colors) -> None:
    """Compact two-column key/value table of the effective spec + tool versions."""
    configs = [getattr(r, "config", None) or {} for r in results]
    config = next((c for c in configs if c), {})
    rows = spec_rows(config)
    if not rows:
        return
    distinct = {repr(sorted(c.items())) for c in configs if c}
    label_w, value_w = USABLE_W * 0.26, USABLE_W * 0.74
    pdf.set_font(pdf.ff, "", 7.5)
    heights = [max(5.0, pdf.text_height(value_w, pdf._text(v), 3.6) + 1.6) for _, v in rows]
    needed = 7 + 2 + sum(heights) + 12
    if not pdf.room_for(min(needed, 80)):
        pdf.add_page()
    pdf.set_font(pdf.ff, "B", 13)
    pdf.set_text_color(*colors["text_primary"])
    pdf.cell(0, 7, "Scan Specification", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    for index, ((label, value), height) in enumerate(zip(rows, heights)):
        if not pdf.room_for(height):
            pdf.add_page()
        y = pdf.get_y()
        pdf.row_band(y, height, colors["bg_secondary"] if index % 2 == 0 else colors["bg_primary"])
        pdf.set_font(pdf.ff, "B", 7.5)
        pdf.set_text_color(*colors["text_primary"])
        pdf.cell_text(MARGIN_L, y, label_w, height, pdf._text(label))
        pdf.set_font(pdf.ff, "", 7.5)
        pdf.set_text_color(*colors["text_secondary"])
        pdf.wrapped_text(MARGIN_L + label_w, y + 0.8, value_w, pdf._text(value), line_h=3.6)
        pdf.set_xy(MARGIN_L, y + height)
    pdf.ln(1.5)
    pdf.set_font(pdf.ff, "I", 7.5)
    pdf.set_text_color(*colors["text_tertiary"])
    note = "Tools: " + _tools_line(results)
    if len(distinct) > 1:
        note += f"   |   {len(distinct)} different configurations in this batch; see the JSON export"
    pdf.set_x(MARGIN_L)
    pdf.multi_cell(USABLE_W, 3.8, pdf._text(note), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)
