"""index_callers: who uses a name (and who uses those), or what it calls."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_callers",
        "Who uses a function, class, method or variable, and what breaks if you change it — in "
        "ONE call: every use grouped by the function it sits in; depth=2 or 3 also shows who uses "
        "THOSE (the blast radius). Use it before renaming or changing a signature. Write a method "
        "as 'Class.method'. direction='callees' lists what it CALLS instead.",
        {"name":  {"type": "string", "description": "What to look up, e.g. 'parse_config', 'Harness.send'"},
         "depth": {"type": "integer", "description": "1 = direct uses (default); 2 or 3 = also the users of those functions (callees: 2 = also what those call)"},
         "direction": {"type": "string", "description": "callers (default): who uses it; callees: what it calls"},
         "role":  {"type": "string", "description": "Only direct uses of this kind: call, import, type, other (default: all). Parameters/locals that share the name and C prototypes are never counted as uses."}},
        ["name"]),
    run=code_index.index_callers, order=220, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={"name": "parse_config", "depth": 2})
