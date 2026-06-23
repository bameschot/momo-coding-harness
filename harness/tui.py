from __future__ import annotations

import curses
import json
import os
import queue
import sys
import threading
import time
import textwrap
from pathlib import Path
from typing import Any

from .commands import handle as handle_command
from .harness import (
    Harness, ChatEvent, ToolCallEvent, ToolResultEvent, StatusEvent, ErrorEvent, DoneEvent
)


# ── color pair ids ────────────────────────────────────────────────────────────
_C_USER      = 1
_C_ASSISTANT = 2
_C_SYSTEM    = 3
_C_TOOL_NAME = 4
_C_TOOL_RES  = 5
_C_STATUS    = 6
_C_WARN      = 7
_C_DANGER    = 8
_C_BORDER    = 9
_C_BUSY      = 10


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_C_USER,      curses.COLOR_CYAN,    -1)
    curses.init_pair(_C_ASSISTANT, curses.COLOR_GREEN,   -1)
    curses.init_pair(_C_SYSTEM,    curses.COLOR_MAGENTA, -1)
    curses.init_pair(_C_TOOL_NAME, curses.COLOR_YELLOW,  -1)
    curses.init_pair(_C_TOOL_RES,  -1,                   -1)
    curses.init_pair(_C_STATUS,    curses.COLOR_GREEN,   -1)
    curses.init_pair(_C_WARN,      curses.COLOR_YELLOW,  -1)
    curses.init_pair(_C_DANGER,    curses.COLOR_RED,     -1)
    curses.init_pair(_C_BORDER,    curses.COLOR_WHITE,   -1)
    curses.init_pair(_C_BUSY,      curses.COLOR_BLACK,   curses.COLOR_YELLOW)


# ── spinner ───────────────────────────────────────────────────────────────────

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL = 0.1  # seconds per frame


# ── extended key support ─────────────────────────────────────────────────────

KEY_SHIFT_ENTER = 600  # synthetic key code; not used by curses itself

def _enable_shift_enter():
    """Ask the terminal to report Shift+Enter as a distinct sequence."""
    try:
        # xterm modifyOtherKeys level 2 — makes Shift+Enter send \033[27;2;13~
        os.write(sys.stdout.fileno(), b"\033[>4;2m")
    except OSError:
        pass
    try:
        curses.define_key("\033[27;2;13~", KEY_SHIFT_ENTER)  # xterm / VTE
        curses.define_key("\033[13;2u",    KEY_SHIFT_ENTER)  # kitty protocol
    except (AttributeError, curses.error):
        pass

def _disable_shift_enter():
    try:
        os.write(sys.stdout.fileno(), b"\033[>4;0m")
    except OSError:
        pass


# ── scrollable line buffer ────────────────────────────────────────────────────

class _LineBuffer:
    """Holds wrapped display lines and a scroll offset."""

    def __init__(self):
        self._lines: list[tuple[str, int]] = []  # (text, color_pair)
        self._scroll = 0

    def append(self, text: str, color: int):
        self._lines.append((text, color))
        # auto-scroll to bottom when new content added
        self._scroll = max(0, len(self._lines) - 1)

    def scroll_up(self, n: int = 3):
        self._scroll = max(0, self._scroll - n)

    def scroll_down(self, n: int = 3):
        self._scroll = min(max(0, len(self._lines) - 1), self._scroll + n)

    def scroll_to_bottom(self):
        self._scroll = max(0, len(self._lines) - 1)

    def render(self, win, height: int, width: int, edge_color: int = _C_BORDER):
        win.erase()
        total = len(self._lines)

        # text region — leave rightmost column for edge/scrollbar
        if total:
            if total <= height:
                # all content fits — render from row 0, ignore scroll offset
                visible = self._lines
            else:
                start = max(0, self._scroll + 1 - height)
                visible = self._lines[start:start + height]
            for row, (text, color) in enumerate(visible):
                if row >= height:
                    break
                try:
                    win.addnstr(row, 0, text, width - 2, curses.color_pair(color))
                except curses.error:
                    pass

        # right-column edge / scrollbar — always drawn as focus indicator
        sx = width - 1
        if total > height:
            thumb_h = max(1, round(height * height / total))
            scroll_start = max(0, self._scroll + 1 - height)
            ratio = scroll_start / max(1, total - height)
            thumb_top = round(ratio * (height - thumb_h))
            for r in range(height):
                ch = "█" if thumb_top <= r < thumb_top + thumb_h else "│"
                try:
                    win.addch(r, sx, ch, curses.color_pair(edge_color))
                except curses.error:
                    pass
        else:
            for r in range(height):
                try:
                    win.addch(r, sx, "│", curses.color_pair(edge_color))
                except curses.error:
                    pass

        win.noutrefresh()


