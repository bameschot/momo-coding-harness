from __future__ import annotations

import json
import queue
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import session as session_mod
from .logger import Logger
from .ollama_client import OllamaClient
from .tools import DESIGN_TOOLS, ALL_TOOLS, dispatch

_ROLES_DIR = Path(__file__).parent.parent / "roles"


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


def _extract_text_tool_calls(text: str, tools: list[dict]) -> list[dict]:
    """
    Recover tool calls embedded in plain text when the model bypassed the tool
    API.  Tries known tagged formats (tiers 1–2), then falls back to a
    bare-JSON scan anchored to tool-name occurrences in the text (tier 3).

    Tier-3 builds its name-detection pattern dynamically from `tools`, so
    adding a tool to tools.py automatically extends coverage without touching
    this function.

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

    # ── Tier 1: tagged JSON formats ───────────────────────────────────────────

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

    # ── Tier 2: structured text / array formats ───────────────────────────────

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
        segment = text[window_start:]
        for i, ch in enumerate(segment):
            if ch != '{':
                continue
            try:
                obj, _ = _JSON_DECODER.raw_decode(segment, i)
                # Case A: {"name": "write_file", "arguments": {...}}
                hit = _accept_full(obj)
                if hit and hit["name"] == found:
                    if _append(hit):
                        break  # new result added — move on to the next name match
                # Case B: write_file( {...} ) — JSON follows "(" after the tool
                # name, with optional whitespace between "(" and "{".
                pre = segment[max(0, i - 10):i].rstrip()
                if pre.endswith('('):
                    hit = _accept_args(found, obj)
                    if hit and _append(hit):
                        break
            except json.JSONDecodeError:
                continue

    return results


_WRITE_INTENT = (
    "let me write", "i will write", "i'll write", "i'm going to write",
    "writing the design", "writing the spec", "writing it now",
    "write the complete", "write the design", "write the specification",
    "write the spec", "write it now", "now write", "will now write",
)

def _has_write_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _WRITE_INTENT)


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


# ── token estimation ──────────────────────────────────────────────────────────

def _estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        total += len(content) // 4
    return total


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
        self.messages: list[dict] = [
            {"role": "system", "content": _design_prompt()}
        ]
        self._token_estimate = 0
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

    def _sync_context_limit(self, emit: bool = False):
        """Query the model's native context window and use it as the compaction limit."""
        reported = self.client.context_length()
        if reported:
            self.context_limit = reported
            msg = f"Model context: {reported:,} tokens ({self.client.model})"
        else:
            msg = f"Model context: unknown — using default {self.context_limit:,} tokens ({self.client.model})"
        if emit:
            self.event_queue.put(ChatEvent("system", msg))

    def set_model(self, model: str):
        """Switch model and re-sync context limit from the new model's capabilities."""
        self.client.set_model(model)
        self._sync_context_limit(emit=True)
        self._emit_status()

    # ── mode switching ────────────────────────────────────────────────────────

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "design":
            self.messages[0] = {"role": "system", "content": _design_prompt()}
        else:
            self.messages[0] = {"role": "system", "content": _coding_prompt(str(self.workdir))}
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

    def compact(self) -> str:
        before = self._token_estimate
        removed = 0
        target = self.context_limit // 2

        # Pass 1: remove old tool result messages + their triggering assistant message
        i = 1  # keep system prompt at 0
        while i < len(self.messages) and self._estimate() > target:
            msg = self.messages[i]
            if msg["role"] in ("tool", "thinking"):
                # also remove the assistant message immediately before it (if any)
                if i > 0 and self.messages[i - 1]["role"] == "assistant":
                    del self.messages[i - 1]
                    removed += 1
                    i = max(1, i - 1)
                del self.messages[i]
                removed += 1
            else:
                i += 1

        # Pass 2: remove oldest user+assistant pairs
        i = 1
        while i < len(self.messages) and self._estimate() > target:
            msg = self.messages[i]
            if msg["role"] == "user":
                del self.messages[i]
                removed += 1
                # remove following assistant if present
                if i < len(self.messages) and self.messages[i]["role"] == "assistant":
                    del self.messages[i]
                    removed += 1
            else:
                i += 1

        after = self._estimate()
        self._token_estimate = after
        notice = (f"Context compacted: removed {removed} messages "
                  f"(was ~{before} tokens, now ~{after} tokens)")
        self.logger.log_compact(self.mode, self.client.model, removed, before, after)
        return notice

    def _estimate(self) -> int:
        return _estimate_tokens(self.messages)

    # ── send ──────────────────────────────────────────────────────────────────

    def send(self, text: str):
        """Called from the harness worker thread."""
        self.messages.append({"role": "user", "content": text})

        tools = DESIGN_TOOLS if self.mode == "design" else ALL_TOOLS
        _MAX_ITERATIONS = 15 if self.mode == "design" else 20
        _NUDGE_AFTER = 4  # consecutive tool-only turns before injecting a respond prompt

        iteration = 0
        tool_only_turns = 0
        last_tool: str | None = None
        empty_retried = False
        _length_retried = False  # disable thinking for one turn after a length cutoff
        _nudged = False
        _write_nudged = False  # one write-intent recovery nudge per send()
        while True:
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
                think_this_turn = False if _length_retried else self.think
                _length_retried = False
                response = self.client.chat(api_messages, tools,
                                            think=think_this_turn,
                                            num_ctx=self.context_limit)
            except Exception as e:
                self.event_queue.put(ErrorEvent(f"Ollama error: {e}"))
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
            # Complete block: <think>…</think>
            think_in_content = re.search(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)
            if think_in_content and not raw_thinking:
                raw_thinking = think_in_content.group(1).strip()
            content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
            # Incomplete block: generation cut off mid-thinking → "<think>..." with no closing tag.
            # Must be stripped or the raw tag leaks into the stored message and breaks Ollama's
            # XML template on the next API call (500: element <function> closed by </parameter>).
            incomplete_think = re.search(r"<think>(.*?)$", content, flags=re.DOTALL)
            if incomplete_think:
                if not raw_thinking:
                    raw_thinking = incomplete_think.group(1).strip()
                content = re.sub(r"<think>.*$", "", content, flags=re.DOTALL).strip()

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
                            "Design written. Check the file to review it."))
                    elif not empty_retried:
                        # Retry once with an explicit prompt. Only inject a user message
                        # if the last turn is not already a user turn — consecutive user
                        # messages are not valid in Ollama's turn format.
                        empty_retried = True
                        if self.messages[-1]["role"] != "user":
                            if done_reason == "length":
                                # Generation was cut off by the context window — the
                                # thinking block consumed all available tokens.
                                # Disable thinking for the retry so it has budget to respond.
                                _length_retried = True
                                self.event_queue.put(ChatEvent("system",
                                    "Response cut off (context limit). Retrying without thinking."))
                                retry_text = (
                                    "Your previous response was cut off. "
                                    "Do NOT output any reasoning or thinking. "
                                    "Call write_file directly with the complete document, "
                                    "or call ask_user if you need information."
                                    if self.mode == "design" else
                                    "Your previous response was cut off. "
                                    "Do NOT output any reasoning or thinking. "
                                    "Call a tool directly or write a brief response."
                                )
                            elif raw_thinking:
                                # Model reasoned but produced no output — common when
                                # think=True and the model gets stuck mid-reasoning.
                                retry_text = (
                                    "You produced reasoning but no response or tool call. "
                                    "Based on your analysis, call write_file now with the complete document, "
                                    "or call ask_user if you need more information."
                                    if self.mode == "design" else
                                    "You produced reasoning but no response or tool call. "
                                    "Based on your analysis, call a tool to continue or write your conclusion."
                                )
                            else:
                                retry_text = (
                                    "Please respond. If you are ready to write the design, "
                                    "call write_file now with the full document in the content parameter."
                                    if self.mode == "design" else
                                    "Please respond with your current analysis or next step."
                                )
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
                if (self.mode == "design"
                        and _has_write_intent(content)
                        and not _write_nudged):
                    _write_nudged = True
                    self.messages.append({"role": "assistant", "content": content or None})
                    self.messages.append({"role": "user", "content":
                        "You said you would write the design but did not call write_file. "
                        "Call write_file now with the complete document in the content parameter."
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
            if msg.tool_calls:
                # Preserve the actual arguments object from Ollama's response.
                self.messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                })
            else:
                # Text-recovered calls — use extracted (name, args) tuples.
                self.messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"function": {"name": n, "arguments": a}}
                        for n, a in _calls
                    ],
                })

            for name, args in _calls:
                # write_file is sticky — it must not be overwritten by a later
                # tool in the same batch or the terminal detection below misses it.
                if last_tool != "write_file":
                    last_tool = name
                self.event_queue.put(ToolCallEvent(name, args))
                self.logger.log_tool_call(self.mode, self.client.model, name, args)

                if name == "ask_user":
                    # Block the worker thread until the TUI routes the user's answer back.
                    # The TUI detects AskUserEvent, switches to waiting-for-input state,
                    # and calls provide_user_input() when the user submits a response.
                    question = args.get("question", "")
                    self.event_queue.put(AskUserEvent(question))
                    answer = self._user_input_queue.get()
                    result = f"User answered: {answer}"
                else:
                    result = dispatch(name, args, self.workdir)
                    if self.max_tool_result > 0 and len(result) > self.max_tool_result:
                        result = result[:self.max_tool_result] + f"\n... (truncated, {len(result)} chars total)"

                self.event_queue.put(ToolResultEvent(name, result))
                self.logger.log_tool_result(self.mode, self.client.model, name, len(result))

                self.messages.append({"role": "tool", "content": result})

                # write_file is a terminal action — reset the counter so the nudge
                # doesn't fire immediately after and confuse the follow-up summary
                if name == "write_file":
                    tool_only_turns = 0

            # Inject as role "user" — Ollama's tool-use turn format expects
            # assistant → tool(s) → user; a mid-conversation system message is
            # not supported and would break the alternating turn structure.
            # Capped at one nudge per send() call to avoid polluting the history.
            if tool_only_turns >= _NUDGE_AFTER and not _nudged:
                _nudged = True
                nudge = (
                    "You have been calling tools for several turns without a text response. "
                    "If you have gathered enough information to write the design, call write_file now with the complete spec. "
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
        ))

    def _autosave(self):
        session_mod.save(
            self._ts, self.client.model, self.mode,
            self.workdir, self.messages, self.context_limit,
        )

    def load_session(self, path: Path) -> str:
        data = session_mod.load(path)
        self.messages = data["messages"]
        self.mode = data.get("mode", "design")
        self.workdir = Path(data.get("workdir", str(self.workdir)))
        self.context_limit = data.get("context_limit", self.context_limit)
        self.client.set_model(data.get("model", self.client.model))
        # Always refresh the system prompt from the current role file on disk.
        # Saved sessions carry a snapshot of the old prompt; without this the
        # model runs stale instructions regardless of role file edits.
        if self.messages and self.messages[0].get("role") == "system":
            prompt = (_design_prompt() if self.mode == "design"
                      else _coding_prompt(str(self.workdir)))
            self.messages[0] = {"role": "system", "content": prompt}
        self._token_estimate = self._estimate()
        self._emit_status()
        return f"Session loaded: {path.name} ({len(self.messages)} messages)"

    def session_path(self) -> Path:
        return session_mod.session_path(self._ts)
