"""edit_files: several edits, in one file or across files, all or nothing
(harness/editing.py).  Offered with the "batch" edit feature."""
from __future__ import annotations

from ... import editing
from ..common import edit_list, edit_paths
from ..dispatcher import has_cut_marker, is_placeholder
from ..registry import Tool, tool_schema


def _guard(args: dict) -> str | None:
    """Sessions saved before the harness stopped eliding write content hold
    "[written to <path>]" in place of past file bodies; never let that (or a
    shortened tool result) reach the disk as an edit's new text."""
    for i, e in enumerate(edit_list(args.get("edits")), 1):
        body = e.get("new_string") if isinstance(e, dict) else None
        if is_placeholder(body) or has_cut_marker(body):
            return (f"ERROR: edit {i}'s new_string holds a harness note or history placeholder, "
                    f"not file text — nothing was written. Copy the real text from read_file.")
    return None


TOOL = Tool(
    tool_schema("edit_files",
        "Make several edits — in one file or across files — in ONE call: edits is a list of "
        "{path, old_string, new_string} (add replace_all=true to an edit to replace every "
        "occurrence), applied in order. Use it whenever you already know more than one change: "
        "updating every caller after a signature change, the imports and uses of a renamed name, "
        "two changes in one function. Each old_string must match like edit_file's; if any edit "
        "fails, nothing is written and the error says which one, so fix it and send all the "
        "edits again. The result lists every changed line with its new line number.",
        {"edits": {"type": "array", "description": "The edits, in order: [{\"path\": ..., \"old_string\": ..., \"new_string\": ...}, ...]",
                   "items": {"type": "object",
                             "properties": {"path": {"type": "string"},
                                            "old_string": {"type": "string"},
                                            "new_string": {"type": "string", "description": "Replacement text, exactly as it should appear in the file — quotes and backslashes are not escaped again"},
                                            "replace_all": {"type": "boolean"}},
                             "required": ["path", "old_string", "new_string"]}}},
        ["edits"]),
    # edit_files re-indexes its own files (editing.py, before its definition
    # checks), so it names them for .momo/ only (written), not in writes.
    run=editing.edit_files, order=460, group="edit & shell", needs_edit_feature="batch",
    inject=("index", "cancel", "edit_features"), written=edit_paths, guard=_guard, diff=True,
    example={"edits": [{"path": "src/api.py", "old_string": "load(path)", "new_string": "load(path, strict=True)"},
                       {"path": "src/cli.py", "old_string": "load(args.file)", "new_string": "load(args.file, strict=True)"}]})
