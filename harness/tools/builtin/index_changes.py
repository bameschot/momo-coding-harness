"""index_changes: what uncommitted edits (or changes since a revision) touch."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_changes",
        "What your changes touch, in one call: every definition the uncommitted edits (or the "
        "changes since base) removed, re-signatured, changed or added — with the places outside "
        "the change that still use it and the tests that cover it. Use it before running tests "
        "or finishing a task.",
        {"base": {"type": "string", "description": "Git revision to compare with (default: HEAD, i.e. the uncommitted changes)"},
         "path": {"type": "string", "description": "Only changes under this directory or file (default: whole project)"}},
        []),
    run=code_index.index_changes, order=240, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={})
