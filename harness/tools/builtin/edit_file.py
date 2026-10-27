"""edit_file: replace old_string with new_string (harness/editing.py).

Two descriptions, switched by the session's edit_features: with "match" on
(the default) it names what the extras do — whitespace- and quote-tolerant
matching, the changed lines in the result."""
from __future__ import annotations

from ... import editing
from ..registry import WRITE_ROLES, Tool, tool_schema

_PARAMS = {
    "path": {"type": "string", "description": "File to modify"},
    "old_string": {"type": "string", "description": "Exact text to find (copy it verbatim from read_file output)"},
    "new_string": {"type": "string", "description": "Replacement text, exactly as it should appear in the file — quotes and backslashes are not escaped again"},
    "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring exactly one (default: false)"}}

_CLASSIC = tool_schema("edit_file",
    "Change text INSIDE an existing file: replace old_string with new_string. "
    "Use this tool (not write_file) whenever you are modifying part of a file you have read. "
    "By default it replaces exactly one occurrence and fails if old_string is not found "
    "exactly once; set replace_all=true to replace every occurrence (e.g. renaming a symbol "
    "throughout the file). "
    "Copy old_string verbatim from read_file output; a leading line-number prefix (e.g. "
    "'  12: ') is stripped automatically if you include it by mistake, and a single-occurrence "
    "edit still matches when only indentation or surrounding whitespace differs. If old_string "
    "is already absent because new_string is present, the tool reports the edit as already "
    "applied rather than erroring — do not blindly retry. "
    "Takes path, old_string, new_string, and optional replace_all — it does NOT take a "
    "'content' argument (that is write_file).",
    _PARAMS, ["path", "old_string", "new_string"])

TOOL = Tool(
    tool_schema("edit_file",
        "Change text INSIDE an existing file: replace old_string with new_string. "
        "Use this tool (not write_file) whenever you are modifying part of a file you have read. "
        "By default it replaces exactly one occurrence and fails if old_string is not found "
        "exactly once (the error lists where each occurrence is); set replace_all=true to replace "
        "every occurrence (e.g. renaming a symbol throughout the file). "
        "Copy old_string verbatim from read_file output; a leading line-number prefix (e.g. "
        "'  12: ') is stripped automatically, and the edit still matches when only indentation, "
        "spacing or quote style differs. The result shows the changed lines with their new line "
        "numbers, so you do not need to read the file again to check the edit. If old_string "
        "is already absent because new_string is present, the tool reports the edit as already "
        "applied rather than erroring — do not blindly retry. "
        "Takes path, old_string, new_string, and optional replace_all — it does NOT take a "
        "'content' argument (that is write_file).",
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
