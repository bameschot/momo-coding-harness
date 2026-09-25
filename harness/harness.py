from __future__ import annotations

import ast
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import session as session_mod
from .companion import MAX_RECAP_LINES, fit_bubble
from .diff import build_diff_body
from .events import EventBus, BusyEvent, DeltaEvent, StreamEndEvent
from .llm import make_client
from .logger import Logger
from .plan import Plan, Step, PLAN_FILENAME, write_plan_file, read_plan_file, delete_plan_file
from .tools import (DESIGN_TOOLS, ALL_TOOLS, CHAT_TOOLS, NET_TOOLS,
                    PLAN_INVESTIGATE_TOOLS, PLAN_EXECUTE_TOOLS,
                    dispatch, render_tool_reference, with_index_tools)
from . import net as net_mod
from . import code_index, code_nav, ignore_rules

# Tools that mutate a file on disk — the harness snapshots the target before and
# after these run to build a DiffEvent for the TUI.  Keyed by the arg holding the
# affected path ("move_file" uses src/dst and is handled separately).
_MUTATING_TOOLS = {
    "edit_file", "append_to_file",
    "write_file", "delete_file", "move_file",
}

_ROLES_DIR  = Path(__file__).parent.parent / "roles"
_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# Project guide files for coding agents, read from the workdir root (matched
# case-insensitively) when /guides is on, in this order.
_GUIDE_FILES = ("AGENTS.md", "CLAUDE.md", "MOMO.md", "GEMINI.md",
                ".github/copilot-instructions.md", ".cursorrules")
_GUIDE_MAX_CHARS = 24_000   # all guides together; also capped at context_limit chars (~1/4)


# ── text tool-call recovery ───────────────────────────────────────────────────
# Static regexes for known tagged formats.  The tier-3 bare-JSON pattern is
# built dynamically inside _extract_text_tool_calls from the live tool set.

# Qwen3, Hermes 2/3, NousResearch — most common Ollama chat models
_RX_QWEN      = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>',         re.DOTALL)
# Functionary / older Hermes variants
_RX_FUNC      = re.compile(r'<functioncall>\s*(\{.*?\})\s*</functioncall>',   re.DOTALL | re.IGNORECASE)
_RX_FUNC2     = re.compile(r'<function_call>\s*(\{.*?\})\s*</function_call>', re.DOTALL | re.IGNORECASE)
# Phi-3 / Phi-4 — no closing tag, JSON follows the token directly
_RX_PHI       = re.compile(r'<\|tool_call\|>\s*(\{.*?\})',                    re.DOTALL)
# DeepSeek-V2/V3/R1 — tool name precedes the args JSON, separated by a special token
_RX_DEEPSEEK  = re.compile(
    r'<｜tool▁call▁begin｜>(.*?)<｜tool▁sep｜>(.*?)<｜tool▁call▁end｜>', re.DOTALL
)
# Mistral / Mixtral — JSON array prefixed by a literal tag
_RX_MISTRAL   = re.compile(r'\[TOOL_CALL\]\s*(\[.*?\])',                       re.DOTALL)
# Command-R / Cohere — text-based action format
_RX_COMMAND_R = re.compile(r'Action:\s*(\S+)\s*\nAction\s+Input:\s*(\{.*?\})', re.DOTALL)

_JSON_DECODER = json.JSONDecoder()


def _match_paren(s: str, i: int) -> int:
    """Given s[i] == '(', return the index of the matching ')', skipping over
    Python string literals (so parens/commas inside quotes are not counted).
    Returns -1 if unbalanced.  Used to carve a `name(...)` call out of free text."""
    depth = 0
    n = len(s)
    quote: str | None = None
    triple = False
    escaped = False
    while i < n:
        ch = s[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif triple:
                if s[i:i + 3] == quote * 3:
                    i += 2
                    quote = None
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            if s[i:i + 3] == ch * 3:
                quote, triple = ch, True
                i += 2
            else:
                quote, triple = ch, False
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _strip_text_tool_calls(text: str) -> str:
    """Remove text-based tool-call markup from an assistant message content string.

    When a model embeds its tool call in plain text (instead of via the native
    tool_calls API field), the harness extracts the call but the raw XML/tagged
    markup is still sitting in `content`.  Re-sending that markup to Ollama causes
    Qwen3's XML template engine to embed it verbatim inside its own XML output,
    producing malformed nesting and a 500 "XML syntax error: element <function>
    closed by </parameter>" on the next request.  Stripping before storage fixes this.
    """
    for rx in (_RX_QWEN, _RX_FUNC, _RX_FUNC2, _RX_MISTRAL):
        text = rx.sub("", text)
    text = _RX_PHI.sub("", text)
    text = _RX_DEEPSEEK.sub("", text)
    text = _RX_COMMAND_R.sub("", text)
    return text.strip()


def _extract_text_tool_calls(text: str, tools: list[dict]) -> list[dict]:
    """
    Recover tool calls embedded in plain text when the model bypassed the tool
    API.  Tries all known tagged/structured formats first, then falls back to a
    bare-JSON scan anchored to tool-name occurrences in the text.

    The bare-JSON pattern is built dynamically from `tools`, so adding a tool
    to tools.py automatically extends coverage without touching this function.

    Returns a list of {"name": str, "arguments": dict}.
    """
    known = {t["function"]["name"] for t in tools}
    results: list[dict] = []
    seen: set[str] = set()

    def _append(hit: dict) -> bool:
        """Dedup-check and append.  Returns True if the hit was new."""
        key = hit["name"] + json.dumps(hit["arguments"], sort_keys=True)
        if key in seen:
            return False
        seen.add(key)
        results.append(hit)
        return True

    def _accept_full(obj: object) -> dict | None:
        """
        Validate a {"name": ..., "arguments"|"parameters": ...} object.
        Requires the arguments/parameters key to be explicitly present so that
        an arbitrary JSON blob with a matching "name" field is not mistaken for
        a tool call.  Does not touch `seen` — dedup is the caller's job.
        """
        if not isinstance(obj, dict):
            return None
        name = obj.get("name")
        if name not in known:
            return None
        if "arguments" in obj:
            args = obj["arguments"]
        elif "parameters" in obj:
            args = obj["parameters"]
        else:
            return None  # no explicit args key → not a tool-call structure
        if not isinstance(args, dict):
            return None
        return {"name": name, "arguments": args}

    def _accept_args(name: str, obj: object) -> dict | None:
        """
        Validate a plain args dict paired with a tool name supplied externally
        (e.g. DeepSeek / Command-R formats where the name precedes the JSON).
        Does not touch `seen`.
        """
        if name not in known or not isinstance(obj, dict):
            return None
        return {"name": name, "arguments": obj}

    # ── Tagged / structured formats ───────────────────────────────────────────

    for rx in (_RX_QWEN, _RX_FUNC, _RX_FUNC2, _RX_PHI):
        for m in rx.finditer(text):
            try:
                hit = _accept_full(json.loads(m.group(1)))
                if hit:
                    _append(hit)
            except json.JSONDecodeError:
                pass

    # DeepSeek: name before separator, captured group 2 is the raw args dict
    for m in _RX_DEEPSEEK.finditer(text):
        name = m.group(1).strip()
        try:
            hit = _accept_args(name, json.loads(m.group(2).strip()))
            if hit:
                _append(hit)
        except json.JSONDecodeError:
            pass

    # Mistral wraps multiple calls in a JSON array
    for m in _RX_MISTRAL.finditer(text):
        try:
            for obj in json.loads(m.group(1)):
                hit = _accept_full(obj)
                if hit:
                    _append(hit)
        except (json.JSONDecodeError, TypeError):
            pass

    for m in _RX_COMMAND_R.finditer(text):
        name = m.group(1).strip()
        try:
            hit = _accept_args(name, json.loads(m.group(2).strip()))
            if hit:
                _append(hit)
        except json.JSONDecodeError:
            pass

    # Nameless <tool_call> payloads: some models (notably gemma) emit the argument
    # object directly inside the tag with no {"name":..., "arguments":...} wrapper.
    # These carry no tool name, but a payload with a "content" key is a file write —
    # attribute it to write_file when that tool is available.  raw_decode reads the
    # full object, so braces inside the content value do not truncate parsing.
    if "write_file" in known:
        for m in re.finditer(r'<tool_call>\s*(\{)', text):
            try:
                obj, _ = _JSON_DECODER.raw_decode(text, m.start(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("name") in known:
                continue  # a named structure — already handled by the tiers above
            if "content" in obj:
                args = {k: v for k, v in obj.items() if k in ("path", "content")}
                _append({"name": "write_file", "arguments": args})

    # Python-call syntax: some models (notably gemma) emit tool calls as
    # name(key='value', ...) — Python source, not JSON — sometimes wrapped in a
    # print(...) call.  Anchor on each known tool name, carve out the balanced
    # call with _match_paren, and parse it with `ast` so quoting/escaping in the
    # argument values is handled correctly.
    if not results:
        # Declared parameter order per tool, so positional args like
        # edit_file("app.py", "foo", "bar") can be mapped to their names.
        param_order = {
            t["function"]["name"]: list(t["function"]["parameters"].get("properties", {}).keys())
            for t in tools
        }
        _call_pat = re.compile(
            r'\b(' + '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True)) + r')\s*\('
        )
        for m in _call_pat.finditer(text):
            fn = m.group(1)
            open_idx = m.end() - 1            # position of the '(' the regex consumed
            close_idx = _match_paren(text, open_idx)
            if close_idx < 0:
                continue
            expr = fn + text[open_idx:close_idx + 1]
            try:
                node = ast.parse(expr, mode="eval").body
            except SyntaxError:
                continue
            if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                    or node.func.id != fn):
                continue
            call_args: dict = {}
            # Positional args → parameter names in declared order.
            names = param_order.get(fn, [])
            for i, an in enumerate(node.args):
                if i < len(names):
                    try:
                        call_args[names[i]] = ast.literal_eval(an)
                    except Exception:
                        pass
            # Keyword args (override/extend positionals).
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                try:
                    call_args[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    pass  # non-literal value (f-string, expression) — skip that arg
            # Require at least one resolved arg so a bare mention like read_file(x)
            # in prose does not become an empty, argument-less call.
            if call_args:
                hit = _accept_args(fn, call_args)
                if hit:
                    _append(hit)

    if results:
        return results

    # ── Tier 3: bare JSON scan anchored to tool-name occurrences ─────────────
    # Build the name pattern dynamically.  Longer names listed first so that
    # "grep_files" cannot be shadowed by the shorter prefix "grep_file".
    name_pat = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True)) + r')\b'
    )
    for nm in name_pat.finditer(text):
        found = nm.group(1)
        # Start the window up to 300 chars before the match: the name may appear
        # inside the JSON ({"name": "write_file", ...}) so the opening brace can
        # precede the name.  Extend to end-of-text because content args can be large.
        window_start = max(0, nm.start() - 300)
        for i in range(window_start, len(text)):
            if text[i] != '{':
                continue
            try:
                obj, _ = _JSON_DECODER.raw_decode(text, i)
                # Case A: {"name": "write_file", "arguments": {...}}
                hit = _accept_full(obj)
                if hit and hit["name"] == found:
                    if _append(hit):
                        break  # new result added — move on to the next name match
                # Case B: write_file( {...} ) — JSON follows "(" after the tool
                # name, with optional whitespace between "(" and "{".
                pre = text[max(window_start, i - 10):i].rstrip()
                if pre.endswith('('):
                    hit = _accept_args(found, obj)
                    if hit and _append(hit):
                        break
            except json.JSONDecodeError:
                continue

    return results


