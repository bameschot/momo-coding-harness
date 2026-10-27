"""replace_symbol: rewrite one whole definition, found by name (tree-sitter,
harness/editing.py).  Offered with the "symbol" edit feature."""
from __future__ import annotations

from ... import code_nav, editing
from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("replace_symbol",
        "Replace ONE whole function, method or class with new source, found by name — no "
        "old_string to copy. new_source replaces exactly the lines read_symbol shows for that "
        "name (decorators included); its indentation is adjusted to fit, so a method keeps its "
        "place in the class. Use it to rewrite a definition; for a change of a few lines use "
        "edit_file. An empty new_source deletes the definition. The result shows the new lines "
        "with their line numbers.",
        {"path":       {"type": "string", "description": "Source file containing the definition"},
         "name":       {"type": "string", "description": "The definition to replace: 'parse', a qualified 'Parser.parse', or a line number inside it like '120'"},
         "new_source": {"type": "string", "description": "The complete new definition, from its first line (decorator or signature) to its last, exactly as it should appear in the file — quotes and backslashes are not escaped again"}},
        ["path", "name", "new_source"]),
    discoverable=True,
    run=editing.replace_symbol, order=450, group="edit & shell", needs_edit_feature="symbol",
    requires=lambda: code_nav.AVAILABLE, inject=("index", "cancel", "edit_features"),
    writes=("path",), body_arg="new_source", diff=True,
    example={"path": "src/parser.py", "name": "Parser.parse",
             "new_source": "    def parse(self, text):\n        return self._parse(text.strip())\n"})
