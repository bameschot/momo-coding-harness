"""ask_choice: a multiple-choice question.  Answered by the harness loop
(Harness._ask_choice): the user picks an option, or adds information and the
harness has the model rewrite the question and options, then asks again."""
from __future__ import annotations

from ..registry import ALL_ROLES, Tool, tool_schema

TOOL = Tool(
    tool_schema("ask_choice",
        "Pause and ask the user a multiple-choice question mid-task. "
        "Use this instead of ask_user when the answer is one of a few distinct options "
        "(2-6) — e.g. which of several approaches, libraries or files. "
        "The user picks an option, or adds information, in which case the question is "
        "refined and asked again; you receive the chosen option and any context given. "
        "Do NOT use this for things discoverable by reading files.",
        {"question": {"type": "string"},
         "options": {"type": "array", "items": {"type": "string"},
                     "description": "2-6 short, distinct answers"}},
        ["question", "options"]),
    harness=True, order=411, group="ask & plan", modes=ALL_ROLES - {"story-writer"}, keeps_green=True,
    example={"question": "Which test framework should I use?",
             "options": ["pytest", "unittest"]})
