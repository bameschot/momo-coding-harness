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
