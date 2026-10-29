"""System prompts: the role files, the per-mode tool sets, and the blocks the
harness appends (code index rules, plan execution rules)."""
from __future__ import annotations

import platform
import re
import shutil
from datetime import date
from pathlib import Path

from . import code_nav
from .momo_dir import SCRATCH_REL
from .tools import ROLES

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

def _story_writer_prompt(workdir: str) -> str:
    raw = _load_role("story-writer")
    if raw:
        return raw.replace("{workdir}", workdir)
    return (
        "You are the story writer of a phased plan. Investigate the code the current phase "
        "builds on with the read tools, outline small, dependency-ordered stories with "
        "plan_stories, then write them one per turn with write_story, each with exact technical "
        "details and acceptance criteria with check commands, and call finish_stories. "
        f"Do not edit files. Working directory: {workdir}"
    )


# The planner role's closing-verification rule, which a TDD plan replaces: its
# acceptance stage runs the suite, and with the rule in place the 9B added a
# "run the full test suite" step in 3 of 3 runs.
_RX_VERIFIED_RULE = re.compile(r"^6\. \*\*Verified at the end\*\*.*?(?=^7\. )", re.M | re.S)


def planner_for_tdd(text: str) -> str:
    """The planner role with rule 6 rewritten for a TDD plan (unchanged when the
    rule is not there, e.g. in an edited role file)."""
    return _RX_VERIFIED_RULE.sub(
        "6. **Verified by the harness** — the acceptance stage runs the whole suite; do not add "
        "a step for it.\n", text, count=1)


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
    "story-writer": "Trace what the phase builds on by structure: code_outline the files it "
                    "touches, read_symbol the definitions stories will call, find_references "
                    "for anything a story changes.",
    "story-exec": "Start with read_symbol of the definitions the story names; before changing "
                  "a definition, find_references.",
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
                 "not list_directory + read_file; before changing a definition, index_callers; "
                 "before running tests, index_changes.",
    "plan-exec": "Start each step with index_search / index_file for what it touches; before "
                 "changing a definition, index_callers; before running tests, index_changes.",
    "story-writer": "Investigate with index_map, index_search, index_file and index_callers — "
                    "not list_directory + read_file or grep; read_symbol reads one definition.",
    "story-exec": "Start with index_search / read_symbol for the definitions the story names; "
                  "before changing a definition, index_callers.",
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
                       "file type, or for a file the index skips (git-ignored or filtered out, "
                       "binary, over 2 MB).")
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
    if has("edit_file") and (has("replace_symbol") or has("edit_files")):
        parts = ["`edit_file` changes a few lines"]
        if has("edit_files"):
            parts.append("`edit_files` makes several changes — in any files — in ONE call (every "
                         "caller after a signature change, two changes in one function)")
        if has("replace_symbol"):
            parts.append("`replace_symbol` rewrites a whole definition by name")
        bullets.append("**Editing code:** " + "; ".join(parts) + ". They show the changed lines "
                       "with their new line numbers — no need to read the files again to check.")
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


# ── cavecat ──────────────────────────────────────────────────────────────────
# /cavecat: caveman-style compression of what the model writes for itself and
# the harness, and a short reply to the user.  A local model's output tokens
# are wall-clock time, and plan text is in the prompt on every execution turn.
# The harness side (run_store.compact on command output, a later tool-only
# nudge) follows the same switch.

CAVECAT_LEVELS = ("off", "on")
# Earlier levels.  "lite" also trimmed thinking and made a 9B loop on hard
# edits; "text" and "full" differed only by the short reply once the thinking
# rule was gone, and the reply is the one large text bucket left to compress.
CAVECAT_ALIASES = {"lite": "on", "text": "on", "full": "on"}

# Thinking is left alone.  "Conclusions only" made a 9B verify by tool call
# instead (more edits, reruns and scratch scripts); "cut the narration, keep the
# reasoning" saved nothing measurable and the 9B's thinking ignored it.
# The reply stays readable sentences, not caveman.
_CAVECAT_REPLY = ("**Your reply:** short. Lead with the result, then what changed (file + change) "
                  "and anything the user must decide. No preamble, step-by-step recap or closing "
                  "offer. Plain sentences, still readable.")
# caveman's "no chatter between tool calls": the text alongside a call is
# output the user mostly never reads, and the reply says what was found.
_CAVECAT_BETWEEN = ("**Between tool calls:** no narration. Call the next tool without "
                    "announcing it; what you found goes in your reply at the end.")
_CAVECAT_WRITE = ("telegraphic: verb + file + change. Not \"We should add retry logic to the "
                  "fetch function so that…\" but \"Add retry to fetch() in net.py: 3 tries, "
                  "1s backoff.\"")
