"""finish_verification: the verifier's verdict on every expectation of the
story (a story of a stories plan).  Answered by the harness
(StoryRunnerMixin), which holds each verdict against the saved test's run."""
from __future__ import annotations

from ..registry import Tool, tool_schema

TOOL = Tool(
    tool_schema("finish_verification",
        "End the verification with a verdict for every expectation: 'pass' (its saved test "
        "passed), 'fail' (its saved test fails because the product does not behave as expected), "
        "'not_yet' (the interface it acts through does not exist yet, e.g. the server is not "
        "written: it is verified at the end of the phase) or 'unclear' (it cannot be checked as "
        "written). This ends your work: other tool calls after it in the same turn are not run.",
        {"results": {"type": "array",
                     "items": {"type": "object",
                               "properties": {
                                   "expectation": {"type": "integer", "description": "The expectation's number"},
                                   "verdict":     {"type": "string", "enum": ["pass", "fail", "not_yet", "unclear"]},
                                   "evidence":    {"type": "string",
                                                   "description": "What the product did (for a fail), what is "
                                                                  "missing (for not_yet), or why it cannot be "
                                                                  "checked (for unclear)"}},
                               "required": ["expectation", "verdict"]},
                     "description": "One entry per expectation"}},
        ["results"]),
    harness=True, locked=True, order=735, group="ask & plan", modes=frozenset({"story-verifier"}),
    keeps_green=True,
    example={"results": [{"expectation": 1, "verdict": "pass"},
                         {"expectation": 2, "verdict": "fail",
                          "evidence": "`take rusty sword` answered \"There is no rusty here\""}]})
