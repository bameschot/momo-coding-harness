"""index_status: what the code index covers and what it skipped."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_status",
        "Show what the code index covers: files per language, skipped files and memory use. Only "
        "needed when another index_* result says something is missing.",
        {},
        []),
    discoverable=True,
    run=code_index.index_status, order=260, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={})
