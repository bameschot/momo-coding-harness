"""index_file: one file's outline, imports, importers and most-used definitions."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_file",
        "Everything about ONE file in one call: its outline with line ranges, what it imports, "
        "which files import it, and which of its definitions the rest of the project uses most. "
        "Use it before changing a file, instead of reading the whole file.",
        {"path": {"type": "string", "description": "File to describe, e.g. 'src/parser.py'"}},
        ["path"]),
    run=code_index.index_file, order=250, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={"path": "src/parser.py"})
