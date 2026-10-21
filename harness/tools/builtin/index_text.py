"""index_text: text search over every indexed file, each hit with the
definition it is in."""
from __future__ import annotations

from ... import code_index, code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("index_text",
        "Search the text of every file in the project (code, docs, config) from the code index — "
        "the fast replacement for grep_files. Plain text, case-insensitive, by default; "
        "regex=true for a Python regex (case-sensitive). Each hit names the definition it sits "
        "in, e.g. 'L120 [in Parser.parse]'. To find where something is DEFINED, use index_search "
        "instead.",
        {"query": {"type": "string", "description": "Text to find, e.g. 'retry_delay' or 'TODO'; a regex when regex is true"},
         "path":  {"type": "string", "description": "Only under this directory or file, or matching a glob like '*.sql' (default: whole project)"},
         "regex": {"type": "boolean", "description": "Treat query as a Python regular expression (default: false)"}},
        ["query"]),
    run=code_index.index_text, order=210, group="index", modes=ALL_ROLES, keeps_green=True,
    needs_index=True, requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel"),
    example={"query": "retry_delay", "path": "src"})
