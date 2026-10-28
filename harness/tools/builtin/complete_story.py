"""complete_story: mark the current story done (a story of a stories plan).
Answered by the harness (PlanRunnerMixin), which runs the story's acceptance
checks first and records what the story delivered."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("complete_story",
        "Mark the CURRENT story as done once its change is made and verified. The harness first "
        "runs every acceptance check of the story and refuses while one fails, showing its output. "
        "For a criterion without a check, say in 'evidence' how you verified it. This ends the "
        "story: other tool calls after it in the same turn are not run.",
        {"summary":  {"type": "string",
                      "description": "What you built, for the stories that build on it: the definitions and their "
                                     "signatures, and anything that differs from what the story said it provides"},
         "evidence": {"type": "array",
                      "items": {"type": "object",
                                "properties": {"criterion": {"type": "string", "description": "The criterion's text"},
                                               "how":       {"type": "string", "description": "How you verified it"}},
                                "required": ["criterion", "how"]},
                      "description": "For each acceptance criterion without a check command: how you verified it"}},
        ["summary"]),
    harness=True, locked=True, order=705, group="ask & plan", modes=frozenset({"story-exec"}),
    keeps_green=True,
    example={"summary": "Added paginate(items, size) -> list[list] to src/pager.py; size < 1 raises "
                        "ValueError as specified."})
