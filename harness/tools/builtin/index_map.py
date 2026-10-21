"""index_map: the project's files ranked by how much the rest uses them."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_map",
        "Get your bearings in a project in one call: its files ranked by how much the rest of the "
        "code uses them, each with its most-used definitions, fitted to a size budget. Start here "
        "in an unfamiliar codebase; pass path to zoom into one directory.",
        {"path":   {"type": "string", "description": "Only this directory (default: whole project)"},
         "budget": {"type": "integer", "description": "Approximate size of the answer in tokens (default 1500)"},
         "show":   {"type": "string", "description": "files (default): the ranked map; unused: definitions nothing in the project uses"}},
        []),
    run=code_index.index_map, order=230, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={})
