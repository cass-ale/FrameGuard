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
from datetime import datetime
from typing import List
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from frameguard import __version__
from frameguard.core import QAResult
from frameguard.theme import get_theme, ThemeColors


def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert hex color to RGB tuple."""
    h = hex_color.lstrip('#')
    # Handle 8-char hex (with alpha) — take first 6
    if len(h) == 8:
        h = h[:6]
    if len(h) != 6:
        return (128, 128, 128)
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


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


# Backward-compatible aliases (fallback if theme import fails)
STATUS_COLORS = {
    "PASS": (34, 197, 94),
    "PASS_WITH_WARNINGS": (245, 158, 11),
    "FAIL": (239, 68, 68),
    "WARN": (245, 158, 11),
    "SKIP": (107, 114, 128),
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


class HandoffReport(FPDF):
    """Styled PDF report with modern design language."""

    def __init__(self, preset_label: str = "", editor: str = "", colors: dict = None):
        super().__init__()
        self.preset_label = _safe_text(preset_label)
        self.editor = _safe_text(editor)
        self.c = colors or _get_report_colors()
        self.set_auto_page_break(auto=False)  # tables manage their own breaks
        self.set_margins(MARGIN_L, MARGIN_T, MARGIN_R)

    # fpdf2's built-in fonts are latin-1 only and raise on anything outside it.
    # Sanitizing centrally means a stray Unicode character in a filename, codec
    # name, preset label or editor name can never abort a report mid-render.
    def cell(self, *args, **kwargs):
        if "text" in kwargs:
            kwargs["text"] = _safe_text(kwargs["text"])
        elif len(args) >= 3:
            args = args[:2] + (_safe_text(args[2]),) + args[3:]
        return super().cell(*args, **kwargs)

    def multi_cell(self, *args, **kwargs):
        if "text" in kwargs:
            kwargs["text"] = _safe_text(kwargs["text"])
        elif len(args) >= 3:
            args = args[:2] + (_safe_text(args[2]),) + args[3:]
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
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*self.c["text_tertiary"])
        self.set_xy(MARGIN_L, MARGIN_T - 4)
        self.cell(0, 6, f"FrameGuard Report   |   {self.preset_label}   |   DSS-2", align="L")
        self.set_draw_color(*self.c["border"])
        self.set_line_width(0.2)
        y = MARGIN_T + 3
        self.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)
        self.set_xy(MARGIN_L, y + 5)

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*self.c["text_tertiary"])
        self.cell(0, 8, f"Page {self.page_no()}/{{nb}}", align="C")

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
        text = _safe_text(text)
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
        self.set_font("Helvetica", "B", size)
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
    
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    
    # Final safety: encode to latin-1, replacing any remaining unknowns
    try:
        text = text.encode("latin-1", errors="replace").decode("latin-1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        text = text.encode("ascii", errors="replace").decode("ascii")

    return text


def generate_report(results: List[QAResult], output_path: str,
                    preset_label: str = "Custom", editor: str = "") -> str:
    """Generate a styled PDF handoff report.

    Returns the output file path.
    """
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
    colors = _get_report_colors()
    pdf = HandoffReport(preset_label=preset_label, editor=editor, colors=colors)
    pdf.alias_nb_pages()
    pdf.add_page()

    c = colors  # shorthand

    # ---- Title block ----
    pdf.set_xy(MARGIN_L, MARGIN_T)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(*c["accent"])
    pdf.cell(0, 11, "FrameGuard Report", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*c["text_secondary"])
    now = datetime.now().strftime("%B %d, %Y at %H:%M")
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
        ("Passed", passed, c["status_pass"]),
        ("Failed", mismatched, c["status_fail"]),
        ("Warnings", warned, c["status_warn"]),
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
        pdf.set_font("Helvetica", "B", 8)
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

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*c["text_primary"])
    pdf.cell(0, 7, "Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    _summary_header()

    for idx, r in enumerate(results):
        summary = _safe_text(r.plain_summary() if hasattr(r, "plain_summary") else "")

        pdf.set_font("Helvetica", "", 7.5)
        row_h = max(ROW_MIN_H, pdf.text_height(sum_cols[4], summary, 3.6) + 3)

        if not pdf.room_for(row_h):
            pdf.add_page()
            _summary_header()

        y = pdf.get_y()
        pdf.row_band(y, row_h, c["bg_secondary"] if idx % 2 == 0 else c["bg_primary"])

        metadata = r.metadata or {}
        fname = _safe_text(r.filename)
        # Ellipsize to the actual column width rather than a fixed char count.
        max_w = sum_cols[0] - 2 * CELL_PAD
        if pdf.get_string_width(fname) > max_w:
            while fname and pdf.get_string_width(fname + "...") > max_w:
                fname = fname[:-1]
            fname += "..."

        x = MARGIN_L
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*c["text_primary"])
        pdf.cell_text(x, y, sum_cols[0], row_h, fname)
        x += sum_cols[0]

        pdf.set_text_color(*c["text_secondary"])
        pdf.cell_text(x, y, sum_cols[1], row_h, _safe_text(metadata.get("video_codec", "-")))
        x += sum_cols[1]
        pdf.cell_text(x, y, sum_cols[2], row_h, _safe_text(metadata.get("resolution", "-")))
        x += sum_cols[2]

        status = _result_status(r)
        pill_h = 5.6
        pdf.pill(x + CELL_PAD, y + (row_h - pill_h) / 2, sum_cols[3] - 2 * CELL_PAD, pill_h,
                 _status_label(status), STATUS_COLORS.get(status, c["text_secondary"]), size=7)
        x += sum_cols[3]

        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*c["text_secondary"])
        pdf.wrapped_text(x, y + 2, sum_cols[4], summary, line_h=3.6)
        pdf.set_xy(MARGIN_L, y + row_h)

    pdf.ln(10)

    # ---- Detailed results ----
    chk_cols = [w * USABLE_W for w in (0.13, 0.24, 0.63)]
    chk_headers = ["Status", "Check", "Detail"]

    def _check_header():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*c["accent"])
        pdf.rect(MARGIN_L, pdf.get_y(), USABLE_W, 6.5, style="F",
                 round_corners=("TOP_LEFT", "TOP_RIGHT"), corner_radius=2)
        pdf.set_text_color(255, 255, 255)
        x, y = MARGIN_L, pdf.get_y()
        for i, h in enumerate(chk_headers):
            pdf.cell_text(x, y, chk_cols[i], 6.5, h, align="C" if i == 0 else "L")
            x += chk_cols[i]
        pdf.set_xy(MARGIN_L, y + 6.5)

    if not pdf.room_for(24):
        pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*c["text_primary"])
    pdf.cell(0, 7, "Detailed Results", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    for r in results:
        # Keep the file header, metadata line and at least one check row
        # together; an orphaned header at the foot of a page looks broken.
        if not pdf.room_for(38):
            pdf.add_page()

        overall = _result_status(r)
        overall_color = STATUS_COLORS.get(overall, c["text_secondary"])

        y = pdf.get_y()
        pdf.set_draw_color(*c["border"])
        pdf.set_line_width(0.2)
        pdf.line(MARGIN_L, y, PAGE_W - MARGIN_R, y)
        y += 5

        label_text = _status_label(overall)
        pdf.set_font("Helvetica", "B", 8)
        pill_w = pdf.get_string_width(label_text) + 8
        pdf.pill(MARGIN_L, y, pill_w, 6, label_text, overall_color, size=8)

        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*c["text_primary"])
        pdf.set_xy(MARGIN_L + pill_w + 4, y)
        pdf.cell(USABLE_W - pill_w - 4, 6, _safe_text(r.filename))
        y += 8

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
        if metadata.get("file_size_mb"):
            meta_parts.append(f"{metadata['file_size_mb']} MB")
        if meta_parts:
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*c["text_tertiary"])
            pdf.set_xy(MARGIN_L, y)
            pdf.cell(USABLE_W, 4.5, _safe_text("   |   ".join(meta_parts)))
            y += 6

        summary = _safe_text(r.plain_summary()) if hasattr(r, "plain_summary") else ""
        if summary and summary != "All checks passed.":
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(*overall_color)
            h = pdf.text_height(USABLE_W, summary, 4.4)
            pdf.wrapped_text(MARGIN_L, y, USABLE_W, summary, line_h=4.4)
            y += h + 2

        pdf.set_xy(MARGIN_L, y + 1)
        _check_header()

        for check in r.checks:
            if not isinstance(check, (list, tuple)) or len(check) < 3:
                continue
            check_name, status, detail = _safe_text(check[0]), check[1], _safe_text(check[2])

            pdf.set_font("Helvetica", "", 8)
            row_h = max(ROW_MIN_H, pdf.text_height(chk_cols[2], detail, 4.0) + 3)

            if not pdf.room_for(row_h):
                pdf.add_page()
                _check_header()

            y = pdf.get_y()
            pdf.row_band(y, row_h, c["bg_secondary"])

            pill_h = 5.2
            pdf.pill(MARGIN_L + CELL_PAD, y + (row_h - pill_h) / 2,
                     chk_cols[0] - 2 * CELL_PAD, pill_h, _status_label(status),
                     STATUS_COLORS.get(status, c["text_secondary"]), size=7)

            x = MARGIN_L + chk_cols[0]
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*c["text_primary"])
            pdf.cell_text(x, y, chk_cols[1], row_h, check_name)
            x += chk_cols[1]

            pdf.set_font("Helvetica", "", 8)
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
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*c["text_tertiary"])
    pdf.set_x(MARGIN_L)
    pdf.cell(
        0, 5,
        f"FrameGuard Desktop v{__version__}   |   Published by Capri la Ria   |   Developed by Cassian Aléria   |   "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}   |   Processed locally",
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
    )

    # Save
    if not os.path.dirname(output_path):
        docs_dir = os.path.join(os.path.expanduser("~"), "Documents", "FrameGuard")
        os.makedirs(docs_dir, exist_ok=True)
        output_path = os.path.join(docs_dir, os.path.basename(output_path))
    else:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
    try:
        pdf.output(output_path)
    except Exception as e:
        raise RuntimeError(f"Failed to write PDF to '{output_path}': {e}") from e
    return output_path


def get_default_output_dir() -> str:
    """Get the default output directory for PDF reports.
    Creates it if it doesn't exist.
    Returns: Path to ~/Documents/FrameGuard/
    """
    docs_dir = os.path.join(os.path.expanduser("~"), "Documents", "FrameGuard")
    os.makedirs(docs_dir, exist_ok=True)
    return docs_dir
