"""code_outline: a file's definitions with line ranges, or a folder's map
(tree-sitter, harness/code_nav.py)."""
from __future__ import annotations

from ... import code_nav
from ..common import CODE_NAV_LANGS, dir_map_hint
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("code_outline",
        f"Show the structure of source code ({CODE_NAV_LANGS}). A FILE: every class, function "
        "and method with its line range — understand a file without reading it all, then "
        "read_symbol the part you need. A DIRECTORY: a one-line-per-file map of every source file "
        "under it — start here in an unfamiliar project.",
        {"path":  {"type": "string", "description": "Source file to outline, or a directory to map (default: .)"},
         "depth": {"type": "integer", "description": "Nesting levels to show: 1 = top-level definitions only, 2 = their methods too (default: 1 for a directory, all levels for a file)"}},
        []),
    run=code_nav.code_outline, order=300, group="code nav", modes=ALL_ROLES, keeps_green=True,
    requires=lambda: code_nav.AVAILABLE, example={"path": "src", "depth": 1}, after=dir_map_hint)
