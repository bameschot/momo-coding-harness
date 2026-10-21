"""append_to_file: add text to the end of a file (harness/editing.py)."""
from __future__ import annotations

from ... import editing
from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("append_to_file",
        "Add text to the END of an existing file (creates it if absent). "
        "Takes ONLY path and content. It cannot change existing text — to replace text "
        "inside a file use edit_file; to overwrite the whole file use write_file.",
        {"path":    {"type": "string", "description": "File to append to (created if absent)"},
         "content": {"type": "string", "description": "Text to add at the end of the file."}},
        ["path", "content"]),
    run=editing.append_to_file, order=430, group="edit & shell", writes=("path",),
    body_arg="content", diff=True, example={"path": "notes.md", "content": "\n## New section\n"})
