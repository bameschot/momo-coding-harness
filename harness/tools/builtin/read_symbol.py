"""read_symbol: the full source of one definition, by name or by a line in it."""
from __future__ import annotations

from ... import code_nav
from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("read_symbol",
        "Read the complete source of ONE class, function or method from a file, with line numbers "
        "in the same format as read_file (so you can copy text for edit_file). `name` may be either "
        "the definition's name or a LINE NUMBER ('1300' / 'L1300'), which reads whichever definition "
        "contains that line — use the line form after a grep_files hit or a stack trace. Use a "
        "qualified name like 'Class.method' when a bare name is ambiguous; if it still matches "
        "several definitions you get their line ranges instead. To learn only WHICH definition a "
        "line is in, without its body, use find_symbol with the line number instead.",
        {"path": {"type": "string", "description": "Source file containing the definition"},
         "name": {"type": "string", "description": "A line number like '1300' to read the definition containing it, or a name like 'parse' or 'Parser.parse'"}},
        ["path", "name"]),
    run=code_nav.read_symbol, order=320, group="code nav", modes=ALL_ROLES, keeps_green=True,
    requires=lambda: code_nav.AVAILABLE, example={"path": "src/parser.py", "name": "Parser.parse"})
