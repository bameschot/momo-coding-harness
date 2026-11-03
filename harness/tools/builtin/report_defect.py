"""report_defect: end the current story because a bug in code an earlier story
wrote keeps its checks failing (a story of a stories plan).  Answered by the
harness (PlanRunnerMixin), which has a fix story written and runs this story
again after it."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("report_defect",
        "Use when this story cannot work because of a bug in code an EARLIER story wrote, and "
        "the fix is more than a few lines (fix a small one yourself and say so in complete_story's "
        "summary), or when one of this story's own acceptance criteria is wrong (pass 'criterion'). The "
        "harness has a separate story written that fixes the bug (or a corrected story), then runs "
        "it. This ends the story: other tool calls after it in the same turn are not run.",
        {"file":     {"type": "string", "description": "The file with the bug (workdir-relative)"},
         "problem":  {"type": "string",
                      "description": "What is wrong there and what it breaks, e.g. 'add_player() never "
                                     "puts the player in a room, so say reaches no one'"},
         "evidence": {"type": "string", "description": "The failing output that shows it"},
         "criterion": {"type": "string",
                       "description": "Only when the defect is in one of THIS story's acceptance "
                                      "criteria (it asks for something the story is not about, "
                                      "contradicts another, or no correct product can meet it): that "
                                      "criterion's text. The story writer then corrects it"}},
        ["problem"]),
    harness=True, locked=True, order=706, group="ask & plan", modes=frozenset({"story-exec"}),
    keeps_green=True,
    example={"file": "game/engine.py",
             "problem": "add_player() never sets player.room, so say reaches no one in the room",
             "evidence": "AssertionError: 'Bob says: hi' not found in []"})
