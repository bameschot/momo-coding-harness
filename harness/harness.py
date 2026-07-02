from __future__ import annotations

import json
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import session as session_mod
from .diff import build_diff_body
from .logger import Logger
from .ollama_client import OllamaClient
from .tools import DESIGN_TOOLS, WRITER_TOOLS, ALL_TOOLS, CHAT_TOOLS, dispatch

# Tools that mutate a file on disk — the harness snapshots the target before and
# after these run to build a DiffEvent for the TUI.  Keyed by the arg holding the
# affected path ("move_file" uses src/dst and is handled separately).
_MUTATING_TOOLS = {
    "edit_file", "replace_all_in_file", "append_to_file",
    "write_file", "delete_file", "move_file",
}

_ROLES_DIR  = Path(__file__).parent.parent / "roles"
_SKILLS_DIR = Path(__file__).parent.parent / "skills"


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


# Tools whose "content" argument can be large and contain characters (<, >, &)
# that break Ollama's Qwen3 XML template when re-sent in message history.
# We replace the content with a short placeholder after storage — the file is
# on disk and the model can read_file it back if needed.
_CONTENT_ARG_TOOLS = {"write_file", "append_to_file"}


def _sanitize_tool_args(name: str, args: dict) -> dict:
    """Replace the 'content' value for file-writing tools with a placeholder.
    Prevents large file bodies (Python scripts, etc.) with XML-special characters
    from being embedded verbatim in Ollama's XML chat template on subsequent turns."""
    if name in _CONTENT_ARG_TOOLS and "content" in args:
        return {k: (f"[written to {args.get('path', 'file')}]" if k == "content" else v)
                for k, v in args.items()}
    return args


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


def _is_qwen(model: str) -> bool:
    return "qwen" in model.lower()


