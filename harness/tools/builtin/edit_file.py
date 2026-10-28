"""edit_file: replace old_string with new_string (harness/editing.py).

Two descriptions, switched by the session's edit_features: with "match" on
(the default) it names what the extras do — whitespace- and quote-tolerant
matching, the changed lines in the result."""
from __future__ import annotations

from ... import editing
from ..registry import WRITE_ROLES, Tool, tool_schema

_PARAMS = {
    "path": {"type": "string"},
    "old_string": {"type": "string", "description": "Exact text to find (copy it verbatim from read_file output)"},
    "new_string": {"type": "string", "description": "Replacement text, exactly as it should appear in the file — quotes and backslashes are not escaped again"},
    "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring exactly one (default: false)"}}

_CLASSIC = tool_schema("edit_file",
    "Change text INSIDE an existing file: replace old_string with new_string. Use this (not "
    "write_file) to modify part of a file you have read. It replaces exactly one occurrence "
    "and fails if old_string is not found exactly once; replace_all=true replaces every one. "
    "Copy old_string verbatim from read_file output.",
    _PARAMS, ["path", "old_string", "new_string"])

TOOL = Tool(
    tool_schema("edit_file",
        "Change text INSIDE an existing file: replace old_string with new_string. Use this (not "
        "write_file) to modify part of a file you have read. It replaces exactly one occurrence "
        "(the error lists where each occurrence is); replace_all=true replaces every one. "
        "Copy old_string from read_file output; indentation, spacing and quote style may differ. "
        "The result shows the changed lines with their new numbers: no need to read the file again.",
        _PARAMS, ["path", "old_string", "new_string"]),
    variants={"classic": _CLASSIC},
    select=lambda ctx: "" if "match" in ctx.edit_features else "classic",
    # Design mode revises the spec it wrote: edit_file changes a section
    # without rewriting a long document (which a small context can truncate).
    suggests=("replace_symbol",),
    run=editing.edit_file, order=440, group="edit & shell", modes=WRITE_ROLES | {"design"},
    inject=("index", "cancel", "edit_features"), writes=("path",), body_arg="new_string",
    diff=True,
    example={"path": "main.py", "old_string": "existing line", "new_string": "replacement line"})
