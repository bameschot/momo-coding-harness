"""System prompts: the role files, the per-mode tool sets, and the blocks the
harness appends (code index rules, plan execution rules)."""
from __future__ import annotations

import platform
from datetime import date
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

def _index_banner(tool_names: set[str]) -> str:
    """First line of the prompt while the code index is on."""
    avoid = "grep_files, find_files" + (" or run_command" if "run_command" in tool_names else "")
    return ("**Code index is ON for this project.** Find code, text, files and usages with the "
            "index_* tools (index_search, index_text, index_callers, index_file, index_map) — "
            f"not {avoid}. The file tools are only a fallback; see \"Navigating code\" at the end.")

# ── navigating code ──────────────────────────────────────────────────────────
# One section, generated per role and tool set, instead of a paragraph copied
# into every role file: it only names tools the role actually has, and with the
# index on it teaches the index tools rather than contradicting the role text.

# What to reach for first, per role, without the index.
_NAV_FIRST = {
    "coding":    "Map an unfamiliar tree with code_outline on a directory; jump to a named "
                 "thing with find_symbol; before changing a definition, find_references.",
    "plan-exec": "Start each step with code_outline / read_symbol of what it touches; before "
                 "changing a definition, find_references.",
    "plan":      "Trace the code path the change touches by structure. For a rename, signature "
                 "change or removal, find_references lists every place your steps must cover.",
    "momo":      "Map an unfamiliar tree with code_outline on a directory; jump to a named "
                 "thing with find_symbol.",
    "chat":      "When the user points at code, outline it and read the definitions that "
                 "matter rather than whole files.",
    "design":    "Map an existing project with code_outline on a directory before you "
                 "interview, and read the definitions that matter rather than whole files.",
}

# What to reach for first, per role, with the index on.
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


_NO_SHELL_SEARCH = ("**Never explore with `run_command`** (`grep`, `find`, `sed`, `awk`, `head`, "
                    "`tail`, `wc`) — keep it for building, testing and running things.")


def _nav_rules(role: str, tool_names: set[str], index: bool) -> str:
    """The "Navigating code" section for this role and exactly these tools."""
    has = tool_names.__contains__
    out = []
    if index:
        out.append(f"## Navigating code — the code index is ON\n\nThis project is indexed. For "
                   "code and config, the index tools answer in one call what grep and find need "
                   "several for, and they are always current. "
                   + _INDEX_FIRST.get(role, _INDEX_FIRST["coding"]))
        out.append("| To find | Use |\n|---|---|\n"
                   "| a definition, config key, CSS rule or file by name | index_search "
                   "(kind=\"file\" for files) |\n"
                   "| any text | index_text |\n"
                   "| every caller or use of a name | index_callers |\n"
                   "| a file's imports and importers | index_file |\n"
                   "| a map of the project or a directory | index_map |\n"
                   "| which definition line N is in | find_symbol / read_symbol with the line |")
    else:
        head = "## Navigating code"
        if has("code_outline"):
            head += "\n\n" + _NAV_FIRST.get(role, _NAV_FIRST["coding"])
        out.append(head)
    bullets = []
    if has("code_outline") and has("read_symbol"):
        bullets.append("**Outline before you read.** `code_outline` on a source file costs about a "
                       "twentieth of reading it whole and lists every definition with its line "
                       "range; then `read_symbol` the one you need (a line number reads whatever "
                       "definition contains it — use that on a grep hit or a traceback line)."
                       + ("" if index else " `code_outline` on a DIRECTORY maps the whole tree in "
                          "one call.")
                       + " Read a whole source file only when it is short or you need all of it.")
    if not index and has("find_symbol"):
        bullets.append("`find_symbol` jumps to a definition (`name=\"*\"` with `kind=` lists them "
                       "all). Config keys in YAML/TOML/JSON are definitions too: `find_symbol` "
                       "finds `LOG_LEVEL` or `services.web.ports` by name, with its file and line.")
    if not index and has("find_references"):
        bullets.append("**Before renaming, changing a signature or deleting, `find_references`** "
                       "(`role=\"call\"` for call sites only): it lists every real use and skips "
                       "comments and strings."
                       + (" `file_dependencies` shows what a file imports and what imports it."
                          if has("file_dependencies") else ""))
    if has("code_outline") or index:
        bullets.append("**Trust these results.** Their lists are complete for the files they "
                       "cover; do not re-check them by reading the files they name.")
    if index:
        bullets.append(f"Indexed: {code_nav.SUPPORTED_EXTENSIONS} (definitions and uses), and "
                       "every other text file for index_text. Use grep_files, grep_file or "
                       "find_files only when an index tool found nothing, for a regex, for another "
                       "file type, or for a file the index skips (git-ignored, binary, over 2 MB).")
    elif has("code_outline"):
        bullets.append("The structure tools cover source files and config keys. For anything else "
                       "(Markdown, plain text, other languages) `read_file` a small file whole, or "
                       "`grep_file` / `grep_files` to locate the lines in a large one first.")
    else:
        bullets.append("Locate before you read: `grep_files` / `find_files` to find the file and "
                       "line, then `read_file` with `start_line`/`end_line` for a large file.")
    bullets.append("**Answer from what you found.** A search hit line often holds the answer "
                   "itself (a config value, a constant, a message); read the file only for context "
                   "you actually need. Once a search has answered the question, stop searching — "
                   "put any doubt (\"this is a test fixture\") in your answer instead of hunting "
                   "for a better match.")
    if has("run_command"):
        bullets.append(_NO_SHELL_SEARCH)
    return "\n\n".join(out) + "\n\n" + "\n".join(f"- {b}" for b in bullets)


