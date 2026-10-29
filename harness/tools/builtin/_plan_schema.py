"""The plan step schema shared by create_plan and revise_plan (not a tool:
the registry only registers modules that define TOOL or TOOLS)."""

STEP = {
    "type": "object",
    "properties": {
        "title":   {"type": "string", "description": "Short imperative summary, e.g. 'Add retry to fetch_page()'"},
        "details": {"type": "string", "description": "Exactly what to change: file, function/section, the change itself, and how to verify it"},
        "files":   {"type": "array", "items": {"type": "string"}, "description": "Files this step touches (workdir-relative)"},
    },
    "required": ["title", "details"],
}

# TDD plans: every step states the outcomes its tests will check, and the plan
# names the command that runs the whole suite.
STEP_TDD = {
    **STEP,
    "properties": {
        **STEP["properties"],
        "tests": {"type": "array", "items": {"type": "string"},
                  "description": "Expected outcomes this step's tests check, each concrete: "
                                 "'<input or action> → <expected result>'"},
    },
    "required": ["title", "details", "tests"],
}


def tdd_variant(ctx) -> str:
    """create_plan / revise_plan show their TDD schema in a test-driven plan."""
    return "tdd" if ctx.tdd else ""


# Stories plans: create_plan's steps are phases, each with what exists when it
# is done and the commands that close it.  Its stories come later, written per
# phase by the story writer (plan_stories, write_story, finish_stories) against
# the code as it is then.
PHASE = {
    "type": "object",
    "properties": {
        "title":   {"type": "string", "description": "Short name for the phase, e.g. 'Frame encoding module'"},
        "details": {"type": "string", "description": "What the phase covers: files, components, the design decisions it carries out"},
        "outcome": {"type": "string", "description": "What exists and works when the phase is done, seen from outside"},
        "checks":  {"type": "array", "items": {"type": "string"},
                    "description": "Commands that prove the phase is done and exit non-zero otherwise, e.g. "
                                   "'python3 -m unittest tests.test_frames'; the harness runs them before the next phase"},
        "files":   {"type": "array", "items": {"type": "string"}, "description": "Files this phase creates or changes (workdir-relative)"},
    },
    "required": ["title", "details", "outcome", "checks"],
}

CRITERION = {
    "type": "object",
    "properties": {
        "text":  {"type": "string", "description": "What must be true, concretely: '<input or action> → <expected result>'"},
        "check": {"type": "string", "description": "A command that proves it and exits non-zero otherwise; the harness "
                                                   "runs it. Leave it out only for what no command can show."},
    },
    "required": ["text"],
}

STORY = {
    "type": "object",
    "properties": {
        "id":                {"type": "string", "description": "S1, S2, … in this phase"},
        "title":             {"type": "string", "description": "Short imperative summary naming the definition, e.g. 'Add parse_header() to src/parser.py'"},
        "description":       {"type": "string", "description": "What to build and why, in the context of the code as it is now"},
        "technical_details": {"type": "string", "description": "Exact signatures, data formats, file and function names, the existing code to call"},
        "hints":             {"type": "array", "items": {"type": "string"}, "description": "Existing helpers to reuse, where to look, how to verify"},
        "pitfalls":          {"type": "array", "items": {"type": "string"}, "description": "Concrete traps: format details, off-by-ones, ordering, what not to change"},
        "files":             {"type": "array", "items": {"type": "string"}, "description": "Files this story changes (workdir-relative)"},
        "depends_on":        {"type": "array", "items": {"type": "string"},
                              "description": "Ids of stories that must be done first: S<n> in this phase, P<n>.S<m> for an earlier phase's"},
        "provides":          {"type": "array", "items": {"type": "string"},
                              "description": "What later stories may rely on once this is done: exact signatures and behaviour"},
        "acceptance":        {"type": "array", "items": CRITERION, "description": "When the story is done; at least one, with check commands wherever possible"},
    },
    "required": ["id", "title", "description", "technical_details", "acceptance"],
}

# plan_stories: one line per story, the split and the order only; the details
# come one story at a time with write_story.
OUTLINE_ITEM = {
    "type": "object",
    "properties": {
        "id":         {"type": "string", "description": "S1, S2, … in this phase"},
        "title":      {"type": "string", "description": "Short imperative summary naming the definition"},
        "depends_on": {"type": "array", "items": {"type": "string"},
                       "description": "Ids of stories that must be done first: S<n> in this phase, P<n>.S<m> for an earlier phase's"},
        "provides":   {"type": "string", "description": "One line: what later stories may rely on"},
    },
    "required": ["id", "title"],
}


def plan_variant(ctx) -> str:
    """create_plan's schema for the plan kind: test-driven, stories, or the plain one."""
    return "tdd" if ctx.tdd else "stories" if ctx.stories else ""
