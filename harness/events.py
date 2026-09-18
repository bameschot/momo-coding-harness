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
from typing import Any

_BACKLOG_MAX = 5000


@dataclasses.dataclass
class UserEvent:
    """Text the user submitted from any frontend (shown in all of them)."""
    text: str


@dataclasses.dataclass
class ResetEvent:
    """The transcript was replaced (e.g. a session was loaded) — clear the view."""
    pass


@dataclasses.dataclass
class DeltaEvent:
    """A streamed piece of the reply being generated (kind: "content" | "thinking")."""
    kind: str
    text: str


@dataclasses.dataclass
class StreamEndEvent:
    """The streamed preview is over; the final Think/Chat events follow."""
    pass


@dataclasses.dataclass
class BusyEvent:
    """Shared busy / waiting-for-input state changed."""
    busy: bool
    waiting: bool


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
    _LATEST_ONLY = ("StatusEvent", "BusyEvent")
    # Live-only events: delivered to current subscribers but never replayed —
    # a reconnecting page sees the final messages, not the stream that built them.
    _TRANSIENT = ("DeltaEvent", "StreamEndEvent")

    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._backlog: collections.deque[Any] = collections.deque(maxlen=_BACKLOG_MAX)
        self._latest: dict[str, Any] = {}

    def put(self, ev: Any):
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
    return {"type": snake, **data}