# ── using the internet ───────────────────────────────────────────────────────
# Only while a net tool is offered.  The tools are additive (no habit to
# override, and curl is guarded in run_command), so this is about how to use
# them well, not which tool to pick.

_NET_FIRST = {
    "plan": "Verify any external library or API behaviour your steps rely on before you "
            "write them into the plan.",
}


def _net_rules(role: str, tool_names: set[str], source_names: set[str]) -> str | None:
    """The "Using the internet" section, or None when no net tool is offered."""
    fetch, search = "fetch_url" in tool_names, "web_search" in tool_names
    if not (fetch or search):
        return None
    look = " / ".join(n for n, on in (("web_search", search), ("fetch_url", fetch)) if on)
    bullets = [f"**Check, don't recall.** Your knowledge is out of date: never state a version, "
               f"API signature, flag or advisory from memory — look it up with {look}.",
               "**Project first.** What the project uses (lockfile, pom.xml, pyproject, "
               "package.json) is in the repo; read it before searching the web for \"latest\"."]
    if search:
        hint = ("General facts — a standard, an algorithm, a protocol, a term — you may answer "
                "from what you know, but look them up")
        if "wikipedia" in source_names:
            hint += " (web_search with source=\"wikipedia\")"
        bullets.append(hint + " when the topic is recent or ongoing, when you are not sure, or "
                       "when the user asks you to.")
    bullets.append("**Stop when answered.** One good source is enough; do not keep searching "
                   "to confirm it.")
    bullets.append("**Cite.** Name the URL each fact came from.")
    head = "## Using the internet"
    if role in _NET_FIRST:
        head += "\n\n" + _NET_FIRST[role]
    return head + "\n\n" + "\n".join(f"- {b}" for b in bullets)


# ── environment ──────────────────────────────────────────────────────────────

def _git_state(workdir: Path) -> str:
    """'a git repository, branch `x`' / 'not a git repository', from the files
    alone (no subprocess on every prompt build)."""
    for d in (workdir, *workdir.parents):
        git = d / ".git"
        if git.is_dir():
            try:
                head = (git / "HEAD").read_text(encoding="utf-8").strip()
            except OSError:
                return "a git repository"
            if head.startswith("ref: refs/heads/"):
                return f"a git repository, branch `{head[len('ref: refs/heads/'):]}`"
            return "a git repository (detached HEAD)"
        if git.is_file():                        # a worktree or submodule
            return "a git repository"
    return "not a git repository"


def _system_line() -> str:
    system = platform.system()
    if system == "Darwin":
        return (f"macOS {platform.mac_ver()[0]}".rstrip() + "; run_command uses /bin/sh with the "
                "BSD tools (`sed -i ''`, no GNU-only flags)")
    if system == "Linux":
        return "Linux; run_command uses /bin/sh with the GNU tools"
    return f"{system}; run_command uses the system shell"


def _environment(workdir: Path, tool_names: set[str], net_access: str) -> str:
    lines = [f"- Working directory: `{workdir}`. Every path is relative to it; a path that "
             "escapes it with `..` is rejected.",
             f"- Today: {date.today().isoformat()}"]
    if "run_command" in tool_names:
        lines.append(f"- System: {_system_line()}")
        lines.append(f"- Git: {_git_state(workdir)}")
    if net_access != "off" and "fetch_url" in tool_names:
        lines.append("- Internet: on — use fetch_url / web_search for documentation, APIs and "
                     "package versions" + (" (local network too)" if net_access == "local" else "")
                     + "; never curl or wget a page.")
    else:
        lines.append("- Internet: off — if a task needs the web, say so; do not try curl or wget.")
    return "## Environment\n\n" + "\n".join(lines)


# ── plan execution ───────────────────────────────────────────────────────────
# Appended to the coder prompt while an approved plan is being executed.  The
# live plan state is rendered after it on every step, so the model always knows
# which step it is on even after context compaction.


_PLAN_EXECUTION_RULES = """## Executing an approved plan

You are executing a plan the user approved. The harness drives it one step at a
time: each step arrives as a user message "Plan step i/N". The step marked ▶ below
is the current one.

These rules replace "To finish" in "How the loop works" above: a step ends with complete_step.

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

# The modes in the order the frontends list and cycle them (Shift+Tab, the web picker).
MODES = ("design", "chat", "plan", "coding", "momo")
assert set(MODES) == set(_MODE_TOOLS), "MODES and _MODE_TOOLS must name the same modes"
