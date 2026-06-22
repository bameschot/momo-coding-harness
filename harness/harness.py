from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import session as session_mod
from .logger import Logger
from .ollama_client import OllamaClient
from .tools import READ_ONLY_TOOLS, ALL_TOOLS, dispatch

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
        "You are a software design assistant. "
        "Explore the codebase using the available read-only tools to understand context. "
        "Ask focused clarifying questions to build a clear specification. "
        "Do not write or modify any code or files."
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
        self.context_limit = 8192
        self._ts = session_mod.new_timestamp()
        self.logger = Logger(self._ts)
        self.client = OllamaClient(host=host, model=model)
        self.event_queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.Lock()

        self.max_tool_result = 4000   # chars; configurable via /tool-result or --max-tool-result
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
        self.logger.log_compact(self.mode, removed, before, after)
        return notice

    def _estimate(self) -> int:
        return _estimate_tokens(self.messages)

    # ── send ──────────────────────────────────────────────────────────────────

    def send(self, text: str):
        """Called from the harness worker thread."""
        self.messages.append({"role": "user", "content": text})
        self.event_queue.put(ChatEvent("user", text))

        tools = READ_ONLY_TOOLS if self.mode == "design" else ALL_TOOLS
        _MAX_ITERATIONS = 20

        iteration = 0
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
                response = self.client.chat(self.messages, tools)
            except Exception as e:
                self.event_queue.put(ErrorEvent(f"Ollama error: {e}"))
                self.event_queue.put(DoneEvent())
                return

            msg = response.message
            usage = getattr(response, "usage", None) or {}
            if hasattr(usage, "__dict__"):
                usage = usage.__dict__
            prompt_tokens = usage.get("prompt_eval_count") if usage else None
            eval_tokens = usage.get("eval_count") if usage else None

            if prompt_tokens is not None:
                self._token_estimate = (prompt_tokens or 0) + (eval_tokens or 0)

            tool_calls = getattr(msg, "tool_calls", None) or []
            self.logger.log_response(
                self.mode, self.client.model,
                prompt_tokens, eval_tokens,
                bool(tool_calls),
            )

            # emit assistant text
            content = getattr(msg, "content", "") or ""
            if content:
                self.event_queue.put(ChatEvent("assistant", content))

            if not tool_calls:
                self.messages.append({"role": "assistant", "content": content})
                break

            # assistant message with tool calls
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
                    import json
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                self.event_queue.put(ToolCallEvent(name, args))
                self.logger.log_tool_call(self.mode, name, args)

                result = dispatch(name, args, self.workdir)
                if self.max_tool_result > 0 and len(result) > self.max_tool_result:
                    result = result[:self.max_tool_result] + f"\n... (truncated, {len(result)} chars total)"

                self.event_queue.put(ToolResultEvent(name, result))
                self.logger.log_tool_result(self.mode, name, len(result))

                self.messages.append({"role": "tool", "content": result})

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
        self._token_estimate = self._estimate()
        self._emit_status()
        return f"Session loaded: {path.name} ({len(self.messages)} messages)"

    def session_path(self) -> Path:
        return session_mod.session_path(self._ts)
