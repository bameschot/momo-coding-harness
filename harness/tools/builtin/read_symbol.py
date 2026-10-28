"""read_symbol: the full source of one definition, by name or by a line in it."""
from __future__ import annotations

from ... import code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("read_symbol",
        "Read the complete source of ONE class, function or method, numbered like read_file (copy "
        "from it for edit_file). name may be a LINE NUMBER ('1300'): it reads whichever definition "
        "contains that line — use it after a grep hit or a stack trace. Use 'Class.method' when "
        "a bare name is ambiguous.",
        {"path": {"type": "string", "description": "Source file containing the definition"},
         "name": {"type": "string", "description": "A line number like '1300' to read the definition containing it, or a name like 'parse' or 'Parser.parse'"}},
        ["path", "name"]),
    run=code_nav.read_symbol, order=320, group="code nav", modes=ALL_ROLES, keeps_green=True,
    requires=lambda: code_nav.AVAILABLE, example={"path": "src/parser.py", "name": "Parser.parse"})
