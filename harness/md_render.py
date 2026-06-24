"""Markdown-to-curses renderer.

Pure transformation: no TUI imports.  Call process() to convert a markdown
string into a list of (display_text, color_id, curses_attrs) tuples that can
be fed directly to _LineBuffer.append().

Color IDs must match the constants assigned in tui.py.
"""
from __future__ import annotations

import re
import textwrap
import curses

# Color pair IDs — must match tui.py _C_MD_* assignments
_C_NORMAL   = 0   # default terminal colors
_C_BORDER   = 9   # reuse existing border color for horizontal rules
_C_MD_H1    = 14
_C_MD_H2    = 15
_C_MD_H3    = 16
_C_MD_CODE  = 17
_C_MD_QUOTE = 18
_C_MD_BOLD  = 19   # used for lines that contain bold markers


def process(text: str, cols: int) -> list[tuple[str, int, int]]:
    """Parse markdown text and return (display_text, color_id, curses_attrs) lines."""
    out: list[tuple[str, int, int]] = []
    lines = text.splitlines()

    # --- table accumulation state ---
    table_buf: list[str] = []

    def flush_table():
        if not table_buf:
            return
        rows = _parse_table(table_buf, cols)
        out.extend(rows)
        table_buf.clear()

    in_code = False
    lang_line = False  # True for the opening ``` line itself (skip it)

    for raw in lines:
        stripped = raw.rstrip()

        # ── fenced code block toggle ──────────────────────────────────────────
        if stripped.startswith("```"):
            if not in_code:
                flush_table()
                in_code = True
                lang_line = True   # skip the opening fence line
            else:
                in_code = False
            continue

        if in_code:
            # preserve original indentation; prefix with │
            out.append(("│ " + raw.rstrip(), _C_MD_CODE, 0))
            continue

        # ── table rows ───────────────────────────────────────────────────────
        if stripped.startswith("|") and stripped.endswith("|"):
            table_buf.append(stripped)
            continue
        else:
            flush_table()

        # ── headings ─────────────────────────────────────────────────────────
        m = re.match(r'^(#{1,3})\s+(.*)', stripped)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            heading = _strip_inline(heading)
            w = max(4, cols - 2)
            if level == 1:
                bar = "═" * w
                label = heading.upper()
                padded = f" {label} "
                left = (w - len(padded)) // 2
                right = w - len(padded) - left
                out.append(("═" * left + padded + "═" * right, _C_MD_H1, curses.A_BOLD))
            elif level == 2:
                padded = f" {heading} "
                left = (w - len(padded)) // 2
                right = w - len(padded) - left
                left  = max(0, left)
                right = max(0, right)
                out.append(("─" * left + padded + "─" * right, _C_MD_H2, curses.A_BOLD))
            else:
                out.append((f"▸ {heading}", _C_MD_H3, 0))
            continue

        # ── horizontal rule ───────────────────────────────────────────────────
        if re.match(r'^[-*_]{3,}\s*$', stripped):
            out.append(("─" * max(4, cols - 2), _C_BORDER, 0))
            continue

        # ── blockquote ────────────────────────────────────────────────────────
        m = re.match(r'^>\s?(.*)', stripped)
        if m:
            content = _strip_inline(m.group(1))
            wrap_w = max(4, cols - 4)
            for wl in textwrap.wrap(content, width=wrap_w) or [content]:
                out.append((f"  ▌ {wl}", _C_MD_QUOTE, 0))
            continue

        # ── unordered list items ──────────────────────────────────────────────
        m = re.match(r'^(\s*)[-*+]\s+(.*)', stripped)
        if m:
            indent_n = len(m.group(1))
            content = _strip_inline(m.group(2))
            prefix = "  " + "  " * (indent_n // 2) + "• "
            wrap_w = max(4, cols - len(prefix) - 2)
            lines_w = textwrap.wrap(content, width=wrap_w) or [content]
            cont_prefix = " " * len(prefix)
            for i, wl in enumerate(lines_w):
                out.append(((prefix if i == 0 else cont_prefix) + wl, _C_NORMAL, 0))
            continue

        # ── ordered list items ────────────────────────────────────────────────
        m = re.match(r'^(\s*)(\d+)\.\s+(.*)', stripped)
        if m:
            indent_n = len(m.group(1))
            num = m.group(2)
            content = _strip_inline(m.group(3))
            prefix = "  " + "  " * (indent_n // 2) + f"{num}. "
            wrap_w = max(4, cols - len(prefix) - 2)
            lines_w = textwrap.wrap(content, width=wrap_w) or [content]
            cont_prefix = " " * len(prefix)
            for i, wl in enumerate(lines_w):
                out.append(((prefix if i == 0 else cont_prefix) + wl, _C_NORMAL, 0))
            continue

        # ── blank line ────────────────────────────────────────────────────────
        if not stripped:
            out.append(("", _C_NORMAL, 0))
            continue

        # ── normal paragraph line ─────────────────────────────────────────────
        attrs, color, content = _inline_style(stripped)
        wrap_w = max(4, cols - 2)
        for wl in textwrap.wrap(content, width=wrap_w) or [content]:
            out.append((wl, color, attrs))

    flush_table()
    return out


# ── inline helpers ────────────────────────────────────────────────────────────

def _strip_inline(text: str) -> str:
    """Remove inline markdown markers: **bold**, *italic*, `code`, etc."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # __ and _ only match at word boundaries to avoid false positives in filenames/snake_case
    text = re.sub(r'(?<!\w)__(.+?)__(?!\w)', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', text)
    text = re.sub(r'`(.+?)`', r'‹\1›', text)
    return text


def _inline_style(text: str) -> tuple[int, int, str]:
    """Return (curses_attrs, color_id, cleaned_text) based on dominant inline marker."""
    # Bold — **...** or __...__ present
    if re.search(r'\*\*|__', text):
        return curses.A_BOLD, _C_MD_BOLD, _strip_inline(text)
    # Italic — *...* or word-boundary _word_ (not snake_case)
    if re.search(r'\*[^*]|(?<!\w)_\w', text):
        return 0, _C_MD_H3, _strip_inline(text)  # reuse H3 color as "soft highlight"
    # Inline code only
    cleaned = re.sub(r'`(.+?)`', r'‹\1›', text)
    return 0, _C_NORMAL, cleaned


# ── table renderer ────────────────────────────────────────────────────────────

def _parse_table(rows: list[str], cols: int) -> list[tuple[str, int, int]]:
    """Render a markdown table into aligned display lines."""
    parsed: list[list[str]] = []
    sep_idx: int | None = None
    alignments: list[str] = []   # "left" | "right" | "center" per column

    for i, row in enumerate(rows):
        cells = [_strip_inline(c.strip()) for c in row.strip("|").split("|")]
        # detect separator row: every non-empty cell is only dashes and optional colons
        if cells and all(re.match(r'^:?-+:?$', c) for c in cells if c):
            sep_idx = i
            for cell in cells:
                if cell.startswith(":") and cell.endswith(":"):
                    alignments.append("center")
                elif cell.endswith(":"):
                    alignments.append("right")
                else:
                    alignments.append("left")
            continue
        parsed.append(cells)

    if not parsed:
        return [(r, _C_NORMAL, 0) for r in rows]

    # normalise column count
    ncols = max(len(r) for r in parsed)
    parsed = [r + [""] * (ncols - len(r)) for r in parsed]

    # pad / trim alignments to match ncols
    alignments = (alignments + ["left"] * ncols)[:ncols]

    # auto-detect right-align for numeric columns when there is no separator row
    if sep_idx is None:
        data_rows = parsed[1:] if len(parsed) > 1 else parsed
        for c in range(ncols):
            col_vals = [r[c] for r in data_rows if r[c]]
            if col_vals and all(re.match(r'^-?\d[\d.,]*%?$', v) for v in col_vals):
                alignments[c] = "right"

    # compute column widths from content
    widths = [max(len(r[c]) for r in parsed) for c in range(ncols)]

    def _align(text: str, width: int, align: str) -> str:
        if align == "right":  return text.rjust(width)
        if align == "center": return text.center(width)
        return text.ljust(width)

    def fmt_row(cells: list[str], is_header: bool) -> str:
        parts = []
        for c in range(ncols):
            col_align = "center" if is_header else alignments[c]
            parts.append(f" {_align(cells[c], widths[c], col_align)} ")
        return "│" + "│".join(parts) + "│"

    def sep_row() -> str:
        parts = ["─" * (widths[c] + 2) for c in range(ncols)]
        return "├" + "┼".join(parts) + "┤"

    def top_row() -> str:
        parts = ["─" * (widths[c] + 2) for c in range(ncols)]
        return "┌" + "┬".join(parts) + "┐"

    def bot_row() -> str:
        parts = ["─" * (widths[c] + 2) for c in range(ncols)]
        return "└" + "┴".join(parts) + "┘"

    out: list[tuple[str, int, int]] = []
    out.append((top_row(), _C_MD_H3, 0))
    for i, row in enumerate(parsed):
        is_header = sep_idx is not None and i == 0
        color = _C_MD_H3 if is_header else _C_NORMAL
        attrs = curses.A_BOLD if is_header else 0
        out.append((fmt_row(row, is_header), color, attrs))
        if is_header:
            out.append((sep_row(), _C_MD_H3, 0))
    out.append((bot_row(), _C_MD_H3, 0))
    return out
