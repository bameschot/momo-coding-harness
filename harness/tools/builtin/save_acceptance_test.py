"""save_acceptance_test: the verifier keeps the test it wrote for one of the
story's expectations (a story of a stories plan).  Answered by the harness
(StoryRunnerMixin), which checks the test and runs it."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("save_acceptance_test",
        "Save the black-box test you wrote for one expectation, once you have run it yourself. "
        "The harness checks that the file is in the acceptance folder and contains every expected "
        "value of the expectation's examples, runs the command, and records whether it passes. "
        "Saving again replaces the test.",
        {"expectation": {"type": "integer", "description": "The expectation's number in the first message (1, 2, …)"},
         "path":        {"type": "string", "description": "The test file, in tests/acceptance/"},
         "command":     {"type": "string",
                         "description": "The command that runs the test and exits non-zero when the "
                                        "expectation is not met, e.g. 'sh tests/acceptance/p1_s2_c1_look.sh'"}},
        ["expectation", "path", "command"]),
    harness=True, locked=True, order=730, group="ask & plan", modes=frozenset({"story-verifier"}),
    keeps_green=True,
    example={"expectation": 1, "path": "tests/acceptance/p1_s2_c1_look.sh",
             "command": "sh tests/acceptance/p1_s2_c1_look.sh"})