def _xml_escape_str(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_escape_args(obj: object) -> object:
    """Recursively escape XML special chars in string values within a tool-call argument structure."""
    if isinstance(obj, dict):
        return {k: _xml_escape_args(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_xml_escape_args(v) for v in obj]
    if isinstance(obj, str):
        return _xml_escape_str(obj)
    return obj


def _xml_escape_for_ollama(messages: list[dict]) -> list[dict]:
    """Return a shallow copy of messages with XML-special characters escaped for Qwen3's template.

    Qwen3's Ollama template wraps tool results in <tool_response>…</tool_response> XML and
    embeds past tool-call arguments in XML-like structure.  Unescaped < > & in either location
    break the XML parser and produce a 500.  We escape only the copy sent to the API —
    the stored messages keep the correct, unescaped content so the history is accurate."""
    result = []
    for m in messages:
        role = m.get("role")
        if role == "tool" and isinstance(m.get("content"), str):
            m = dict(m)
            m["content"] = _xml_escape_str(m["content"])
        elif role == "assistant" and m.get("tool_calls"):
            m = dict(m)
            m["tool_calls"] = [
                {"function": {
                    "name": tc["function"]["name"],
                    "arguments": _xml_escape_args(tc["function"]["arguments"]),
                }}
                for tc in m["tool_calls"]
            ]
        result.append(m)
    return result


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
    host: str = ""

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

def _writing_prompt(workdir: str = "") -> str:
    return _load_role("writer") or (
        "You are a writing assistant. Help the user write, edit, and improve documents. "
        "Read existing documents before editing them. "
        "Prefer targeted replacements (replace_all_in_file) over full rewrites. "
        "Match the register and tone of the existing text unless instructed otherwise. "
        "Ask one focused question when intent is ambiguous. "
        "Save documents with write_file or extend them with append_to_file — "
        "do not paste long content in chat."
    )

def _chat_prompt() -> str:
    return _load_role("chat") or (
        "You are a knowledgeable assistant. Read code and documents when the user "
        "points at them, then answer questions and actively ask follow-up questions "
        "to deepen understanding. Never write or modify files."
    )

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
    "writing": lambda wd: _writing_prompt(wd),
    "chat":    lambda wd: _chat_prompt(),
    "momo":    lambda wd: _momo_prompt(),
}

_MODE_TOOLS = {
    "design":  DESIGN_TOOLS,
    "writing": WRITER_TOOLS,
    "coding":  ALL_TOOLS,
    "chat":    CHAT_TOOLS,
    "momo":    ALL_TOOLS,
}


# ── token estimation ──────────────────────────────────────────────────────────

def _estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        total += len(content) // 4
    return total


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


class Harness:
    def __init__(self, host: str, model: str, workdir: Path):
        self.workdir = workdir.resolve()
        self.mode = "design"
        self.context_limit = _DEFAULT_CONTEXT
        self._ts = session_mod.new_timestamp()
        self.logger = Logger(self._ts)
        self.client = OllamaClient(host=host, model=model)
        self.event_queue: queue.Queue[Any] = queue.Queue()
        self._user_input_queue: queue.Queue[str] = queue.Queue()
        self.max_tool_result = 0   # chars; 0 = unlimited; configurable via /tool-result or --max-tool-result
        self.think: bool = True    # enable model thinking/reasoning mode; configurable via /think or --think
        self.tools_enabled: bool = True
        self.run_confirm: bool = False  # when True, prompt y/N before each run_command; toggle via /run-confirm or Shift+P
        self.active_skills: list[str] = []
        self.input_history: list[str] = []
        self.model_max_ctx: int | None = None  # model's real reported context window; used as num_ctx
        self.context_pct: int | None = None  # user-set % of model max; None = use default 50%
        self.messages: list[dict] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        self._token_estimate = 0
        self._cancel = threading.Event()
        self._sync_context_limit(emit=True)

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
        """Called from the TUI thread when the user answers a mid-task ask_user question."""
        self._user_input_queue.put(text)

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
            "edit_file": "edit", "replace_all_in_file": "edit",
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

    def set_model(self, model: str):
        """Switch model and re-sync context limit from the new model's capabilities."""
        self.client.set_model(model)
        self._sync_context_limit(emit=True)
        self._emit_status()

    # ── mode switching ────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        loader = _ROLE_LOADERS.get(self.mode, _ROLE_LOADERS["coding"])
        base = loader(str(self.workdir))
        if str(self.workdir) not in base:
            base += f"\n\nWorking directory: {self.workdir}"
        parts = []
        for name in self.active_skills:
            p = _SKILLS_DIR / f"{name}.md"
            if p.exists():
                parts.append(p.read_text(encoding="utf-8").strip())
        if parts:
            return base + "\n\n---\n\n" + "\n\n---\n\n".join(parts)
        return base

    def set_mode(self, mode: str):
        self.mode = mode
        self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        self._emit_status()

    # ── context management ────────────────────────────────────────────────────

    def _ctx_pct(self) -> int:
        return min(100, int(self._token_estimate / self.context_limit * 100))

    def _ctx_color(self, pct: int) -> str:
        if pct >= 90:
            return "red"
        if pct >= 75:
            return "yellow"
        return "normal"

    def compact(self, summarise: bool = True) -> str:
        before = self._token_estimate
        removed = 0
        target = self.context_limit // 3
        removed_msgs: list[dict] = []

        # Pass 1: remove tool-call groups (assistant + all its tool/thinking results)
        i = 1  # keep system prompt at 0
        while i < len(self.messages) and self._estimate() > target:
            msg = self.messages[i]
            if msg["role"] == "assistant":
                j = i + 1
                while j < len(self.messages) and self.messages[j]["role"] in ("tool", "thinking"):
                    j += 1
                if j > i + 1:
                    group = self.messages[i:j]
                    removed_msgs.extend(group)
                    del self.messages[i:j]
                    removed += len(group)
                    continue
            elif msg["role"] in ("tool", "thinking"):
                # orphaned tool/thinking with no preceding assistant
                removed_msgs.append(self.messages[i])
                del self.messages[i]
                removed += 1
                continue
            i += 1

        # Pass 2: remove oldest user+assistant pairs
        i = 1
        while i < len(self.messages) and self._estimate() > target:
            msg = self.messages[i]
            if msg["role"] == "user":
                removed_msgs.append(self.messages[i])
                del self.messages[i]
                removed += 1
                if i < len(self.messages) and self.messages[i]["role"] == "assistant":
                    removed_msgs.append(self.messages[i])
                    del self.messages[i]
                    removed += 1
            else:
                i += 1

        # Summarise removed messages and inject into oldest remaining user turn
        summary = self._summarize_removed(removed_msgs) if (removed_msgs and summarise) else ""
        if summary:
            for msg in self.messages[1:]:
                if msg["role"] == "user":
                    msg["content"] = f"[Earlier context summary:\n{summary}\n]\n\n{msg['content']}"
                    break

        after = self._estimate()
        self._token_estimate = after
        action = "summarised" if summary else "removed"
        notice = (f"Context compacted: {action} {removed} messages "
                  f"(was ~{before} tokens, now ~{after} tokens)")
        self.logger.log_compact(self.mode, self.client.model, removed, before, after)
        return notice

    def _summarize_removed(self, msgs: list[dict]) -> str:
        """One-shot LLM call to summarise removed messages. Returns '' on failure."""
        conversation = _format_for_summary(msgs)
        if not conversation.strip():
            return ""
        prompt = [{
            "role": "user",
            "content": (
                "Summarize the following conversation fragment concisely. "
                "Preserve: key decisions, file names, code entities, outcomes, and "
                "any facts needed to continue the work. Omit pleasantries and filler.\n\n"
                + conversation
            )
        }]
        try:
            response = self.client.chat(prompt, [])
            return getattr(response.message, "content", "") or ""
        except Exception:
            return ""

    def compact_threaded(self, summarise: bool = True):
        """Run compact() on a worker thread, emitting events back to the TUI."""
        try:
            notice = self.compact(summarise=summarise)
            self.event_queue.put(ChatEvent("system", notice))
            self._emit_status()
        finally:
            self.event_queue.put(DoneEvent())

    def _estimate(self) -> int:
        return _estimate_tokens(self.messages)

    def cancel(self):
        """Interrupt the running LLM call immediately."""
        self._cancel.set()
        self.client.abort()

    # ── send ──────────────────────────────────────────────────────────────────

    def send(self, text: str):
        """Called from the harness worker thread."""
        self._cancel.clear()
        self.messages.append({"role": "user", "content": text})

        tools = [] if not self.tools_enabled else _MODE_TOOLS.get(self.mode, ALL_TOOLS)
        _MAX_ITERATIONS = 40 if self.mode == "design" else 100
        _NUDGE_AFTER = 10  # consecutive tool-only turns before injecting a respond prompt

        iteration = 0
        tool_only_turns = 0
        last_tool: str | None = None
        empty_retried = False
        _suppress_think_next = False  # disable thinking for one turn after cutoff or thinking-only retry
        _nudged = False
        _write_nudged = False  # one write-intent recovery nudge per send()
        while True:
            if self._cancel.is_set():
                self.event_queue.put(ChatEvent("system", "Interrupted."))
                self._autosave()
                self.event_queue.put(DoneEvent())
                return
            if iteration >= _MAX_ITERATIONS:
                self.event_queue.put(ErrorEvent(f"Tool call loop exceeded {_MAX_ITERATIONS} iterations — stopping"))
                self._autosave()
                self.event_queue.put(DoneEvent())
                return
            iteration += 1
            # auto-compact if needed
            self._token_estimate = self._estimate()
            if self._token_estimate > self.context_limit:
                notice = self.compact()
                self.event_queue.put(ChatEvent("system", notice))
                self._emit_status()

            self.logger.log_request(
                self.mode, self.client.model,
                len(self.messages),
                self._token_estimate,
            )

            try:
                api_messages = [m for m in self.messages if m.get("role") != "thinking"]
                if _is_qwen(self.client.model):
                    api_messages = _xml_escape_for_ollama(api_messages)
                think_this_turn = False if _suppress_think_next else self.think
                _suppress_think_next = False
                # num_ctx is the model's real window when known, so the model can
                # use its full context; context_limit governs compaction separately.
                num_ctx = self.model_max_ctx or self.context_limit
                response = self.client.chat(api_messages, tools,
                                            think=think_this_turn,
                                            num_ctx=num_ctx)
            except Exception as e:
                if self._cancel.is_set():
                    self.event_queue.put(ChatEvent("system", "Interrupted."))
                else:
                    self.event_queue.put(ErrorEvent(f"Ollama error: {e}"))
                self._autosave()
                self.event_queue.put(DoneEvent())
                return

            msg = response.message
            prompt_tokens = getattr(response, "prompt_eval_count", None)
            eval_tokens = getattr(response, "eval_count", None)
            done_reason = getattr(response, "done_reason", None)  # "stop" | "length" | None

            if prompt_tokens is not None:
                self._token_estimate = (prompt_tokens or 0) + (eval_tokens or 0)

            # Extract thinking tokens from two sources:
            # 1. msg.thinking — newer Ollama SDK field when think=True.
            # 2. <think>…</think> tags — Qwen3/Qwen3.5 embed them even when think=False.
            # Neither must re-enter the context (stored as role="thinking", filtered at API call).
            raw_thinking = getattr(msg, "thinking", "") or ""
            raw_content  = getattr(msg, "content",  "") or ""
            content, thinking_from_content = _extract_and_strip_thinking(raw_content)
            if thinking_from_content and not raw_thinking:
                raw_thinking = thinking_from_content

            if raw_thinking:
                self.messages.append({"role": "thinking", "content": raw_thinking})
                self.event_queue.put(ThinkEvent(raw_thinking))

            # Normalize API tool calls to (name, args) tuples.  Older Ollama
            # versions return arguments as a raw JSON string rather than a dict.
            _calls: list[tuple[str, dict]] = []
            for tc in (getattr(msg, "tool_calls", None) or []):
                args = tc.function.arguments or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                _calls.append((tc.function.name, args))

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
                                    "Based on your analysis, call a tool to continue or write your conclusion."
                                )
                            else:
                                retry_text = (
                                    "Please respond. If you are ready to write the design, "
                                    "call write_file now with both 'path' (the file path to write) and 'content' (the full document)."
                                    if self.mode == "design" else
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
                    self._autosave()
                    self.event_queue.put(DoneEvent())
                    return
                # Design mode: model announced it would write but produced no tool call.
                # Inject one targeted nudge and continue the loop so it can comply.
                if (self.mode in ("design", "writing")
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

            # Append the assistant turn.  Use Ollama's minimal tool_calls format:
            # {"function": {"name": ..., "arguments": ...}} with no id or type
            # fields — Ollama's template engine rejects those in message history.
            _qwen = _is_qwen(self.client.model)
            if msg.tool_calls:
                self.messages.append({
                    "role": "assistant",
                    "content": _strip_text_tool_calls(content) or None,
                    "tool_calls": [
                        {"function": {"name": tc.function.name,
                                      "arguments": (_sanitize_tool_args(
                                          tc.function.name, tc.function.arguments)
                                          if _qwen else tc.function.arguments)}}
                        for tc in msg.tool_calls
                    ],
                })
            else:
                # Text-recovered calls — also strip tool-call markup from content
                # so raw XML/tagged formats don't corrupt Qwen3's XML prompt.
                self.messages.append({
                    "role": "assistant",
                    "content": _strip_text_tool_calls(content) or None,
                    "tool_calls": [
                        {"function": {"name": n,
                                      "arguments": _sanitize_tool_args(n, a) if _qwen else a}}
                        for n, a in _calls
                    ],
                })

            for name, args in _calls:
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

                self.event_queue.put(ToolCallEvent(name, args))
                self.logger.log_tool_call(self.mode, self.client.model, name, args)

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
                    question = args.get("question", "")
                    self.event_queue.put(AskUserEvent(question))
                    answer = self._user_input_queue.get()
                    result = f"User answered: {answer}"
                else:
                    # run_command confirmation: when enabled, block on a y/N prompt
                    # (reusing the ask_user input plumbing) before executing.
                    if name == "run_command" and self.run_confirm:
                        cmd = args.get("command", "")
                        self.event_queue.put(AskUserEvent(
                            f"Run this command? Reply 'y' to allow, anything else to decline.\n  $ {cmd}"))
                        answer = self._user_input_queue.get().strip().lower()
                        if answer in ("y", "yes"):
                            result = dispatch(name, args, self.workdir)
                        else:
                            result = "ERROR: command declined by user"
                    else:
                        result = dispatch(name, args, self.workdir)
                    if self.max_tool_result > 0 and len(result) > self.max_tool_result:
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
                    "Stop and summarise what you have found or done so far, "
                    "or describe your next step if you are not finished."
                )
                self.messages.append({"role": "user", "content": nudge})
                tool_only_turns = 0

        self._emit_status()
        self._autosave()
        self.event_queue.put(DoneEvent())

    def _emit_status(self):
        pct = self._ctx_pct()
        self.event_queue.put(StatusEvent(
            mode=self.mode,
            model=self.client.model,
            workdir=str(self.workdir),
            ctx_pct=pct,
            ctx_color=self._ctx_color(pct),
            tools_enabled=self.tools_enabled,
            run_confirm=self.run_confirm,
            host=self.client.host,
        ))

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
            self.workdir, self.messages, self.context_limit,
            self.active_skills,
            self.input_history,
            context_pct=self.context_pct,
            host=self.client.host,
        )
        session_mod.save_prefs(model=self.client.model)

    def load_session(self, path: Path) -> str:
        data = session_mod.load(path)
        self.messages = data["messages"]
        self.mode = data.get("mode", "design")
        self.workdir = Path(data.get("workdir", str(self.workdir)))
        self.context_pct = data.get("context_pct", None)
        # Restore the host before set_model, since the context-length query below
        # runs against it. Older sessions without a saved host keep the current one.
        saved_host = data.get("host")
        if saved_host:
            self.client.set_host(saved_host)
        self.client.set_model(data.get("model", self.client.model))
        if self.context_pct is not None:
            self._sync_context_limit(emit=False)
        else:
            self.context_limit = data.get("context_limit", self.context_limit)
            # Still need the model's real window for num_ctx even when the
            # compaction limit is an absolute value rather than a percentage.
            self.model_max_ctx = self.client.context_length()
        self.active_skills = data.get("active_skills", [])
        self.input_history.clear()
        self.input_history.extend(data.get("input_history", []))
        # Always rebuild the system prompt from the current role files and skills on
        # disk — saved sessions carry a snapshot; role/skill edits must take effect.
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        self._token_estimate = self._estimate()
        self._emit_status()
        return f"Session loaded: {path.name} ({len(self.messages)} messages)"

    def session_path(self) -> Path:
        return session_mod.session_path(self._ts)
