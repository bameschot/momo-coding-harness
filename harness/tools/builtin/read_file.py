"""read_file: a file or a line range, numbered, with a footer that points at
the structural tools (the most-executed hint in the tool set)."""
from __future__ import annotations

from pathlib import Path

from ... import code_nav
from ...paths import split_lines
from ..common import read_text
from ..registry import ALL_ROLES, Tool, tool_schema

_READ_FOOTER_LINES = 200  # show footer when file exceeds this length and no range given


def _read_footer(p: Path, path: str, n: int, start_line: int, end_line: int | None) -> str:
    """The trailer after read_file output — the most-executed hint in the tool set.

    On a file code_nav can parse it points at the structural tools, because
    reading a large module whole costs roughly twenty times what code_outline
    does, and a ranged read almost always wants to know which definition the
    range landed in.  Anything code_nav cannot parse keeps the original
    line-range hint.
    """
    idx = None
    if code_nav.AVAILABLE:
        try:
            idx = code_nav.index(p)
        except OSError:
            idx = None
    whole = end_line is None and start_line <= 1
    if idx is None or not idx.symbols:
        if whole and n > _READ_FOOTER_LINES:
            return f"\n[{n} lines total — use start_line/end_line to read specific sections]"
        return ""
    if whole:
        if n <= _READ_FOOTER_LINES:
            return ""
        if code_nav.index_active():
            return (f"\n[{n} lines total, {len(idx.symbols)} definitions — index_file shows this "
                    f"{idx.lang} file's outline, imports and users for a fraction of the tokens, "
                    f"and read_symbol reads one definition; or use start_line/end_line]")
        return (f"\n[{n} lines total, {len(idx.symbols)} definitions — code_outline shows this "
                f"{idx.lang} file's structure for a fraction of the tokens, and read_symbol reads "
                f"one definition; or use start_line/end_line]")
    owner = code_nav.enclosing(idx, start_line)
    shown_end = min(end_line or n, n)
    if owner is None:
        return f"\n[showing lines {start_line}-{shown_end} of {n}]"
    return (f"\n[showing lines {start_line}-{shown_end} of {n} — line {start_line} is inside "
            f"{owner.kind} {owner.qualname} (L{owner.start}-{owner.end}); "
            f'read_symbol("{path}", "{owner.qualname}") reads that definition whole]')


def read_file(path: str, start_line: int = 1, end_line: int | None = None, *, workdir: Path,
              read_budget: int | None = None) -> str:
    """read_budget: the most characters this read may return (the context left,
    less a reserve — see Harness._read_budget); None = no limit."""
    got = read_text(path, workdir, refuse_binary=True)
    if isinstance(got, str):
        return got
    p, text = got
    lines = split_lines(text, keepends=True)
    n = len(lines)
    if n == 0:
        return "(empty)"
    # A negative start_line counts from the end (-50 = the last 50 lines): what a
    # model reaches for to see the end of a log.  Clamping it to line 1 read the
    # whole file instead.
    s = max(0, n + start_line) if start_line < 0 else max(0, start_line - 1)
    first = s + 1
    if s >= n:
        return f"ERROR: start_line {start_line} is past the end — {path} has {n} lines"
    if end_line is not None and end_line < first:
        return f"ERROR: end_line {end_line} is before start_line {first}"
    e = min(end_line if end_line is not None else n, n)
    numbered = "".join(f"{s + i + 1:4}: {l}" for i, l in enumerate(lines[s:e]))
    out = numbered + _read_footer(p, path, n, first, end_line)
    if read_budget is not None and len(out) > read_budget:
        per_line = max(1, len(numbered) // max(1, e - s))
        fit = max(1, read_budget // per_line)
        return (f"ERROR: nothing was read: lines {first}-{e} of {path} are {len(out):,} "
                f"characters (~{len(out) // 4:,} tokens), more than the ~{read_budget // 4:,} "
                f"tokens of context this read may use. About {fit:,} lines fit: read a range "
                f'with read_file(path="{path}", start_line=..., end_line=...), or search the '
                f'file with grep_file(pattern="<text or regex you are looking for>", '
                f'path="{path}").')
    return out


TOOL = Tool(
    tool_schema("read_file",
        "Read a file, optionally restricting to a line range. A read too large for the "
        "context left is refused with the number of lines that fit — then read a smaller "
        "range, or search the file with grep_file.",
        {"path": {"type": "string", "description": "File to read"},
         "start_line": {"type": "integer", "description": "1-based start line (default: 1); a negative number counts from the end, e.g. -50 reads the last 50 lines"},
         "end_line": {"type": "integer", "description": "1-based end line inclusive (default: EOF)"}},
        ["path"]),
    suggests=("file_info",),
    run=read_file, order=130, group="files", modes=ALL_ROLES, keeps_green=True,
    inject=("read_budget",), example={"path": "main.py", "start_line": 1, "end_line": 40})
