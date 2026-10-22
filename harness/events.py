"""Fan-out event bus shared by the frontends (curses TUI and web UI).

The harness emits events with ``put()`` from its worker thread; every frontend
holds its own ``Subscription`` queue so each one sees every event.  A bounded
backlog lets a frontend that connects later (e.g. a browser tab opened
mid-session) replay the transcript before receiving live events.
"""
from __future__ import annotations

import collections
import dataclasses
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_BACKLOG_MAX = 5000


# ── harness events ────────────────────────────────────────────────────────────────

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
    context_limit: int = 0        # compaction threshold (tokens) ctx_pct is measured against
    backend_reachable: bool | None = None  # False: server down, model/ctx are the last known
    tools_enabled: bool = True
    run_confirm: bool = False
    run_mode: str = "new"         # /run-mode: "new" (saved log + view) | "classic"
    run_output_limit: int = 0     # /run-output-limit: chars per run_command view (0 = off)
    max_output_tokens: int = 25000  # /max-output-tokens: cap on one model reply (0 = off)
    net_access: str = "off"     # "off" | "on" | "local"
    net_confirm: bool = True    # ask y/N before a write request
    net_max_bytes: int = 2097152  # ceiling on one fetch_url download
    net_max_chars: int = 24000    # text returned per fetch_url call
    net_cache: list = field(default_factory=list)  # /net-cache: net.cache_snapshot() rows
    net_cache_max: int = 64       # pages the cache holds at most
    net_cache_max_bytes: int = 64 * 1024 * 1024  # and bytes, whichever comes first
    host: str = ""
    provider: str = ""
    plan_progress: str = ""  # plan mode: "awaiting approval" | "exec 3/7" | ""
    plan_kind: str = "normal"  # plan mode: the active plan's type, else the next plan's
    title: str = ""          # session title (model-generated or /title)
    guides: bool = False     # project guide files (AGENTS.md, ...) in the system prompt
    think_level: str = "on"       # /think: off | on | low | medium | high
    think_effective: str = "on"   # what the model is sent: the above, or "n/a"
    think_choices: list = field(default_factory=lambda: ["off", "on"])  # values the model honours
    think_known: bool = False     # the server said what the model takes
    think_history: str = "turn"   # /think-history: off | turn | all
    tools_off: list = field(default_factory=list)  # tools turned off for the current mode
    workspace_pending: list = field(default_factory=list)  # .momo/tools/ tools awaiting approval
    skills: list = field(default_factory=list)     # active skills, in load order
    index_enabled: bool = False   # /index: the code index and its index_* tools
    index_state: str = "off"      # off | building | refreshing | idle | stopped
    index_progress: str = ""      # "812/1873" while building/refreshing
    index_files: int = 0
    index_mem: int = 0            # estimated bytes in use
    # Always filled in by Harness.status_event (code_index's defaults live there),
    # so this module stays free of harness imports.
    index_max_bytes: int = 0
    index_max_files: int = 0
    index_workers: int = 0        # /index-workers: 0 = auto
    index_persist: bool = True    # /index-persist: load/save a pickle
    index_route: bool = True      # /index-route: answer grep_files/find_files from the index
    index_degraded: bool = False  # over budget: a component was dropped
    mcp_enabled: bool = True      # /mcp on|off
    mcp_confirm: bool = True      # /mcp-confirm: ask before MCP calls (per server policy)
    mcp_ready: int = 0            # servers connected
    mcp_total: int = 0            # servers configured
    mcp_attention: bool = False   # one failed, waits for approval, or a config error
    mcp: list = field(default_factory=list)  # [{name, state, tools, error}]

@dataclass
class ErrorEvent:
    text: str

@dataclass
class DoneEvent:
    """A turn (or compaction, or plan run) is over.  After a model turn it carries
    what the turn cost; all zero otherwise (the web UI's per-reply footer)."""
    secs: float = 0.0          # wall-clock time of the turn
    prompt_tokens: int = 0     # the last request's prompt, as the server counted it
    eval_tokens: int = 0       # generated tokens over the whole turn, reasoning included
    tool_calls: int = 0

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


@dataclass
class UserEvent:
    """Text the user submitted from any frontend (shown in all of them)."""
    text: str


@dataclass
class ResetEvent:
    """The transcript was replaced (e.g. a session was loaded) — clear the view."""
    pass


@dataclass
class DeltaEvent:
    """A streamed piece of the reply being generated (kind: "content" | "thinking")."""
    kind: str
    text: str


@dataclass
class StreamEndEvent:
    """The streamed preview is over; the final Think/Chat events follow."""
    pass


@dataclass
class BusyEvent:
    """Shared busy / waiting-for-input state changed."""
    busy: bool
    waiting: bool


@dataclass
class CompanionEvent:
    """momo's idle-recap state: whether the user is idle, the remembered recap lines,
    and the feature's settings (so every frontend's controls stay in sync)."""
    idle: bool
    lines: list[str]
    enabled: bool = False
    secs: int = 90


class Subscription:
    def __init__(self, bus: "EventBus"):
        self._bus = bus
        self._q: queue.Queue[Any] = queue.Queue()

    def get_nowait(self) -> Any:
        return self._q.get_nowait()

    def get(self, timeout: float | None = None) -> Any:
        return self._q.get(timeout=timeout)

    def close(self):
        self._bus.unsubscribe(self)


class EventBus:
    """Drop-in replacement for the harness's former single-consumer queue."""

    # Events that only matter in their latest form: kept outside the backlog and
    # replayed once (latest value) to new subscribers.
    _LATEST_ONLY = ("StatusEvent", "BusyEvent", "CompanionEvent")
    # Live-only events: delivered to current subscribers but never replayed —
    # a reconnecting page sees the final messages, not the stream that built them.
    _TRANSIENT = ("DeltaEvent", "StreamEndEvent")

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._backlog: collections.deque[Any] = collections.deque(maxlen=_BACKLOG_MAX)
        self._latest: dict[str, Any] = {}

    def put(self, ev: Any):
        # When it happened (the web UI shows it), unless the event says already:
        # transcript_events sets None on events rebuilt from a saved session.
        if not hasattr(ev, "_ts"):
            try:
                ev._ts = time.time()
            except AttributeError:      # a slotted or frozen event: no time, no harm
                pass
        with self._lock:
            kind = type(ev).__name__
            if kind in self._LATEST_ONLY:
                self._latest[kind] = ev
            elif kind in self._TRANSIENT:
                pass
            else:
                self._backlog.append(ev)
            for s in self._subs:
                s._q.put(ev)

    def subscribe(self, replay: bool = True) -> Subscription:
        sub = Subscription(self)
        with self._lock:
            if replay:
                for ev in self._backlog:
                    sub._q.put(ev)
                for ev in self._latest.values():
                    sub._q.put(ev)
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription):
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def reset(self):
        """Drop the backlog and tell every subscriber to clear its transcript."""
        with self._lock:
            self._backlog.clear()
        self.put(ResetEvent())


def event_to_json(ev: Any) -> dict:
    """Serialise an event dataclass as {"type": <snake_name>, **fields}."""
    name = type(ev).__name__
    if name.endswith("Event"):
        name = name[:-5]
    # CamelCase → snake_case (ToolCall → tool_call)
    snake = "".join("_" + c.lower() if c.isupper() and i else c.lower()
                    for i, c in enumerate(name))
    data = dataclasses.asdict(ev) if dataclasses.is_dataclass(ev) else {}
    return {"type": snake, **data, "ts": getattr(ev, "_ts", None)}
