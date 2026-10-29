"""write_story: write one story of the outline in full (the story writer of a
stories plan).  Answered by the harness (StoryRunnerMixin), which checks the
story, keeps it as a draft and names the next one."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import STORY

TOOL = Tool(
    tool_schema("write_story",
        "Write ONE story of the outline in full, detailed enough for a small model that sees only "
        "this story and what its dependencies delivered. One call per turn: work out only this "
        "story. Sending an id again replaces that draft, so rewrite an earlier story when this one "
        "shows it must change; an id not in the outline is added to it. The harness checks the "
        "story and names the next one.",
        {"story": {**STORY, "description": "The story"}},
        ["story"]),
    harness=True, locked=True, order=720, group="ask & plan", modes=frozenset({"story-writer"}),
    example={"story": {
        "id": "S1", "title": "Add paginate() to src/pager.py",
        "description": "Split a sequence into pages; the search endpoint (S2) pages results with it.",
        "technical_details": "def paginate(items: Sequence[T], size: int) -> list[list[T]]; "
                             "size < 1 raises ValueError.",
        "hints": ["itertools.batched does the splitting on Python 3.12+"],
        "pitfalls": ["the last page may be shorter; it must not be dropped"],
        "files": ["src/pager.py", "tests/test_pager.py"],
        "provides": ["paginate(items, size) -> list[list] in src/pager.py"],
        "acceptance": [{"text": "paginate(range(11), 5) → pages of 5, 5 and 1",
                        "check": "python -m pytest -q tests/test_pager.py"}]}})
