"""delete_file: remove one file (never a folder)."""
from __future__ import annotations

from pathlib import Path

from ...paths import safe_entry_path
from ..registry import Tool, tool_schema


def delete_file(path: str, *, workdir: Path) -> str:
    p = safe_entry_path(path, workdir)
    if isinstance(p, str):
        return p
    if p.is_dir() and not p.is_symlink():
        return f"ERROR: {path} is a directory — delete_file removes single files only"
    try:
        p.unlink()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except OSError as e:
        return f"ERROR: {e}"
    return "OK"


TOOL = Tool(
    tool_schema("delete_file",
        "Permanently delete a single file (this cannot be undone). Works on files only — it "
        "will error on a directory. The path must be inside the working directory.",
        {"path": {"type": "string", "description": "File to delete"}},
        ["path"]),
    run=delete_file, order=470, group="edit & shell", writes=("path",), diff=True,
    example={"path": "old-file.py"})
