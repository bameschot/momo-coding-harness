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
# An expected behaviour, as the user meets it, with examples: what is done
# through the product's interface and what must come out.  No code: an
# independent verifier writes and runs the test after the story is built.
EXAMPLE = {
    "type": "object",
    "properties": {
        "given":  {"type": "string", "description": "What is done, through the product's interface: a command typed, "
                                                    "a request sent, a file given, a call made"},
        "expect": {"type": "string", "description": "What must come out, with the exact values: `quoted` text, codes, "
                                                    "counts, fields, hashes"},
    },
    "required": ["given", "expect"],
}

CRITERION = {
    "type": "object",
    "properties": {
        "behaviour": {"type": "string", "description": "The expected behaviour in plain words, as the user sees it"},
        "examples":  {"type": "array", "items": EXAMPLE,
                      "description": "At least one concrete example; never code, classes or methods"},
        "covers":    {"type": "array", "items": {"type": "string"},
                      "description": "The plan's requirement ids these examples prove, e.g. ['R3']"},
    },
    "required": ["behaviour", "examples"],
}

PHASE = {
    "type": "object",
    "properties": {
        "title":      {"type": "string", "description": "Short name for the phase, e.g. 'Look around the world'"},
        "details":    {"type": "string", "description": "What the phase covers: files, components, the design decisions it carries out"},
        "outcome":    {"type": "string", "description": "What exists and works when the phase is done, seen from outside"},
        "acceptance": {"type": "array", "items": CRITERION,
                       "description": "The behaviour that closes the phase, observed through the product's "
                                      "interfaces; a verifier tests it before the next phase"},
        "files":      {"type": "array", "items": {"type": "string"}, "description": "Files this phase creates or changes (workdir-relative)"},
    },
    "required": ["title", "details", "outcome", "acceptance"],
}

# Stories plans: the user's request, one requirement per line, each delivered
# by a phase.  Story criteria name the ones they prove (CRITERION.covers).
REQUIREMENT = {
    "type": "object",
    "properties": {
        "id":    {"type": "string", "description": "R1, R2, …"},
        "text":  {"type": "string", "description": "One thing the user asked for: a command, endpoint or behaviour"},
        "phase": {"type": "integer", "description": "The phase (1-based) that delivers it"},
    },
    "required": ["id", "text", "phase"],
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
        "acceptance":        {"type": "array", "items": CRITERION,
                              "description": "When the story is done: expected behaviour with examples, at least one"},
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
