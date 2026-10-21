"""ask_user: pause the turn and ask the user.  Answered by the harness loop
(Harness._ask_user), which blocks until a frontend submits the answer."""
from __future__ import annotations

from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("ask_user",
        "Pause and ask the user a clarifying question mid-task. "
        "Use this when you genuinely cannot determine the answer from the code or context — "
        "e.g. which of two approaches to take, confirmation before a destructive action, "
        "or a preference the user has not expressed. "
        "Do NOT use this for things discoverable by reading files. "
        "Ask one focused question per call and continue after receiving the answer.",
        {"question": {"type": "string",
                      "description": "The question to present to the user"}},
        ["question"]),
    harness=True, order=410, group="ask & plan", modes=ALL_ROLES, keeps_green=True,
    example={"question": "Should I overwrite the existing file?"})
