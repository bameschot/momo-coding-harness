"""create_plan: submit the plan (plan mode).  Answered by the harness
(PlanRunnerMixin), which writes .momo/plan.md and asks the user to approve it."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import STEP, STEP_TDD, tdd_variant

_TDD = tool_schema("create_plan",
    "Submit the test-driven implementation plan once investigation is complete. The harness "
    "writes it to a Markdown file and asks the user to approve it. On approval it first has you "
    "write the tests for every step and for 'acceptance', all skipped; then for each step you "
    "switch on that step's tests and implement it, and the harness runs test_command and only "
    "moves on when it passes. The harness adds the test-writing and acceptance stages itself, so "
    "do not add a step that writes all tests or a final step that only runs the tests. Each step "
    "must be small, ordered, name the file and function/section it changes, and list in 'tests' "
    "the outcomes that prove it works. If the user asks for changes, call create_plan again with "
    "the complete new plan.",
    {"title":        {"type": "string", "description": "Short name for the feature or fix"},
     "goal":         {"type": "string", "description": "What the change achieves and why, including key findings from the investigation"},
     "test_command": {"type": "string", "description": "Command that runs the project's whole test suite and exits non-zero when a test fails, e.g. 'python -m pytest -q'"},
     "step_test_command": {"type": "string",
                           "description": "Optional, for a slow suite: command that runs only some test files, "
                                          "with {files} (their paths), {dirs} (their directories as ./dir) or "
                                          "{stems} (file names without extension), e.g. 'python -m pytest -q {files}', "
                                          "'python -m unittest {files}', 'npx jest {files}', 'go test {dirs}'. "
                                          "Leave it out when the whole suite is fast."},
     "acceptance":   {"type": "array", "items": {"type": "string"},
                      "description": "Expected outcomes of the whole change, checked by end-to-end tests after the last step"},
     "steps":        {"type": "array", "items": STEP_TDD, "description": "Ordered implementation steps"}},
    ["title", "goal", "test_command", "acceptance", "steps"])

_EXAMPLE = {"title": "Fix off-by-one in paginate()",
            "goal": "Last page is dropped because paginate() uses < instead of <=.",
            "steps": [
                {"title": "Fix loop bound in paginate()",
                 "details": "In src/pager.py paginate(), change `while page < total` to `while page <= total`.",
                 "files": ["src/pager.py"]},
                {"title": "Add regression test and run the suite",
                 "details": "Add test_last_page_included to tests/test_pager.py, then run python -m pytest.",
                 "files": ["tests/test_pager.py"]}]}

_EXAMPLE_TDD = {
    "title": "Fix off-by-one in paginate()",
    "goal": "Last page is dropped because paginate() uses < instead of <=.",
    "test_command": "python -m pytest -q",
    "step_test_command": "python -m pytest -q {files}",
    "acceptance": ["paginating 11 items by 5 → pages of 5, 5 and 1, nothing lost"],
    "steps": [{"title": "Fix loop bound in paginate()",
               "details": "In src/pager.py paginate(), change `while page < total` to `while page <= total`.",
               "files": ["src/pager.py"],
               "tests": ["paginate(range(10), size=5) → 2 pages",
                         "paginate(range(11), size=5) → last page is [10]"]}]}

TOOL = Tool(
    tool_schema("create_plan",
        "Submit the implementation plan once investigation is complete. The harness writes it "
        "to a Markdown file, shows it to the user and asks for approval; on approval it executes "
        "the steps one at a time with the full coding tool set. Each step must be small, ordered, "
        "and independently verifiable, and name the file and function/section it changes. The last "
        "step should run the project's tests (or the narrowest real check) to verify the whole change. "
        "If the user asks for changes, revise and call create_plan again with the complete new plan.",
        {"title": {"type": "string", "description": "Short name for the feature or fix"},
         "goal":  {"type": "string", "description": "What the change achieves and why, including key findings from the investigation"},
         "steps": {"type": "array", "items": STEP, "description": "Ordered implementation steps"}},
        ["title", "goal", "steps"]),
    variants={"tdd": _TDD}, examples={"tdd": _EXAMPLE_TDD}, select=tdd_variant,
    harness=True, locked=True, order=600, group="ask & plan", modes=frozenset({"plan"}),
    example=_EXAMPLE)
