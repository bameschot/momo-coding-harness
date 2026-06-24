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
    Harness, ChatEvent, ToolCallEvent, ToolResultEvent,
    StatusEvent, ErrorEvent, DoneEvent, AskUserEvent, ThinkEvent,
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
_C_FOCUS     = 11
_C_THINK     = 12
_C_CMD       = 13  # input text color when typing a /command

_COLOR_ORANGE     = 16   # custom color slot for orange  (requires COLORS > 16)
_COLOR_PURPLE     = 17   # custom color slot for purple  (requires COLORS > 17)
_KEY_SHIFT_ENTER  = 601  # custom curses keycode bound to Shift+Enter escape sequences
_KEY_CTRL_LEFT    = 602  # Ctrl+Left  — word jump left
_KEY_CTRL_RIGHT   = 603  # Ctrl+Right — word jump right
_MODE_CYCLE = ["design", "writing", "data", "coding"]


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(_C_USER,      curses.COLOR_CYAN,    -1)
    curses.init_pair(_C_ASSISTANT, curses.COLOR_BLUE,    -1)
    curses.init_pair(_C_SYSTEM,    curses.COLOR_MAGENTA, -1)
    curses.init_pair(_C_TOOL_NAME, curses.COLOR_YELLOW,  -1)
    curses.init_pair(_C_TOOL_RES,  -1,                   -1)
    curses.init_pair(_C_STATUS,    curses.COLOR_GREEN,   -1)
    curses.init_pair(_C_WARN,      curses.COLOR_YELLOW,  -1)
    curses.init_pair(_C_DANGER,    curses.COLOR_RED,     -1)
    curses.init_pair(_C_BORDER,    curses.COLOR_WHITE,   -1)
    curses.init_pair(_C_BUSY,      curses.COLOR_BLACK,   curses.COLOR_YELLOW)
    curses.init_pair(_C_FOCUS,     curses.COLOR_GREEN,   -1)
    if curses.can_change_color() and curses.COLORS > 17:
        curses.init_color(_COLOR_ORANGE, 1000, 500,    0)
        curses.init_color(_COLOR_PURPLE,  600,   0, 1000)
        curses.init_pair(_C_THINK, _COLOR_ORANGE, -1)
        curses.init_pair(_C_CMD,   _COLOR_PURPLE, -1)
    else:
        curses.init_pair(_C_THINK, curses.COLOR_YELLOW,  -1)
        curses.init_pair(_C_CMD,   curses.COLOR_MAGENTA, -1)


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

