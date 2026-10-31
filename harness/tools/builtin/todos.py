"""todos: the model's checklist for a request with several parts, outside plan
mode.  Answered by the harness loop (Harness._handle_todos, list rules in
harness/todos.py): the whole list each call, kept through compaction."""
from __future__ import annotations

from ... import todos as todos_mod
from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("todos",
        "Keep a checklist for a request with several parts. Send the whole list each time; "
        "mark one item in_progress while you work on it and done when it is finished. "
        "Skip it for a single-step task.",
        {"items": {"type": "array",
                   "items": {"type": "object",
                             "properties": {"text": {"type": "string"},
                                            "status": {"type": "string",
                                                       "enum": list(todos_mod.STATUSES)}},
                             "required": ["text", "status"]}}},
        ["items"]),
    harness=True, order=412, group="ask & plan", modes=frozenset({"coding", "momo"}),
    discoverable=True,
    example={"items": [{"text": "Rename parse() to parse_args()", "status": "done"},
                       {"text": "Add a test for empty input", "status": "in_progress"},
                       {"text": "Update the README", "status": "pending"}]})