def _mask_tool_args(name: str, args: dict) -> dict:
    """Blank out secret request-header values in a fetch_url call.

    Applied to what is displayed, logged and saved — never to the in-memory
    history, so the model can still repeat its own call.  Tool arguments are
    otherwise stored verbatim in the session JSON and the NDJSON log, which is
    how an Authorization header would end up on disk in cleartext.
    """
    if name != "fetch_url" or not isinstance(args.get("headers"), dict):
        return args
    headers = {k: ("***" if str(k).lower() in net_mod.SECRET_HEADERS else v)
               for k, v in args["headers"].items()}
    return {**args, "headers": headers}


def _mask_messages(messages: list[dict]) -> list[dict]:
    """A copy of `messages` with secret header values masked, for session files."""
    out = []
    for m in messages:
        calls = m.get("tool_calls")
        if not calls:
            out.append(m)
            continue
        out.append({**m, "tool_calls": [
            {**c, "function": {**c["function"],
                               "arguments": _mask_tool_args(c["function"]["name"],
                                                            c["function"].get("arguments") or {})}}
            if c.get("function") else c
            for c in calls
        ]})
    return out


def _derive_write_path(content: str, mode: str) -> str:
    """Infer a filename for a write_file/append_to_file call that arrived with
    'content' but no 'path'.  Some models (notably gemma) emit the large content
    argument first and drop the trailing 'path', which would otherwise fail the
    required-argument check.  Prefer the document's first Markdown H1 as the name,
    else fall back to a mode-appropriate default."""
    m = re.search(r'^\s{0,3}#\s+(.+?)\s*$', content, re.MULTILINE)
    if m:
        slug = re.sub(r'[^a-z0-9]+', '-', m.group(1).lower()).strip('-')
        if slug:
            return f"{slug[:60]}.md"
    return "design.md" if mode == "design" else "untitled.md"


_WRITE_INTENT = (
    "let me write", "i will write", "i'll write", "i'm going to write",
    "writing the design", "writing the spec", "writing it now",
    "write the complete", "write the design", "write the specification",
    "write the spec", "write it now", "now write", "will now write",
    "let me create", "i'll draft", "i'll compose", "i'm going to create",
    "going to write", "going to draft", "composing the", "drafting the",
    "creating the design", "creating the spec", "i'm writing", "i'm creating",
)

def _has_write_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _WRITE_INTENT)


def _extract_and_strip_thinking(raw_content: str) -> tuple[str, str]:
    """Return (content_without_think_tags, thinking_text).

    Handles complete <think>…</think> blocks and incomplete <think>… blocks
    (generation cut off mid-thinking).  Either or both may be absent.
    """
    thinking = ""
    complete = re.search(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)
    if complete:
        thinking = complete.group(1).strip()
    content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    incomplete = re.search(r"<think>(.*?)$", content, flags=re.DOTALL)
    if incomplete:
        if not thinking:
            thinking = incomplete.group(1).strip()
        content = content[:incomplete.start()].strip()
    return content, thinking


# ── TUI events ────────────────────────────────────────────────────────────────

@dataclass
class ChatEvent:
    role: str   # "user" | "assistant" | "system"
    text: str

@dataclass
class ToolCallEvent:
    name: str
    args: dict

@dataclass
class ToolResultEvent:
    name: str
    result: str

@dataclass
class StatusEvent:
    mode: str
    model: str
    workdir: str
    ctx_pct: int
    ctx_color: str  # "normal" | "yellow" | "red"
    tools_enabled: bool = True
    run_confirm: bool = False
    net_access: str = "off"     # "off" | "on" | "local"
    net_confirm: bool = True    # ask y/N before a write request
    net_max_bytes: int = 2097152  # ceiling on one fetch_url download
    net_max_chars: int = 24000    # text returned per fetch_url call
    host: str = ""
    provider: str = ""
    plan_progress: str = ""  # plan mode: "awaiting approval" | "exec 3/7" | ""
    guides: bool = False     # project guide files (AGENTS.md, ...) in the system prompt
    index_enabled: bool = False   # /index: the code index and its index_* tools
    index_state: str = "off"      # off | building | refreshing | idle | stopped
    index_progress: str = ""      # "812/1873" while building/refreshing
    index_files: int = 0
    index_mem: int = 0            # estimated bytes in use
    index_max_bytes: int = code_index.DEFAULT_MAX_BYTES
    index_max_files: int = code_index.DEFAULT_MAX_FILES
    index_workers: int = 0        # /index-workers: 0 = auto
    index_persist: bool = True    # /index-persist: load/save a pickle
    index_route: bool = True      # /index-route: answer grep_files/find_files from the index
    index_degraded: bool = False  # over budget: a component was dropped

@dataclass
class ErrorEvent:
    text: str

@dataclass
class DoneEvent:
    pass

@dataclass
class AskUserEvent:
    question: str

@dataclass
class ThinkEvent:
    text: str

@dataclass
class DiffEvent:
    op: str                        # "edit" | "write" | "append" | "delete" | "move"
    path: str                      # target path (for "move", the source path)
    added: int
    removed: int
    body: list[tuple[str, int | None, int | None, str]]  # (kind, old_no, new_no, text); empty for "move"
    dst: str | None = None         # destination path for "move"
    is_new: bool = False           # write_file created a new file


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


# ── token estimation ──────────────────────────────────────────────────────────

def _content_text(m: dict) -> str:
    content = m.get("content") or ""
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content


def _text_tokens(text: str) -> int:
    return len(text) // 4


def _calls_tokens(m: dict) -> int:
    calls = m.get("tool_calls") or []
    if not calls:
        return 0
    return _text_tokens(json.dumps([tc.get("function", tc) for tc in calls], default=str))


def _msg_tokens(m: dict) -> int:
    """Estimated tokens one message costs in a request (thinking is never sent)."""
    if m.get("role") == "thinking":
        return 0
    return _text_tokens(_content_text(m)) + _calls_tokens(m)


# Compaction folds the dropped history into this block at the top of the oldest
# remaining user message; a later compaction replaces it rather than stacking.
_SUMMARY_OPEN = "[Earlier context summary:\n"
_SUMMARY_CLOSE = "\n]\n\n"
_SUMMARY_REPLY_TOKENS = 1024   # room kept free for the summary in the summariser call
_TRIM_FLOOR_TOKENS = 256       # a trimmed tool result keeps at least this much


# Context breakdown categories, in display order: (key, label, sent to the model).
_CTX_CATEGORIES = (
    ("system", "System prompt", True),
    ("guides", "Project guides", True),
    ("tools", "Tools", True),
    ("user", "User", True),
    ("assistant", "Assistant", True),
    ("tool_calls", "Tool calls", True),
    ("tool_results", "Tool results", True),
    ("generating", "Generating", True),
    ("thinking", "Thinking", False),
)


def _split_summary(text: str) -> tuple[str, str]:
    """Split a compaction summary block off the front of a user message:
    returns (summary, rest); summary is '' when there is none."""
    if text.startswith(_SUMMARY_OPEN):
        end = text.find(_SUMMARY_CLOSE, len(_SUMMARY_OPEN))
        if end != -1:
            return text[len(_SUMMARY_OPEN):end], text[end + len(_SUMMARY_CLOSE):]
    return "", text


