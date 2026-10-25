"""write_file: create or overwrite a whole file (harness/editing.py)."""
from __future__ import annotations

from ... import editing
from ..registry import WRITE_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("write_file",
        "Create a NEW file, or COMPLETELY overwrite an existing one, with the given content. "
        "Takes ONLY path and content. It cannot change part of a file and does NOT accept "
        "old_string/new_string — to replace specific text in an existing file, use edit_file instead. "
        "Use this to save any file: scripts (.py, .sh), data files (.csv, .json, .jsonl), "
        "documents (.md, .txt), or any other content. "
        "The content parameter must contain the raw file content exactly as it should appear on disk — "
        "do NOT wrap code in markdown code fences (no ```python or ``` markers) unless the target "
        "file is itself a Markdown document.",
        {"path":    {"type": "string", "description": "Destination file path, including the correct extension for the file type (e.g. 'process.py', 'output.csv', 'report.md')."},
         "content": {"type": "string", "description": "Raw file content to write, exactly as it should appear on disk — quotes and backslashes are not escaped again. For scripts, this is source code only — no surrounding markdown."}},
        ["path", "content"]),
    # Design mode writes one spec and revises it, so it has write_file too.
    run=editing.write_file, order=400, group="edit & shell", modes=WRITE_ROLES | {"design"},
    inject=("index", "cancel", "edit_features"), writes=("path",), body_arg="content", diff=True,
    example={"path": "hello.py", "content": "print('hello!')"})
