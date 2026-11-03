"""complete_story: mark the current story done (a story of a stories plan).
Answered by the harness (PlanRunnerMixin), which runs the story's acceptance
checks first and records what the story delivered."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("complete_story",
        "Submit the CURRENT story once it is built and you have tried its examples. The harness "
        "runs the unit tests you wrote and refuses while one fails, showing its output; then an "
        "independent verifier tests the story's examples, and an unmet one comes back to you here. "
        "This ends your turn: other tool calls after it in the same turn are not run.",
        {"summary":  {"type": "string",
                      "description": "What you built, for the stories that build on it: the definitions and their "
                                     "signatures, and anything that differs from what the story said it provides"}},
        ["summary"]),
    harness=True, locked=True, order=705, group="ask & plan", modes=frozenset({"story-exec"}),
    keeps_green=True,
    example={"summary": "Added paginate(items, size) -> list[list] to src/pager.py; size < 1 raises "
                        "ValueError as specified."})