def _format_for_summary(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            if content:
                parts.append(f"Assistant: {content}")
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                parts.append(f"  [Tool call: {fn.get('name', '?')} {json.dumps(fn.get('arguments', {}))}]")
        elif role == "tool":
            name = m.get("name", "tool")
            snippet = content[:500] + ("…" if len(content) > 500 else "")
            parts.append(f"  [Tool result ({name}): {snippet}]")
    return "\n".join(parts)


# ── harness ───────────────────────────────────────────────────────────────────

_DEFAULT_CONTEXT = 32768  # fallback when the model does not report its context size
_RECAP_NUM_CTX = 4096     # idle recap: context window of the recap call
_RECAP_REPLY_TOKENS = 384 # ... tokens kept free for momo's reply
_RECAP_LINE_LEN = "20-36" # ... asked-for characters per line (bubble fits 38 after "< ")
_RECAP_SHORT = 15         # ... a reply whose lines are all shorter gets one "longer" retry
_RECAP_MAX_TURNS = 4      # ... never looks back further than this many user turns
_MOMO_LINES_MAX = 30      # remembered recap lines per session


def _has_model(available: list[str], name: str) -> bool:
    """Ollama lists an untagged name with its implicit :latest tag."""
    return name in available or f"{name}:latest" in available


class Harness:
    def __init__(self, host: str, model: str, workdir: Path, provider: str = "ollama"):
        self.workdir = workdir.resolve()
        self.mode = "design"
        self.context_limit = _DEFAULT_CONTEXT
        self._ts = session_mod.new_timestamp()
        self.logger = Logger(self._ts)
        self.provider = provider
        self.client = make_client(provider, host=host, model=model)
        self.event_queue = EventBus()  # fan-out to every frontend (TUI, web)
        self._user_input_queue: queue.Queue[str] = queue.Queue()
        self.awaiting_input: bool = False  # True while the worker blocks in _ask_user
        self.max_tool_result = 0   # chars; 0 = unlimited; configurable via /tool-result or --max-tool-result
        self.think: bool = True    # enable model thinking/reasoning mode; configurable via /think or --think
        self.stream: bool = True   # stream replies to the frontends as they are generated; --no-stream
        self.tools_enabled: bool = True
        self.run_confirm: bool = False  # when True, prompt y/N before each run_command; toggle via /run-confirm or Shift+P
        # Internet access for fetch_url: "off" | "on" (public hosts only) |
        # "local" (also loopback/LAN).  Off by default and never persisted --
        # this is the only tool that sends data off the machine.  /net
        self.net_access: str = "off"
        # When True, POST/PUT/PATCH/DELETE ask y/N first.  /net-confirm
        self.net_confirm: bool = True
        # Ceiling on a single fetch_url download.  /net-max-bytes
        self.net_max_bytes: int = net_mod.DEFAULT_MAX_BYTES
        # Text returned per fetch_url call (the rest is paged with offset=/find=).
        # /net-max-chars
        self.net_max_chars: int = net_mod.DEFAULT_MAX_CHARS
        self.active_skills: list[str] = []
        self.input_history: list[str] = []
        # Idle recap: when on, momo recaps the last turns in its speech bubble after
        # the user has been idle for idle_recap_secs (driven by the Controller).
        self.idle_recap: bool = False
        self.idle_recap_secs: int = 90
        self.momo_lines: list[str] = []   # remembered recap lines, newest last
        self._momo_recap_turn: int = 0    # user_turns() at the last recap attempt
        self._turn_count: int = 0         # user turns sent; monotonic (compaction can drop messages)
        self.model_max_ctx: int | None = None  # model's real reported context window; used as num_ctx
        self.context_pct: int | None = None  # user-set % of model max; None = use default 50%
        # An absolute limit the user set (/context <n>, --context): kept across
        # sessions.  Otherwise the limit follows the model's window.
        self.context_fixed: bool = False
        # Plan mode state: phase is "investigating" → "awaiting_approval" (plan kept
        # for later) → "executing" (approved; paused if not currently running).
        self.plan: Plan | None = None
        self.plan_phase: str = "investigating"
        self._tool_ref = ""
        # Project guides: when on, the workdir's AGENTS.md / CLAUDE.md / ... are
        # appended to the system prompt; re-read on new/loaded session, /clear,
        # compaction and a workdir change (reload_guides).  /guides
        self.guides: bool = False
        self._guides: list[tuple[str, str]] = []   # (relpath, text) as last read
        self._guides_text = ""                     # the rendered prompt block
        # Code index: when on, a ProjectIndex for the workdir is kept current in
        # the background and the index_* tools replace find_references /
        # file_dependencies.  /index, /index-max-mem, /index-max-files, /index-workers,
        # /index-persist
        self.index_enabled: bool = False
        self.index_max_bytes: int = code_index.DEFAULT_MAX_BYTES
        self.index_max_files: int = code_index.DEFAULT_MAX_FILES
        self.index_workers: int = 0            # 0 = auto (code_index.auto_workers)
        self.index_persist: bool = True
        self.index_route: bool = True      # /index-route: grep/find answered from the index
        self.index: code_index.ProjectIndex | None = None
        self._index_saved_version = -1
        self._schema_cache: tuple = (None, 0)
        self._turn_user: dict | None = None  # the user message the running turn answers
        self.messages: list[dict] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        self._token_estimate = 0
        self._stream_chars = 0      # chars of the reply currently streaming in
        self._measured_tokens: int | None = None  # last prompt+eval the server reported
        self._cancel = threading.Event()
        # What the saved prefs / session asked for; check_backend reports how the
        # server's answer differs from it.
        self._expected_backend: tuple[str, int | None] = (self.client.model, None)
        # For a fixed-model backend, adopt the server's actually-loaded model before
        # reading its context window.  check_backend() reports the result at startup.
        self._reconcile_fixed_model()
        self._sync_context_limit(emit=False)

    # ── public properties ─────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self.client.model if hasattr(self, "client") else self._model

    @model.setter
    def model(self, value: str):
        self._model = value
        if hasattr(self, "client"):
            self.client.set_model(value)

    def provide_user_input(self, text: str):
        """Called from a frontend thread when the user answers a mid-task question."""
        self.awaiting_input = False
        self._user_input_queue.put(text)

    def _ask_user(self, question: str) -> str:
        """Emit an AskUserEvent and block the worker thread until a frontend answers."""
        self.awaiting_input = True
        self.event_queue.put(AskUserEvent(question))
        # Only ever called from a worker thread, so the harness is busy by definition.
        self.event_queue.put(BusyEvent(busy=True, waiting=True))
        try:
            return self._user_input_queue.get()
        finally:
            self.awaiting_input = False

    def _fetch_chars(self) -> int:
        """Text budget for one fetch_url result: /net-max-chars, but never above
        /tool-result, which fetch_url is exempt from because it windows itself."""
        if self.max_tool_result > 0:
            return min(self.net_max_chars, self.max_tool_result)
        return self.net_max_chars

    def _net_confirm_prompt(self, args: dict) -> str | None:
        """The y/N question to ask before a fetch_url write, or None to just run it.

        GET and HEAD never ask.  Writes ask whenever net_confirm is on — and also
        when the target is not a public address, *even with net_confirm off*.
        That carve-out matters: the harness's own web API is on 127.0.0.1 and its
        same-origin check passes any request with no Origin header (which a
        non-browser client never sends), so "/net local" plus unattended writes
        would otherwise let a fetched page drive this harness through
        POST /api/submit.
        """
        method = str(args.get("method") or "GET").upper()
        if method in ("GET", "HEAD"):
            return None
        url = str(args.get("url") or "")
        private = net_mod.is_private_target(url)
        if not self.net_confirm and not private:
            return None
        why = ("\nThis is a local/private address, so it is confirmed even though "
               "write confirmation is off." if private and not self.net_confirm else "")
        body = str(args.get("body") or "")
        shown = body if len(body) <= 500 else body[:500] + f"… (+{len(body) - 500} chars)"
        return (f"Send this request? Reply 'y' to allow, anything else to decline.{why}\n"
                f"  {method} {url}" + (f"\n  body: {shown}" if shown else ""))

    # ── file-edit diffs ─────────────────────────────────────────────────────────

    def _read_text_safe(self, rel_path: str) -> tuple[str, bool]:
        """Read the text of a workdir-relative path. Returns (text, existed).
        Missing files, directories, and read errors all yield ("", False)."""
        try:
            return (self.workdir / rel_path).read_text(encoding="utf-8", errors="replace"), True
        except (FileNotFoundError, IsADirectoryError, OSError):
            return "", False

    def _emit_diff(self, name: str, args: dict, result: str,
                   old_text: str | None, existed: bool) -> bool:
        """Emit a DiffEvent for a successful mutating tool call.

        Returns True if a diff was emitted (so the caller skips the plain
        ToolResultEvent), False otherwise (errors, no-op edits, non-mutating tools).
        """
        if name not in _MUTATING_TOOLS or result.startswith("ERROR:"):
            return False
        if name == "move_file":
            self.event_queue.put(DiffEvent(
                op="move", path=args.get("src", ""), dst=args.get("dst", ""),
                added=0, removed=0, body=[]))
            return True
        path = args.get("path", "")
        new_text = "" if name == "delete_file" else self._read_text_safe(path)[0]
        body, added, removed = build_diff_body(old_text or "", new_text)
        if not body:
            return False  # no visible change — fall back to the plain result line
        op_map = {
            "edit_file": "edit",
            "append_to_file": "append", "write_file": "write",
            "delete_file": "delete",
        }
        self.event_queue.put(DiffEvent(
            op=op_map.get(name, "edit"), path=path,
            added=added, removed=removed, body=body,
            is_new=(name == "write_file" and not existed)))
        return True

    def _sync_context_limit(self, emit: bool = False):
        """Query the model's native context window and compute the compaction limit."""
        reported = self.client.context_length()
        if reported:
            # The model's real window is used as num_ctx so the full context is
            # available; context_limit is only the compaction threshold (the point
            # at which we start dropping old history to leave room for the reply).
            self.model_max_ctx = reported
            pct = self.context_pct if self.context_pct is not None else 50
            self.context_limit = max(256, int(reported * pct / 100))
            msg = f"Model context: {self.context_limit:,} tokens compaction threshold ({pct}% of {reported:,} max, {self.client.model})"
        else:
            self.model_max_ctx = None
            msg = f"Model context: unknown — using default {self.context_limit:,} tokens ({self.client.model})"
        if emit:
            self.event_queue.put(ChatEvent("system", msg))

    def _reconcile_fixed_model(self):
        """For a backend that can't switch models at runtime (llama.cpp serves the
        single model it was launched with and ignores the request's `model` field),
        the *server* — not a saved label from CLI args, prefs, or a restored session
        — is the source of truth for what is loaded. Query it and adopt the real id
        so the UI never shows a stale name after the server was relaunched with a
        different model. No-op for switchable backends (Ollama) and when the server
        is unreachable (list_models() returns [])."""
        if self.client.can_switch_model:
            return
        loaded = self.client.list_models()
        if loaded and loaded[0] != self.client.model:
            self.client.set_model(loaded[0])

    def check_backend(self) -> str:
        """Startup check against the server: which model it serves (or has) and
        its real context window, since the saved session and prefs may be stale.
        Adopts what it finds and returns the notice to show."""
        c = self.client
        want_model, want_limit = self._expected_backend
        where = f"{c.provider_name} at {c.host}"
        available = c.list_models()
        if not available:
            return (f"Model: {c.model} — {where} is not reachable, so the model and its "
                    f"context size are unchecked (compaction at {self.context_limit:,} tokens). "
                    f"/model lists the server's models once it is up.")
        changes = []
        if not c.can_switch_model:
            self._reconcile_fixed_model()
        elif not _has_model(available, c.model):
            loaded = [m for m in c.loaded_models() if _has_model(available, m)]
            if loaded:
                c.set_model(loaded[0])
            else:
                changes.append(f"WARNING: {c.model} is not on the server — pick one with /model "
                               f"({len(available)} available)")
        if c.model != want_model:
            why = ("the server's loaded model" if not c.can_switch_model
                   else f"{want_model} is not on the server; {c.model} is loaded")
            changes.append(f"model {want_model} → {c.model} ({why})")
        reported = c.context_length()
        if self.context_fixed:
            self.model_max_ctx = reported
            if reported and self.context_limit > reported:
                changes.append(f"your {self.context_limit:,}-token context limit is over the "
                               f"model's {reported:,}-token window; now its default half")
                self.context_fixed = False
                self._sync_context_limit(emit=False)
        else:
            self._sync_context_limit(emit=False)
        if want_limit is not None and want_limit != self.context_limit:
            changes.append(f"context limit {want_limit:,} → {self.context_limit:,} tokens")
        if reported:
            how = ("set by you" if self.context_fixed
                   else f"{self.context_pct if self.context_pct is not None else 50}%")
            ctx = (f"context window {reported:,} tokens, compaction at "
                   f"{self.context_limit:,} ({how})")
        else:
            ctx = (f"context window not reported, compaction at {self.context_limit:,} tokens "
                   f"({'set by you' if self.context_fixed else 'default'})")
        if c.model != want_model:
            session_mod.save_prefs(model=c.model, provider=self.provider)
        self._emit_status()
        msg = f"Model: {c.model} ({where}) — {ctx}."
        if changes:
            msg += "\nUpdated from the saved settings: " + "; ".join(changes) + "."
        return msg

    def switch_backend(self, provider: str, host: str, model: str):
        """Point the harness at a backend, rebuilding the client when the provider
        changes, then re-read the served model and its context window."""
        self._expected_backend = (model, None)     # asked for now, not the saved one
        if provider != self.provider:
            self.client = make_client(provider, host=host, model=model,
                                      auth_token=self.client._auth_token)
            self.provider = provider
        else:
            self.client.set_host(host)
            self.client.set_model(model)
        self._reconcile_fixed_model()
        if not self.context_fixed:
            self._sync_context_limit(emit=False)
        else:
            self.model_max_ctx = self.client.context_length()
        self._emit_status()

    def set_model(self, model: str):
        """Switch model and re-sync context limit from the new model's capabilities."""
        self.client.set_model(model)
        self._sync_context_limit(emit=True)
        self._emit_status()

    # ── mode switching ────────────────────────────────────────────────────────

    def _plan_investigating(self) -> bool:
        return self.mode == "plan" and self.plan_phase != "executing"

    def _plan_executing(self) -> bool:
        return self.mode == "plan" and self.plan_phase == "executing" and self.plan is not None

    def _current_tools(self) -> list[dict]:
        """Tool set for the current mode (and, in plan mode, the current phase).

        fetch_url is appended only while internet access is on, so a model that
        cannot use it never sees it in the schema or in the generated reference.
        """
        if self._plan_executing():
            tools = PLAN_EXECUTE_TOOLS
        else:
            tools = _MODE_TOOLS.get(self.mode, ALL_TOOLS)
        if self.net_access != "off":
            tools = tools + NET_TOOLS
        if self.index is not None:
            tools = with_index_tools(tools, self.index_route)
        return tools

    # ── code index ────────────────────────────────────────────────────────────

    def set_index(self, enabled: bool) -> str:
        """Turn the code index on or off; returns a notice."""
        self.index_enabled = enabled
        if enabled and self.index is None:
            self._start_index()
            note = (f"Code index: on — indexing {self.workdir} in the background "
                    f"(budget {net_mod.format_size(self.index_max_bytes)}"
                    + (", loading the saved index first" if self.index_persist else "") + ").")
        elif not enabled and self.index is not None:
            note = "Code index: off" + self._stop_index()
        else:
            note = f"Code index: {'on' if enabled else 'off'}"
        self.rebuild_system_prompt()
        self._emit_status()
        return note

    def _start_index(self) -> None:
        idx = code_index.ProjectIndex(self.workdir, self.index_max_bytes,
                                      on_change=self._on_index_change,
                                      max_files=self.index_max_files,
                                      workers=self.index_workers)
        self.index = idx
        self._index_saved_version = -1
        code_nav.set_index_provider(idx.provide, idx.paths_under)
        idx.start(load_pickle=self.index_persist)

    def _stop_index(self, save: bool = True) -> str:
        """Stop the index (saving it first when persistence is on).  Returns
        a notice suffix, '' when there is nothing to say."""
        idx, self.index = self.index, None
        if idx is None:
            return ""
        note = ""
        if save and self.index_persist and idx.is_fresh():
            note = "\n" + idx.save()
        idx.stop()
        code_nav.set_index_provider(None)
        return note

    def restart_index(self) -> str:
        """The workdir changed: index the new one instead."""
        if not self.index_enabled:
            return ""
        note = self._stop_index()
        self._start_index()
        self.rebuild_system_prompt()
        self._emit_status()
        return f"Code index: re-indexing {self.workdir}" + note

    def shutdown_index(self) -> None:
        """On exit: save when persistence is on, then stop the thread."""
        self._stop_index()

    def _on_index_change(self, idx: "code_index.ProjectIndex") -> None:
        # Called from the indexer thread.
        if idx is not self.index:
            return
        for note in idx.pop_notices():
            self.event_queue.put(ChatEvent("system", note))
        if (self.index_persist and idx.state == "idle" and self._index_saved_version < 0
                and idx.is_fresh()):
            # Save once after the first complete build; exit and workdir changes save again.
            self._index_saved_version = idx.version
            threading.Thread(target=self._save_index_quietly, args=(idx,), daemon=True).start()
        self._emit_status()

    def index_filter(self) -> tuple[str, str]:
        """(filter text, its path).  Reading it seeds it when it doesn't exist yet."""
        idx = self.index
        text = idx.filter_text if idx is not None and not idx.filter_pending \
            else code_index.load_filter(self.workdir)[0]
        return text, str(code_index.filter_path(self.workdir))

    def set_index_filter(self, text: str) -> str:
        """Save the index filter and apply it to a running index.  Returns a notice."""
        err = code_index.save_filter(self.workdir, text)
        if err:
            return err
        n = len(ignore_rules.Rules.parse(text))
        if self.index is not None:
            self.index.set_filter(text)
            return f"Code index filter saved: {n} rules; re-listing files."
        return f"Code index filter saved: {n} rules (applies when the index is on)."

    def reset_index_filter(self) -> str:
        """Re-seed the filter from the project's current .gitignore files."""
        return self.set_index_filter(ignore_rules.seed_text(self.workdir))

    def index_breakdown(self) -> dict:
        """The index's memory composition (/index, web INDEX popover)."""
        idx = self.index
        if idx is None:
            return {"enabled": False, "limit": self.index_max_bytes,
                    "max_files": self.index_max_files, "workers": self.index_workers,
                    "workers_used": self.index_workers or code_index.auto_workers(),
                    "persist": self.index_persist,
                    "route": self.index_route}
        return {**code_index.breakdown(idx), "persist": self.index_persist,
                "route": self.index_route}

    def _save_index_quietly(self, idx) -> None:
        msg = idx.save()
        if msg.startswith("ERROR"):
            self.event_queue.put(ChatEvent("system", msg))

    def _wait_index(self, name: str) -> str | None:
        """Block an index-backed tool until the index is current.  Returns an
        ERROR string when cancelled, else None."""
        idx = self.index
        if idx is None or name not in code_index.WAIT_TOOLS:
            return None

        def on_wait(done: int, total: int) -> None:
            prog = f" ({done}/{total} files)" if total else ""
            self.event_queue.put(ChatEvent("system", f"Waiting for the code index{prog}…"))
        return idx.wait_fresh(self._cancel, on_wait=on_wait)

    def _dispatch(self, name: str, args: dict) -> str:
        err = self._wait_index(name)
        if err:
            return err
        # A tool that raises must still produce a result: the assistant turn with
        # this call is already in the history, and an unanswered call breaks the
        # next request (and an escaped traceback would land on the curses screen).
        try:
            return dispatch(name, args, self.workdir, self.net_access, self.net_max_bytes,
                            self._fetch_chars(), index=self.index, cancel=self._cancel,
                            index_route=self.index_route)
        except Exception as e:
            return f"ERROR: {name} failed: {type(e).__name__}: {e}"

    def _build_system_prompt(self) -> str:
        if self._plan_executing():
            # Execution runs with exactly the coding agent's prompt plus the plan.
            base = (_coding_prompt(str(self.workdir)) + "\n\n---\n\n" + _PLAN_EXECUTION_RULES
                    + "\n\n### Current plan state\n\n" + self.plan.render_for_prompt())
        else:
            loader = _ROLE_LOADERS.get(self.mode, _ROLE_LOADERS["coding"])
            base = loader(str(self.workdir))
            if self.mode == "plan" and self.plan is not None:
                base += ("\n\n---\n\n## Current draft plan\n\nYou already submitted this plan; "
                         "it was not approved yet. Revise it according to the user's feedback and "
                         "call create_plan again with the complete plan.\n\n"
                         + self.plan.render_for_prompt())
        if str(self.workdir) not in base:
            base += f"\n\nWorking directory: {self.workdir}"
        # Append the tool reference generated from the schemas for exactly this
        # mode's tool set, so the reference is always in sync with the real tools
        # (the role .md files no longer carry a hand-copied version).
        # Kept so the context breakdown can attribute it to tools without re-rendering.
        self._tool_ref = render_tool_reference(self._current_tools())
        base += "\n\n---\n\n" + self._tool_ref
        self._guides_text = ""
        if self._guides:
            self._guides_text = (
                "## Project guides\n\nInstructions from this project's own guide files "
                "— follow them.\n\n"
                + "\n\n".join(f"### {name}\n\n{text}" for name, text in self._guides))
            base += "\n\n---\n\n" + self._guides_text
        parts = []
        for name in self.active_skills:
            p = _SKILLS_DIR / f"{name}.md"
            if p.exists():
                parts.append(p.read_text(encoding="utf-8").strip())
        if self.index is not None and any(t["function"]["name"] == "index_text"
                                          for t in self._current_tools()):
            # First and last, where a small model weighs it most: the role
            # texts in between still teach grep_files / find_references.
            base = _INDEX_BANNER + "\n\n" + base
            role = "plan-exec" if self._plan_executing() else self.mode
            parts.append(_index_rules(role))
        if parts:
            return base + "\n\n---\n\n" + "\n\n---\n\n".join(parts)
        return base

    def set_mode(self, mode: str):
        self.mode = mode
        self.rebuild_system_prompt()
        self._emit_status()

    def rebuild_system_prompt(self):
        """Re-render the system prompt in place.

        The prompt embeds a tool reference generated from the current tool set,
        so anything that changes which tools are offered mid-session has to call
        this or the reference goes stale — which matters for the small models
        that emit text-format tool calls by copying that reference.
        """
        prompt = {"role": "system", "content": self._build_system_prompt()}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = prompt
        else:                       # a session file without one (hand-edited, damaged)
            self.messages.insert(0, prompt)

    def _read_guides(self) -> list[tuple[str, str]]:
        """The workdir's guide files as (relpath, text), capped in total size."""
        budget = min(_GUIDE_MAX_CHARS, self.context_limit)
        found, seen = [], set()
        for rel in _GUIDE_FILES:
            parent = (self.workdir / rel).parent
            try:
                path = next((p for p in sorted(parent.iterdir())
                             if p.name.lower() == Path(rel).name.lower() and p.is_file()), None)
            except OSError:
                continue
            if path is None:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            name = str(path.relative_to(self.workdir))
            if budget <= 0:
                found.append((name, f"[… omitted: {name} is {len(text):,} chars, over the guide size cap]"))
                continue
            if len(text) > budget:
                text = text[:budget] + f"\n\n[… truncated: {name} is {len(text):,} chars]"
            budget -= len(text)
            found.append((name, text))
        return found

    def reload_guides(self) -> str:
        """Re-read the project guide files (or drop them when /guides is off) and
        rebuild the system prompt.  Returns a notice, '' when the toggle is off."""
        self._guides = self._read_guides() if self.guides else []
        if self.messages:
            self.rebuild_system_prompt()
        return self.guides_summary()

    def guides_summary(self) -> str:
        """What the last reload_guides() loaded, as a notice; '' when /guides is off."""
        if not self.guides:
            return ""
        if not self._guides:
            return f"No project guide files found in {self.workdir}"
        names = ", ".join(name for name, _ in self._guides)
        return f"Project guides loaded: {names} (~{_text_tokens(self._guides_text):,} tokens)"

    # ── context management ────────────────────────────────────────────────────

    @property
    def _stream_tokens(self) -> int:
        return self._stream_chars // 4

    def _ctx_pct(self) -> int:
        used = self._token_estimate + self._stream_tokens
        return min(100, int(used / self.context_limit * 100))

    def _context_categories(self) -> dict[str, int]:
        """Estimated tokens per context category (chars/4, see _text_tokens).
        Thinking is kept in the transcript but filtered before every API call, so
        it is reported but never counted as sent."""
        cats = {key: 0 for key, _, _ in _CTX_CATEGORIES}
        # Kept apart as well as counted under "tools", so _estimate can leave it out.
        cats["schemas"] = self._schema_tokens()
        cats["tools"] += cats["schemas"]
        for m in list(self.messages):
            role = m.get("role")
            text = _content_text(m)
            if role == "system":
                # The generated tool reference is part of the system prompt text,
                # but it is spent on tools, so it is attributed to them.
                ref = self._tool_ref
                if ref and ref in text:
                    cats["tools"] += _text_tokens(ref)
                    text = text.replace(ref, "", 1)
                guides = self._guides_text
                if guides and guides in text:
                    cats["guides"] += _text_tokens(guides)
                    text = text.replace(guides, "", 1)
                cats["system"] += _text_tokens(text)
            elif role == "user":
                cats["user"] += _text_tokens(text)
            elif role == "assistant":
                cats["assistant"] += _text_tokens(text)
                cats["tool_calls"] += _calls_tokens(m)
            elif role == "tool":
                cats["tool_results"] += _text_tokens(text)
            elif role == "thinking":
                cats["thinking"] += _text_tokens(text)
        cats["generating"] = self._stream_tokens
        return cats

    def _schema_tokens(self) -> int:
        """Estimated tokens of the tool schemas sent with each request, cached per
        tool set (serialising them is the expensive part of an estimate)."""
        tools = self._current_tools() if self.tools_enabled else []
        key = tuple(t["function"]["name"] for t in tools)
        if self._schema_cache[0] != key:
            n = _text_tokens(json.dumps(tools, separators=(",", ":"))) if tools else 0
            self._schema_cache = (key, n)
        return self._schema_cache[1]

    def context_breakdown(self) -> dict:
        """Where the context is spent, per category — backs /context and the web
        UI's context popover.  ``used`` is the status-bar figure (the server's
        measured count after a reply, the estimate before one) plus whatever is
        streaming in; the categories are always estimates."""
        cats = self._context_categories()
        return {
            "limit": self.context_limit,
            "model_max": self.model_max_ctx,
            "used": self._token_estimate + self._stream_tokens,
            "estimated": sum(cats[k] for k, _, sent in _CTX_CATEGORIES if sent),
            "measured": self._measured_tokens,
            "pct": self._ctx_pct(),
            "streaming": self._stream_tokens > 0,
            "categories": [{"key": k, "label": label, "tokens": cats[k], "sent": sent}
                           for k, label, sent in _CTX_CATEGORIES],
        }

    def _ctx_color(self, pct: int) -> str:
        if pct >= 90:
            return "red"
        if pct >= 75:
            return "yellow"
        return "normal"

    def compact(self, summarise: bool = True) -> str:
        return self._compact(summarise)[1]

    def _compact(self, summarise: bool = True) -> tuple[bool, str]:
        """Shrink the history to a third of its budget (the limit minus the fixed
        system prompt).  Returns (changed, notice).

        Pass 1 drops old tool-call groups (assistant + its tool/thinking messages),
        pass 2 drops whole old turns, pass 3 trims the largest tool results that
        are left.  The running turn's question and its latest tool group are never
        dropped — deleting the result the model just asked for makes it ask again.
        Nothing is changed until the summary is in, so a cancel leaves it intact.
        """
        # Re-read the guides first, so the fixed size below includes any edits.
        old_guides = self._guides
        guides_notice = self.reload_guides() if self.guides else ""
        if self._guides == old_guides:
            guides_notice = ""
        msgs = self.messages
        before = self._token_estimate or self._estimate(schemas=True)
        costs = [_msg_tokens(m) for m in msgs]
        fixed = costs[0] if msgs else 0
        if fixed >= self.context_limit:
            return False, (f"Context not compacted: the system prompt and tool reference alone are "
                           f"~{fixed:,} tokens, over the {self.context_limit:,}-token limit. "
                           "Raise it with /context <n> or /context <n>%.")
        target = fixed + (self.context_limit - fixed) // 3
        total = sum(costs)

        users = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
        anchor = next((i for i, m in enumerate(msgs) if m is self._turn_user), None)
        if anchor is None:
            anchor = users[-1] if users else len(msgs)
        # The latest tool group of the running turn stays (it may be in flight).
        last_calls = next((i for i in range(len(msgs) - 1, anchor, -1)
                           if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls")), None)
        tail = last_calls if last_calls is not None else len(msgs)

        drop: set[int] = set()

        def take(idx):
            nonlocal total
            drop.add(idx)
            total -= costs[idx]

        # Pass 1: tool-call groups, oldest first.
        i = 1
        while i < tail and total > target:
            role = msgs[i].get("role")
            if role == "assistant":
                j = i + 1
                while j < tail and msgs[j].get("role") in ("tool", "thinking"):
                    j += 1
                if j > i + 1:
                    for k in range(i, j):
                        take(k)
                    i = j
                    continue
            elif role in ("tool", "thinking"):
                take(i)   # orphaned tool/thinking with no preceding assistant
            i += 1

        # Pass 2: whole turns before the running one, oldest first, so what is
        # left still starts with a user message and keeps its tool pairing.
        starts = [u for u in users if u < anchor]
        if not starts or starts[0] > 1:
            starts.insert(0, 1)
        for n, s0 in enumerate(starts):
            if total <= target:
                break
            s1 = starts[n + 1] if n + 1 < len(starts) else anchor
            for k in range(s0, s1):
                if k not in drop:
                    take(k)

        # Pass 3: still over the target — trim the largest remaining tool results.
        # Trimming only to the limit would leave the context a few tokens under
        # it: every later request then sits at ~100% yet never re-triggers.
        trims: dict[int, str] = {}
        if total > target:
            for k in sorted((k for k in range(1, len(msgs))
                             if k not in drop and msgs[k].get("role") == "tool"),
                            key=lambda k: -costs[k]):
                excess = total - target
                if excess <= 0:
                    break
                keep = max(costs[k] - excess - 16, _TRIM_FLOOR_TOKENS)  # 16: the marker
                if keep >= costs[k]:
                    continue
                text = _content_text(msgs[k])
                head, tail_chars = keep * 4 * 2 // 3, keep * 4 // 3
                cut = len(text) - head - tail_chars
                trims[k] = (f"{text[:head]}\n[… {cut:,} chars removed by context compaction …]\n"
                            f"{text[len(text) - tail_chars:]}")
                total -= costs[k] - _text_tokens(trims[k])

        if not drop and not trims:
            return False, (f"Context not compacted: nothing left to remove "
                           f"(~{total:,} of {self.context_limit:,} tokens).")

        # Summarise what goes, folding in a summary already at the top of the
        # oldest surviving user message so the new one replaces it.
        keep_idx = [k for k in range(len(msgs)) if k not in drop]
        first_user = next((k for k in keep_idx if msgs[k].get("role") == "user"), None)
        prev_summary, first_body = "", None
        if first_user is not None:
            prev_summary, first_body = _split_summary(_content_text(msgs[first_user]))
        removed_msgs = [msgs[k] for k in sorted(drop)]
        summary = ""
        if summarise and removed_msgs:
            summary = self._summarize_removed(removed_msgs, prev_summary)
            if self._cancel.is_set():
                return False, "Compaction cancelled — history unchanged."

        # Commit.
        # (In place: _turn_user tracks the running turn's message by identity.)
        for k, text in trims.items():
            msgs[k]["content"] = text
        if summary and first_user is not None:
            msgs[first_user]["content"] = f"{_SUMMARY_OPEN}{summary}{_SUMMARY_CLOSE}{first_body}"
        self.messages = [msgs[k] for k in keep_idx]

        after = self._estimate(schemas=True)
        self._token_estimate = after
        self._measured_tokens = None
        action = "summarised" if summary else "removed"
        notice = f"Context compacted: {action} {len(drop)} messages"
        if trims:
            notice += f", trimmed {len(trims)} tool result{'s' if len(trims) > 1 else ''}"
        notice += f" (was ~{before:,} tokens, now ~{after:,} tokens)"
        if guides_notice:
            notice += f"\n{guides_notice}"
        self.logger.log_compact(self.mode, self.client.model, len(drop), before, after)
        return True, notice

    def _summarize_removed(self, msgs: list[dict], previous: str = "") -> str:
        """One-shot LLM call to summarise removed messages (superseding `previous`,
        an earlier summary). Returns '' on failure."""
        conversation = _format_for_summary(msgs)
        if not conversation.strip():
            return previous
        num_ctx = self.model_max_ctx or self.context_limit
        instruction = ("Summarize the following conversation fragment concisely. "
                       "Preserve: key decisions, file names, code entities, outcomes, and "
                       "any facts needed to continue the work. Omit pleasantries and filler.\n\n")
        if previous:
            instruction += f"It continues from this earlier summary; fold it in:\n{previous}\n\n"
        # Fit the call's window: keep the newest part of the fragment.
        budget = (num_ctx - _SUMMARY_REPLY_TOKENS - _text_tokens(instruction)) * 4
        if budget <= 0:
            return previous
        if len(conversation) > budget:
            conversation = conversation[-budget:]
            conversation = conversation[conversation.find("\n") + 1:]
        prompt = [{"role": "user", "content": instruction + conversation}]
        try:
            response = self.client.chat(prompt, [], think=False, num_ctx=num_ctx)
        except Exception:
            return ""
        content, _ = _extract_and_strip_thinking(response.content or "")
        return content

    def user_turns(self) -> int:
        return self._turn_count

    def _recap_context(self, turns: int, budget_tokens: int) -> str:
        """The conversation since the last recap: the last `turns` user turns (capped at
        _RECAP_MAX_TURNS), dropping the oldest messages until it fits budget_tokens."""
        body = self.messages[1:]
        turns = max(1, min(turns, _RECAP_MAX_TURNS))
        start, seen = 0, 0
        for i in range(len(body) - 1, -1, -1):
            if body[i].get("role") == "user":
                seen += 1
                if seen == turns:
                    start = i
                    break
        parts: list[str] = []
        used = 0
        for m in reversed(body[start:]):          # newest first, so the oldest get dropped
            text = _format_for_summary([m])
            if not text:
                continue
            cost = len(text) // 4 + 1              # same estimate as _text_tokens
            if used + cost > budget_tokens:
                if not parts:                      # always keep the end of the newest message
                    parts.append(text[-budget_tokens * 4:])
                break
            parts.append(text)
            used += cost
        return "\n".join(reversed(parts))

    def momo_recap(self, client, turns: int = 1) -> list[str]:
        """Recap the `turns` user turns since the last recap as several short lines in
        momo's voice (up to MAX_RECAP_LINES), each sized to fit the companion speech
        bubble ("< "-prefixed). The context is trimmed to fit the recap call's window.
        Uses `client` (not self.client) so the caller can abort it independently.
        Returns [] on failure."""
        system = _load_role("momo-companion") or (
            f"You are Momo, a playful cat. Reply with 1-5 lines of {_RECAP_LINE_LEN} characters.")
        num_ctx = min(_RECAP_NUM_CTX, self.model_max_ctx or _RECAP_NUM_CTX)
        # Budget for the conversation: the window minus the system prompt, the
        # instruction wrapped around it (~64 tokens) and room for the reply.
        budget = num_ctx - len(system) // 4 - 64 - _RECAP_REPLY_TOKENS
        if budget <= 0:
            return []
        conversation = self._recap_context(turns, budget)
        if not conversation.strip():
            return []
        want = min(MAX_RECAP_LINES, 2 + turns)   # more happened → more to recap
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"What we did since your last recap:\n{conversation}\n\n"
                                        f"Write up to {want} speech-bubble lines, "
                                        f"{_RECAP_LINE_LEN} characters each, each about a "
                                        "different moment."},
        ]
        try:
            reply = client.chat(msgs, [], think=False, num_ctx=num_ctx).content or ""
        except Exception:
            return []
        lines = fit_bubble(reply)
        # One retry within this attempt when the reply missed the length: nothing
        # fit (or every line had to be cut), or every line is a tiny fragment.
        note = None
        if not lines or all(line.endswith("…") for line in lines):
            note = f"too long! {_RECAP_LINE_LEN} characters per line"
        elif all(len(line) < _RECAP_SHORT for line in lines):
            note = (f"a bit longer please: {_RECAP_LINE_LEN} characters per line, "
                    "full little sentences")
        if note:
            msgs += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": note}]
            try:
                retry = fit_bubble(client.chat(msgs, [], think=False, num_ctx=num_ctx).content)
            except Exception:
                retry = []
            # Keep whichever gives momo more to say (ties keep the first reply).
            if sum(map(len, retry)) > sum(map(len, lines)):
                lines = retry
        return [f"< {line}" for line in lines]

    def remember_momo_lines(self, lines: list[str]):
        for line in lines:
            if line in self.momo_lines:
                self.momo_lines.remove(line)
            self.momo_lines.append(line)
        del self.momo_lines[:-_MOMO_LINES_MAX]

    def compact_threaded(self, summarise: bool = True):
        """Run compact() on a worker thread, emitting events back to the TUI."""
        self._cancel.clear()   # an earlier Esc must not cancel this compaction
        try:
            notice = self.compact(summarise=summarise)
            self.event_queue.put(ChatEvent("system", notice))
            self._emit_status()
        finally:
            self.event_queue.put(DoneEvent())

    def _estimate(self, schemas: bool = False) -> int:
        """Estimated tokens of the messages the next request sends (stored thinking
        is never sent).  With ``schemas`` the tool schemas are included too — the
        full request, as the status bar shows it.  Compaction measures without
        them: they are fixed per mode, so compacting cannot shrink them."""
        cats = self._context_categories()
        total = sum(cats[k] for k, _, sent in _CTX_CATEGORIES if sent and k != "generating")
        return total if schemas else total - cats["schemas"]

    def cancel(self):
        """Interrupt the running LLM call immediately."""
        self._cancel.set()
        self.client.abort()

    # ── send ──────────────────────────────────────────────────────────────────

    def send(self, text: str):
        """Called from the harness worker thread."""
        self._cancel.clear()
        self._turn_count += 1
        try:
            if self._plan_executing():
                # A paused plan: the user's message is extra context for the step
                # being resumed, and execution continues where it stopped.
                self._append_user(text)
                self._execute_plan()
                return
            self.messages.append({"role": "user", "content": text})
            self._turn_user = self.messages[-1]
            tools = [] if not self.tools_enabled else self._current_tools()
            outcome = self._run_loop(tools, 40 if self.mode == "design" else 100)
            if outcome == "plan_approved":
                self._execute_plan()
        finally:
            self._emit_status()
            self._autosave()
            self.event_queue.put(DoneEvent())

    def _append_user(self, text: str):
        """Append a user turn, keeping the history well-formed: bridge a tool→user
        gap with an empty assistant turn (Qwen3's template requires it) and merge
        into a trailing user turn rather than creating consecutive user messages."""
        last = self.messages[-1]["role"] if self.messages else ""
        if last == "user":
            self.messages[-1]["content"] = f"{self.messages[-1].get('content') or ''}\n\n{text}".strip()
        else:
            if last == "tool":
                self.messages.append({"role": "assistant", "content": None})
            self.messages.append({"role": "user", "content": text})
        self._turn_user = self.messages[-1]

    def _run_loop(self, tools: list[dict], max_iterations: int) -> str:
        """Run the model/tool loop on the current history until the model gives a
        text-only reply.  Returns the outcome: "done", "empty" (no usable reply),
        "cancelled", "error", "limit" (iteration cap), or "plan_approved" (the
        user approved a create_plan).  The caller emits DoneEvent and autosaves."""
        _MAX_ITERATIONS = max_iterations
        _NUDGE_AFTER = 10  # consecutive tool-only turns before injecting a respond prompt
        investigating = self._plan_investigating()
        executing = self._plan_executing()
        plan_approved = False
        self._step_summary = None  # set by complete_step; ends the loop after this batch

        iteration = 0
        tool_only_turns = 0
        last_tool: str | None = None
        empty_retried = False
        _suppress_think_next = False  # disable thinking for one turn after cutoff or thinking-only retry
        _nudged = False
        _write_nudged = False  # one write-intent recovery nudge per send()
        compact_notice = ""    # last auto-compaction notice, to not repeat a no-op one
        while True:
            if self._cancel.is_set():
                self.event_queue.put(ChatEvent("system", "Interrupted."))
                return "cancelled"
            if iteration >= _MAX_ITERATIONS:
                self.event_queue.put(ErrorEvent(f"Tool call loop exceeded {_MAX_ITERATIONS} iterations — stopping"))
                return "limit"
            iteration += 1
            # auto-compact if needed
            self._stream_chars = 0
            self._token_estimate = self._estimate(schemas=True)
            if self._estimate() > self.context_limit:
                changed, notice = self._compact()
                # A compaction that could not help is reported once, not every iteration.
                if changed or notice != compact_notice:
                    self.event_queue.put(ChatEvent("system", notice))
                    self._emit_status()
                compact_notice = notice

            self.logger.log_request(
                self.mode, self.client.model,
                len(self.messages),
                self._token_estimate,
            )

            try:
                # role="thinking" is a harness-internal representation; strip it
                # before handing the canonical history to the provider adapter.
                # Provider-specific outbound transforms (e.g. Ollama's Qwen XML
                # escaping) happen inside the adapter's chat().
                api_messages = [m for m in self.messages if m.get("role") != "thinking"]
                think_this_turn = False if _suppress_think_next else self.think
                _suppress_think_next = False
                # num_ctx is the model's real window when known, so the model can
                # use its full context; context_limit governs compaction separately.
                num_ctx = self.model_max_ctx or self.context_limit
                stream_kw = {"on_delta": self._delta_sink()} if self.stream else {}
                try:
                    response = self.client.chat(api_messages, tools,
                                                think=think_this_turn,
                                                num_ctx=num_ctx, **stream_kw)
                finally:
                    if stream_kw:
                        stream_kw["on_delta"].flush()
                        self.event_queue.put(StreamEndEvent())
                    self._stream_chars = 0
            except Exception as e:
                if self._cancel.is_set():
                    self.event_queue.put(ChatEvent("system", "Interrupted."))
                    return "cancelled"
                self.event_queue.put(ErrorEvent(f"{self.client.provider_name} error: {e}"))
                return "error"

            # response is a normalized ChatResponse — the adapter has already mapped
            # its provider's wire format onto this shape.
            prompt_tokens = response.prompt_tokens
            eval_tokens = response.eval_tokens
            done_reason = response.done_reason  # "stop" | "length" | None

            if prompt_tokens is not None:
                self._token_estimate = (prompt_tokens or 0) + (eval_tokens or 0)
                self._measured_tokens = self._token_estimate
            else:
                # No server count: fold the streamed reply into the estimate so the
                # bar does not fall back when the stream preview ends.
                self._token_estimate += _text_tokens((response.content or "") + (response.thinking or ""))

            # Extract thinking tokens from two sources:
            # 1. response.thinking — the adapter's reasoning field (Ollama's msg.thinking,
            #    llama.cpp's reasoning_content) when reasoning is enabled.
            # 2. <think>…</think> tags — Qwen3/Qwen3.5 embed them even when think=False.
            # Neither must re-enter the context (stored as role="thinking", filtered at API call).
            raw_thinking = response.thinking
            content, thinking_from_content = _extract_and_strip_thinking(response.content)
            if thinking_from_content and not raw_thinking:
                raw_thinking = thinking_from_content

            if raw_thinking:
                self.messages.append({"role": "thinking", "content": raw_thinking})
                self.event_queue.put(ThinkEvent(raw_thinking))

            # Native tool calls the adapter parsed from the API response.
            _calls: list[tuple[str, dict]] = [(tc.name, tc.arguments) for tc in response.tool_calls]

            # Recover tool calls embedded as text when the model bypassed the
            # tool API.  Suppress the raw content in that case — the tool events
            # shown in the TUI carry the information without redundancy.
            if not _calls and content:
                recovered = _extract_text_tool_calls(content, tools)
                if recovered:
                    _calls = [(r["name"], r["arguments"]) for r in recovered]
                    content = ""

            self.logger.log_response(
                self.mode, self.client.model,
                prompt_tokens, eval_tokens,
                bool(_calls),
            )

            # Model produced something — reset the empty-retry window so a later
            # empty response gets one fresh retry rather than failing immediately.
            if content or _calls:
                empty_retried = False

            # Emit preamble text before tool events so it appears above tool output
            # in the TUI. Deferring it until after tools run places it below the
            # result, making the response look cut off.
            if content:
                tool_only_turns = 0
                self.event_queue.put(ChatEvent("assistant", content))

            if not _calls:
                if not content:
                    if last_tool == "write_file":
                        # After write_file the model sometimes returns nothing — the
                        # write already happened so this is a clean terminal state.
                        self.event_queue.put(ChatEvent("system",
                            "File written."))
                    elif not empty_retried:
                        # Retry once with an explicit prompt. Only inject a user message
                        # if the last turn is not already a user turn — consecutive user
                        # messages are not valid in Ollama's turn format.
                        empty_retried = True
                        if self.messages[-1]["role"] != "user":
                            if done_reason == "length":
                                # Generation cut off by context window — thinking consumed all tokens.
                                # Disable thinking for the retry so it has budget to respond.
                                _suppress_think_next = True
                                self.event_queue.put(ChatEvent("system",
                                    "Response cut off (context limit). Retrying without thinking."))
                                retry_text = (
                                    "Your previous response was cut off. "
                                    "Do NOT output any reasoning or thinking. "
                                    "Call write_file with both 'path' (the file path to write) and 'content' (the complete document). "
                                    "Or call ask_user if you need information."
                                    if self.mode == "design" else
                                    "Your previous response was cut off. "
                                    "Do NOT output any reasoning or thinking. "
                                    "Call create_plan now if your investigation is complete, "
                                    "or call a tool to continue investigating, or ask_user if you need information."
                                    if investigating else
                                    "Your previous response was cut off. "
                                    "Do NOT output any reasoning or thinking. "
                                    "Call a tool directly or write a brief response."
                                )
                            elif raw_thinking:
                                # Model reasoned but produced no output or tool call.
                                # Disable thinking for the retry — passing think=True on a
                                # retry after a thinking-only turn causes Ollama's Qwen3
                                # XML template to generate malformed tool definitions (500).
                                _suppress_think_next = True
                                retry_text = (
                                    "You produced reasoning but no response or tool call. "
                                    "Based on your analysis, call write_file now with both 'path' (the file path to write) and 'content' (the complete document). "
                                    "Or call ask_user if you need more information."
                                    if self.mode == "design" else
                                    "You produced reasoning but no response or tool call. "
                                    "Based on your analysis, call create_plan now if you are ready, "
                                    "call a tool to keep investigating, or call ask_user if you need information."
                                    if investigating else
                                    "You produced reasoning but no response or tool call. "
                                    "Based on your analysis, call a tool to continue or write your conclusion."
                                )
                            else:
                                retry_text = (
                                    "Please respond. If you are ready to write the design, "
                                    "call write_file now with both 'path' (the file path to write) and 'content' (the full document)."
                                    if self.mode == "design" else
                                    "Please respond. If your investigation is complete, call create_plan now; "
                                    "otherwise call a tool to continue."
                                    if investigating else
                                    "Please respond with your current analysis or next step."
                                )
                            # Bridge a tool→user gap: Qwen3 expects an assistant turn
                            # between tool results and the next user turn. Without it
                            # the template may produce malformed XML for tool definitions.
                            if self.messages[-1]["role"] == "tool":
                                self.messages.append({"role": "assistant", "content": None})
                            self.messages.append({"role": "user", "content": retry_text})
                        tool_only_turns = 0
                        continue
                    else:
                        # Second consecutive empty. Remove the injected retry message
                        # so the next user send does not arrive as a consecutive-user pair.
                        if (self.messages
                                and self.messages[-1]["role"] == "user"
                                and self.messages[-1].get("content", "").startswith("Please respond")):
                            self.messages.pop()
                        self.event_queue.put(ChatEvent("system",
                            "No response. Please rephrase or add more detail and try again."))
                        return "empty"
                    return "done"
                # Design mode: model announced it would write but produced no tool call.
                # Inject one targeted nudge and continue the loop so it can comply.
                if (self.mode == "design"
                        and _has_write_intent(content)
                        and not _write_nudged):
                    _write_nudged = True
                    self.messages.append({"role": "assistant", "content": content or None})
                    self.messages.append({"role": "user", "content":
                        "You said you would write the design but did not call write_file. "
                        "Call write_file now with both 'path' (the file path to write) and 'content' (the complete document)."
                    })
                    continue
                self.messages.append({"role": "assistant", "content": content or None})
                break

            # track consecutive tool-only turns
            if not content:
                tool_only_turns += 1

            # Append the assistant turn.  Use the minimal tool_calls format:
            # {"function": {"name": ..., "arguments": ...}} with no id or type
            # fields — Ollama's template engine rejects those in message history.
            # _calls holds both native (adapter-parsed) and text-recovered calls;
            # _strip_text_tool_calls scrubs any raw tool markup left in content so
            # tagged/XML formats don't corrupt Qwen3's prompt on the next turn.
            # Arguments are stored verbatim, write_file content included.  Replacing
            # it with a placeholder made the model doubt its write landed: it
            # rewrote the same file in a loop and eventually copied the placeholder
            # itself, overwriting the file.  Ollama's Qwen3 template is protected
            # by the adapter's XML escaping instead.
            self.messages.append({
                "role": "assistant",
                "content": _strip_text_tool_calls(content) or None,
                "tool_calls": [
                    {"function": {"name": n, "arguments": a}}
                    for n, a in _calls
                ],
            })

            for name, args in _calls:
                # complete_step ends the step: calls after it in the same batch are not
                # run, but each still gets a result so the call/result pairing holds.
                if self._step_summary is not None:
                    self.event_queue.put(ToolResultEvent(name, "not run — step already completed"))
                    self.messages.append({"role": "tool", "name": name, "content":
                        "Not run: complete_step was called earlier in this turn, which ended the step."})
                    continue

                # Rescue a file write that arrived with content but no path (some
                # models drop the trailing 'path' after a large 'content' value):
                # infer a filename instead of failing the required-argument check.
                if (name in ("write_file", "append_to_file")
                        and isinstance(args, dict)
                        and args.get("content") and not args.get("path")):
                    inferred = _derive_write_path(args["content"], self.mode)
                    args = {**args, "path": inferred}
                    self.event_queue.put(ChatEvent("system",
                        f"write_file was missing 'path' — inferred '{inferred}' from the content."))

                shown = _mask_tool_args(name, args)
                self.event_queue.put(ToolCallEvent(name, shown))
                self.logger.log_tool_call(self.mode, self.client.model, name, shown)

                # Snapshot the target file before a mutating tool runs so the
                # post-edit diff can be built against its previous contents.
                diff_old: str | None = None
                diff_existed = False
                if name in _MUTATING_TOOLS:
                    snap_path = args.get("src") if name == "move_file" else args.get("path")
                    if snap_path:
                        diff_old, diff_existed = self._read_text_safe(snap_path)

                if name == "ask_user":
                    # Block the worker thread until the TUI routes the user's answer back.
                    # The TUI detects AskUserEvent, switches to waiting-for-input state,
                    # and calls provide_user_input() when the user submits a response.
                    answer = self._ask_user(args.get("question", ""))
                    result = f"User answered: {answer}"
                elif name == "create_plan":
                    result = self._handle_create_plan(args)
                    if self.plan_phase == "executing":
                        plan_approved = True
                elif name == "revise_plan":
                    result = self._handle_revise_plan(args)
                elif name == "complete_step":
                    if executing:
                        self._step_summary = str(args.get("summary") or "").strip()
                        result = "Step marked complete."
                    else:
                        result = "ERROR: complete_step is only available while executing an approved plan."
                else:
                    # run_command confirmation: when enabled, block on a y/N prompt
                    # (reusing the ask_user input plumbing) before executing.
                    if name == "run_command" and self.run_confirm:
                        cmd = args.get("command", "")
                        answer = self._ask_user(
                            f"Run this command? Reply 'y' to allow, anything else to decline.\n  $ {cmd}"
                        ).strip().lower()
                        if answer in ("y", "yes"):
                            result = self._dispatch(name, args)
                        else:
                            result = "ERROR: command declined by user"
                    elif name == "fetch_url" and (q := self._net_confirm_prompt(args)):
                        answer = self._ask_user(q).strip().lower()
                        if answer in ("y", "yes"):
                            result = self._dispatch(name, args)
                        else:
                            result = ("ERROR: the user declined this request, so it was "
                                      "never sent. The server was not contacted and nothing "
                                      "is wrong with it. Do not retry — ask the user what "
                                      "they would like to do instead.")
                    else:
                        result = self._dispatch(name, args)
                    # fetch_url windows its own output (net_max_chars) and says how to
                    # page on; a generic cut here would slice through the untrusted-
                    # content fence and point the model at read_file.
                    if (self.max_tool_result > 0 and len(result) > self.max_tool_result
                            and name != "fetch_url"):
                        total = len(result)
                        cutoff = result.rfind("\n", 0, self.max_tool_result)
                        if cutoff < self.max_tool_result // 2:
                            cutoff = self.max_tool_result
                        result = result[:cutoff] + f"\n... (truncated after {cutoff} chars of {total} — use read_file with start_line/end_line for specific sections)"

                # For mutating tools, show a diff of what changed on disk instead
                # of the terse "OK" result. Falls back to ToolResultEvent on errors
                # or no-op edits so failures stay visible.
                if not self._emit_diff(name, args, result, diff_old, diff_existed):
                    self.event_queue.put(ToolResultEvent(name, result))
                self.logger.log_tool_result(self.mode, self.client.model, name, len(result))

                # Store the tool name on the message so export/replay can label
                # results and templates can match responses to their calls.
                self.messages.append({"role": "tool", "name": name, "content": result})

                # Track last_tool and reset counter only on successful calls.
                # write_file is sticky — it must not be overwritten by a later
                # tool in the same batch so the terminal detection below works.
                _call_ok = not result.startswith("ERROR:")
                if name == "write_file" and _call_ok:
                    last_tool = "write_file"
                    tool_only_turns = 0  # terminal action; suppress nudge on follow-up
                elif last_tool != "write_file":
                    last_tool = name

            # The user approved the plan: stop investigating and let the caller
            # drive execution.  Remaining calls in the batch have already run.
            if plan_approved:
                return "plan_approved"
            if self._step_summary is not None:
                if self._step_summary:
                    self.event_queue.put(ChatEvent("assistant", self._step_summary))
                return "done"

            # Inject as role "user" — Ollama's tool-use turn format expects
            # assistant → tool(s) → user; a mid-conversation system message is
            # not supported and would break the alternating turn structure.
            # Capped at one nudge per send() call to avoid polluting the history.
            if tool_only_turns >= _NUDGE_AFTER and not _nudged:
                _nudged = True
                nudge = (
                    "You have been calling tools for several turns without a text response. "
                    "If you have gathered enough information to write the design, call write_file now with both 'path' (the file path to write) and 'content' (the complete spec). "
                    "If you need more information, ask the user with ask_user. "
                    "Otherwise write a text response summarising what you have found so far."
                    if self.mode == "design" else
                    "You have been calling tools for several turns without a text response. "
                    "If your investigation is complete, call create_plan now. "
                    "If a decision needs the user, call ask_user. "
                    "Otherwise keep investigating, with one line of text alongside your next tool call."
                    if investigating else
                    "You have been calling tools for several turns without a text response. "
                    "Write one line on your progress with your next tool call. Once the current "
                    "plan step is complete and verified, call complete_step with a short summary."
                    if executing else
                    "You have been calling tools for several turns without a text response. "
                    "Stop and summarise what you have found or done so far, "
                    "or describe your next step if you are not finished."
                )
                self.messages.append({"role": "user", "content": nudge})
                tool_only_turns = 0

        return "done"

    # ── plan mode ─────────────────────────────────────────────────────────────

    def _delta_sink(self):
        """Streaming callback that coalesces deltas (~50 ms / 200 chars) before
        putting them on the bus, so frontends are not flooded token by token."""
        buf: list[str] = []
        state = {"kind": None, "ts": time.monotonic()}

        def flush():
            if buf and state["kind"]:
                text = "".join(buf)
                self.event_queue.put(DeltaEvent(state["kind"], text))
                # Streamed tokens (content and thinking alike) occupy the window
                # while they are generated: move the status bar with them.
                before = self._ctx_pct()
                self._stream_chars += len(text)
                if self._ctx_pct() != before:
                    self._emit_status()
            buf.clear()
            state["ts"] = time.monotonic()

        def emit(kind: str, text: str):
            if not text:
                return
            if kind != state["kind"]:
                flush()
                state["kind"] = kind
            buf.append(text)
            if time.monotonic() - state["ts"] >= 0.05 or sum(map(len, buf)) >= 200:
                flush()

        # Qwen-style models put reasoning inside <think>…</think> in the content
        # stream; route it to "thinking" live, as the final response does. Tags may
        # be split across chunks, so a possible partial tag is held back.
        tag = {"in": False, "carry": ""}

        def on_delta(kind: str, text: str):
            if kind != "content":
                emit(kind, text)
                return
            text = tag["carry"] + text
            tag["carry"] = ""
            while text:
                marker = "</think>" if tag["in"] else "<think>"
                idx = text.find(marker)
                if idx == -1:
                    keep = next((k for k in range(min(len(marker) - 1, len(text)), 0, -1)
                                 if marker.startswith(text[-k:])), 0)
                    emit("thinking" if tag["in"] else "content", text[:len(text) - keep])
                    tag["carry"] = text[len(text) - keep:]
                    return
                emit("thinking" if tag["in"] else "content", text[:idx])
                tag["in"] = not tag["in"]
                text = text[idx + len(marker):]

        def finish():
            if tag["carry"]:
                emit("thinking" if tag["in"] else "content", tag["carry"])
                tag["carry"] = ""
            flush()

        on_delta.flush = finish
        return on_delta

    def _refresh_system_prompt(self):
        self.messages[0] = {"role": "system", "content": self._build_system_prompt()}

    def _sync_plan_file(self):
        """Mirror the in-memory plan to the plan file; report (not raise) on failure."""
        if self.plan is not None:
            err = write_plan_file(self.plan, self.workdir)
            if err:
                self.event_queue.put(ErrorEvent(err))

    @staticmethod
    def _parse_steps(raw) -> list[Step]:
        """Build steps from a tool's 'steps' argument.  Tolerates a JSON-encoded
        string and bare strings (title only), which small models sometimes send."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [line for line in raw.splitlines() if line.strip()]
        steps = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                steps.append(Step.from_dict({k: v for k, v in item.items() if k != "status"}))
            elif isinstance(item, str) and item.strip():
                steps.append(Step(title=item.strip()))
        return steps

    def approve_plan(self) -> str:
        """Approve the current plan for execution, re-reading the plan file first so
        the user's hand edits are honoured.  Returns '' on success or an error."""
        if self.plan is None:
            return "No plan to run. Switch to /plan and describe a feature or bug first."
        edited = read_plan_file(self.workdir)
        if edited is not None:
            self.plan = edited
        if not self.plan.steps:
            return "The plan has no steps."
        self.plan_phase = "executing"
        self._sync_plan_file()
        self._refresh_system_prompt()
        return ""

    def cancel_plan(self) -> str:
        if self.plan is None:
            return "No active plan."
        title = self.plan.title
        self.plan = None
        self.plan_phase = "investigating"
        delete_plan_file(self.workdir)
        self._refresh_system_prompt()
        self._emit_status()
        return f"Plan discarded: {title} ({PLAN_FILENAME} removed)"

    def _handle_create_plan(self, args: dict) -> str:
        steps = self._parse_steps(args.get("steps"))
        if not steps:
            return ("ERROR: create_plan needs a non-empty 'steps' list of "
                    "{title, details, files} objects. Call it again with the steps.")
        self.plan = Plan(title=str(args.get("title") or "Implementation plan").strip(),
                         goal=str(args.get("goal") or "").strip(), steps=steps)
        self._sync_plan_file()
        self.event_queue.put(ChatEvent("assistant", self.plan.to_markdown()))
        answer = self._ask_user(
            f"Execute this plan? y = run it · n = keep it for later (/plan run) · "
            f"anything else = feedback to revise it. You can edit {PLAN_FILENAME} before answering."
        ).strip()
        low = answer.lower().rstrip(".!")
        if low in ("y", "yes", "ok", "go", "run", "approve", "approved"):
            err = self.approve_plan()
            if err:
                return f"ERROR: {err}"
            n = len(self.plan.steps)
            return (f"User approved the plan ({n} steps). The harness will now drive "
                    f"execution one step at a time.")
        if low in ("", "n", "no", "later", "not now"):
            self.plan_phase = "awaiting_approval"
            self._refresh_system_prompt()
            self._emit_status()
            return (f"User chose to keep the plan for later (saved to {PLAN_FILENAME}). "
                    "Do not call any more tools; reply with one short sentence.")
        self.plan_phase = "investigating"
        self._refresh_system_prompt()
        return (f"User requested changes to the plan: {answer}\n"
                "Investigate further if needed, then call create_plan again with the complete revised plan.")

    def _handle_revise_plan(self, args: dict) -> str:
        if not self._plan_executing():
            return "ERROR: revise_plan is only available while executing an approved plan."
        steps = self._parse_steps(args.get("steps"))
        if not steps:
            return "ERROR: revise_plan needs a non-empty 'steps' list of the remaining steps."
        new_titles = {st.title.strip().lower() for st in steps}
        dropped = [st.title for st in self.plan.steps
                   if not st.finished and st.title.strip().lower() not in new_titles]
        self.plan.replace_remaining(steps)
        steps[0].status = "in_progress"
        self._sync_plan_file()
        self._refresh_system_prompt()
        self._emit_status()
        reason = str(args.get("reason") or "").strip()
        start = len(self.plan.steps) - len(steps) + 1
        listing = "\n".join(f"  {start + i}. {st.title}" for i, st in enumerate(steps))
        dropped_txt = ("\nDropped steps:\n" + "\n".join(f"  - {t}" for t in dropped)) if dropped else ""
        self.event_queue.put(ChatEvent("system",
            f"Plan revised{': ' + reason if reason else ''}\nRemaining steps:\n{listing}{dropped_txt}"))
        warn = ("\nThese steps were dropped because they are not in your list — if that was not "
                "intended (or they are not actually done), call revise_plan again including them:"
                + dropped_txt) if dropped else ""
        return (f"Plan revised. Remaining steps:\n{listing}{warn}\n"
                f"Continue with step {start} only: {steps[0].title}")

    def _execute_plan(self):
        """Drive an approved plan one step at a time.  Each step runs the same tool
        loop as coding mode; a text-only reply completes the step.  Any other loop
        outcome (interrupt, error, iteration cap) pauses execution so it can be
        resumed with /plan resume.  The plan file is deleted when all steps are done."""
        plan = self.plan
        if plan is None:
            self.event_queue.put(ChatEvent("system", "No plan to execute."))
            return
        if self.mode != "plan":
            self.mode = "plan"
        self.plan_phase = "executing"
        while (idx := plan.current_index()) is not None:
            step = plan.steps[idx]
            step.status = "in_progress"
            self._sync_plan_file()
            self._refresh_system_prompt()
            self._emit_status()
            n = len(plan.steps)
            self.event_queue.put(ChatEvent("system", f"▶ Plan step {idx + 1}/{n}: {step.title}"))
            body = [f"Plan step {idx + 1}/{n}: {step.title}"]
            if step.details:
                body.append(step.details)
            if step.files:
                body.append(f"Files: {', '.join(step.files)}")
            body.append("Implement only this step and verify it, then call complete_step with a "
                        "short summary of what you changed and how you verified it.")
            self._append_user("\n\n".join(body))

            tools = self._current_tools() if self.tools_enabled else []
            outcome = self._run_loop(tools, 100)
            if outcome != "done":
                self._sync_plan_file()
                self.event_queue.put(ChatEvent("system",
                    f"Plan paused at step {plan.progress()} — type /plan resume (or any "
                    f"message) to continue, /plan cancel to discard. Progress is kept in {PLAN_FILENAME}."))
                return

            # The step that is in progress now may differ from `idx` if the model
            # called revise_plan during the step.
            cur = plan.current_index()
            if cur is not None and plan.steps[cur].status == "in_progress":
                if self._step_summary:
                    note = self._step_summary
                else:  # text-only reply ended the step
                    last = self.messages[-1] if self.messages else {}
                    note = (last.get("content") or "") if last.get("role") == "assistant" else ""
                note = " ".join(note.split())
                plan.steps[cur].status = "done"
                plan.steps[cur].note = note[:200] + ("…" if len(note) > 200 else "")
            self._sync_plan_file()
            self._autosave()

        total = len(plan.steps)
        self.plan = None
        self.plan_phase = "investigating"
        delete_plan_file(self.workdir)
        self._refresh_system_prompt()
        self.event_queue.put(ChatEvent("system",
            f"Plan complete: {plan.title} ({total} step{'s' if total != 1 else ''}). {PLAN_FILENAME} removed."))

    def execute_plan_threaded(self):
        """Run (or resume) plan execution on a worker thread for /plan run|resume."""
        self._cancel.clear()
        try:
            self._execute_plan()
        finally:
            self._emit_status()
            self._autosave()
            self.event_queue.put(DoneEvent())

    def _plan_progress(self) -> str:
        if self.mode != "plan" or self.plan is None:
            return ""
        if self.plan_phase == "executing":
            return f"exec {self.plan.progress()}"
        if self.plan_phase == "awaiting_approval":
            return "awaiting approval"
        return "draft"

    def _emit_status(self):
        self.event_queue.put(self.status_event())

    def status_event(self) -> StatusEvent:
        """Snapshot of the status-bar fields (also served to the web UI)."""
        pct = self._ctx_pct()
        return StatusEvent(
            mode=self.mode,
            model=self.client.model,
            workdir=str(self.workdir),
            ctx_pct=pct,
            ctx_color=self._ctx_color(pct),
            tools_enabled=self.tools_enabled,
            run_confirm=self.run_confirm,
            net_access=self.net_access,
            net_confirm=self.net_confirm,
            net_max_bytes=self.net_max_bytes,
            net_max_chars=self.net_max_chars,
            host=self.client.host,
            provider=self.provider,
            plan_progress=self._plan_progress(),
            guides=self.guides,
            **self._index_status_fields(),
        )

    def _index_status_fields(self) -> dict:
        idx = self.index
        base = {"index_enabled": self.index_enabled, "index_max_bytes": self.index_max_bytes,
                "index_max_files": self.index_max_files, "index_workers": self.index_workers,
                "index_persist": self.index_persist, "index_route": self.index_route}
        if idx is None:
            return {**base, "index_state": "off"}
        busy = idx.state in ("building", "refreshing") and idx.total
        return {**base, "index_state": idx.state,
                "index_progress": f"{idx.done}/{idx.total}" if busy else "",
                "index_files": idx.live_count(), "index_mem": idx.mem_used(),
                "index_degraded": idx.degraded()}

    def list_available_skills(self) -> list[str]:
        if not _SKILLS_DIR.exists():
            return []
        return sorted(p.stem for p in _SKILLS_DIR.glob("*.md"))

    def load_skill(self, name: str) -> str:
        name = name.removesuffix(".md")
        p = _SKILLS_DIR / f"{name}.md"
        if not p.exists():
            available = self.list_available_skills()
            hint = f"Available: {', '.join(available)}" if available else "No skills found in skills/ folder."
            return f"ERROR: skill '{name}' not found. {hint}"
        if name in self.active_skills:
            return f"Skill '{name}' is already active."
        self.active_skills.append(name)
        self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        return f"Skill loaded: {name}"

    def unload_skill(self, name: str) -> str:
        name = name.removesuffix(".md")
        if name not in self.active_skills:
            return f"Skill '{name}' is not active."
        self.active_skills.remove(name)
        self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        return f"Skill unloaded: {name}"

    def _autosave(self):
        session_mod.save(
            self._ts, self.client.model, self.mode,
            self.workdir, _mask_messages(self.messages), self.context_limit,
            self.active_skills,
            self.input_history,
            context_pct=self.context_pct,
            context_fixed=self.context_fixed,
            host=self.client.host,
            provider=self.provider,
            plan=self.plan.to_dict() if self.plan is not None else None,
            plan_phase=self.plan_phase,
            momo_lines=self.momo_lines,
            momo_recap_turn=self._momo_recap_turn,
            turn_count=self._turn_count,
        )
        session_mod.save_prefs(model=self.client.model, provider=self.provider)

    def load_session(self, path: Path) -> str:
        data = session_mod.load(path)
        self.messages = data["messages"]
        # Sessions saved in a mode that no longer exists (e.g. the removed
        # "writing" mode) fall back to design mode.
        mode = data.get("mode", "design")
        self.mode = mode if mode in _MODE_TOOLS else "design"
        self.workdir = Path(data.get("workdir", str(self.workdir)))
        self.context_pct = data.get("context_pct", None)
        # Sessions from before context_fixed saved every limit, derived or not:
        # they now follow the model's window.
        self.context_fixed = bool(data.get("context_fixed", False)) and self.context_pct is None
        # Restore the provider first: if it changed, rebuild the client so the
        # right adapter (and its default transport) is used.  The saved host is
        # provider-specific, so it must be applied against the matching adapter.
        saved_provider = data.get("provider")
        saved_host = data.get("host")
        saved_model = data.get("model", self.client.model)
        self._expected_backend = (saved_model, data.get("context_limit"))
        new_client = None
        if saved_provider and saved_provider != self.provider:
            try:
                new_client = make_client(
                    saved_provider,
                    host=saved_host or self.client.host,
                    model=saved_model,
                    auth_token=self.client._auth_token,
                )
            except ValueError:
                # Unknown provider in the session file: keep the current backend
                # (and its host/model) rather than failing to start.
                self.event_queue.put(ChatEvent("system",
                    f"Session used unknown provider {saved_provider!r}; "
                    f"staying on {self.provider} ({self.client.host})."))
        if new_client is not None:
            self.provider = saved_provider
            self.client = new_client
        elif saved_provider and saved_provider != self.provider:
            pass  # unknown provider — its host/model don't apply to the current backend
        else:
            # Restore the host before set_model, since the context-length query
            # below runs against it. Older sessions without a saved host keep the
            # current one.
            if saved_host:
                self.client.set_host(saved_host)
            self.client.set_model(saved_model)
        # A fixed-model backend may now be serving a different model than the one
        # saved in this session; trust the server over the saved label.
        self._reconcile_fixed_model()
        if not self.context_fixed:
            self._sync_context_limit(emit=False)
        else:
            self.context_limit = data.get("context_limit", self.context_limit)
            # Still need the model's real window for num_ctx even when the
            # compaction limit is an absolute value rather than a percentage.
            self.model_max_ctx = self.client.context_length()
        self.active_skills = data.get("active_skills", [])
        saved_plan = data.get("plan")
        self.plan = Plan.from_dict(saved_plan) if saved_plan else None
        self.plan_phase = (data.get("plan_phase") or "investigating") if self.plan else "investigating"
        self.input_history.clear()
        self.input_history.extend(data.get("input_history", []))
        self.momo_lines = list(data.get("momo_lines", []))
        self._momo_recap_turn = data.get("momo_recap_turn", 0)
        self._turn_count = data.get("turn_count",
                                    sum(1 for m in self.messages if m.get("role") == "user"))
        self._guides = self._read_guides() if self.guides else []
        # A restored session can point at another workdir: index that one instead.
        if self.index_enabled and (self.index is None or self.index.root != self.workdir.resolve()):
            self._stop_index()
            self._start_index()
        # Always rebuild the system prompt from the current role files and skills on
        # disk — saved sessions carry a snapshot; role/skill edits must take effect.
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        self._token_estimate = self._estimate(schemas=True)
        self._measured_tokens = None
        self._emit_status()
        return f"Session loaded: {path.name} ({len(self.messages)} messages)"

    def session_path(self) -> Path:
        return session_mod.session_path(self._ts)

    def new_session(self) -> str:
        """Save the current session and start an empty one (same model/host/mode)."""
        if len(self.messages) > 1:
            self._autosave()
        self.logger.close()
        self._ts = session_mod.new_timestamp()
        self.logger = Logger(self._ts)
        self.plan = None
        self.plan_phase = "investigating"
        self.momo_lines = []
        self._momo_recap_turn = 0
        self._turn_count = 0
        self._guides = self._read_guides() if self.guides else []
        self.messages = [{"role": "system", "content": self._build_system_prompt()}]
        self._token_estimate = self._estimate(schemas=True)
        self._measured_tokens = None
        self._emit_status()
        notice = f"Started a new session: {self.session_path().name}"
        if (guides := self.guides_summary()):
            notice += f"\n{guides}"
        return notice

    def truncate_at_last_user(self) -> str | None:
        """Drop the last user message and everything after it (replies, tool calls,
        thinking). Returns that message's content, or None if there is none."""
        for i in range(len(self.messages) - 1, 0, -1):
            if self.messages[i].get("role") == "user":
                content = self.messages[i].get("content") or ""
                del self.messages[i:]
                self._token_estimate = self._estimate(schemas=True)
                self._measured_tokens = None
                self._autosave()
                self._emit_status()
                return content
        return None
