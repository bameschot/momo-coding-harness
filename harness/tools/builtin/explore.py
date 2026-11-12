"""explore: hand a research question to a sub-agent with a fresh context.
Answered by the harness (explorer.ExploreMixin): the explorer investigates
with the read tools and read-only commands and returns a short report; only
the call and the report stay in the caller's context."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("explore",
        "Hand a research question to a sub-agent that starts with a fresh context. It searches "
        "and reads the code (and may run read-only commands), then returns a short report with "
        "path:line references; its reads do not fill your context. Use it for a broad question "
        "that needs many reads (where something is handled, how a flow works end to end, every "
        "place that does X), not for one file or one symbol a single call finds. It sees nothing "
        "of this conversation: put everything it needs to know in `question`.",
        {"question": {"type": "string",
                      "description": "The question, complete on its own, with the names, files "
                                     "or behaviour it is about"},
         "paths": {"type": "array", "items": {"type": "string"},
                   "description": "Optional: files or directories to start from"}},
        ["question"]),
    harness=True, order=390, group="ask & plan",
    modes=frozenset({"design", "chat", "plan", "coding", "momo", "plan-exec"}), keeps_green=True,
    example={"question": "Where is a session saved to disk, and which code calls it?"})