_CAVECAT_TEXT = {
    "design":    "The Markdown specs you write and ask_user questions",
    "chat":      "ask_user questions",
    "plan":      "create_plan steps and ask_user questions",
    "coding":    "ask_user questions and commit messages",
    "plan-exec": "complete_step summaries, revise_plan steps, ask_user questions and commit "
                 "messages",
    "story-exec": "complete_story summaries, ask_user questions and commit messages",
}


def _cavecat_rules(level: str, role: str) -> str | None:
    """The "Cavecat" section, or None when it is off or the role is momo."""
    if level != "on" or role not in _CAVECAT_TEXT:
        return None
    write = ("terse bullets, one fact or decision each, no prose padding."
             if role == "design" else _CAVECAT_WRITE)
    bullets = [_CAVECAT_REPLY, _CAVECAT_BETWEEN,
               f"**{_CAVECAT_TEXT[role]}:** {write}",
               "**Never compress** code, file contents you write"
               + (" other than specs" if role == "design" else "")
               + ", paths, symbols, exact error text, numbers or test names."]
    return ("## Cavecat\n\nCompress what you write for yourself and the harness, and keep "
            "your reply short:\n\n" + "\n".join(f"- {b}" for b in bullets))


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


def _python_line() -> str | None:
    """How to run Python here, when it is not the obvious `python`: in evals
    a fifth of all commands were `python: command not found` on a Mac that
    only has python3."""
    if shutil.which("python") or not shutil.which("python3"):
        return None
    return "run it as `python3` (there is no `python` command)"


