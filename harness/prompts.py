"""System prompts: the role files, the per-mode tool sets, and the blocks the
harness appends (code index rules, plan execution rules)."""
from __future__ import annotations

from pathlib import Path

from . import code_nav
from .tools import ALL_TOOLS, CHAT_TOOLS, DESIGN_TOOLS, PLAN_INVESTIGATE_TOOLS

_ROLES_DIR = Path(__file__).parent.parent / "roles"


# ── system prompts ────────────────────────────────────────────────────────────

def _load_role(name: str) -> str:
    try:
        return (_ROLES_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def _design_prompt() -> str:
    return _load_role("designer") or (
        "You are a design assistant. When the user describes something to build, "
        "have a short conversation to clarify the design, then write it up as a "
        "Markdown spec using write_file when asked or when you have enough information."
    )

def _coding_prompt(workdir: str) -> str:
    raw = _load_role("coder")
    if raw:
        return raw.replace("{workdir}", workdir)
    return (
        "You are an expert software engineer. "
        "Use the provided tools to implement the user's request. "
        "Always read files before editing them. "
        "Use old_string/new_string for targeted edits. "
        "Keep changes minimal. "
        f"Working directory: {workdir}"
    )

def _chat_prompt() -> str:
    return _load_role("chat") or (
        "You are a knowledgeable assistant. Read code and documents when the user "
        "points at them, then answer questions and actively ask follow-up questions "
        "to deepen understanding. Never write or modify files."
    )

def _planner_prompt(workdir: str) -> str:
    raw = _load_role("planner")
    if raw:
        return raw.replace("{workdir}", workdir)
    return (
        "You are an expert software engineer in plan mode. Investigate the user's "
        "feature request or bug with the read tools and run_command, ask the user "
        "about genuine uncertainties with ask_user, then call create_plan with a "
        "specific, ordered, verifiable implementation plan. Do not edit files. "
        f"Working directory: {workdir}"
    )

# Appended to the coder prompt while an approved plan is being executed.  The
# live plan state is rendered after it on every step, so the model always knows
# which step it is on even after context compaction.
_INDEX_BANNER = (
    "**Code index is ON for this project.** Find code, text, files and usages with the index_* "
    "tools (index_search, index_text, index_callers, index_file, index_map) — not grep_files, "
    "find_files or run_command. The file tools are only a fallback; see \"Code index: ON\" at "
    "the end.")

# What to reach for first, per role.
_INDEX_FIRST = {
    "coding":    "Start a task with index_map (unfamiliar code) or index_search (a named thing), "
                 "not list_directory + read_file; before changing a definition, index_callers.",
    "plan-exec": "Start each step with index_search / index_file for what it touches; before "
                 "changing a definition, index_callers.",
    "plan":      "Investigate with index_map, index_search, index_file and index_callers — not "
                 "list_directory + read_file or grep; read_symbol reads one definition.",
    "momo":      "Start with index_map (unfamiliar code) or index_search (a named thing), not "
                 "list_directory + read_file.",
    "chat":      "Look things up with index_search (definitions, config keys, files) and "
                 "index_text (any text) before reading files.",
    "design":    "Look things up with index_search (definitions, config keys, CSS rules, files) "
                 "and index_text (any text) before reading files.",
}


def _index_rules(role: str) -> str:
    """The system-prompt section while the code index is on."""
    first = _INDEX_FIRST.get(role, _INDEX_FIRST["coding"])
    return f"""## Code index: ON — search with the index first

This project is indexed. For code and config, the index tools answer in one call
what grep and find need several for, and they are always current. {first}

| Instead of | Use |
|---|---|
| grep_files (text in files) | index_text |
| find_files (a file by name or glob) | index_search(query, kind="file"), or index_map |
| find_references / grep for usages | index_callers |
| file_dependencies | index_file |
| code_outline of a directory | index_map |
| find_symbol by name | index_search (find_symbol still answers "which definition is line N in") |

Indexed file types: {code_nav.SUPPORTED_EXTENSIONS} (definitions and uses), and every
other text file for index_text.

Use grep_files, grep_file or find_files only when an index tool found nothing, for a
regex, for a file type not listed above, or for a file the index does not cover
(git-ignored, binary, over 2 MB). Never grep or find with run_command.
Where the role instructions above say grep_files, find_files, find_references or
file_dependencies, read them as the index tools in this table."""


_PLAN_EXECUTION_RULES = """## Executing an approved plan

You are executing a plan the user approved. The harness drives it one step at a
time: each step arrives as a user message "Plan step i/N". The step marked ▶ below
is the current one.

- Work only on the current step. Do not start later steps — the harness gives you each one in
  turn — and do not redo finished ones.
- Apply the Workflow above to the step: read the files it touches, make the change, verify it.
- When the step is complete and verified, call complete_step with a short summary. That ends the
  step; do not put other tool calls after it.
- If you need the user's input, use ask_user. A plain-text reply without a tool call also ends the step.
- If you discover the remaining plan is wrong or incomplete, call revise_plan with the complete
  corrected list of remaining steps (every step you omit is dropped). Never deviate silently.
- If the step turns out to be done already, verify that and call complete_step."""

def _momo_prompt() -> str:
    return _load_role("momo") or (
        "You are Momo, a small enthusiastic black cat who lives in the coding harness. "
        "Keep the user company, celebrate their wins, and help out when they ask. "
        "You have access to all tools — read, write, edit, run things when asked or when "
        "your curiosity takes over. Be warm, curious, and easily distracted."
    )

_ROLE_LOADERS = {
    "design":  lambda wd: _design_prompt(),
    "coding":  lambda wd: _coding_prompt(wd),
    "chat":    lambda wd: _chat_prompt(),
    "momo":    lambda wd: _momo_prompt(),
    "plan":    lambda wd: _planner_prompt(wd),
}

_MODE_TOOLS = {
    "design":  DESIGN_TOOLS,
    "coding":  ALL_TOOLS,
    "chat":    CHAT_TOOLS,
    "momo":    ALL_TOOLS,
    "plan":    PLAN_INVESTIGATE_TOOLS,
}
