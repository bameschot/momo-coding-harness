"""find_tools: load tools listed in the "More tools" catalog (/tools discover).
Answered by the harness loop (Harness._find_tools), which owns the loaded set."""
from __future__ import annotations

from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("find_tools",
        "Load tools from the \"More tools\" list in the system prompt, so you can call them. "
        "Pass their exact names, or describe what you need in query and the best matches load. "
        "A loaded tool stays available for the rest of the session.",
        {"names": {"type": "array", "items": {"type": "string"},
                   "description": "Exact tool names from the More tools list"},
         "query": {"type": "string",
                   "description": "What you need to do, e.g. \"add text to the end of a file\" (used when names is empty)"}},
        []),
    harness=True, needs_discover=True, order=405, group="ask & plan",
    modes=ALL_ROLES, keeps_green=True, example={"names": ["append_to_file"]})
