"""list_directory: a folder's entries, folders first."""
from __future__ import annotations

import os
from pathlib import Path

from ...paths import safe_path
from ..common import dir_map_hint
from ..registry import ALL_ROLES, Tool, tool_schema


def list_directory(path: str = ".", show_hidden: bool = False, *, workdir: Path) -> str:
    root = safe_path(path, workdir)
    if isinstance(root, str):
        return root
    if not root.is_dir():
        return f"ERROR: not a directory: {path}"
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        return f"ERROR: {e}"
    entries = [(e.is_dir(follow_symlinks=False), e) for e in entries
               if show_hidden or not e.name.startswith(".")]
    entries.sort(key=lambda de: (not de[0], de[1].name.lower()))     # dirs first
    lines = []
    for is_dir, e in entries:
        if is_dir:
            lines.append(f"[D] {e.name}/")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            lines.append(f"[F] {e.name}  ({size:,} bytes)")
    return "\n".join(lines) if lines else "(empty)"


TOOL = Tool(
    tool_schema("list_directory",
        "List the contents of a directory. Returns '[D] name/' for subdirectories "
        "and '[F] name  (N bytes)' for files, sorted dirs first then files.",
        {"path":        {"type": "string",  "description": "Directory to list (default: .)"},
         "show_hidden": {"type": "boolean", "description": "Include hidden entries starting with '.' (default: false)"}},
        []),
    run=list_directory, order=100, group="files", modes=ALL_ROLES, keeps_green=True,
    example={}, after=dir_map_hint)
