"""Headless one-shot runs: ``momo -p "<prompt>"``.

Runs one prompt to completion without a frontend: the final answer goes to
stdout, progress (tool calls, edits, notes, auto-answered questions) to stderr,
and the exit code says how it ended.  Questions get automatic answers via
``Harness.unattended`` (see ``Harness._unattended_answer``), so nothing blocks.

No Controller: its idle loop (recap, titles, backend poll), steering and the
/command routing only matter to an interactive frontend.  The pattern is the
one evals/run_evals.py uses: subscribe to the bus, run ``send()`` on a worker,
pump events until it is done.
"""
from __future__ import annotations

import json
import queue
import signal
import sys
import threading
from typing import Any, TextIO

from .events import (AskUserEvent, ChatEvent, DiffEvent, ErrorEvent, ToolCallEvent)
from .net import CONTROL_CHARS

EXIT_OK = 0
EXIT_ERROR = 1          # backend unreachable, an ErrorEvent, a crash
EXIT_PLAN_STOPPED = 3   # an approved plan stopped before its last step (paused, TDD gate)
EXIT_TIMEOUT = 124      # --timeout (the coreutils `timeout` convention)
EXIT_INTERRUPTED = 130  # Ctrl+C / SIGTERM

# What a tool call line shows of the call's arguments, first match wins.
_ARG_KEYS = ("path", "command", "pattern", "query", "url", "name", "symbol", "question")
_CANCEL_GRACE = 30.0    # seconds to wait for the worker after a cancel


def _clean(text: str) -> str:
    """Model- and tool-supplied text headed for a terminal: no escape sequences."""
    return CONTROL_CHARS.sub("", str(text))


def _arg_summary(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in _ARG_KEYS:
        if args.get(key):
            shown = str(args[key])
            break
    else:
        shown = json.dumps(args, ensure_ascii=False) if args else ""
    shown = " ".join(shown.split())
    return shown if len(shown) <= 100 else shown[:99] + "…"


def _plan_stopped(harness) -> bool:
    """An approved plan is still executing with steps left: it paused."""
    plan = harness.plan
    return (plan is not None and harness.plan_phase == "executing"
            and any(not s.finished for s in plan.steps))


class _Printer:
    """Turns events into stderr progress lines and remembers the answer."""

    def __init__(self, err: TextIO | None):
        self.err = err              # None: --quiet
        self.answer: str | None = None
        self.failed = False

    def line(self, text: str) -> None:
        if self.err is not None:
            print(_clean(text), file=self.err, flush=True)

    def _not_the_answer(self) -> None:
        """The model went on (a tool call, a question, another reply), so the text
        held as the answer was progress: show it now, in order."""
        if self.answer:
            self.line(self.answer)
        self.answer = None

    def handle(self, ev: Any) -> None:
        if isinstance(ev, (ToolCallEvent, AskUserEvent)) or (
                isinstance(ev, ChatEvent) and ev.role == "assistant"):
            self._not_the_answer()
        if isinstance(ev, ToolCallEvent):
            self.line(f"→ {ev.name}  {_arg_summary(ev.args)}".rstrip())
        elif isinstance(ev, DiffEvent):
            target = f"{ev.path} → {ev.dst}" if ev.op == "move" else ev.path
            what = "created" if ev.is_new else {"edit": "edited", "write": "wrote",
                                                "append": "appended", "delete": "deleted",
                                                "move": "moved"}.get(ev.op, ev.op)
            counts = "" if ev.op == "move" else f" (+{ev.added} −{ev.removed})"
            self.line(f"  {what} {target}{counts}")
        elif isinstance(ev, AskUserEvent):
            self.line(f"? {ev.question}")
        elif isinstance(ev, ChatEvent):
            if ev.role == "assistant":
                # Held until the model goes on (then it was progress: a preamble,
                # a plan, a finished plan step); the last one is the answer.
                self.answer = ev.text
            elif ev.role == "system" and ev.text:
                self.line(ev.text)
        elif isinstance(ev, ErrorEvent):
            self.failed = True
            self.line(f"ERROR: {ev.text}")


def run(harness, prompt: str, *, quiet: bool = False, timeout: float | None = None,
        out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Send `prompt`, print the answer to `out` (stdout) and return the exit code.
    Expects `harness.unattended` to be set (main.py does)."""
    out = out or sys.stdout
    printer = _Printer(None if quiet else (err or sys.stderr))
    # replay: the start-up notes (backend check, guides, MCP) were put before now.
    sub = harness.event_queue.subscribe(replay=True)

    def drain() -> None:
        while True:
            try:
                printer.handle(sub.get_nowait())
            except queue.Empty:
                return

    try:
        drain()
        if harness.backend_reachable is False:
            # The check's own message is on stderr already, unless --quiet.
            if quiet:
                c = harness.client
                print(f"error: {c.provider_name} at {c.host} is not reachable",
                      file=err or sys.stderr, flush=True)
            return EXIT_ERROR

        def work():
            try:
                harness.send(prompt)
            except Exception as e:          # a thread must report, not vanish
                printer.failed = True
                harness.report_crash("send", e)

        worker = threading.Thread(target=work, daemon=True, name="momo-headless")
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            harness.cancel()
        timer = threading.Timer(timeout, on_timeout) if timeout else None

        # SIGTERM (a CI runner, `kill`) is handled like Ctrl+C.  Only the main
        # thread may set a handler; a test calling run() elsewhere keeps the default.
        old_term = None
        if threading.current_thread() is threading.main_thread():
            def _on_term(signum, frame):
                raise KeyboardInterrupt
            old_term = signal.signal(signal.SIGTERM, _on_term)

        interrupted = False
        worker.start()
        if timer:
            timer.daemon = True
            timer.start()
        try:
            while worker.is_alive():
                try:
                    printer.handle(sub.get(timeout=0.2))
                except queue.Empty:
                    pass
        except KeyboardInterrupt:
            interrupted = True
            printer.line("Interrupted — stopping…")
            harness.cancel()
            worker.join(_CANCEL_GRACE)
        finally:
            if timer:
                timer.cancel()
            if old_term is not None:
                signal.signal(signal.SIGTERM, old_term)
        drain()

        if printer.answer:
            out.write(printer.answer.rstrip("\n") + "\n")
            out.flush()
        if interrupted:
            return EXIT_INTERRUPTED
        if timed_out.is_set():
            printer.line(f"Stopped: --timeout {timeout:g}s reached.")
            return EXIT_TIMEOUT
        if printer.failed:
            return EXIT_ERROR
        if _plan_stopped(harness):
            printer.line(f"The plan stopped at step {harness.plan.progress()}.")
            return EXIT_PLAN_STOPPED
        return EXIT_OK
    finally:
        sub.close()
        try:
            harness._autosave()
        except Exception as e:
            harness.logger.log_error("headless autosave", e)
