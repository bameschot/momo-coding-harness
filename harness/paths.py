"""Workdir path safety and the shared limits for walking project files.

A leaf module (no harness imports), so tools, code_nav, code_index,
file_search and the web server can all import it at load time.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

# Folders never worth searching or indexing (the index filter seeds them as
# editable `name/` lines, see ignore_rules.DEFAULT_EXCLUDES).
SKIP_DIRS = (".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build",
             ".mypy_cache", ".pytest_cache")
MAX_SCAN_FILE_BYTES = 2_000_000  # larger files are skipped by grep_files, code nav and the index
BINARY_SNIFF_BYTES = 4096        # bytes inspected for a NUL byte to detect binary files


def split_lines(text: str, keepends: bool = False) -> list[str]:
    """Lines as tree-sitter, git and editors number them: split on "\\n" only.
    str.splitlines() also splits on form feeds, \\u2028, \\x1c... and a lone
    "\\r", which shifts every later line number off the parser's.  A "\\r"
    before the "\\n" belongs to the line ending."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()                   # "a\nb\n" is two lines, not three
    if keepends:
        return [p + "\n" for p in parts[:-1]] + ([parts[-1] + ("\n" if text.endswith("\n") else "")]
                                                 if parts else [])
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def safe_path(raw: str, workdir: Path) -> Path | str:
    """raw resolved against the workdir, or an ERROR string when it leaves it."""
    if "\0" in raw:
        return "ERROR: path contains a NUL character"
    p = (workdir / raw).resolve()
    try:
        p.relative_to(workdir.resolve())
    except ValueError:
        return "ERROR: path outside working directory"
    return p


def safe_entry_path(raw: str, workdir: Path) -> Path | str:
    """Like safe_path, but a symlink stays the link itself (only its folder is
    resolved): deleting or moving a link must not act on the file it points to."""
    if "\0" in raw:
        return "ERROR: path contains a NUL character"
    root = workdir.resolve()
    q =Path(os.path.normpath(root / raw))
    if q == root:
        return "ERROR: path is the working directory itself"
    parent = q.parent.resolve()
    try:
        parent.relative_to(root)
    except ValueError:
        return "ERROR: path outside working directory"
    return parent / q.name


def walk_files(root: Path, *, skip_hidden: bool = False) -> Iterator[Path]:
    """Every file under root in sorted order, pruning SKIP_DIRS (and dot-names
    with skip_hidden)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                             and not (skip_hidden and d.startswith(".")))
        for f in sorted(filenames):
            if not (skip_hidden and f.startswith(".")):
                yield Path(dirpath) / f
