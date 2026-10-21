"""complete_step: mark the current plan step done.  Answered by the harness
(PlanRunnerMixin), which runs the TDD gate first in a test-driven plan."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("complete_step",
        "Mark the CURRENT plan step as done once its change is made and verified. This ends the "
        "step immediately: any other tool calls after it in the same turn are not run. The harness "
        "then gives you the next step. Do not start the next step's work before calling this. "
        "In a TDD plan the harness first runs the plan's tests and refuses the step if they fail.",
        {"summary": {"type": "string", "description": "One or two sentences: what changed and how it was verified"}},
        ["summary"]),
    harness=True, locked=True, order=700, group="ask & plan", modes=frozenset({"plan-exec"}),
    keeps_green=True,
    example={"summary": "Changed the loop bound in paginate(); python -m pytest tests/test_pager.py passes."})
