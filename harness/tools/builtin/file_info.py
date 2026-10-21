"""file_info: whether a path exists, what it is, its size, age and lines."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ...paths import BINARY_SNIFF_BYTES, safe_entry_path, safe_path
from ..registry import ALL_ROLES, Tool, tool_schema


def file_info(path: str, *, workdir: Path) -> str:
    # The entry itself, so a symlink is reported as one rather than as its target.
    p = safe_entry_path(path, workdir)
    if isinstance(p, str):
        p = safe_path(path, workdir)          # "." is the workdir itself
        if isinstance(p, str):
            return p
    if not p.exists() and not p.is_symlink():
        return f"exists:   no\npath:     {path}"
    try:
        st = p.lstat()
    except OSError as e:
        return f"ERROR: {e}"
    if p.is_symlink():
        try:
            kind = f"symlink -> {os.readlink(p)}"
        except OSError:
            kind = "symlink"
    elif p.is_dir():
        kind = "directory"
    else:
        kind = "file"
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"exists:   yes",
        f"type:     {kind}",
        f"size:     {st.st_size:,} bytes",
        f"modified: {mtime}",
    ]
    if kind == "file":
        lines.append(f"lines:    {_count_lines(p)}")
    return "\n".join(lines)


def _count_lines(p: Path) -> str:
    """Line count read in chunks (a big log is never held whole), or "(binary)"."""
    try:
        with p.open("rb") as f:
            chunk = f.read(BINARY_SNIFF_BYTES)
            if b"\x00" in chunk:
                return "(binary)"
            n, last = 0, b""
            while chunk:
                n += chunk.count(b"\n")
                last = chunk[-1:]
                chunk = f.read(1 << 20)
    except OSError:
        return "(unreadable)"
    return str(n + (1 if last and last != b"\n" else 0))


TOOL = Tool(
    tool_schema("file_info",
        "Return metadata for a path: existence, type (file/directory/symlink), "
        "size in bytes, last-modified timestamp, and line count for text files.",
        {"path": {"type": "string", "description": "Path to inspect"}},
        ["path"]),
    run=file_info, order=110, group="files", modes=ALL_ROLES, keeps_green=True,
    example={"path": "main.py"})
