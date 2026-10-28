"""write_file: create or overwrite a whole file (harness/editing.py)."""
from __future__ import annotations

from ... import editing
from ..registry import WRITE_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("write_file",
        "Create a NEW file, or COMPLETELY overwrite an existing one, with the given content. It "
        "cannot change part of a file — use edit_file for that. content is the raw file exactly "
        "as it should be on disk: no ``` code fences unless the file is Markdown.",
        {"path":    {"type": "string", "description": "File path with its extension, e.g. 'process.py'"},
         "content": {"type": "string", "description": "The file's text exactly as on disk — quotes and backslashes are not escaped again"}},
        ["path", "content"]),
    # Design mode writes one spec and revises it, so it has write_file too.
    suggests=("replace_symbol",),
    run=editing.write_file, order=400, group="edit & shell", modes=WRITE_ROLES | {"design"},
    inject=("index", "cancel", "edit_features"), writes=("path",), body_arg="content", diff=True,
    example={"path": "hello.py", "content": "print('hello!')"})
