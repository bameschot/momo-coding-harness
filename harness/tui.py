from __future__ import annotations

import curses
import json
import queue
import threading
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


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_C_USER,      curses.COLOR_CYAN,    -1)
    curses.init_pair(_C_ASSISTANT, curses.COLOR_GREEN,   -1)
    curses.init_pair(_C_SYSTEM,    curses.COLOR_MAGENTA, -1)
    curses.init_pair(_C_TOOL_NAME, curses.COLOR_YELLOW,  -1)
    curses.init_pair(_C_TOOL_RES,  -1,                   -1)
    curses.init_pair(_C_STATUS,    curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(_C_WARN,      curses.COLOR_YELLOW,  -1)
    curses.init_pair(_C_DANGER,    curses.COLOR_RED,     -1)
    curses.init_pair(_C_BORDER,    curses.COLOR_WHITE,   -1)


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

    def render(self, win, height: int, width: int):
        win.erase()
        if not self._lines:
            win.noutrefresh()
            return
        # compute visible window: show lines ending at scroll+1, filling height rows upward
        end = self._scroll + 1
        start = max(0, end - height)
        visible = self._lines[start:end]
        row = 0
        for text, color in visible:
            if row >= height:
                break
            try:
                win.addnstr(row, 0, text, width - 1, curses.color_pair(color))
            except curses.error:
                pass
            row += 1
        win.noutrefresh()


# ── layout ────────────────────────────────────────────────────────────────────

def _compute_layout(rows: int, cols: int) -> dict:
    chat_h   = max(4, int(rows * 0.60))
    tool_h   = max(3, rows - chat_h - 3)  # 1 status + 1 input + 1 divider handled by borders
    return {
        "chat_y": 0, "chat_h": chat_h,
        "tool_y": chat_h, "tool_h": tool_h,
        "status_y": chat_h + tool_h,
        "input_y": chat_h + tool_h + 1,
        "cols": cols,
    }


# ── main TUI ──────────────────────────────────────────────────────────────────

class TUI:
    def __init__(self, stdscr, harness: Harness):
        self.stdscr = stdscr
        self.harness = harness
        self._chat_buf  = _LineBuffer()
        self._tool_buf  = _LineBuffer()
        self._input     = ""
        self._status    = f"MODE: {harness.mode} | MODEL: {harness.client.model} | CTX: 0% | DIR: {harness.workdir}"
        self._ctx_color = _C_STATUS
        self._busy      = False

        _init_colors()
        curses.curs_set(1)
        curses.halfdelay(1)  # getch() blocks for up to 100ms

        rows, cols = stdscr.getmaxyx()
        self._layout = _compute_layout(rows, cols)
        self._build_windows()

    def _build_windows(self):
        L = self._layout
        cols = L["cols"]
        self._chat_win  = curses.newwin(L["chat_h"],  cols, L["chat_y"],   0)
        self._tool_win  = curses.newwin(L["tool_h"],  cols, L["tool_y"],   0)
        self._status_win = curses.newwin(1,            cols, L["status_y"], 0)
        self._input_win  = curses.newwin(1,            cols, L["input_y"],  0)

    def _rebuild(self):
        rows, cols = self.stdscr.getmaxyx()
        self._layout = _compute_layout(rows, cols)
        self._build_windows()
        self.stdscr.clear()
        self.stdscr.noutrefresh()
        self._redraw()

    def _redraw(self):
        L = self._layout
        self._chat_buf.render(self._chat_win,  L["chat_h"],  L["cols"])
        self._tool_buf.render(self._tool_win,  L["tool_h"],  L["cols"])
        self._draw_status()
        self._draw_input()
        curses.doupdate()

    def _draw_status(self):
        win = self._status_win
        win.erase()
        cols = self._layout["cols"]
        text = self._status.ljust(cols - 1)[:cols - 1]
        try:
            win.addnstr(0, 0, text, cols - 1, curses.color_pair(self._ctx_color))
        except curses.error:
            pass
        win.noutrefresh()

    def _draw_input(self):
        win = self._input_win
        win.erase()
        cols = self._layout["cols"]
        prefix = "> "
        display = prefix + self._input
        try:
            win.addnstr(0, 0, display, cols - 1)
            # position cursor
            cx = min(len(display), cols - 2)
            win.move(0, cx)
        except curses.error:
            pass
        win.noutrefresh()

    # ── adding lines to buffers ───────────────────────────────────────────────

    def _add_chat(self, role: str, text: str):
        cols = max(20, self._layout["cols"] - 2)
        label_map = {
            "user": ("[user]", _C_USER),
            "assistant": ("[assistant]", _C_ASSISTANT),
            "system": ("[system]", _C_SYSTEM),
        }
        label, color = label_map.get(role, (f"[{role}]", _C_SYSTEM))
        indent = " " * (len(label) + 1)

        # split on existing newlines first, then word-wrap each line
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
        self._chat_buf.append("", 0)  # blank separator

    def _add_tool_call(self, name: str, args: dict):
        cols = max(20, self._layout["cols"] - 2)
        args_str = json.dumps(args, separators=(",", ":"))
        header = f"▶ {name}({args_str})"
        for line in textwrap.wrap(header, width=cols) or [header]:
            self._tool_buf.append(line, _C_TOOL_NAME)

    def _add_tool_result(self, name: str, result: str):
        cols = max(20, self._layout["cols"] - 4)
        prefix = "  → "
        lines = result.splitlines() or ["(empty)"]
        # show at most 20 lines of result to keep tool pane readable
        display = lines[:20]
        if len(lines) > 20:
            display.append(f"  ... ({len(lines) - 20} more lines)")
        for line in display:
            for wrapped in textwrap.wrap(prefix + line, width=cols) or [prefix + line]:
                self._tool_buf.append(wrapped, _C_TOOL_RES)
        self._tool_buf.append("", 0)

    # ── event processing ──────────────────────────────────────────────────────

    def _drain_events(self):
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
                    changed = True
                elif isinstance(ev, ErrorEvent):
                    self._add_chat("system", f"ERROR: {ev.text}")
                    self._busy = False
                    changed = True
        except queue.Empty:
            pass
        return changed

    # ── input handling ────────────────────────────────────────────────────────

    def _submit(self):
        text = self._input.strip()
        self._input = ""
        if not text:
            return

        if text.startswith("/"):
            result = handle_command(text, self.harness)
            if result.exit_app:
                raise SystemExit(0)
            if result.handled:
                if result.output:
                    self._add_chat("system", result.output)
                return
            # unknown command — treat as chat input anyway
            self._add_chat("system", f"Unknown command: {text}")
            return

        if self._busy:
            self._add_chat("system", "Busy — waiting for response...")
            return

        self._busy = True
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
                continue

            # scrolling
            if ch == curses.KEY_UP:
                self._chat_buf.scroll_up()
                self._redraw()
                continue
            if ch == curses.KEY_DOWN:
                self._chat_buf.scroll_down()
                self._redraw()
                continue
            if ch == curses.KEY_PPAGE:  # page up
                self._chat_buf.scroll_up(self._layout["chat_h"] - 1)
                self._redraw()
                continue
            if ch == curses.KEY_NPAGE:  # page down
                self._chat_buf.scroll_down(self._layout["chat_h"] - 1)
                self._redraw()
                continue

            # input editing
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                self._input = self._input[:-1]
            elif ch in (10, 13, curses.KEY_ENTER):  # enter
                self._submit()
            elif 32 <= ch <= 126:
                self._input += chr(ch)

            if changed or ch != curses.ERR:
                self._redraw()


def run_tui(stdscr, harness: Harness):
    tui = TUI(stdscr, harness)
    tui.run()
