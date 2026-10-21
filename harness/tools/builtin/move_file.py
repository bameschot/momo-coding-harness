"""move_file: rename or move a file or folder inside the workdir."""
from __future__ import annotations

import os
from pathlib import Path

from ...paths import safe_entry_path
from ..registry import WRITE_ROLES, Tool, tool_schema


def _same_entry(a: Path, b: Path) -> bool:
    """The same directory entry (a case-only rename on macOS), not following links."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def move_file(src: str, dst: str, *, workdir: Path) -> str:
    sp = safe_entry_path(src, workdir)
    if isinstance(sp, str):
        return sp
    dp = safe_entry_path(dst, workdir)
    if isinstance(dp, str):
        return dp
    if not sp.exists() and not sp.is_symlink():
        return f"ERROR: source not found: {src}"
    if (dp.exists() or dp.is_symlink()) and not _same_entry(sp, dp):
        return (f"ERROR: destination already exists: {dst} — nothing was moved. "
                f"Delete it first if it should be replaced.")
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        sp.rename(dp)
    except OSError as e:
        return f"ERROR: {e}"
    return "OK"


TOOL = Tool(
    tool_schema("move_file",
        "Move or rename a file or directory. Both source and destination must be inside the "
        "working directory. Parent directories of the destination are created automatically. "
        "If the destination already exists nothing is moved and an error is returned — "
        "delete the destination first if it should be replaced.",
        {"src": {"type": "string", "description": "Current path"},
         "dst": {"type": "string", "description": "Target path"}},
        ["src", "dst"]),
    run=move_file, order=420, group="edit & shell", writes=("src", "dst"), diff=True,
    example={"src": "old/path.py", "dst": "new/path.py"})
