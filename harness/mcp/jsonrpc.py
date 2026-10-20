"""JSON-RPC 2.0 over any MCP transport.

A transport only moves whole messages: ``start(on_message, on_close)``,
``send(msg)`` and ``close()``.  `Peer` adds request ids, waits for responses
(polling a cancel event, so Esc interrupts a call), answers the requests a
server sends us, and fails every waiting call when the connection drops.
"""
from __future__ import annotations

import itertools
import threading
from collections.abc import Callable

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
_POLL_S = 0.1


class RPCError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Cancelled(Exception):
    pass


class Peer:
    def __init__(self, transport, *, handlers: dict[str, Callable] | None = None,
                 on_notification: Callable[[str, dict], None] | None = None,
                 on_close: Callable[[str], None] | None = None):
        self.transport = transport
        self.handlers = handlers or {}
        self.on_notification = on_notification
        self._on_close_cb = on_close
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, list] = {}    # id -> [Event, response or None]
        self.closed: str | None = None

    def start(self) -> None:
        self.transport.start(self._on_message, self._on_close)

    # ── outgoing ─────────────────────────────────────────────────────────────

    def request(self, method: str, params: dict | None = None, *,
                timeout: float = 60.0, cancel: threading.Event | None = None) -> dict:
        """Send a request and wait for its result (raises RPCError, Cancelled,
        TimeoutError or ConnectionError)."""
        if self.closed:
            raise ConnectionError(self.closed)
        rid = next(self._ids)
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.transport.send(msg)
            waited = 0.0
            while not slot[0].wait(_POLL_S):
                waited += _POLL_S
                if cancel is not None and cancel.is_set():
                    self._cancel_remote(rid, "interrupted by the user")
                    raise Cancelled(f"{method} interrupted")
                if waited >= timeout:
                    self._cancel_remote(rid, "timed out")
                    raise TimeoutError(f"{method}: no answer after {timeout:.0f} s")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        resp = slot[1]
        if resp is None:                # woken by _on_close
            raise ConnectionError(self.closed or "connection closed")
        if "error" in resp:
            err = resp["error"] if isinstance(resp["error"], dict) else {}
            raise RPCError(int(err.get("code", INTERNAL_ERROR)),
                           str(err.get("message", "error")), err.get("data"))
        result = resp.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.transport.send(msg)

    def _cancel_remote(self, rid: int, reason: str) -> None:
        try:
            self.notify("notifications/cancelled", {"requestId": rid, "reason": reason})
        except (OSError, ConnectionError):
            pass

    def close(self) -> None:
        self.transport.close()
        self._on_close("closed")

    # ── incoming ─────────────────────────────────────────────────────────────

    def _on_message(self, msg) -> None:
        if isinstance(msg, list):               # a JSON-RPC batch
            for m in msg:
                self._on_message(m)
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:
                self._answer(msg)
            elif self.on_notification:
                params = msg.get("params")
                self.on_notification(str(msg["method"]), params if isinstance(params, dict) else {})
            return
        if "id" in msg:
            with self._lock:
                slot = self._pending.get(msg["id"])
            if slot is not None:
                slot[1] = msg
                slot[0].set()

    def _answer(self, msg: dict) -> None:
        handler = self.handlers.get(msg["method"])
        if handler is None:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": METHOD_NOT_FOUND, "message": f"{msg['method']} is not supported"}}
        else:
            try:
                reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handler(msg.get("params") or {})}
            except Exception as e:      # a handler bug must not kill the reader thread
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": INTERNAL_ERROR, "message": str(e)}}
        try:
            self.transport.send(reply)
        except (OSError, ConnectionError):
            pass

    def _on_close(self, reason: str) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = reason or "connection closed"
            slots = list(self._pending.values())
        for slot in slots:
            slot[0].set()
        if self._on_close_cb:
            self._on_close_cb(self.closed)
