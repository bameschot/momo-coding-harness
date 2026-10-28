"""write_stories: submit a phase's stories (the story writer of a stories plan).
Answered by the harness (PlanRunnerMixin), which validates the set, writes it
to the plan's stories folder and asks the user to approve it."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import PHASE, STORY

TOOL = Tool(
    tool_schema("write_stories",
        "Submit the stories for the current phase once you know the code they build on. Each "
        "story is one small change (about one function or class, with its tests), detailed enough "
        "for a small model that sees only this story and what its dependencies delivered. Order "
        "them with depends_on; the harness runs a story only when its dependencies are done, and "
        "accepts it only when its acceptance checks pass. The harness validates the set and returns "
        "what is wrong; then call write_stories again with the complete corrected set. With "
        "'replaces', the stories replace the named failed story and its pending dependents instead "
        "of the whole phase.",
        {"stories":        {"type": "array", "items": STORY, "description": "The phase's stories (or the replacements)"},
         "replaces":       {"type": "array", "items": {"type": "string"},
                            "description": "Only when asked to re-split: the ids of the stories these replace"},
         "phase_revision": {"type": "object",
                            "properties": {
                                "reason": {"type": "string", "description": "What in the code shows the later phases are wrong"},
                                "phases": {"type": "array", "items": PHASE,
                                           "description": "The complete new list of phases after this one"}},
                            "required": ["reason", "phases"],
                            "description": "Optional: a change to the LATER phases, shown to the user with the stories "
                                           "and applied only if they approve it"}},
        ["stories"]),
    harness=True, locked=True, order=720, group="ask & plan", modes=frozenset({"story-writer"}),
    example={"stories": [
        {"id": "S1", "title": "Add paginate() to src/pager.py",
         "description": "Split a sequence into pages; the search endpoint (S2) pages results with it.",
         "technical_details": "def paginate(items: Sequence[T], size: int) -> list[list[T]]; "
                              "size < 1 raises ValueError.",
         "hints": ["itertools.batched does the splitting on Python 3.12+"],
         "pitfalls": ["the last page may be shorter; it must not be dropped"],
         "files": ["src/pager.py", "tests/test_pager.py"],
         "provides": ["paginate(items, size) -> list[list] in src/pager.py"],
         "acceptance": [{"text": "paginate(range(11), 5) → pages of 5, 5 and 1",
                         "check": "python -m pytest -q tests/test_pager.py"}]},
        {"id": "S2", "title": "Add GET /search to src/api.py", "depends_on": ["S1"],
         "description": "Serve Index.query() results a page at a time.",
         "technical_details": "Query params q (required) and page (1-based, default 1).",
         "acceptance": [{"text": "GET /search?q=a&page=2 → the second page as JSON",
                         "check": "python -m pytest -q tests/test_api.py"}]}]})
