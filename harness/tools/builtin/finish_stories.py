"""finish_stories: submit the phase's written stories (the story writer of a
stories plan).  Answered by the harness (StoryRunnerMixin), which validates the
set, writes it to the plan's stories folder and asks the user to approve it."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import PHASE

TOOL = Tool(
    tool_schema("finish_stories",
        "Submit the phase's stories once every story of the outline is written with write_story. "
        "The harness validates the set and returns what is wrong; fix it with write_story (or the "
        "outline with plan_stories), then call finish_stories again. The harness runs a story only "
        "when its dependencies are done, and accepts it only when its acceptance checks pass.",
        {"phase_revision": {"type": "object",
                            "properties": {
                                "reason": {"type": "string", "description": "What in the code shows the later phases are wrong"},
                                "phases": {"type": "array", "items": PHASE,
                                           "description": "The complete new list of phases after this one"}},
                            "required": ["reason", "phases"],
                            "description": "Optional: a change to the LATER phases, shown to the user with the stories "
                                           "and applied only if they approve it"}},
        []),
    harness=True, locked=True, order=722, group="ask & plan", modes=frozenset({"story-writer"}),
    example={})