def _environment(workdir: Path, tool_names: set[str], net_access: str) -> str:
    lines = [f"- Working directory: `{workdir}`. Every path is relative to it; a path that "
             "escapes it with `..` is rejected.",
             f"- Today: {date.today().isoformat()}"]
    if "run_command" in tool_names:
        lines.append(f"- System: {_system_line()}")
        if (py := _python_line()):
            lines.append(f"- Python: {py}")
        lines.append(f"- Git: {_git_state(workdir)}")
    if "scratch_write" in tool_names:
        lines.append(f"- Scratchpad: `{SCRATCH_REL}/` — your folder for temporary scripts, notes and "
                     "intermediate results; write them with scratch_write, never into the project "
                     "tree. Git-ignored, not indexed, skipped by project-wide searches, kept "
                     "between sessions."
                     + (" In run_command it is `$MOMO_SCRATCH`." if "run_command" in tool_names else ""))
    net_tools = [n for n in ("fetch_url", "web_search") if n in tool_names]
    if net_access != "off" and net_tools:
        lines.append(f"- Internet: on — use {' / '.join(net_tools)} for documentation, APIs and "
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
- If you need the user's input, use ask_user (or ask_choice for a few distinct options). A plain-text reply without a tool call also ends the step.
- If you discover the remaining plan is wrong or incomplete, call revise_plan with the complete
  corrected list of remaining steps (every step you omit is dropped). Never deviate silently.
- If the step turns out to be done already, verify that and call complete_step."""

# ── stories plans ────────────────────────────────────────────────────────────
# _PLAN_STORIES_RULES is appended to the planner role while the next plan is a
# stories plan; _STORY_EXECUTION_RULES follows the coder role while one story
# runs, with the story itself in the first user message (a fresh context).

_PLAN_STORIES_RULES = """## This plan is phased (stories)

The user chose a stories plan. After approval the harness runs it one phase at a time: when a
phase starts, a story writer looks at the code as it is then and breaks the phase into small
stories, a model implements them one by one, and the harness runs the phase's checks before the
next phase begins. Later phases are broken down only after earlier ones are built, so they can
build on what was actually delivered.

For this plan, these replace rules 1, 3 and 6 of "What makes a good plan" above:
- **Each step is a phase**: a coherent milestone (a module, a feature, a layer), usually 2-7 of
  them. Name the files and components it covers and the design decisions it carries out, but
  leave the step-by-step detail to the stories.
- **Each phase has an `outcome`**: what exists and works when it is done, seen from outside.
- **Each phase has `checks`**: commands that prove the outcome and exit non-zero otherwise (a test
  module, a smoke script). The harness runs them, so a phase that creates tests should check them.
  Do not add a phase that only runs the tests."""

_STORY_EXECUTION_RULES = """## Implementing one story

You are implementing one story of a phased plan the user approved. The story is in the first
message: what to build, its technical details, hints, pitfalls and acceptance criteria, and what
the stories it depends on delivered. That is everything you get — earlier work on the plan is not
in this conversation, but it is in the code.

These rules replace "To finish" in "How the loop works" above: a story ends with complete_story.

- Implement only this story. Other stories of the phase come later, each in its own conversation.
- Build on what the dependencies delivered, with exactly the signatures they list; read their
  code when you need more. Change files outside the story's files only when the story cannot be
  done otherwise, and say why in your summary.
- What the story **provides** is a contract later stories are written against: match it exactly,
  or say in the summary how and why you differ.
- Run the acceptance checks yourself before calling complete_story. The harness runs them again
  and refuses the story while one fails.
- If you need the user's input, use ask_user. A plain-text reply without a tool call also ends
  the story, as not done."""

# ── TDD plans ────────────────────────────────────────────────────────────────
# Appended to the planner role while the next plan is test-driven, and after
# _PLAN_EXECUTION_RULES while a TDD plan runs.  Neither may name a tool its
# phase does not offer (test_prompt_names_only_offered_tools).

_PLAN_TDD_RULES = """## This plan is test-driven (TDD)

The user chose a TDD plan. After approval the harness runs it in stages:

1. **Write the tests.** Tests for every step and for the acceptance outcomes are written first,
   each one skipped with the framework's own skip marker and its step's tag in the skip reason.
2. **One stage per step.** The step's tests are switched on, the step is implemented, and the
   harness runs `test_command`; the plan only moves on when it passes.
3. **Acceptance.** The end-to-end tests are switched on and the whole suite must pass.

The harness adds stages 1 and 3 itself. Your plan needs, beyond the usual:
- `test_command`: the command that runs the project's WHOLE test suite and exits non-zero when a
  test fails. Find it during investigation and run it once to see that it works today.
- `tests` on every step: the concrete outcomes that prove the step works, each written as
  "<input or action> → <expected result>". These become the step's tests.
- `acceptance`: the outcomes of the whole change, seen from the user's side.
- If the whole suite is slow (more than a few seconds), also `step_test_command`: the same
  runner for only some test files, with `{files}` where their paths go.
- Note in `goal` how this project marks a test as skipped with a marker on its own line above
  the test (e.g. `@unittest.skip("…")`, `@pytest.mark.skip(reason="…")`, `@Disabled("…")`), so
  the test-writing stage uses the right one.
Do not add a step that writes all the tests, or a final step that only runs them."""

_PLAN_TDD_EXECUTION_RULES = """## This plan is test-driven (TDD)

Pending tests are tests whose step is not implemented yet. They must not break the build, so
each complete test is skipped with the test framework's own marker, on its own line directly
above the test, and the skip reason carries its tag (shown in [brackets] in the plan state
below), for example:
- Python unittest: `@unittest.skip("tdd-step-2")` · pytest: `@pytest.mark.skip(reason="tdd-step-2")`
- JUnit: `@Disabled("tdd-step-2")` · Rust: `#[ignore = "tdd-step-2"]`
- Go: `t.Skip("tdd-step-2")` as the first line of the complete test body
- JavaScript/TypeScript (Jest, Vitest, Mocha): `it.skip("… (tdd-step-2)", …)`
Write the tag only in skip markers, never anywhere else: the harness finds pending tests by it.
A test whose body is only the skip marker checks nothing; the harness refuses it.

Stages:
- **Write tests** (first stage): write the complete tests for every step and for acceptance —
  setup, the call, the assertions — each skipped with its tag. Change no production code, except
  minimal stubs (a signature that raises "not implemented") where a skipped test would otherwise
  not compile or import.
- **A step**: the harness removes the skip markers carrying the step's tag and runs the tests, so
  the step starts from their failures. Implement the step until they pass.
- **Acceptance** (last stage): the harness switches the `tdd-acceptance` tests on the same way;
  make the whole suite pass.

When you call complete_step the harness checks the markers and runs the plan's test command, so
you do not need to run the whole suite yourself first. If anything fails, complete_step is
refused with the reason and the test output: fix it and call complete_step again. Never delete,
skip or weaken a test to get it green. If an expectation itself is wrong, ask the user with
ask_user or correct the plan with revise_plan."""

def _momo_prompt() -> str:
    return _load_role("momo") or (
        "You are Momo, a small enthusiastic black cat who lives in the coding harness. "
        "Keep the user company, celebrate their wins, and help out when they ask. "
        "Change files or run commands only when the user asks; when your curiosity takes "
        "over, just look around with the read tools. Be warm, curious, and easily distracted."
    )

_ROLE_LOADERS = {
    "design":  lambda wd: _design_prompt(),
    "coding":  lambda wd: _coding_prompt(wd),
    "chat":    lambda wd: _chat_prompt(),
    "momo":    lambda wd: _momo_prompt(),
    "plan":    lambda wd: _planner_prompt(wd),
}

# The modes in the order the frontends list and cycle them (Shift+Tab, the web picker).
# Each is also a tool role (a tool's Tool.modes says which roles offer it).
MODES = ("design", "chat", "plan", "coding", "momo")
assert set(MODES) <= set(ROLES) and set(MODES) == set(_ROLE_LOADERS), \
    "every mode needs a role prompt and must be a tool role"
