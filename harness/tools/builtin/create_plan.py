"""create_plan: submit the plan (plan mode).  Answered by the harness
(PlanRunnerMixin), which writes .momo/plan.md and asks the user to approve it."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import PHASE_OUTLINE, REQUIREMENT, STEP, STEP_TDD, plan_variant

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

_STORIES = tool_schema("create_plan",
    "Start the phased plan once investigation is complete: its frame (title, goal, interfaces, "
    "requirements) and an outline, one {title, outcome} line per phase. Then write each phase in "
    "full with write_phase, one per turn, and submit the plan with finish_plan; the harness then "
    "asks the user to approve it. On approval the phases run in order: when a phase starts, a "
    "story writer breaks it into small stories against the code as it is then, and the harness "
    "checks the phase's expected behaviour before the next phase starts. Call create_plan again "
    "to change the outline; the phases already written are kept.",
    {"title": {"type": "string", "description": "Short name for the feature or project"},
     "goal":  {"type": "string", "description": "What the change achieves and why, including key findings, decisions and the user's answers"},
     "interfaces": {"type": "array", "items": {"type": "string"},
                    "description": "How the product is used, one line per way in, e.g. "
                                   "\"CLI: `python3 tool.py <file>`\", \"Service: `python3 server.py`, HTTP on "
                                   "localhost:8000\", \"Library: `import pager`\", \"Output files in `build/`\""},
     "steps": {"type": "array", "items": PHASE_OUTLINE,
               "description": "The outline: one line per phase, in order (usually 2-7)"},
     "requirements": {"type": "array", "items": REQUIREMENT,
                      "description": "The user's request split into one requirement per line — every "
                                     "command, endpoint and behaviour they named, one per item of a "
                                     "list — each with the phase that delivers it"}},
    ["title", "goal", "interfaces", "steps", "requirements"])

_EXAMPLE_STORIES = {
    "title": "Paginated search API",
    "goal": "Add /search with paging; results come from the existing index in src/index.py.",
    "interfaces": ["Service: `python3 -m src.api`, HTTP on localhost:8080"],
    "steps": [{"title": "Search returns results", "outcome": "GET /search?q=a returns the matches as JSON"},
              {"title": "Pages of results", "outcome": "GET /search?q=a&page=2 returns the second page of 5"}],
    "requirements": [
        {"id": "R1", "text": "GET /search?q= returns matching results as JSON", "phase": 1},
        {"id": "R2", "text": "?page=N returns the Nth page of 5 results", "phase": 2},
        {"id": "R3", "text": "the last page holds the remainder, nothing lost", "phase": 2}]}

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
    variants={"tdd": _TDD, "stories": _STORIES},
    examples={"tdd": _EXAMPLE_TDD, "stories": _EXAMPLE_STORIES}, select=plan_variant,
    harness=True, locked=True, order=600, group="ask & plan", modes=frozenset({"plan"}),
    example=_EXAMPLE)
