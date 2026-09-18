from __future__ import annotations

import curses
import json
import os
import queue
import random
import sys
import time
import textwrap
from pathlib import Path
from typing import Any

from . import md_render
from .controller import Controller
from .events import BusyEvent, DeltaEvent, ResetEvent, StreamEndEvent, UserEvent
from .harness import (
    Harness, ChatEvent, ToolCallEvent, ToolResultEvent,
    StatusEvent, ErrorEvent, DoneEvent, AskUserEvent, ThinkEvent, DiffEvent,
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

# markdown renderer color pairs (assigned in _init_colors)
_C_MD_H1    = 14
_C_MD_H2    = 15
_C_MD_H3    = 16
_C_MD_CODE  = 17
_C_MD_QUOTE = 18
_C_MD_BOLD  = 19
_C_COMPANION = 20
_C_DIFF_ADD  = 21  # diff added lines   (green)
_C_DIFF_DEL  = 22  # diff removed lines (red)
_C_DIFF_HUNK = 23  # diff @@ hunk header (cyan)
_C_DIFF_META = 24  # diff file header line

_COLOR_ORANGE     = 16   # custom color slot for orange  (requires COLORS > 16)
_COLOR_PURPLE     = 17   # custom color slot for purple  (requires COLORS > 17)
_KEY_SHIFT_ENTER  = 601  # custom curses keycode bound to Shift+Enter escape sequences
_KEY_CTRL_LEFT    = 602  # Ctrl+Left  — word jump left
_KEY_CTRL_RIGHT   = 603  # Ctrl+Right — word jump right
_MODE_CYCLE = ["design", "chat", "plan", "coding", "momo"]


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
    # markdown renderer pairs (always standard colors — no custom slots needed)
    curses.init_pair(_C_MD_H1,    curses.COLOR_CYAN,   -1)
    curses.init_pair(_C_MD_H2,    curses.COLOR_CYAN,   -1)
    curses.init_pair(_C_MD_H3,    curses.COLOR_WHITE,  -1)
    curses.init_pair(_C_MD_CODE,  curses.COLOR_WHITE,  -1)
    curses.init_pair(_C_MD_QUOTE, curses.COLOR_YELLOW, -1)
    curses.init_pair(_C_MD_BOLD,  curses.COLOR_WHITE,  -1)
    curses.init_pair(_C_COMPANION, curses.COLOR_MAGENTA, -1)
    curses.init_pair(_C_DIFF_ADD,  curses.COLOR_GREEN,  -1)
    curses.init_pair(_C_DIFF_DEL,  curses.COLOR_RED,    -1)
    curses.init_pair(_C_DIFF_HUNK, curses.COLOR_CYAN,   -1)
    curses.init_pair(_C_DIFF_META, curses.COLOR_WHITE,  -1)


# ── spinner ───────────────────────────────────────────────────────────────────

_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_INTERVAL = 0.1  # seconds per frame

# ── momo companion ────────────────────────────────────────────────────────────

_CAT_W              = 8    # visible width of every frame line
_COMPANION_H        = 4    # 4 art rows
_COMPANION_INTERVAL = 0.12  # seconds per animation tick

from .companion import (  # noqa: E402  (shared with the web UI)
    _MOMO_WR, _MOMO_WL, _MOMO_SIT, _MOMO_SIT_L, _MOMO_WR_BLINK, _MOMO_WL_BLINK,
    _SPEECH_TEXTS, _SPEECH_TEXTS_DEFAULT,
)


# ── extended key support ─────────────────────────────────────────────────────


# ── scrollable line buffer ────────────────────────────────────────────────────

class _LineBuffer:
    """Holds wrapped display lines and scroll offsets (vertical + horizontal)."""

    def __init__(self):
        self._lines: list[tuple[str, int, int]] = []  # (text, color_pair, attrs)
        self._scroll  = 0   # vertical: 0-based index of the last visible line
        self._hscroll = 0   # horizontal: columns scrolled from the left
        self._max_line_w: int = 0  # running maximum of len(text) across all lines

    def append(self, text: str, color: int, attrs: int = 0):
        self._lines.append((text, color, attrs))
        if len(text) > self._max_line_w:
            self._max_line_w = len(text)
        # Always track the latest line so the view follows new output by default.
        # _drain_events overrides this after a batch to anchor the *top* of the
        # new message, so callers should not rely on _scroll staying at the tail.
        self._scroll = max(0, len(self._lines) - 1)

    def scroll_up(self, n: int = 3):
        self._scroll = max(0, self._scroll - n)

    def scroll_down(self, n: int = 3):
        self._scroll = min(max(0, len(self._lines) - 1), self._scroll + n)

    def scroll_to_bottom(self):
        self._scroll = max(0, len(self._lines) - 1)

    def scroll_to_top_of(self, line_idx: int, height: int):
        """Place line_idx at the top of a viewport of `height` rows."""
        self._scroll = min(line_idx + height - 1, max(0, len(self._lines) - 1))

    def scroll_left(self, n: int = 8):
        self._hscroll = max(0, self._hscroll - n)

    def scroll_right(self, n: int = 8, display_w: int = 0):
        max_w = self._max_line_w
        # Cap at max_w - display_w: scrolling further would show only blank space.
        # Also prevents hscroll going positive when content fits (max_w <= display_w),
        # which would shift content without triggering the scrollbar (has_hscroll uses
        # strict >, so content exactly equal to display_w shows no bar).
        max_hscroll = max(0, max_w - display_w) if display_w > 0 else max(0, max_w - 1)
        self._hscroll = min(max_hscroll, self._hscroll + n)

    def render(self, win, height: int, width: int, edge_color: int = _C_BORDER):
        win.erase()
        total = len(self._lines)
        display_w = width - 2   # rightmost column reserved for vertical scrollbar
        content_h = height - 1  # bottom row always reserved for horizontal scrollbar

        # thumb is only shown when content is wider than the display area
        max_w    = self._max_line_w
        has_hscroll = max_w > display_w

        # text region
        if total:
            if total <= content_h:
                visible = self._lines
            else:
                start = max(0, self._scroll + 1 - content_h)
                visible = self._lines[start:start + content_h]
            for row, (text, color, attrs) in enumerate(visible):
                if row >= content_h:
                    break
                display = text[self._hscroll : self._hscroll + display_w]
                try:
                    win.addnstr(row, 0, display, display_w, curses.color_pair(color) | attrs)
                except curses.error:
                    pass

        # right-column vertical scrollbar
        sx = width - 1
        if total > content_h:
            thumb_h = max(1, round(content_h * content_h / total))
            scroll_start = max(0, self._scroll + 1 - content_h)
            ratio = scroll_start / max(1, total - content_h)
            thumb_top = round(ratio * (content_h - thumb_h))
            for r in range(content_h):
                ch = "█" if thumb_top <= r < thumb_top + thumb_h else "│"
                try:
                    win.addch(r, sx, ch, curses.color_pair(edge_color))
                except curses.error:
                    pass
        else:
            for r in range(content_h):
                try:
                    win.addch(r, sx, "│", curses.color_pair(edge_color))
                except curses.error:
                    pass

        # horizontal scrollbar — always drawn; thumb only when content overflows
        bar_w = max(2, width - 4)
        if has_hscroll:
            scrollable = max(1, max_w - display_w)
            ratio_h    = self._hscroll / scrollable
            thumb_w    = max(1, round(display_w / max_w * bar_w))
            thumb_pos  = max(0, min(round(ratio_h * (bar_w - thumb_w)), bar_w - thumb_w))
            hbar = "─" * thumb_pos + "█" * thumb_w + "─" * (bar_w - thumb_pos - thumb_w)
        else:
            hbar = "─" * bar_w  # track only, no thumb
        try:
            win.addnstr(height - 1, 0, "◀" + hbar + "▶", width - 1,
                        curses.color_pair(edge_color))
        except curses.error:
            pass

        # noutrefresh (not refresh) so the caller can batch multiple window
        # updates before the single curses.doupdate() that pushes them all to
        # the terminal simultaneously.  Calling win.refresh() here would do an
        # immediate screen update for every rendered window, causing visible flicker.
        win.noutrefresh()


# ── layout ────────────────────────────────────────────────────────────────────

def _shorten_path_left(path: str, budget: int) -> str:
    """Shorten a path to at most `budget` chars, trimming from the front with a
    leading '…' so the tail (the most specific part) stays visible.  Returns ''
    when there is no room."""
    if budget <= 1:
        return ""
    if len(path) <= budget:
        return path
    return "…" + path[-(budget - 1):]


_INPUT_H  = 5  # fixed height of the multi-line input area
_STATUS_H = 3  # top border + text + bottom border

def _compute_layout(rows: int, cols: int, companion_h: int = 0) -> dict:
    # chat_h absorbs all slack; clamped to 4 so there is always some chat area.
    # HAZARD: when the terminal is very short (rows < _STATUS_H + _INPUT_H +
    # companion_h + 4) the clamp forces chat_h = 4 regardless, which pushes
    # status_y and input_y past `rows`.  curses.newwin will then raise an
    # uncaught exception in _build_windows.  A defensive lower-bound on rows
    # (e.g. max(rows, _STATUS_H + _INPUT_H + companion_h + 4)) before computing
    # would make the layout safe at any terminal size.
    chat_h      = max(4, rows - _STATUS_H - _INPUT_H - companion_h)
    companion_y = chat_h
    status_y    = chat_h + companion_h
    input_y     = status_y + _STATUS_H
    return {
        "chat_y":      0,       "chat_h":      chat_h,
        "companion_y": companion_y, "companion_h": companion_h,
        "status_y":    status_y,
        "input_y":     input_y, "input_h":     _INPUT_H,
        "cols":        cols,
    }


# ── main TUI ──────────────────────────────────────────────────────────────────

class TUI:
    def __init__(self, stdscr, harness: Harness, controller: Controller):
        self.stdscr = stdscr
        self.harness = harness
        self.controller = controller                # shared with the web UI
        self._events = harness.event_queue.subscribe()  # replays the backlog first
        # Streaming preview: lines from _stream_start on are a live render of the
        # reply being generated; they are replaced when the final events arrive.
        self._stream_start: int | None = None
        self._stream_text = {"thinking": "", "content": ""}
        self._chat_buf    = _LineBuffer()
        self._chat_events: list[tuple] = []  # raw events for toggle rebuild
        self._tools_expanded: bool = True    # True = full tool output; False = abbreviated
        self._think_expanded: bool = True    # toggle with /toggle-think-output or Shift+T
        self._md_expanded: bool = True       # markdown rendering; toggle with /toggle-markdown or Shift+M
        self._diff_expanded: bool = True     # show edit diffs; toggle with /diff or Shift+D
        self._diff_style: str = "compact"    # "compact" | "git"; set with /diff-style
        self._input: str = ""
        self._cursor: int = 0           # insertion point within _input
        self._history = harness.input_history  # submitted entries, oldest first; shared with harness for persistence
        self._history_idx: int = -1     # -1 = not browsing
        self._history_stash: str = ""   # saves live input while browsing
        self._focus: str = "input"      # "input" | "chat"
        # Status bar components — assembled (with DIR shortening) in _draw_status.
        self._st_mode  = harness.mode
        self._st_model = harness.client.model
        self._st_host  = harness.client.host
        self._st_provider = harness.provider
        self._st_ctx   = 0
        self._st_dir   = str(harness.workdir)
        self._st_extra = ""             # trailing " | TOOLS: off" / " | RUN: confirm"
        self._ctx_color = _C_STATUS
        self._too_small = False   # set when the terminal is too small to host the layout
        self._spinner_frame = 0
        self._spinner_ts    = 0.0
        self._companion_visible:       bool       = True
        self._companion_x:             int        = 4
        self._companion_dir:           int        = 1
        self._companion_state:         str        = "walk"
        self._companion_sit_ticks:     int        = 0
        self._companion_walk_step:     int        = 0
        self._companion_current_frame: list[str]  = _MOMO_SIT[0]
        self._companion_blink_ticks:   int        = 0
        self._companion_mew_ticks:     int        = 0
        self._companion_mew_text:      str        = ""
        self._companion_ts:            float      = 0.0
        # Cache for _draw_companion: skip the full erase+redraw when nothing
        # visible has changed since the last paint.
        self._companion_drawn_x:        int        = -1
        self._companion_drawn_frame:    int        = -1   # id() of last frame list
        self._companion_drawn_mew:      bool       = False
        self._companion_drawn_mew_text: str        = ""

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
        self._layout = _compute_layout(rows, cols, companion_h=_COMPANION_H)
        self._build_windows()

    # Busy / waiting state is owned by the Controller so the TUI and the web UI
    # agree on it (a turn started from the browser shows as busy here too).
    @property
    def _busy(self) -> bool:
        return self.controller.busy

    @property
    def _waiting_for_input(self) -> bool:
        return self.controller.waiting

    def _build_windows(self):
        # Called on startup and on every resize / companion toggle.  Previous
        # window objects are simply abandoned — curses cleans up the C-level
        # WINDOW structs when the Python objects are GC'd.  There is no
        # explicit delwin() call here, which is safe as long as no background
        # thread holds a reference to the old windows.
        # Invalidate the companion draw cache so the first _draw_companion call
        # after a rebuild always paints the new window from scratch.
        self._companion_drawn_x        = -1
        self._companion_drawn_frame    = -1
        self._companion_drawn_mew      = False
        self._companion_drawn_mew_text = ""
        L = self._layout
        cols = L["cols"]
        # Guard against a terminal too small to fit the fixed regions: newwin
        # raises if a window would extend past the screen. Detect it up front,
        # flag it, and let _redraw paint a "too small" placeholder instead of
        # building (and then drawing into) windows that don't fit.
        rows, _ = self.stdscr.getmaxyx()
        if cols < 20 or L["chat_h"] < 1 or L["input_y"] + L["input_h"] > rows:
            self._too_small = True
            self._chat_win = self._status_win = self._input_win = None
            self._companion_win = None
            return
        self._too_small = False
        self._chat_win   = curses.newwin(L["chat_h"],  cols, L["chat_y"],   0)
        self._status_win = curses.newwin(_STATUS_H,    cols, L["status_y"], 0)
        self._input_win  = curses.newwin(L["input_h"], cols, L["input_y"],  0)
        # leaveok(True) tells curses it does NOT need to park the hardware
        # cursor in this window after refreshing it.  Without this,
        # curses.doupdate() would move the terminal cursor to wherever the last
        # addstr landed in the chat or status window, fighting the explicit
        # win.move() call in _draw_input.  Only _input_win is left with
        # leaveok=False (the default) so doupdate always parks the cursor there.
        self._chat_win.leaveok(True)
        self._status_win.leaveok(True)
        if L["companion_h"] > 0:
            self._companion_win = curses.newwin(L["companion_h"], cols, L["companion_y"], 0)
            self._companion_win.leaveok(True)
        else:
            self._companion_win = None

    def _rebuild(self):
        rows, cols = self.stdscr.getmaxyx()
        self._layout = _compute_layout(
            rows, cols,
            companion_h=_COMPANION_H if self._companion_visible else 0,
        )
        self._build_windows()
        self.stdscr.clear()
        self.stdscr.noutrefresh()
        self._redraw()

    def _redraw(self):
        # Each draw method calls win.noutrefresh() to mark its backing buffer as
        # dirty without touching the physical screen.  The single curses.doupdate()
        # at the end flushes all dirty buffers to the terminal in one pass.
        # This noutrefresh/doupdate split is why partial redraws (e.g. spinner,
        # animation) can update individual windows without flickering the rest.
        # _draw_input() MUST be called last so its noutrefresh() is the final
        # one recorded — doupdate() parks the cursor at the last leaveok=False
        # window's noutrefresh position, which must be the input window.
        if self._too_small:
            self._draw_too_small()
            return
        L = self._layout
        chat_edge = _C_FOCUS if self._focus == "chat" else _C_BORDER
        self._chat_buf.render(self._chat_win, L["chat_h"], L["cols"], edge_color=chat_edge)
        self._draw_companion()
        self._draw_status()
        self._draw_input()
        curses.doupdate()

    def _draw_too_small(self):
        """Render a placeholder when the terminal can't fit the layout, instead
        of crashing in newwin/addnstr. Recovers automatically on the next resize."""
        self.stdscr.erase()
        rows, cols = self.stdscr.getmaxyx()
        msg = "Terminal too small — please enlarge"
        try:
            self.stdscr.addnstr(max(0, rows // 2), max(0, (cols - len(msg)) // 2),
                                msg, max(1, cols - 1))
        except curses.error:
            pass
        self.stdscr.noutrefresh()
        curses.doupdate()

    def _draw_status(self, spinner_only: bool = False):
        win = self._status_win
        cols = self._layout["cols"]
        top_color    = _C_FOCUS if self._focus == "chat"  else _C_BORDER
        bottom_color = _C_FOCUS if self._focus == "input" else _C_BORDER
        if self._busy and self._waiting_for_input:
            prefix = " ? waiting for input  "
            line_attr = curses.color_pair(_C_WARN)
        elif self._busy:
            spinner = _SPINNER[self._spinner_frame % len(_SPINNER)]
            prefix = f" {spinner} thinking  "
            line_attr = curses.color_pair(_C_BUSY)
        else:
            prefix = " "
            line_attr = curses.color_pair(self._ctx_color)
        # Compose the status text into the space left after the prefix, shortening
        # the working directory from the front so its tail (the important part)
        # stays visible instead of being clipped off the right edge.
        avail = max(0, cols - 1 - len(prefix))
        line = prefix + self._compose_status(avail)
        line = line[:cols - 1].ljust(cols - 1)

        # When called from a spinner tick, only the middle text row (row 1)
        # needs to be repainted — the top and bottom rule rows are static
        # between events, so erasing and redrawing them on every 10 Hz tick
        # is pure waste.
        if not spinner_only:
            win.erase()
            rule = "─" * (cols - 1)
            # All three addnstr calls are wrapped because curses raises an error
            # when writing to the last cell of a window (bottom-right corner
            # causes an automatic scroll attempt).  Silently swallowing here is
            # intentional — the status bar degrades gracefully on very narrow
            # terminals.
            # HAZARD: a programming error (e.g. wrong row index) would also be
            # swallowed invisibly.  If the status bar ever disappears
            # unexpectedly, temporarily replace `pass` with a raise to expose
            # the real error.
            try:
                win.addnstr(0, 0, rule, cols - 1, curses.color_pair(top_color))
            except curses.error:
                pass
            try:
                win.addnstr(2, 0, rule, cols - 1, curses.color_pair(bottom_color))
            except curses.error:
                pass
        try:
            win.addnstr(1, 0, line, cols - 1, line_attr)
        except curses.error:
            pass
        win.noutrefresh()

    def _compose_status(self, avail: int) -> str:
        """Build the status text, shortening DIR from the front to fit `avail` cols."""
        head = (f"MODE: {self._st_mode} | VIA: {self._st_provider} | MODEL: {self._st_model} | "
                f"HOST: {self._st_host} | CTX: {self._st_ctx}% | DIR: ")
        budget = avail - len(head) - len(self._st_extra)
        return head + _shorten_path_left(self._st_dir, budget) + self._st_extra

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
        if self._too_small:
            self._draw_too_small()
            return
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
        if self._md_expanded and role == "assistant":
            self._chat_buf.append(label, color)
            for (line_text, line_color, line_attrs) in md_render.process(text, cols):
                self._chat_buf.append(line_text, line_color, line_attrs)
            self._chat_buf.append("", 0)
            return
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

    def _add_diff(self, ev: DiffEvent):
        self._chat_events.append(("diff", ev))
        self._render_diff(ev)

    _DIFF_KIND_COLOR = {
        "add":  _C_DIFF_ADD,
        "del":  _C_DIFF_DEL,
        "hunk": _C_DIFF_HUNK,
        "ctx":  _C_TOOL_RES,
    }
    _DIFF_MAX_LINES = 40  # cap body lines shown per diff, like tool results

    def _render_diff(self, ev: DiffEvent):
        if not self._diff_expanded:
            return

        # Header line(s) — presentation differs by style; the body is identical.
        if ev.op == "move":
            self._chat_buf.append(f"± renamed {ev.path} → {ev.dst}", _C_DIFF_META,
                                  curses.A_BOLD)
            self._chat_buf.append("", 0)
            return

        if self._diff_style == "git":
            self._chat_buf.append(f"diff --git a/{ev.path} b/{ev.path}", _C_DIFF_META,
                                  curses.A_BOLD)
            self._chat_buf.append(f"--- {'/dev/null' if ev.is_new else 'a/' + ev.path}",
                                  _C_DIFF_DEL)
            self._chat_buf.append(f"+++ {'/dev/null' if ev.op == 'delete' else 'b/' + ev.path}",
                                  _C_DIFF_ADD)
        else:  # compact
            if ev.is_new:
                note = f"(new file +{ev.added})"
            elif ev.op == "delete":
                note = f"(deleted -{ev.removed})"
            else:
                note = f"(+{ev.added} -{ev.removed})"
            self._chat_buf.append(f"± {ev.path}  {note}", _C_DIFF_META, curses.A_BOLD)

        body = ev.body[:self._DIFF_MAX_LINES]
        # Two-column line-number gutter (old | new), width-matched to the largest
        # number in this diff. Removed lines show only old_no, added lines only
        # new_no, context both, hunk headers neither.
        num_w = max((len(str(n)) for _, o, nw, _ in body
                     for n in (o, nw) if n is not None), default=1)
        def _fmt(n: int | None) -> str:
            return str(n).rjust(num_w) if n is not None else " " * num_w
        for kind, old_no, new_no, text in body:
            attr = curses.A_BOLD if kind == "hunk" else 0
            gutter = f"{_fmt(old_no)} {_fmt(new_no)} │ "
            self._chat_buf.append(gutter + text, self._DIFF_KIND_COLOR.get(kind, _C_TOOL_RES), attr)
        if len(ev.body) > self._DIFF_MAX_LINES:
            self._chat_buf.append(f"  ... ({len(ev.body) - self._DIFF_MAX_LINES} more diff lines)",
                                  _C_TOOL_RES)
        self._chat_buf.append("", 0)

    def _rebuild_chat_buf(self):
        # Re-render all events from scratch. Called when display options change
        # (e.g. tool expand/collapse toggle) so the layout is consistent.
        self._chat_buf = _LineBuffer()
        streaming = self._stream_start is not None
        for ev in self._chat_events:
            if ev[0] == "chat":
                self._render_chat(ev[1], ev[2])
            elif ev[0] == "tool_call":
                self._render_tool_call(ev[1], ev[2])
            elif ev[0] == "tool_result":
                self._render_tool_result(ev[1], ev[2])
            elif ev[0] == "think":
                self._render_think(ev[1])
            elif ev[0] == "diff":
                self._render_diff(ev[1])
        if streaming:  # keep the live preview below the rebuilt transcript
            self._stream_start = len(self._chat_buf._lines)
            self._render_stream_preview()

    # ── event processing ──────────────────────────────────────────────────────

    def _drain_events(self):
        # Drain the entire queue before redrawing — one redraw per poll cycle
        # is sufficient and avoids screen flicker from partial updates.
        # Thread safety: the subscription is a queue.Queue; get_nowait() is
        # thread-safe.  All other state (_chat_buf, etc.) is only mutated
        # here and in the key-handler path, both of which run on the main thread,
        # so no additional locking is required.
        changed = False
        stream_dirty = False
        # Buffer index just before the last ChatEvent rendered this cycle.
        # Used below to scroll the start of the new message into view.
        last_chat_start: int | None = None
        try:
            while True:
                ev = self._events.get_nowait()
                if isinstance(ev, ChatEvent):
                    last_chat_start = len(self._chat_buf._lines)
                    self._add_chat(ev.role, ev.text)
                    changed = True
                elif isinstance(ev, UserEvent):
                    last_chat_start = len(self._chat_buf._lines)
                    self._add_chat("user", ev.text)
                    changed = True
                elif isinstance(ev, ResetEvent):
                    self._chat_events = []
                    self._chat_buf = _LineBuffer()
                    self._end_stream_preview()
                    last_chat_start = None
                    changed = True
                elif isinstance(ev, DeltaEvent):
                    if self._stream_start is None:
                        self._stream_start = len(self._chat_buf._lines)
                    self._stream_text[ev.kind] = self._stream_text.get(ev.kind, "") + ev.text
                    stream_dirty = True
                    changed = True
                elif isinstance(ev, StreamEndEvent):
                    self._end_stream_preview()
                    stream_dirty = False
                    changed = True
                elif isinstance(ev, BusyEvent):
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
                    tools_str = "" if ev.tools_enabled else " | TOOLS: off"
                    run_str = " | RUN: confirm" if ev.run_confirm else ""
                    self._st_mode  = f"{ev.mode} [{ev.plan_progress}]" if ev.plan_progress else ev.mode
                    self._st_model = ev.model
                    self._st_host  = ev.host
                    self._st_provider = ev.provider or self._st_provider
                    self._st_ctx   = ev.ctx_pct
                    self._st_dir   = ev.workdir
                    self._st_extra = f"{tools_str}{run_str}"
                    changed = True
                elif isinstance(ev, ThinkEvent):
                    self._add_think(ev.text)
                    changed = True
                elif isinstance(ev, DiffEvent):
                    self._add_diff(ev)
                    changed = True
                elif isinstance(ev, AskUserEvent):
                    self._add_chat("assistant", ev.question)
                    changed = True
                elif isinstance(ev, DoneEvent):
                    self._spinner_frame = 0
                    changed = True
                elif isinstance(ev, ErrorEvent):
                    last_chat_start = len(self._chat_buf._lines)
                    self._add_chat("system", f"ERROR: {ev.text}")
                    changed = True
        except queue.Empty:
            pass

        if stream_dirty:
            self._render_stream_preview()

        # Place the start of the new message at the top of the viewport so the
        # beginning is always visible rather than the end.
        if last_chat_start is not None:
            self._chat_buf.scroll_to_top_of(last_chat_start, self._layout["chat_h"])

        return changed

    def _render_stream_preview(self):
        """Redraw the in-progress reply below the final transcript lines."""
        del self._chat_buf._lines[self._stream_start:]
        if self._stream_text.get("thinking"):
            self._render_think(self._stream_text["thinking"])
        if self._stream_text.get("content"):
            self._render_chat("assistant", self._stream_text["content"] + " ▍")
        self._chat_buf.scroll_to_bottom()

    def _end_stream_preview(self):
        if self._stream_start is not None:
            del self._chat_buf._lines[self._stream_start:]
            self._chat_buf.scroll_to_bottom()
        self._stream_start = None
        self._stream_text = {"thinking": "", "content": ""}

    # ── input handling ────────────────────────────────────────────────────────

    def _toggle_tools(self):
        self._tools_expanded = not self._tools_expanded
        self._rebuild_chat_buf()
        self._redraw()

    def _toggle_think(self):
        self._think_expanded = not self._think_expanded
        self._rebuild_chat_buf()
        self._redraw()

    def _toggle_md(self):
        self._md_expanded = not self._md_expanded
        self._rebuild_chat_buf()
        self._redraw()

    def _toggle_diff(self):
        self._diff_expanded = not self._diff_expanded
        self._rebuild_chat_buf()
        self._redraw()

    def _set_diff_style(self, style: str):
        self._diff_style = style
        self._rebuild_chat_buf()
        self._redraw()

    def _toggle_run_confirm(self):
        self.controller.toggle_run_confirm()
        self._drain_events()  # consume the StatusEvent so the bar updates now
        self._redraw()

    def _advance_companion(self):
        cols  = self._layout["cols"]
        max_x = max(0, cols - 2 - _CAT_W)

        if self._companion_state == "walk":
            self._companion_walk_step ^= 1
            self._companion_x = max(0, min(self._companion_x + self._companion_dir, max_x))

            if self._companion_x == 0 or self._companion_x == max_x or random.random() < 0.02:
                self._companion_state       = "sit"
                self._companion_sit_ticks   = random.randint(50, 100)
                self._companion_blink_ticks = 0
                if random.random() < 0.75:
                    key  = (self.harness.mode, self._busy and not self._waiting_for_input)
                    pool = _SPEECH_TEXTS.get(key, _SPEECH_TEXTS_DEFAULT)
                    self._companion_mew_text  = random.choice(pool)
                    self._companion_mew_ticks = random.randint(18, 32)

            if self._companion_blink_ticks > 0:
                self._companion_blink_ticks -= 1
                self._companion_current_frame = (
                    _MOMO_WR_BLINK if self._companion_dir > 0 else _MOMO_WL_BLINK
                )
            else:
                frames = _MOMO_WR if self._companion_dir > 0 else _MOMO_WL
                self._companion_current_frame = frames[self._companion_walk_step]
                if random.random() < 0.03:
                    self._companion_blink_ticks = 2

        else:  # sit
            self._companion_sit_ticks -= 1
            if self._companion_sit_ticks <= 0:
                # Choose a new walk direction before leaving sit so the first
                # walk tick moves in the right direction.
                if self._companion_x <= 2:
                    self._companion_dir = 1
                elif self._companion_x >= max_x - 2:
                    self._companion_dir = -1
                else:
                    self._companion_dir = random.choice([-1, 1])
                self._companion_state = "walk"
                # Clear mew on sit→walk so the speech bubble doesn't persist
                # into the walking animation (mew is only drawn in _draw_companion
                # when mew_ticks > 0, so the effect is purely visual).
                # NOTE: mew_ticks is NOT cleared on walk→sit (there is no
                # equivalent reset there), but mew is never triggered during
                # walking so a counter started during sit would simply expire.
                self._companion_mew_ticks = 0

            # Sit blink is probabilistic each tick, independent of blink_ticks.
            # This is intentionally simpler than the walk blink (which uses a
            # multi-tick counter to hold the blink frame open).
            sit_frames = _MOMO_SIT_L if self._companion_dir < 0 else _MOMO_SIT
            if random.random() < 0.08:
                self._companion_current_frame = sit_frames[1]   # blink
            else:
                self._companion_current_frame = sit_frames[0]   # normal

            if self._companion_mew_ticks > 0:
                self._companion_mew_ticks -= 1

    def _draw_companion(self):
        win = self._companion_win
        if win is None:
            return

        # Skip a full erase+redraw when the visible state is identical to the
        # last paint — position, frame, and mew visibility are all unchanged.
        mew_visible = self._companion_mew_ticks > 0
        frame_id    = id(self._companion_current_frame)
        if (
            self._companion_x        == self._companion_drawn_x
            and frame_id             == self._companion_drawn_frame
            and mew_visible          == self._companion_drawn_mew
            and self._companion_mew_text == self._companion_drawn_mew_text
        ):
            return

        cols  = self._layout["cols"]
        attr  = curses.color_pair(_C_COMPANION)
        win.erase()
        x = 1 + self._companion_x
        for i, line in enumerate(self._companion_current_frame):
            try:
                win.addnstr(i, x, line, cols - x - 1, attr)
            except curses.error:
                pass
        if mew_visible:
            mew_txt = self._companion_mew_text
            if self._companion_dir < 0:
                display = (mew_txt[2:] + " >") if mew_txt.startswith("< ") else mew_txt
                mew_x = x - len(display) - 1
                if mew_x >= 0:
                    try:
                        win.addnstr(1, mew_x, display, len(display), attr)
                    except curses.error:
                        pass
            else:
                mew_x = x + _CAT_W + 1
                if mew_x + len(mew_txt) < cols - 1:
                    try:
                        win.addnstr(1, mew_x, mew_txt, cols - mew_x - 1, attr)
                    except curses.error:
                        pass

        # Update cache so subsequent identical ticks are skipped.
        self._companion_drawn_x        = self._companion_x
        self._companion_drawn_frame    = frame_id
        self._companion_drawn_mew      = mew_visible
        self._companion_drawn_mew_text = self._companion_mew_text
        win.noutrefresh()

    def _toggle_companion(self):
        self._companion_visible = not self._companion_visible
        rows, cols = self.stdscr.getmaxyx()
        self._layout = _compute_layout(
            rows, cols,
            companion_h=_COMPANION_H if self._companion_visible else 0,
        )
        self._build_windows()
        self.stdscr.clear()
        self.stdscr.noutrefresh()
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

    def _submit(self):
        text = self._input
        self._input = ""
        self._cursor = 0
        self._history_idx = -1
        self._history_stash = ""
        outcome = self.controller.submit(text, source="tui")
        if outcome.exit_app:
            raise SystemExit(0)
        v = outcome.view
        if "tool_output" in v:
            self._tools_expanded = v["tool_output"]
        if "think_output" in v:
            self._think_expanded = v["think_output"]
        if "md_render" in v:
            self._md_expanded = v["md_render"]
        if "diff_output" in v:
            self._diff_expanded = v["diff_output"]
        if "diff_style" in v:
            self._diff_style = v["diff_style"]
        if "companion" in v and v["companion"] != self._companion_visible:
            self._toggle_companion()  # rebuilds and redraws
        elif v:
            self._rebuild_chat_buf()
        self._drain_events()  # show the echoed input / command output right away
        self._redraw()

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        # Clear stdscr before the first draw so leftover terminal content
        # doesn't bleed through behind the sub-windows.  _rebuild() already
        # does this on every resize; the startup path must do the same.
        self.stdscr.clear()
        self.stdscr.noutrefresh()
        # emit initial status; a pre-loaded session was already pushed onto the
        # bus by the Controller and arrives here through the backlog replay.
        self.harness._emit_status()
        self._drain_events()
        self._chat_buf.scroll_to_bottom()
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

            # `changed` means the harness queue produced at least one event this
            # cycle.  It is checked *after* key dispatch so that event-driven
            # and key-driven changes are both coalesced into a single redraw.
            # Keys that perform their own redraw (via _redraw or _redraw_input_only)
            # before reaching the bottom of the loop should `continue` to skip the
            # redundant redraw triggered by `changed`.
            changed = self._drain_events()

            if ch == curses.ERR:
                if changed:
                    self._redraw()
                elif self._too_small:
                    # No windows to animate; the placeholder is static until resize.
                    pass
                else:
                    _any = False
                    if self._busy and not self._waiting_for_input:
                        now = time.time()
                        if now - self._spinner_ts >= _SPINNER_INTERVAL:
                            self._spinner_frame += 1
                            self._spinner_ts = now
                            self._draw_status(spinner_only=True)
                            _any = True
                    if self._companion_visible:
                        now = time.time()
                        if now - self._companion_ts >= _COMPANION_INTERVAL:
                            self._companion_ts = now
                            self._advance_companion()
                            self._draw_companion()
                            _any = True
                    if _any:
                        # _input_win.noutrefresh() MUST be the last noutrefresh
                        # call before doupdate.  curses.doupdate() parks the
                        # hardware cursor at the position recorded by the most
                        # recent noutrefresh on a window with leaveok=False.
                        # _input_win is the only such window (chat/status/companion
                        # all have leaveok=True), so refreshing it last guarantees
                        # the cursor stays in the input box even during animation
                        # ticks that only repaint the companion or status.
                        self._input_win.noutrefresh()
                        curses.doupdate()
                time.sleep(0.02)  # idle — avoids CPU spin without adding key lag
                continue

            # Shift+Tab cycles through all four modes
            if ch == curses.KEY_BTAB:
                idx = _MODE_CYCLE.index(self.harness.mode) if self.harness.mode in _MODE_CYCLE else 0
                new_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
                self.controller.set_mode(new_mode)
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

            # cursor movement in input / horizontal scroll in chat
            if ch == curses.KEY_LEFT:
                if self._focus == "chat":
                    self._chat_buf.scroll_left()
                    self._redraw()
                elif self._cursor > 0:
                    self._cursor -= 1
                    self._redraw_input_only()
                continue
            if ch == curses.KEY_RIGHT:
                if self._focus == "chat":
                    self._chat_buf.scroll_right(display_w=self._layout["cols"] - 2)
                    self._redraw()
                elif self._cursor < len(self._input):
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

            # Shift+T/M/Q/C are gated on chat focus so that typing an uppercase
            # letter in the input field is never swallowed as a command.  The
            # trade-off is that these shortcuts are unavailable while composing
            # text — the user must Tab to chat focus first.
            # NOTE: because curses.getch() in nodelay mode returns the raw ASCII
            # value, ord('T') == 84 which is indistinguishable from a shifted 't'
            # regardless of which modifier the terminal reports.  The _focus guard
            # is the only disambiguation.

            # Shift+T toggles thinking output (only when chat pane has focus,
            # so typing 'T' in the input field still works normally)
            if ch == ord('T') and self._focus == "chat":
                self._toggle_think()
                continue

            # Shift+M toggles markdown rendering (chat focus only)
            if ch == ord('M') and self._focus == "chat":
                self._toggle_md()
                continue

            # Shift+D toggles edit-diff rendering (chat focus only)
            if ch == ord('D') and self._focus == "chat":
                self._toggle_diff()
                continue

            # Shift+Q toggles the momo companion bar (chat focus only)
            if ch == ord('Q') and self._focus == "chat":
                self._toggle_companion()
                continue

            # Shift+C interrupts the running LLM (chat focus only)
            if ch == ord('C') and self._focus == "chat":
                self.controller.cancel()
                continue

            # Shift+P toggles run_command confirmation (chat focus only)
            if ch == ord('P') and self._focus == "chat":
                self._toggle_run_confirm()
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


def run_tui(stdscr, harness: Harness, controller: Controller):
    tui = TUI(stdscr, harness, controller)
    try:
        tui.run()
    finally:
        tui._events.close()
