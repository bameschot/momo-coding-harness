"""Frontend-agnostic orchestration shared by the curses TUI and the web UI.

Both frontends drive one Harness.  The Controller owns the state that must be
consistent between them — busy / waiting-for-input, a pending y/N confirmation —
and routes submitted text to /commands, ask_user answers or a new model turn.
Everything user-visible goes through the harness event bus so every frontend
renders the same transcript; only per-frontend view toggles are returned.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import attachments as attach_mod
from .commands import handle as handle_command
from .events import (BusyEvent, ChatEvent, CompanionEvent, ThinkEvent, ToolCallEvent,
                     ToolResultEvent, UserEvent)
from .harness import Harness
from .llm import make_client

# CommandResult fields that change how a frontend renders, not harness state.
VIEW_FIELDS = ("tool_output", "think_output", "md_render", "diff_output", "diff_style", "companion")

# Commands that mutate harness.messages — refused while a worker thread runs.
_MUTATING = ("/clear", "/compact", "/fast-compact", "/new", "/retry")
# Commands that switch the mode: the running turn keeps its tool set, so a new
# mode's prompt and rules would not match what the model can call.
_MODE_COMMANDS = ("/code", "/design", "/chat", "/momo")
_BUSY_MSG = "Busy — finish the response or interrupt before running that command."


def _blocked_while_busy(cmd: str, parts: list[str]) -> bool:
    """Commands that would change the turn a worker thread is running."""
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    return (cmd in _MUTATING or cmd in _MODE_COMMANDS
            or (cmd == "/plan" and arg != "show")
            or (cmd in ("/session", "/workspace", "/workdir", "/run-mode",
                        "/search-sources") and bool(arg)))

# Idle recap: how often the watcher checks, and the minimum gap between two recap
# attempts (on top of "once per user turn" and "once per idle period").
_IDLE_POLL = 5.0
_RECAP_COOLDOWN = 300.0


@dataclass
class SubmitOutcome:
    view: dict[str, Any] = field(default_factory=dict)  # subset of VIEW_FIELDS to apply
    exit_app: bool = False


def transcript_events(messages: list[dict]) -> list[Any]:
    """Rebuild displayable events from stored conversation messages."""
    # Map tool_call_id → name so results are labelled correctly regardless of
    # ordering (parallel tool calls).
    call_id_to_name: dict[str, str] = {}
    for msg in messages:
        for tc in (msg.get("tool_calls") or []):
            cid = tc.get("id") or ""
            if cid:
                call_id_to_name[cid] = tc["function"]["name"]

    events: list[Any] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system":
            continue
        elif role == "thinking":
            events.append(ThinkEvent(content))
        elif role == "user":
            events.append(UserEvent(attach_mod.summarize(content)))
        elif role == "assistant":
            if content:
                events.append(ChatEvent("assistant", content))
            for tc in (msg.get("tool_calls") or []):
                events.append(ToolCallEvent(tc["function"]["name"],
                                            tc["function"].get("arguments") or {}))
        elif role == "tool":
            cid = msg.get("tool_call_id") or ""
            name = call_id_to_name.get(cid) or msg.get("name") or ""
            events.append(ToolResultEvent(name, content))
    return events


def _mask_token(raw: str) -> str:
    n = len(raw)
    return (raw[:2] + "*" * (n - 5) + raw[-3:]) if n > 5 else "*" * n


class Controller:
    def __init__(self, harness: Harness):
        self.harness = harness
        self.bus = harness.event_queue
        self._lock = threading.RLock()
        self._busy = False
        self._pending_confirm: Callable[[], str | None] | None = None
        self.web_url: str | None = None  # set by main when the web server is up
        # Idle recap state (see _idle_tick).
        self._last_activity = time.monotonic()
        self._idle = False
        self._recapped_this_idle = False
        self._last_recap_ts: float | None = None
        self._recap_client = None   # set while a recap is generating
        self._activity_gen = 0      # bumped on user input; a recap from an older gen is stale
        if len(harness.messages) > 1:
            self.replay_transcript(reset=False)
        self._emit_busy()
        self._emit_companion()
        threading.Thread(target=self._idle_loop, daemon=True, name="momo-idle").start()

    # ── shared state ──────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def waiting(self) -> bool:
        """The model is blocked on an ask_user / confirmation answer."""
        return self._busy and self.harness.awaiting_input

    def _emit_busy(self):
        self.bus.put(BusyEvent(busy=self._busy, waiting=self.waiting))

    def _set_busy(self, busy: bool):
        with self._lock:
            self._busy = busy
            if not busy:
                self._last_activity = time.monotonic()  # idle time counts from the reply's end
            self._emit_busy()

    def _emit_companion(self):
        h = self.harness
        self.bus.put(CompanionEvent(idle=self._idle, lines=list(h.momo_lines),
                                    enabled=h.idle_recap, secs=h.idle_recap_secs))

    # ── idle recap ────────────────────────────────────────────────────────────

    def _note_activity(self):
        """The user did something: leave idle, and abort an in-flight recap so the
        user's request doesn't queue behind it on the LLM server."""
        with self._lock:
            self._last_activity = time.monotonic()
            self._recapped_this_idle = False
            self._activity_gen += 1
            client = self._recap_client
            was_idle, self._idle = self._idle, False
        if client is not None:
            client.abort()
        if was_idle:
            self._emit_companion()

    def _idle_loop(self):
        while True:
            time.sleep(_IDLE_POLL)
            try:
                self._idle_tick()
            except Exception:
                pass  # never kill the watcher (and never print — it would corrupt curses)

    def _recap_due(self, now: float) -> bool:
        h = self.harness
        last = h.messages[-1] if len(h.messages) > 1 else {}
        return (
            not self._recapped_this_idle
            and self._recap_client is None
            and h.user_turns() > h._momo_recap_turn            # at most once per turn
            and last.get("role") == "assistant"                # ... and only a finished one
            and bool((last.get("content") or "").strip())
            and (self._last_recap_ts is None
                 or now - self._last_recap_ts >= max(h.idle_recap_secs, _RECAP_COOLDOWN))
        )

    def _idle_tick(self):
        h = self.harness
        with self._lock:
            if not h.idle_recap:
                if self._idle:
                    self._idle = False
                    self._emit_companion()
                return
            now = time.monotonic()
            if (self._busy or self._pending_confirm is not None or h._plan_executing()
                    or now - self._last_activity < h.idle_recap_secs):
                return
            if not self._idle:
                self._idle = True
                self._emit_companion()  # momo can reuse remembered lines right away
            if not self._recap_due(now):
                return
            # Record the attempt up front: a failed or aborted recap still uses up
            # this turn, idle period and cooldown, so failures never retry in a loop.
            self._recapped_this_idle = True
            self._last_recap_ts = now
            new_turns = h.user_turns() - h._momo_recap_turn   # recap only what's new
            h._momo_recap_turn = h.user_turns()
            gen = self._activity_gen
            client = self._recap_client = make_client(
                h.provider, host=h.client.host, model=h.client.model,
                auth_token=h.client.auth_token)
        try:
            lines = h.momo_recap(client, new_turns)
        finally:
            with self._lock:
                self._recap_client = None
        with self._lock:
            if gen != self._activity_gen or self._busy:
                return  # the user came back mid-recap; don't touch the session now
            if lines:
                h.remember_momo_lines(lines)
            h._autosave()  # persists the recap turn even when the attempt failed
            if lines:
                self._emit_companion()

    def _system(self, text: str):
        self.bus.put(ChatEvent("system", text))

    def _run_bg(self, fn: Callable, *args):
        self._set_busy(True)

        def _run():
            try:
                fn(*args)
            finally:
                self._set_busy(False)

        threading.Thread(target=_run, daemon=True).start()

    # ── actions ───────────────────────────────────────────────────────────────

    def replay_transcript(self, reset: bool = True, notice: str | None = None):
        """Push the stored conversation onto the bus (after a reset when loading a session)."""
        if reset:
            self.bus.reset()
        for ev in transcript_events(self.harness.messages):
            self.bus.put(ev)
        h = self.harness
        guides = h.guides_summary()
        self._system(notice or (
            f"Session loaded: {h.session_path().name} ({len(h.messages)} messages)\n"
            f"Model: {h.client.model} | Mode: {h.mode} | Dir: {h.workdir}"
            + (f"\n{guides}" if guides else "")))
        self._emit_companion()  # the loaded session's remembered recap lines

    def _resend_guard(self) -> str | None:
        if self._busy:
            return "Busy — finish the response or interrupt first."
        if self.harness._plan_executing():
            return "Not available while a plan is executing."
        return None

    def retry_last(self) -> bool:
        """Re-send the last user message, discarding everything that followed it."""
        with self._lock:
            if (err := self._resend_guard()):
                self._system(err)
                return False
            content = self.harness.truncate_at_last_user()
            if content is None:
                self._system("Nothing to retry.")
                return False
            self.replay_transcript(reset=True, notice="Retrying your last message…")
            self.bus.put(UserEvent(attach_mod.summarize(content)))
            self._run_bg(self.harness.send, content)
            return True

    def edit_last(self, text: str) -> bool:
        """Replace the typed part of the last user message (keeping its attachments) and re-send."""
        with self._lock:
            if (err := self._resend_guard()):
                self._system(err)
                return False
            last = next((m for m in reversed(self.harness.messages) if m.get("role") == "user"), None)
            if last is None:
                self._system("Nothing to edit.")
                return False
            _, atts = attach_mod.split(last.get("content") or "")
            if not text.strip() and not atts:
                self._system("The edited message is empty.")
                return False
            self.harness.truncate_at_last_user()
            full = attach_mod.compose(text, atts) if atts else text.strip()
            if text.strip() and (not self.harness.input_history or self.harness.input_history[-1] != text.strip()):
                self.harness.input_history.append(text.strip())
            self.replay_transcript(reset=True, notice="Edited your last message…")
            self.bus.put(UserEvent(attach_mod.summarize(full)))
            self._run_bg(self.harness.send, full)
            return True

    def last_user_message(self) -> dict | None:
        """{typed, attachments: [names]} for the web UI's edit action."""
        last = next((m for m in reversed(self.harness.messages) if m.get("role") == "user"), None)
        if last is None:
            return None
        typed, atts = attach_mod.split(last.get("content") or "")
        return {"typed": typed, "attachments": [a["name"] for a in atts]}

    def cancel(self) -> bool:
        """Interrupt the running LLM call. Returns True if something was interrupted."""
        if self._busy and not self.waiting:
            self.harness.cancel()
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        """Switch mode, unless a turn is running (its tools were chosen for the
        old mode).  Returns whether the mode changed."""
        if self._busy and not self.waiting:
            self._system("Busy — finish the response or interrupt before switching mode.")
            self.harness.emit_status()      # frontends put their mode control back
            return False
        self.harness.set_mode(mode)
        return True

    def cycle_net(self):
        """off -> on -> off.  '/net local' is deliberately not in the cycle: it is
        the setting that exposes this harness's own API, so it stays explicit."""
        h = self.harness
        h.net_access = "off" if h.net_access != "off" else "on"
        self._system(f"Internet access: {h.net_access}")
        h.rebuild_system_prompt()
        h.emit_status()

    def toggle_run_confirm(self):
        self.harness.run_confirm = not self.harness.run_confirm
        self._system(f"run_command confirmation: {'on' if self.harness.run_confirm else 'off'}")
        self.harness.emit_status()

    def submit(self, text: str, source: str = "tui",
               attachments: list[dict] | None = None) -> SubmitOutcome:
        """Handle one line of user input from a frontend.

        `attachments` ([{name, text}], already converted to text) are appended to a
        chat message or an answer to the model; they are not valid with /commands."""
        text = text.strip()
        attachments = [a for a in (attachments or []) if a.get("text")]
        if not text and not attachments:
            return SubmitOutcome()
        self._note_activity()
        with self._lock:
            if attachments and text.startswith("/"):
                self._system("Attachments can only be sent with a message, not with a /command.")
                return SubmitOutcome()
            return self._submit(text, source, attachments)

    def _submit(self, text: str, source: str, attachments: list[dict]) -> SubmitOutcome:
        h = self.harness
        parts = text.split(None, 1) or [""]  # empty text: attachments-only message
        cmd = parts[0].lower()

        # Sensitive command: never add to history or show the raw token.
        if cmd == "/token" and len(parts) > 1:
            self.bus.put(UserEvent(f"/token {_mask_token(parts[1])}"))
            result = handle_command(text, h)
            if result.output:
                self._system(result.output)
            return SubmitOutcome()

        if text and (not h.input_history or h.input_history[-1] != text):
            h.input_history.append(text)  # typed text only — never file contents
        # The model gets the full attachment text; frontends show a 📎 summary.
        full = attach_mod.compose(text, attachments) if attachments else text
        self.bus.put(UserEvent(attach_mod.summarize(full)))

        # A pending y/N confirmation consumes this line.
        if self._pending_confirm is not None:
            action, self._pending_confirm = self._pending_confirm, None
            if text.lower() in ("y", "yes"):
                output = action()
                if output:
                    self._system(output)
            else:
                self._system("Cancelled.")
            return SubmitOutcome()

        if text.startswith("/"):
            return self._command(text, cmd, parts, source)

        if self._busy:
            if self.waiting:
                h.provide_user_input(full)
                self._emit_busy()
            else:
                self._system("Busy — waiting for response..." +
                             (" (attachments were not sent)" if attachments else ""))
            return SubmitOutcome()

        self._run_bg(h.send, full)
        return SubmitOutcome()

    def _command(self, text: str, cmd: str, parts: list[str], source: str) -> SubmitOutcome:
        if source == "web" and cmd in ("/exit", "/quit"):
            self._system("/exit is not available from the web UI — close the tab, "
                         "or quit from the terminal.")
            return SubmitOutcome()

        # Commands that mutate harness.messages while a worker thread is running
        # would corrupt the turn structure. Read-only commands are fine.
        if self._busy and not self.waiting and _blocked_while_busy(cmd, parts):
            self._system(_BUSY_MSG)
            return SubmitOutcome()

        result = handle_command(text, self.harness)
        if result.exit_app:
            return SubmitOutcome(exit_app=True)
        if not result.handled:
            self._system(f"Unknown command: {text}")
            return SubmitOutcome()

        if cmd == "/companion-idle-recap":
            self._emit_companion()  # push the new settings to every frontend's controls
        if result.edit_index_filter:
            if source == "web":
                self._system("Edit the filter under View → Code index → Filter…, or use "
                             "/index-filter add|remove <pattern>.")
                return SubmitOutcome()
            return SubmitOutcome(view={"edit_index_filter": result.edit_index_filter})
        view = {f: getattr(result, f) for f in VIEW_FIELDS if getattr(result, f) is not None}
        if view:
            return SubmitOutcome(view=view)
        if result.replay_session:
            # /new passes its own notice; /session <name> uses the default one.
            self.replay_transcript(reset=True, notice=result.output if cmd == "/new" else None)
            return SubmitOutcome()
        if result.retry:
            self.retry_last()
            return SubmitOutcome()
        if result.run_plan:
            self._run_bg(self.harness.execute_plan_threaded)
            return SubmitOutcome()
        if result.run_compact:
            self._system("Compacting context...")
            self._run_bg(self.harness.compact_threaded, result.compact_summarise)
            return SubmitOutcome()
        if result.send_prompt:
            # A command that starts a model turn (/search-sources suggest): the
            # prompt goes to the model exactly like a typed message.
            if result.output:
                self._system(result.output)
            self._run_bg(self.harness.send, result.send_prompt)
            return SubmitOutcome()
        if result.confirm_prompt:
            self._pending_confirm = result.confirm_action
            self._system(result.confirm_prompt + " [y/N]")
        elif result.output:
            self._system(result.output)
        return SubmitOutcome()
