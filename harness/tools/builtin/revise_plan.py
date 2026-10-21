"""revise_plan: replace the rest of the approved plan while it executes.
Answered by the harness (PlanRunnerMixin)."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import STEP, STEP_TDD, tdd_variant

_DESCRIPTION = (
    "Rewrite the rest of the approved plan when a discovery during execution shows it is wrong "
    "or incomplete. 'steps' REPLACES the whole remaining plan: the current step plus EVERY later "
    "step, including the ones that do not change — any step you leave out is dropped. Finished "
    "steps are kept automatically. Explain the change in 'reason'.")


def _schema(step: dict) -> dict:
    return tool_schema("revise_plan", _DESCRIPTION,
        {"reason": {"type": "string", "description": "What was discovered that forces the change"},
         "steps":  {"type": "array", "items": step,
                    "description": "The complete new list of remaining steps (current step first), in order"}},
        ["reason", "steps"])


_EXAMPLE = {"reason": "paginate() is also duplicated in api/pager.py",
            "steps": [{"title": "Fix loop bound in both paginate() copies",
                       "details": "Apply the <= fix in src/pager.py and api/pager.py.",
                       "files": ["src/pager.py", "api/pager.py"]}]}

TOOL = Tool(
    _schema(STEP),
    variants={"tdd": _schema(STEP_TDD)},
    examples={"tdd": {**_EXAMPLE, "steps": [{**_EXAMPLE["steps"][0],
                                             "tests": ["paginate(range(10), size=5) in api/pager.py → 2 pages"]}]}},
    select=tdd_variant, harness=True, locked=True, order=710, group="ask & plan",
    modes=frozenset({"plan-exec"}), example=_EXAMPLE)
