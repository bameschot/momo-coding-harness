"""finish_plan: submit a stories plan once every phase is written (plan
mode).  Answered by the harness (PlanRunnerMixin), which checks the whole plan
and asks the user to approve it."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("finish_plan",
        "Submit the plan once every phase of the outline is written with write_phase. The harness "
        "checks the whole plan, writes it to a Markdown file and asks the user to approve it.",
        {}, []),
    harness=True, locked=True, order=604, group="ask & plan", modes=frozenset({"plan"}),
    needs_stories=True, example={})
