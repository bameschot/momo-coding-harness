"""plan_stories: outline a phase's stories (the story writer of a stories plan).
Answered by the harness (StoryRunnerMixin), which checks the outline and keeps
it for write_story, so the split is decided before any story is written in
full."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import OUTLINE_ITEM

TOOL = Tool(
    tool_schema("plan_stories",
        "Outline the current phase's stories once you know the code they build on: one short line "
        "per story (id, title, depends_on, what it provides). Decide the split and the order here, "
        "not the details: those come one story at a time with write_story. Call it again to change "
        "the outline; drafts of stories left out are dropped.",
        {"outline": {"type": "array", "items": OUTLINE_ITEM,
                     "description": "The phase's stories in order (or, when asked to re-split or fix, only the new ones)"}},
        ["outline"]),
    harness=True, locked=True, order=718, group="ask & plan", modes=frozenset({"story-writer"}),
    example={"outline": [
        {"id": "S1", "title": "Add paginate() to src/pager.py",
         "provides": "paginate(items, size) -> list[list] in src/pager.py"},
        {"id": "S2", "title": "Add GET /search to src/api.py", "depends_on": ["S1"],
         "provides": "GET /search?q=&page= returning one page of results as JSON"}]})