_INPUT_H  = 5  # fixed height of the multi-line input area
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
        self._think_expanded: bool = True    # toggle with /toggle-think-output or Shift+T
        self._input: str = ""
        self._cursor: int = 0           # insertion point within _input
        self._history = harness.input_history  # submitted entries, oldest first; shared with harness for persistence
        self._history_idx: int = -1     # -1 = not browsing
        self._history_stash: str = ""   # saves live input while browsing
        self._focus: str = "input"      # "input" | "chat"
        self._status    = f"MODE: {harness.mode} | MODEL: {harness.client.model} | CTX: 0% | DIR: {harness.workdir}"
        self._ctx_color = _C_STATUS
        self._busy      = False
        self._spinner_frame = 0
        self._spinner_ts    = 0.0
        self._pending_confirm: "callable | None" = None  # set while waiting for y/N
        self._waiting_for_input: bool = False             # set while model is blocked on ask_user

        _init_colors()
        curses.curs_set(1)
        # Disable CR→NL translation so Enter (\r, 13) and Ctrl+J (\n, 10) stay
        # distinct.  Without this, ncurses maps \r → \n on input and the two codes
        # collide, making Ctrl+J indistinguishable from Enter.
        curses.nonl()
        # Also try escape-sequence bindings for terminals that support them.
        # \x1b\r / \x1b\n — Option+Enter (macOS iTerm2 with "+Esc" Option key setting).
        # \x1b[13;2u     — Shift+Enter (kitty/wezterm or iTerm2 with CSI-u mode).
        for _seq in ("\x1b\r", "\x1b\n", "\x1b[13;2u"):
            try:
                curses.define_key(_seq, _KEY_SHIFT_ENTER)
            except Exception:
                pass
        # Ctrl+Left / Ctrl+Right word-jump sequences (xterm/iTerm2 variants).
        for _seq in ("\x1b[1;5D", "\x1b[5D"):
            try: curses.define_key(_seq, _KEY_CTRL_LEFT)
            except Exception: pass
        for _seq in ("\x1b[1;5C", "\x1b[5C"):
            try: curses.define_key(_seq, _KEY_CTRL_RIGHT)
            except Exception: pass
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
        chat_edge = _C_FOCUS if self._focus == "chat" else _C_BORDER
        self._chat_buf.render(self._chat_win, L["chat_h"], L["cols"], edge_color=chat_edge)
        self._draw_status()
        self._draw_input()
        curses.doupdate()

    def _draw_status(self):
        win = self._status_win
        win.erase()
        cols = self._layout["cols"]
        top_color    = _C_FOCUS if self._focus == "chat"  else _C_BORDER
        bottom_color = _C_FOCUS if self._focus == "input" else _C_BORDER
        if self._busy and self._waiting_for_input:
            line = f" ? waiting for input  {self._status}"
            line_color = _C_WARN
        elif self._busy:
            spinner = _SPINNER[self._spinner_frame % len(_SPINNER)]
            line = f" {spinner} thinking  {self._status}"
            line_color = _C_BUSY
        else:
            line = f" {self._status}"
            line_color = self._ctx_color
        line = line[:cols - 1].ljust(cols - 1)
        rule = "─" * (cols - 1)
        try:
            win.addnstr(0, 0, rule, cols - 1, curses.color_pair(top_color))
            win.addnstr(1, 0, line, cols - 1, curses.color_pair(line_color))
            win.addnstr(2, 0, rule, cols - 1, curses.color_pair(bottom_color))
        except curses.error:
            pass
        win.noutrefresh()

    def _build_screen_state(self) -> tuple[list[str], list[int], int, int]:
        """Return (screen_lines, line_starts_raw, cursor_screen_line, cursor_screen_col).

        screen_lines[i]    — text of screen row i
        line_starts_raw[i] — index in (prefix + self._input) where row i begins
        cursor_screen_line/col — cursor's current screen position
        """
        cols = self._layout["cols"]
        w    = max(1, cols - 2)  # rightmost column reserved for scrollbar
        prefix = "? " if self._waiting_for_input else "⊘ " if self._busy else "› "
        plen   = len(prefix)
        raw_full      = prefix + self._input
        cursor_in_raw = plen + self._cursor

        screen_lines:    list[str] = []
        line_starts_raw: list[int] = []
        csl = csc = sc_line = sc_col = 0
        line_buf:  list[str] = []
        line_start = 0

        for i in range(len(raw_full) + 1):
            if i == cursor_in_raw:
                csl, csc = sc_line, sc_col
            if i == len(raw_full):
                screen_lines.append("".join(line_buf))
                line_starts_raw.append(line_start)
                break
            ch = raw_full[i]
            if ch == "\n":
                screen_lines.append("".join(line_buf))
                line_starts_raw.append(line_start)
                line_buf = []; line_start = i + 1
                sc_line += 1; sc_col = 0
            else:
                line_buf.append(ch); sc_col += 1
                if sc_col >= w:
                    screen_lines.append("".join(line_buf))
                    line_starts_raw.append(line_start)
                    line_buf = []; line_start = i + 1
                    sc_line += 1; sc_col = 0

        if not screen_lines:
            screen_lines = [""]; line_starts_raw = [0]
        return screen_lines, line_starts_raw, csl, csc

    def _word_start_left(self) -> int:
        """Return the index in _input for the start of the previous word."""
        i = self._cursor - 1
        while i > 0 and self._input[i - 1] in " \t\n":
            i -= 1
        while i > 0 and self._input[i - 1] not in " \t\n":
            i -= 1
        return i

    def _word_end_right(self) -> int:
        """Return the index in _input for the end of the next word."""
        i = self._cursor
        n = len(self._input)
        while i < n and self._input[i] in " \t\n":
            i += 1
        while i < n and self._input[i] not in " \t\n":
            i += 1
        return i

    def _cursor_move_vertical(self, direction: int) -> bool:
        """Move caret up (-1) or down (+1) by one screen line.
        Returns True if moved; False at the boundary (caller falls back to history nav)."""
        prefix = "? " if self._waiting_for_input else "⊘ " if self._busy else "› "
        plen   = len(prefix)
        screen_lines, line_starts_raw, csl, csc = self._build_screen_state()

        target = csl + direction
        if target < 0 or target >= len(screen_lines):
            return False

        target_col   = min(csc, len(screen_lines[target]))
        self._cursor = max(0, line_starts_raw[target] + target_col - plen)
        return True

    def _draw_input(self):
        win = self._input_win
        win.erase()
        cols = self._layout["cols"]
        h    = self._layout["input_h"]
        focused = self._focus == "input"

        prefix = "? " if self._waiting_for_input else "⊘ " if self._busy else "› "
        plen   = len(prefix)
        screen_lines, _starts, cursor_screen_line, cursor_screen_col = self._build_screen_state()

        # Scroll so the cursor row is always visible.
        first_visible = max(0, cursor_screen_line + 1 - h)
        visible    = screen_lines[first_visible:first_visible + h]
        cursor_row = cursor_screen_line - first_visible

        prefix_attr = curses.color_pair(_C_FOCUS) if focused else curses.color_pair(0)
        is_cmd   = self._input.startswith("/")
        cmd_attr = curses.color_pair(_C_CMD) if is_cmd else curses.color_pair(0)

        text_w = cols - 2  # rightmost column belongs to the scrollbar
        for row, text in enumerate(visible):
            if row >= h:
                break
            try:
                if first_visible + row == 0:
                    # First screen line — render prefix in its own colour.
                    head = text[:plen]
                    tail = text[plen:]
                    win.addnstr(row, 0, head, len(head), prefix_attr)
                    if tail:
                        win.addnstr(row, plen, tail, text_w - plen, cmd_attr)
                else:
                    win.addnstr(row, 0, text, text_w, cmd_attr)
            except curses.error:
                pass

        # Scrollbar — mirrors _LineBuffer.render() logic.
        total  = len(screen_lines)
        sx     = cols - 1
        edge_color = _C_FOCUS if focused else _C_BORDER
        if total > h:
            thumb_h   = max(1, round(h * h / total))
            ratio     = first_visible / max(1, total - h)
            thumb_top = round(ratio * (h - thumb_h))
            for r in range(h):
                ch = "█" if thumb_top <= r < thumb_top + thumb_h else "│"
                try:
                    win.addch(r, sx, ch, curses.color_pair(edge_color))
                except curses.error:
                    pass
        else:
            for r in range(h):
                try:
                    win.addch(r, sx, "│", curses.color_pair(edge_color))
                except curses.error:
                    pass

        try:
            if focused:
                curses.curs_set(1)
                win.move(cursor_row, min(cursor_screen_col, text_w - 1))
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

    def _add_think(self, text: str):
        self._chat_events.append(("think", text))
        self._render_think(text)

    def _render_think(self, text: str):
        if not self._think_expanded:
            return
        cols = max(20, self._layout["cols"] - 4)
        self._chat_buf.append("[thinking]", _C_THINK)
        for src in text.splitlines() or [""]:
            for line in textwrap.wrap(src, width=cols - 2) or [src]:
                self._chat_buf.append("  " + line, _C_THINK)
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
            elif ev[0] == "think":
                self._render_think(ev[1])

    # ── event processing ──────────────────────────────────────────────────────

    def _drain_events(self):
        # Drain the entire queue before redrawing — one redraw per poll cycle
        # is sufficient and avoids screen flicker from partial updates.
        changed = False
        # Buffer index just before the last ChatEvent rendered this cycle.
        # Used below to scroll the start of the new message into view.
        last_chat_start: int | None = None
        try:
            while True:
                ev = self.harness.event_queue.get_nowait()
                if isinstance(ev, ChatEvent):
                    last_chat_start = len(self._chat_buf._lines)
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
                elif isinstance(ev, ThinkEvent):
                    self._add_think(ev.text)
                    changed = True
                elif isinstance(ev, AskUserEvent):
                    self._add_chat("assistant", ev.question)
                    self._waiting_for_input = True
                    changed = True
                elif isinstance(ev, DoneEvent):
                    self._busy = False
                    self._waiting_for_input = False
                    self._spinner_frame = 0
                    changed = True
                elif isinstance(ev, ErrorEvent):
                    last_chat_start = len(self._chat_buf._lines)
                    self._add_chat("system", f"ERROR: {ev.text}")
                    self._busy = False
                    changed = True
        except queue.Empty:
            pass

        # _scroll is the 0-based index of the LAST visible line.
        # Setting it to (start + height - 1) places `start` at the top row,
        # so the beginning of the new message is always visible rather than
        # the end — which looks like the output was cut off mid-text.
        if last_chat_start is not None:
            chat_h = self._layout["chat_h"]
            total  = len(self._chat_buf._lines)
            self._chat_buf._scroll = min(last_chat_start + chat_h - 1, total - 1)

        return changed

    # ── input handling ────────────────────────────────────────────────────────

    def _toggle_tools(self):
        self._tools_expanded = not self._tools_expanded
        self._rebuild_chat_buf()
        self._redraw()

    def _toggle_think(self):
        self._think_expanded = not self._think_expanded
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
        self._cursor = len(self._input)

    def _history_next(self):
        if self._history_idx == -1:
            return
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            self._input = self._history[self._history_idx]
        else:
            self._history_idx = -1
            self._input = self._history_stash
        self._cursor = len(self._input)

    def _replay_session(self):
        """Render all stored session messages into the chat buffer, then show a status notice."""
        pending_names: list[str] = []
        for msg in self.harness.messages:
            role = msg.get("role")
            content = msg.get("content") or ""
            if role == "system":
                continue
            elif role == "thinking":
                self._add_think(content)
            elif role == "user":
                self._add_chat("user", content)
            elif role == "assistant":
                if content:
                    self._add_chat("assistant", content)
                pending_names = []
                for tc in (msg.get("tool_calls") or []):
                    name = tc["function"]["name"]
                    args = tc["function"].get("arguments") or {}
                    pending_names.append(name)
                    self._add_tool_call(name, args)
            elif role == "tool":
                name = pending_names.pop(0) if pending_names else ""
                self._add_tool_result(name, content)
        h = self.harness
        notice = (
            f"Session loaded: {h.session_path().name} ({len(h.messages)} messages)\n"
            f"Model: {h.client.model} | Mode: {h.mode} | Dir: {h.workdir}"
        )
        self._add_chat("system", notice)
        self._chat_buf.scroll_to_bottom()
        self._redraw()

    def _submit(self):
        text = self._input.strip()
        self._input = ""
        self._cursor = 0
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
                if result.toggle_think:
                    self._toggle_think()
                    return
                if result.replay_session:
                    self._replay_session()
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
            if self._waiting_for_input:
                self._waiting_for_input = False
                self.harness.provide_user_input(text)
                self._redraw()
                return
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
        # if a session was pre-loaded before the TUI started, replay it now
        if len(self.harness.messages) > 1:
            self._replay_session()
        else:
            self._redraw()

        _pushed_ch: int | None = None  # character pushed back after paste peek

        while True:
            if _pushed_ch is not None:
                ch = _pushed_ch
                _pushed_ch = None
            else:
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

            # Shift+Tab cycles through all four modes
            if ch == curses.KEY_BTAB:
                idx = _MODE_CYCLE.index(self.harness.mode) if self.harness.mode in _MODE_CYCLE else 0
                new_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
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

            # Shift+↑/↓ — explicit history navigation regardless of caret position.
            if ch == curses.KEY_SR:
                if self._focus == "input":
                    self._history_prev()
                else:
                    self._chat_buf.scroll_up()
                self._redraw()
                continue
            if ch == curses.KEY_SF:
                if self._focus == "input":
                    self._history_next()
                else:
                    self._chat_buf.scroll_down()
                self._redraw()
                continue

            # ↑/↓ — caret movement in multi-line input; fall back to history at the boundary.
            if ch == curses.KEY_UP:
                if self._focus == "chat":
                    self._chat_buf.scroll_up()
                elif not self._cursor_move_vertical(-1):
                    self._history_prev()
                self._redraw()
                continue
            if ch == curses.KEY_DOWN:
                if self._focus == "chat":
                    self._chat_buf.scroll_down()
                elif not self._cursor_move_vertical(1):
                    self._history_next()
                self._redraw()
                continue

            # cursor movement within the input field
            if ch == curses.KEY_LEFT:
                if self._cursor > 0:
                    self._cursor -= 1
                    self._redraw_input_only()
                continue
            if ch == curses.KEY_RIGHT:
                if self._cursor < len(self._input):
                    self._cursor += 1
                    self._redraw_input_only()
                continue
            if ch == curses.KEY_HOME:
                self._cursor = 0
                self._redraw_input_only()
                continue
            if ch == curses.KEY_END:
                self._cursor = len(self._input)
                self._redraw_input_only()
                continue

            # Ctrl+Left / Ctrl+Right — word jump (define_key sequences or manual ESC handler)
            if ch == _KEY_CTRL_LEFT:
                self._cursor = self._word_start_left()
                self._redraw_input_only()
                continue
            if ch == _KEY_CTRL_RIGHT:
                self._cursor = self._word_end_right()
                self._redraw_input_only()
                continue

            # Ctrl+A / Ctrl+E — start / end of current logical line
            if ch == 1:  # Ctrl+A
                i = self._input.rfind('\n', 0, self._cursor)
                self._cursor = i + 1  # 0 when no \n found (rfind returns -1)
                self._redraw_input_only()
                continue
            if ch == 5:  # Ctrl+E
                i = self._input.find('\n', self._cursor)
                self._cursor = i if i >= 0 else len(self._input)
                self._redraw_input_only()
                continue

            # Shift+T toggles thinking output (only when chat pane has focus,
            # so typing 'T' in the input field still works normally)
            if ch == ord('T') and self._focus == "chat":
                self._toggle_think()
                continue

            # ESC (27) — manual check for Option+Enter (ESC + CR/LF).
            # curses.define_key registers the sequence but in nodelay mode curses
            # often returns raw ESC before the following \r is buffered, so the
            # assembled keycode never fires.  Peek immediately instead.
            if ch == 27:
                peek = self.stdscr.getch()
                if peek in (10, 13):
                    # Option+Enter — insert newline
                    self._input = self._input[:self._cursor] + "\n" + self._input[self._cursor:]
                    self._cursor += 1
                    self._redraw_input_only()
                elif peek == ord('b'):
                    # Option+Left / Meta+b — word jump left
                    self._cursor = self._word_start_left()
                    self._redraw_input_only()
                elif peek == ord('f'):
                    # Option+Right / Meta+f — word jump right
                    self._cursor = self._word_end_right()
                    self._redraw_input_only()
                elif peek != curses.ERR:
                    _pushed_ch = peek
                continue

            # input editing always works regardless of focus
            input_changed = False
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if self._cursor > 0:
                    self._input = self._input[:self._cursor - 1] + self._input[self._cursor:]
                    self._cursor -= 1
                    input_changed = True
            elif ch in (10, _KEY_SHIFT_ENTER):
                # LF (10) = Ctrl+J  — always insert a newline.
                # curses.nonl() keeps Enter as \r (13) so these two never collide.
                # _KEY_SHIFT_ENTER covers escape-sequence bindings (Option+Enter,
                # Shift+Enter on terminals that send a distinct sequence).
                self._input = self._input[:self._cursor] + "\n" + self._input[self._cursor:]
                self._cursor += 1
                input_changed = True
            elif ch in (13, curses.KEY_ENTER):
                # CR (13) = Enter.  Peek ahead: if more characters are buffered this
                # is a paste — insert a newline and continue.  Otherwise submit.
                next_ch = self.stdscr.getch()
                if next_ch != curses.ERR:
                    self._input = self._input[:self._cursor] + "\n" + self._input[self._cursor:]
                    self._cursor += 1
                    input_changed = True
                    if next_ch != 10:  # skip the LF half of a CRLF pair
                        _pushed_ch = next_ch
                else:
                    self._submit()
                # _submit() handles all its own redraws
            elif ch == 11:  # Ctrl+K — kill to end of line
                i = self._input.find('\n', self._cursor)
                if i < 0:
                    end = len(self._input)
                elif i == self._cursor:
                    end = self._cursor + 1  # cursor is right before \n — kill the newline
                else:
                    end = i                 # kill up to but not including \n
                if end > self._cursor:
                    self._input = self._input[:self._cursor] + self._input[end:]
                    input_changed = True
            elif ch == 21:  # Ctrl+U — kill from start of line to cursor
                i = self._input.rfind('\n', 0, self._cursor)
                start = i + 1  # 0 when no \n (rfind returns -1)
                if start < self._cursor:
                    self._input = self._input[:start] + self._input[self._cursor:]
                    self._cursor = start
                    input_changed = True
            elif 32 <= ch <= 126:
                char = chr(ch)
                self._input = self._input[:self._cursor] + char + self._input[self._cursor:]
                self._cursor += 1
                input_changed = True

            if changed:
                self._redraw()
            elif input_changed:
                self._redraw_input_only()


def run_tui(stdscr, harness: Harness):
    tui = TUI(stdscr, harness)
    tui.run()
