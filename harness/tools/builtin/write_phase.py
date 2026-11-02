"""write_phase: one phase of a stories plan in full, after create_plan's
outline (plan mode).  Answered by the harness (PlanRunnerMixin), which checks
it and keeps it in the plan's draft."""
from __future__ import annotations

from ..registry import Tool, tool_schema
from ._plan_schema import CRITERION

TOOL = Tool(
    tool_schema("write_phase",
        "Write ONE phase of the outlined plan in full — one call per turn. The harness checks it "
        "and keeps it; if something is wrong it says what, and you send that phase again. Once "
        "every phase is written, call finish_plan.",
        {"phase":      {"type": "integer", "description": "The phase's number in the outline (1, 2, …)"},
         "title":      {"type": "string", "description": "The phase's name (left out: the outline's)"},
         "details":    {"type": "string", "description": "What the phase covers: files, components, the design decisions it carries out"},
         "outcome":    {"type": "string", "description": "What exists and works when the phase is done, seen from outside"},
         "acceptance": {"type": "array", "items": CRITERION,
                        "description": "The behaviour that closes the phase, observed through the product's "
                                       "interfaces; a verifier tests it before the next phase"},
         "files":      {"type": "array", "items": {"type": "string"}, "description": "Files this phase creates or changes"}},
        ["phase", "details", "outcome", "acceptance"]),
    harness=True, locked=True, order=602, group="ask & plan", modes=frozenset({"plan"}),
    needs_stories=True,
    example={"phase": 1, "title": "Look around", "details": "server.py with POST /api/command and look",
             "outcome": "a player can look around through the API", "files": ["server.py"],
             "acceptance": [{"behaviour": "look describes the room", "covers": ["R1"],
                             "examples": [{"given": "POST /api/command {\"name\": \"Ann\", \"command\": \"look\"}",
                                           "expect": "status `200`, the reply contains `Great Hall`"}]}]})
