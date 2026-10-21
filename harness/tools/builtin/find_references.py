"""find_references: every use of an identifier, tagged by role.  Replaced by
index_callers while the code index is on, so the model never has to choose
between two tools that answer the same question."""
from __future__ import annotations

from ... import code_nav
from ..common import CODE_NAV_LANGS
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("find_references",
        f"Find every place an identifier is used across the project ({CODE_NAV_LANGS} files). "
        "Each hit names the function or class it sits in, e.g. '[in Parser.parse]', and tags what "
        "the use IS: '(call)' a call site, '(def)' the definition, '(import)' an import, "
        "'(type)' a type reference, '(other)' a plain read or assignment, '(decl)' a C/C++ prototype, "
        "'(local)' a parameter or local variable that only shares the name. A call on an object also "
        "shows the receiver, e.g. '(call, recv ast)' for 'ast.parse(...)' — that is how you spot "
        "unrelated same-named methods. Matches whole identifiers only and skips comments and "
        "strings, so it is more exact than grep_files. Use before renaming or changing a "
        "function's signature, and pass role='call' to see only real call sites. "
        "Results are capped at 200.",
        {"name":      {"type": "string", "description": "Identifier to find, e.g. 'parse_config'"},
         "directory": {"type": "string", "description": "Directory (or single file) to search (default: .)"},
         "role":      {"type": "string", "description": "Only uses of this kind: call, def, decl, import, type, local, other (default: all kinds)"}},
        ["name"]),
    run=code_nav.find_references, order=330, group="code nav", modes=ALL_ROLES, keeps_green=True,
    requires=lambda: code_nav.AVAILABLE, hidden_by_index=True,
    example={"name": "parse_config", "role": "call"})
