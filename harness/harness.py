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

class Harness:
    def __init__(self, host: str, model: str, workdir: Path):
        self.workdir = workdir.resolve()
        self.mode = "design"
        self.context_limit = 100000
        self._ts = session_mod.new_timestamp()
        self.logger = Logger(self._ts)
        self.client = OllamaClient(host=host, model=model)
        self.event_queue: queue.Queue[Any] = queue.Queue()
        self.max_tool_result = 0   # chars; 0 = unlimited; configurable via /tool-result or --max-tool-result
        self.messages: list[dict] = [
            {"role": "system", "content": _design_prompt()}
        ]
        self._token_estimate = 0

    # ── public properties ─────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self.client.model if hasattr(self, "client") else self._model

    @model.setter
    def model(self, value: str):
        self._model = value
        if hasattr(self, "client"):
            self.client.set_model(value)

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
            if msg["role"] == "tool":
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
        _MAX_ITERATIONS = 10 if self.mode == "design" else 20
        _NUDGE_AFTER = 4  # consecutive tool-only turns before injecting a respond prompt

        iteration = 0
        tool_only_turns = 0
        last_tool: str | None = None
        empty_retried = False
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
                response = self.client.chat(self.messages, tools, think=False)
            except Exception as e:
                self.event_queue.put(ErrorEvent(f"Ollama error: {e}"))
                self.event_queue.put(DoneEvent())
                return

            msg = response.message
            prompt_tokens = getattr(response, "prompt_eval_count", None)
            eval_tokens = getattr(response, "eval_count", None)

            if prompt_tokens is not None:
                self._token_estimate = (prompt_tokens or 0) + (eval_tokens or 0)

            tool_calls = getattr(msg, "tool_calls", None) or []
            self.logger.log_response(
                self.mode, self.client.model,
                prompt_tokens, eval_tokens,
                bool(tool_calls),
            )

            # Strip thinking tokens. Qwen3/Qwen3.5 embeds <think>…</think> in the
            # content field even when think=False is passed to Ollama.
            raw_content = getattr(msg, "content", "") or ""
            content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()

            # Emit preamble text before tool events so it appears above tool output
            # in the TUI. Deferring it until after tools run places it below the
            # result, making the response look cut off.
            if content:
                tool_only_turns = 0
                self.event_queue.put(ChatEvent("assistant", content))

            if not tool_calls:
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
                            self.messages.append({"role": "user", "content":
                                "Please respond with your current analysis or next question."})
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
                self.messages.append({"role": "assistant", "content": content})
                break

            # track consecutive tool-only turns
            if not content:
                tool_only_turns += 1

            # Append with stripped content — thinking tokens must not re-enter the
            # context or the model re-emits them on every subsequent turn.
            self.messages.append({"role": "assistant", "content": content,
                                   "tool_calls": [
                                       {"function": {"name": tc.function.name,
                                                     "arguments": tc.function.arguments}}
                                       for tc in tool_calls
                                   ]})

            for tc in tool_calls:
                name = tc.function.name
                args = tc.function.arguments or {}
                if isinstance(args, str):
                    # Older Ollama versions return arguments as a raw JSON string.
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                last_tool = name
                self.event_queue.put(ToolCallEvent(name, args))
                self.logger.log_tool_call(self.mode, self.client.model, name, args)

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
            if tool_only_turns >= _NUDGE_AFTER:
                self.messages.append({"role": "user", "content":
                    "You have been calling tools for several turns without responding. "
                    "If the user asked you to write or save the design, call write_file now with the full design. "
                    "Otherwise stop exploring and write a text response summarising what you found."})
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