# ── layout ────────────────────────────────────────────────────────────────────

_INPUT_H  = 4  # fixed height of the multi-line input area
_STATUS_H = 3  # top border + text + bottom border

def _compute_layout(rows: int, cols: int) -> dict:
    chat_h   = max(4, rows - _STATUS_H - _INPUT_H)
    status_y = chat_h
    input_y  = chat_h + _STATUS_H
    return {
        "chat_y":   0,       "chat_h": chat_h,
        "status_y": status_y,
        "input_y":  input_y,
        "input_h":  _INPUT_H,
        "cols":     cols,
    }


# ── main TUI ──────────────────────────────────────────────────────────────────

class TUI:
    def __init__(self, stdscr, harness: Harness):
        self.stdscr = stdscr
        self.harness = harness
        self._chat_buf    = _LineBuffer()
        self._chat_events: list[tuple] = []  # raw events for toggle rebuild
        self._tools_expanded: bool = True    # True = full tool output; False = abbreviated
        self._input: str = ""
        self._history: list[str] = []   # submitted entries, oldest first
        self._history_idx: int = -1     # -1 = not browsing
        self._history_stash: str = ""   # saves live input while browsing
        self._focus: str = "input"      # "input" | "chat"
        self._status    = f"MODE: {harness.mode} | MODEL: {harness.client.model} | CTX: 0% | DIR: {harness.workdir}"
        self._ctx_color = _C_STATUS
        self._busy      = False
        self._spinner_frame = 0
        self._spinner_ts    = 0.0
        self._pending_confirm: "callable | None" = None  # set while waiting for y/N

        _init_colors()
        curses.curs_set(1)
        self.stdscr.nodelay(True)  # non-blocking getch — keys processed immediately

        rows, cols = stdscr.getmaxyx()
        self._layout = _compute_layout(rows, cols)
        self._build_windows()

    def _build_windows(self):
        L = self._layout
        cols = L["cols"]
        self._chat_win   = curses.newwin(L["chat_h"],  cols, L["chat_y"],   0)
        self._status_win = curses.newwin(_STATUS_H,    cols, L["status_y"], 0)
        self._input_win  = curses.newwin(L["input_h"], cols, L["input_y"],  0)

    def _rebuild(self):
        rows, cols = self.stdscr.getmaxyx()
        self._layout = _compute_layout(rows, cols)
        self._build_windows()
        self.stdscr.clear()
        self.stdscr.noutrefresh()
        self._redraw()

    def _redraw(self):
        L = self._layout
        chat_edge = _C_ASSISTANT if self._focus == "chat" else _C_BORDER
        self._chat_buf.render(self._chat_win, L["chat_h"], L["cols"], edge_color=chat_edge)
        self._draw_status()
        self._draw_input()
        curses.doupdate()

    def _draw_status(self):
        win = self._status_win
        win.erase()
        cols = self._layout["cols"]
        border_color = self._ctx_color if self._focus == "input" else _C_BORDER
        if self._busy:
            spinner = _SPINNER[self._spinner_frame % len(_SPINNER)]
            line = f" {spinner} thinking  {self._status}"
            line_color = _C_BUSY
        else:
            line = f" {self._status}"
            line_color = border_color
        line = line[:cols - 1].ljust(cols - 1)
        rule = "─" * (cols - 1)
        try:
            win.addnstr(0, 0, rule, cols - 1, curses.color_pair(border_color))
            win.addnstr(1, 0, line, cols - 1, curses.color_pair(line_color))
            win.addnstr(2, 0, rule, cols - 1, curses.color_pair(border_color))
        except curses.error:
            pass
        win.noutrefresh()

    def _draw_input(self):
        win = self._input_win
        win.erase()
        cols = self._layout["cols"]
        h    = self._layout["input_h"]
        focused = self._focus == "input"

        # Hard-chunk into rows of (cols-1) chars — preserves trailing spaces
        # so the cursor advances correctly after typing a space.
        raw = ("⊘ " if self._busy else "› ") + self._input
        w = max(1, cols - 1)
        chunks = [raw[i:i+w] for i in range(0, len(raw), w)] or ["› "]

        # scroll to keep the end of the text in view
        visible = chunks[max(0, len(chunks) - h):]
        prefix_attr = curses.color_pair(_C_USER) if focused else curses.color_pair(0)
        for row, text in enumerate(visible):
            if row >= h:
                break
            try:
                attr = prefix_attr if row == 0 else curses.color_pair(0)
                win.addnstr(row, 0, text, cols - 1, attr)
            except curses.error:
                pass

        last_row = min(len(visible) - 1, h - 1)
        last_text = visible[last_row] if visible else "› "
        cx = min(len(last_text), cols - 2)
        try:
            if focused:
                curses.curs_set(1)
                win.move(last_row, cx)
            else:
                curses.curs_set(0)
        except curses.error:
            pass
        win.noutrefresh()

    def _redraw_input_only(self):
        self._draw_input()
        curses.doupdate()

    # ── adding lines to chat buffer ───────────────────────────────────────────

    def _add_chat(self, role: str, text: str):
        self._chat_events.append(("chat", role, text))
        self._render_chat(role, text)

    def _render_chat(self, role: str, text: str):
        cols = max(20, self._layout["cols"] - 2)
        label_map = {
            "user": ("[user]", _C_USER),
            "assistant": ("[assistant]", _C_ASSISTANT),
            "system": ("[system]", _C_SYSTEM),
        }
        label, color = label_map.get(role, (f"[{role}]", _C_SYSTEM))
        indent = " " * (len(label) + 1)
        source_lines = text.splitlines() or [""]
        first = True
        for src in source_lines:
            wrapped = textwrap.wrap(src, width=cols - len(label) - 1) or [""]
            for wl in wrapped:
                if first:
                    self._chat_buf.append(f"{label} {wl}", color)
                    first = False
                else:
                    self._chat_buf.append(f"{indent}{wl}", color)
        self._chat_buf.append("", 0)

    def _add_tool_call(self, name: str, args: dict):
        self._chat_events.append(("tool_call", name, args))
        self._render_tool_call(name, args)

    def _render_tool_call(self, name: str, args: dict):
        cols = max(20, self._layout["cols"] - 2)
        args_str = json.dumps(args, separators=(",", ":"))
        full = f"▶ {name}({args_str})"
        if self._tools_expanded:
            for line in textwrap.wrap(full, width=cols) or [full]:
                self._chat_buf.append(line, _C_TOOL_NAME)
        else:
            abbrev = (full[:50] + "…") if len(full) > 50 else full
            self._chat_buf.append(abbrev, _C_TOOL_NAME)

    def _add_tool_result(self, name: str, result: str):
        self._chat_events.append(("tool_result", name, result))
        self._render_tool_result(name, result)

    def _render_tool_result(self, name: str, result: str):
        if not self._tools_expanded:
            return
        cols = max(20, self._layout["cols"] - 4)
        prefix = "  → "
        lines = result.splitlines() or ["(empty)"]
        display = lines[:20]
        if len(lines) > 20:
            display.append(f"  ... ({len(lines) - 20} more lines)")
        for line in display:
            for wrapped in textwrap.wrap(prefix + line, width=cols) or [prefix + line]:
                self._chat_buf.append(wrapped, _C_TOOL_RES)
        self._chat_buf.append("", 0)

    def _rebuild_chat_buf(self):
        # Re-render all events from scratch. Called when display options change
        # (e.g. tool expand/collapse toggle) so the layout is consistent.
        self._chat_buf = _LineBuffer()
        for ev in self._chat_events:
            if ev[0] == "chat":
                self._render_chat(ev[1], ev[2])
            elif ev[0] == "tool_call":
                self._render_tool_call(ev[1], ev[2])
            elif ev[0] == "tool_result":
                self._render_tool_result(ev[1], ev[2])

    # ── event processing ──────────────────────────────────────────────────────

    def _drain_events(self):
        # Drain the entire queue before redrawing — one redraw per poll cycle
        # is sufficient and avoids screen flicker from partial updates.
        changed = False
        try:
            while True:
                ev = self.harness.event_queue.get_nowait()
                if isinstance(ev, ChatEvent):
                    self._add_chat(ev.role, ev.text)
                    changed = True
                elif isinstance(ev, ToolCallEvent):
                    self._add_tool_call(ev.name, ev.args)
                    changed = True
                elif isinstance(ev, ToolResultEvent):
                    self._add_tool_result(ev.name, ev.result)
                    changed = True
                elif isinstance(ev, StatusEvent):
                    ctx_map = {"normal": _C_STATUS, "yellow": _C_WARN, "red": _C_DANGER}
                    self._ctx_color = ctx_map.get(ev.ctx_color, _C_STATUS)
                    self._status = (
                        f"MODE: {ev.mode} | MODEL: {ev.model} | "
                        f"CTX: {ev.ctx_pct}% | DIR: {ev.workdir}"
                    )
                    changed = True
                elif isinstance(ev, DoneEvent):
                    self._busy = False
                    self._spinner_frame = 0
                    changed = True
                elif isinstance(ev, ErrorEvent):
                    self._add_chat("system", f"ERROR: {ev.text}")
                    self._busy = False
                    changed = True
        except queue.Empty:
            pass
        return changed

    # ── input handling ────────────────────────────────────────────────────────

    def _toggle_tools(self):
        self._tools_expanded = not self._tools_expanded
        self._rebuild_chat_buf()
        self._redraw()

    def _history_prev(self):
        if not self._history:
            return
        if self._history_idx == -1:
            self._history_stash = self._input
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self._input = self._history[self._history_idx]

    def _history_next(self):
        if self._history_idx == -1:
            return
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self._input = self._history[self._history_idx]
        else:
            self._history_idx = -1
            self._input = self._history_stash

    def _submit(self):
        text = self._input.strip()
        self._input = ""
        self._history_idx = -1
        self._history_stash = ""
        if not text:
            return

        if not self._history or self._history[-1] != text:
            self._history.append(text)

        # show what the user typed (common to all paths below)
        self._add_chat("user", text)

        # handle pending y/N confirmation
        if self._pending_confirm is not None:
            action = self._pending_confirm
            self._pending_confirm = None
            if text.lower() in ("y", "yes"):
                output = action()
                if output:
                    self._add_chat("system", output)
            else:
                self._add_chat("system", "Cancelled.")
            self._redraw()
            return

        if text.startswith("/"):
            result = handle_command(text, self.harness)
            if result.exit_app:
                raise SystemExit(0)
            if result.handled:
                if result.toggle_tools:
                    self._toggle_tools()
                    return
                if result.confirm_prompt:
                    self._pending_confirm = result.confirm_action
                    self._add_chat("system", result.confirm_prompt + " [y/N]")
                elif result.output:
                    self._add_chat("system", result.output)
                self._redraw()
                return
            self._add_chat("system", f"Unknown command: {text}")
            self._redraw()
            return

        if self._busy:
            self._add_chat("system", "Busy — waiting for response...")
            self._redraw()
            return

        self._busy = True
        self._redraw()  # show user message before thread starts
        t = threading.Thread(target=self.harness.send, args=(text,), daemon=True)
        t.start()

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        # emit initial status
        self.harness._emit_status()
        self._redraw()

        while True:
            ch = self.stdscr.getch()

            if ch == curses.KEY_RESIZE:
                self._rebuild()
                continue

            changed = self._drain_events()

            if ch == curses.ERR:
                if changed:
                    self._redraw()
                elif self._busy:
                    now = time.time()
                    if now - self._spinner_ts >= _SPINNER_INTERVAL:
                        self._spinner_frame += 1
                        self._spinner_ts = now
                        self._draw_status()
                        curses.doupdate()
                time.sleep(0.02)  # idle — avoids CPU spin without adding key lag
                continue

            # Shift+Tab toggles design ↔ coding mode
            if ch == curses.KEY_BTAB:
                new_mode = "coding" if self.harness.mode == "design" else "design"
                self.harness.set_mode(new_mode)
                self._drain_events()  # consume the StatusEvent set_mode just enqueued
                self._redraw()
                continue

            # Tab toggles focus between chat and input
            if ch == 9:
                self._focus = "input" if self._focus == "chat" else "chat"
                self._redraw()
                continue

            # PgUp/PgDn scroll the chat pane
            if ch == curses.KEY_PPAGE:
                self._chat_buf.scroll_up(self._layout["chat_h"] - 1)
                self._redraw()
                continue
            if ch == curses.KEY_NPAGE:
                self._chat_buf.scroll_down(self._layout["chat_h"] - 1)
                self._redraw()
                continue

            # ↑/↓ scroll chat when focused there; navigate history otherwise
            if ch == curses.KEY_UP:
                if self._focus == "chat":
                    self._chat_buf.scroll_up()
                else:
                    self._history_prev()
                self._redraw()
                continue
            if ch == curses.KEY_DOWN:
                if self._focus == "chat":
                    self._chat_buf.scroll_down()
                else:
                    self._history_next()
                self._redraw()
                continue

            # input editing always works regardless of focus
            input_changed = False
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self._input = self._input[:-1]
                input_changed = True
            elif ch in (10, 13, curses.KEY_ENTER):
                self._submit()
                # _submit() handles all its own redraws
            elif 32 <= ch <= 126:
                self._input += chr(ch)
                input_changed = True

            if changed:
                self._redraw()
            elif input_changed:
                self._redraw_input_only()


def run_tui(stdscr, harness: Harness):
    tui = TUI(stdscr, harness)
    tui.run()
