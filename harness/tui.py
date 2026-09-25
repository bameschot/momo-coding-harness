from __future__ import annotations

import curses
import json
import os
import queue
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time
import textwrap
from dataclasses import dataclass, field

from . import md_render
from .commands import help_commands
from .file_search import fuzzy_search, workspace_files_nowait
from .controller import Controller
from .events import (AskUserEvent, ChatEvent, CompanionEvent, DeltaEvent, DiffEvent,
                     DoneEvent, ErrorEvent, ResetEvent, StatusEvent, StreamEndEvent, ThinkEvent,
                     ToolCallEvent, ToolResultEvent, UserEvent)
from .harness import Harness
from .prompts import MODES


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

# Key codes of our own, above curses' KEY_MAX range: define_key() binds escape
# sequences to them, and _next_key() returns them for sequences it decodes itself.
_KEY_SHIFT_ENTER  = 601  # Shift+Enter / Option+Enter — insert a newline
_KEY_CTRL_LEFT    = 602  # Ctrl+Left  — word jump left
_KEY_CTRL_RIGHT   = 603  # Ctrl+Right — word jump right
_KEY_PASTE_START  = 604  # bracketed paste: ESC [ 200 ~
_KEY_PASTE_END    = 605  # bracketed paste: ESC [ 201 ~
_KEY_ESC          = 606  # a lone Esc (not the start of a sequence)
_KEY_IGNORED      = 607  # an escape sequence we don't bind, swallowed whole

# Suggestions, as in the web UI: "@query" right before the caret opens workspace
# path suggestions; a leading "/" opens slash-command suggestions.
_AT_RX = re.compile(r"(?:^|\s)@([\w./~+-]*)$")
_SUGGEST_MAX_ROWS = 8
_ARG_MAX = 60           # tool-call argument values are cut to this, as in the web UI
_PASTE_IDLE_S = 1.0     # a bracketed paste whose end marker never comes is closed after this


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

_COMPANION_H        = 4    # 4 art rows
_COMPANION_INTERVAL = 0.12  # seconds per animation tick

from .companion import (  # noqa: E402  (shared with the web UI)
    _MOMO_WR, _MOMO_WL, _MOMO_SIT, _MOMO_SIT_L, _MOMO_WR_BLINK, _MOMO_WL_BLINK,
    _SPEECH_TEXTS, _SPEECH_TEXTS_DEFAULT, RecapPicker,
    CAT_W, bubble_dir, bubble_room, walk_max_x,
)


@dataclass
class _Companion:
    """The walking cat in the companion bar: its animation state, and a cache of
    what was last painted so an unchanged tick skips the redraw."""
    x: int = 4
    dir: int = 1
    state: str = "walk"          # "walk" | "sit"
    sit_ticks: int = 0
    step: int = 0                # walk leg frame, 0/1
    frame: list = field(default_factory=lambda: _MOMO_SIT[0])
    blink_ticks: int = 0
    mew_ticks: int = 0
    mew_text: str = ""
    ts: float = 0.0              # last animation tick
    # Idle recap: while the user is idle momo speaks model-written recap lines
    # (newest first, then random recent ones) instead of the canned pool.
    idle: bool = False
    recaps: RecapPicker = field(default_factory=RecapPicker)
    drawn: tuple | None = None   # (x, frame id, mew shown, mew text) last painted

    def advance(self, cols: int, speech_key: tuple[str, bool]):
        max_x = walk_max_x(cols)   # keeps room for a full speech bubble on the right
        if self.state == "walk":
            self.step ^= 1
            self.x = max(0, min(self.x + self.dir, max_x))
            if self.x == 0 or self.x == max_x or random.random() < 0.02:
                self._sit_down(cols, speech_key)
            if self.blink_ticks > 0:
                self.blink_ticks -= 1
                self.frame = _MOMO_WR_BLINK if self.dir > 0 else _MOMO_WL_BLINK
            else:
                self.frame = (_MOMO_WR if self.dir > 0 else _MOMO_WL)[self.step]
                if random.random() < 0.03:
                    self.blink_ticks = 2
            return
        self.sit_ticks -= 1
        if self.sit_ticks <= 0:
            # Choose the walk direction before leaving sit so the first step goes that way.
            self.dir = 1 if self.x <= 2 else -1 if self.x >= max_x - 2 else random.choice([-1, 1])
            self.state = "walk"
            self.mew_ticks = 0     # the bubble doesn't walk along
        sit_frames = _MOMO_SIT_L if self.dir < 0 else _MOMO_SIT
        self.frame = sit_frames[1 if random.random() < 0.08 else 0]   # [1] = blink
        if self.mew_ticks > 0:
            self.mew_ticks -= 1

    def _sit_down(self, cols: int, speech_key: tuple[str, bool]):
        self.state = "sit"
        self.sit_ticks = random.randint(50, 100)
        self.blink_ticks = 0
        # Only lines that fit beside momo here; it turns to face its bubble.
        room = bubble_room(self.x, cols)
        recap = self.recaps.pick(time.monotonic(), room) if self.idle else None
        text = recap
        if not text and random.random() < 0.75:
            pool = [t for t in _SPEECH_TEXTS.get(speech_key, _SPEECH_TEXTS_DEFAULT) if len(t) <= room]
            text = random.choice(pool) if pool else None
        if text:
            self.mew_text = text
            # Longer lines stay up longer; recaps a little longer still.
            self.mew_ticks = random.randint(18, 32) + len(text) // 2 + (10 if recap else 0)
            self.sit_ticks = max(self.sit_ticks, self.mew_ticks + 10)
            self.dir = bubble_dir(self.x, cols, self.dir, len(text)) or self.dir

    def draw(self, win, cols: int):
        mew_visible = self.mew_ticks > 0
        key = (self.x, id(self.frame), mew_visible, self.mew_text)
        if key == self.drawn:
            return                 # nothing visible changed since the last paint
        attr = curses.color_pair(_C_COMPANION)
        win.erase()
        x = 1 + self.x
        for i, line in enumerate(self.frame):
            _put(win, i, x, line, cols - x - 1, attr)
        if mew_visible:
            if self.dir < 0:
                t = self.mew_text
                display = (t[2:] + " >") if t.startswith("< ") else t
                mew_x = x - len(display) - 1
                if mew_x >= 0:
                    _put(win, 1, mew_x, display, len(display), attr)
            else:
                mew_x = x + CAT_W + 1
                if mew_x + len(self.mew_text) < cols - 1:
                    _put(win, 1, mew_x, self.mew_text, cols - mew_x - 1, attr)
        self.drawn = key
        win.noutrefresh()


# ── drawing helpers ───────────────────────────────────────────────────────────

def _put(win, y: int, x: int, text: str, n: int, attr: int = 0):
    """addnstr that ignores curses' error for the last cell of a window."""
    try:
        win.addnstr(y, x, text, n, attr)
    except curses.error:
        pass


def _draw_vbar(win, x: int, h: int, total: int, first: int, color: int):
    """A vertical scrollbar in column x over h rows: a thumb when `total` rows
    don't fit, `first` being the first one shown; a plain rule otherwise."""
    thumb_top, thumb_h = 0, 0
    if total > h:
        thumb_h = max(1, round(h * h / total))
        thumb_top = round(first / max(1, total - h) * (h - thumb_h))
    for r in range(h):
        try:
            win.addch(r, x, "█" if thumb_top <= r < thumb_top + thumb_h else "│",
                      curses.color_pair(color))
        except curses.error:
            pass


def _brief_args(args) -> str:
    """Tool-call arguments for the transcript, each value cut to _ARG_MAX chars
    (the web UI's argsBrief): a write_file body is in the diff, not here."""
    def cut(v) -> str:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return s if len(s) <= _ARG_MAX else s[:_ARG_MAX - 3] + "…"
    if not isinstance(args, dict):
        return json.dumps(cut(args), ensure_ascii=False)
    return ", ".join(f"{k}={json.dumps(cut(v), ensure_ascii=False)}" for k, v in args.items())


# ── scrollable line buffer ────────────────────────────────────────────────────

class _LineBuffer:
    """Holds wrapped display lines and scroll offsets (vertical + horizontal).
    Appending never moves the view: the TUI decides whether to follow new output."""

    def __init__(self):
        self._lines: list[tuple[str, int, int]] = []  # (text, color_pair, attrs)
        self._scroll  = 0   # vertical: 0-based index of the last visible line
        self._hscroll = 0   # horizontal: columns scrolled from the left
        self._max_line_w: int = 0  # running maximum of len(text) across all lines

    def __len__(self) -> int:
        return len(self._lines)

    def append(self, text: str, color: int, attrs: int = 0):
        self._lines.append((text, color, attrs))
        if len(text) > self._max_line_w:
            self._max_line_w = len(text)

    def truncate(self, n: int):
        del self._lines[n:]

    def at_bottom(self) -> bool:
        return self._scroll >= len(self._lines) - 1

    def scroll_up(self, n: int = 3):
        self._scroll = max(0, self._scroll - n)

    def scroll_down(self, n: int = 3):
        self._scroll = min(max(0, len(self._lines) - 1), self._scroll + n)

    def scroll_to_bottom(self):
        self._scroll = max(0, len(self._lines) - 1)

    def scroll_to(self, line_idx: int):
        self._scroll = max(0, min(line_idx, len(self._lines) - 1))

    def scroll_to_top_of(self, line_idx: int, height: int):
        """Place line_idx at the top of a viewport of `height` rows."""
        self._scroll = min(line_idx + height - 1, max(0, len(self._lines) - 1))

    def scroll_left(self, n: int = 8):
        self._hscroll = max(0, self._hscroll - n)

    def scroll_right(self, n: int = 8, display_w: int = 0):
        # Never past the widest line: further would show only blank space.
        max_hscroll = max(0, self._max_line_w - display_w) if display_w > 0 else max(0, self._max_line_w - 1)
        self._hscroll = min(max_hscroll, self._hscroll + n)

    def render(self, win, height: int, width: int, edge_color: int = _C_BORDER):
        win.erase()
        total = len(self._lines)
        display_w = width - 2   # rightmost column reserved for vertical scrollbar
        content_h = height - 1  # bottom row always reserved for horizontal scrollbar
        max_w = self._max_line_w
        first = 0 if total <= content_h else max(0, self._scroll + 1 - content_h)
        for row, (text, color, attrs) in enumerate(self._lines[first:first + content_h]):
            _put(win, row, 0, text[self._hscroll:self._hscroll + display_w], display_w,
                 curses.color_pair(color) | attrs)
        _draw_vbar(win, width - 1, content_h, total, first, edge_color)

        # horizontal scrollbar — always drawn; thumb only when content overflows
        bar_w = max(2, width - 4)
        if max_w > display_w:
            ratio_h   = self._hscroll / max(1, max_w - display_w)
            thumb_w   = max(1, round(display_w / max_w * bar_w))
            thumb_pos = max(0, min(round(ratio_h * (bar_w - thumb_w)), bar_w - thumb_w))
            hbar = "─" * thumb_pos + "█" * thumb_w + "─" * (bar_w - thumb_pos - thumb_w)
        else:
            hbar = "─" * bar_w
        _put(win, height - 1, 0, "◀" + hbar + "▶", width - 1, curses.color_pair(edge_color))
        # noutrefresh, not refresh: the caller batches every window into one doupdate().
        win.noutrefresh()


class _StreamPart:
    """One streamed block (the reasoning or the reply), wrapped as it grows:
    finished source lines are wrapped once, only the unfinished last line again
    on each delta — re-wrapping the whole reply every poll was quadratic."""

    def __init__(self, width: int, first: str, indent: str):
        self.width, self.first, self.indent = max(10, width), first, indent
        self.text = ""
        self._done = 0                  # text[:_done] is wrapped into _lines
        self._lines: list[str] = []

    def add(self, delta: str):
        self.text += delta
        nl = self.text.rfind("\n")
        if nl >= self._done:
            for src in self.text[self._done:nl].split("\n"):
                self._lines.extend(self._wrap(src, bool(self._lines)))
            self._done = nl + 1

    def _wrap(self, src: str, started: bool) -> list[str]:
        out = []
        for wl in textwrap.wrap(src, width=self.width) or [""]:
            out.append((self.indent if (started or out) else self.first) + wl)
        return out

    def lines(self, cursor: str = "") -> list[str]:
        return self._lines + self._wrap(self.text[self._done:] + cursor, bool(self._lines))


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
    # chat_h absorbs all slack.  A terminal too short for the fixed regions is
    # caught by _build_windows, which shows a placeholder instead.
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


# View options, keyed like the CommandResult fields that set them.
_VIEW_DEFAULTS = {"tool_output": True, "think_output": True, "md_render": True,
                  "diff_output": True, "diff_style": "compact", "companion": True}
# Shift+letter shortcuts (chat focus): the view option each one toggles.
_VIEW_KEYS = {"T": "think_output", "M": "md_render", "D": "diff_output", "Q": "companion"}


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
        self._stream: dict[str, _StreamPart] = {}   # "thinking" / "content"
        self._chat_buf    = _LineBuffer()
        self._chat_events: list[tuple] = []  # raw events for toggle rebuild
        self._view = dict(_VIEW_DEFAULTS)
        # Follow new output only while the user hasn't scrolled away from it;
        # otherwise the status line says there is more below.
        self._follow = True
        self._new_below = False
        self._input: str = ""
        self._cursor: int = 0           # insertion point within _input
        self._input_dirty = False       # typed keys not drawn yet (coalesced while keys arrive)
        self._pending_keys: list = []   # keys read ahead and put back
        self._history = harness.input_history  # submitted entries, oldest first; shared with harness for persistence
        self._history_idx: int = -1     # -1 = not browsing
        self._history_stash: str = ""   # saves live input while browsing
        self._focus: str = "input"      # "input" | "chat"
        # @-path / slash-command suggestions, drawn over the bottom of the chat pane.
        # Each item is {"path": p}, {"note": text}, or a help_commands() entry {cmd, usage, desc}.
        self._sugg: list[dict] = []
        self._sugg_idx: int = -1        # -1 = nothing highlighted (Enter still submits)
        self._sugg_query: tuple | None = None  # query the list was computed for
        self._sugg_waiting = False      # the file list is still being walked
        self._commands = help_commands()
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
        self._companion = _Companion()
        workspace_files_nowait(harness.workdir)   # warm the @-search list in the background

        _init_colors()
        curses.curs_set(1)
        # A lone Esc is a key here (interrupt, close suggestions): don't wait the
        # default second for an escape sequence to follow it.
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        # Disable CR→NL translation so Enter (\r, 13) and Ctrl+J (\n, 10) stay
        # distinct.  Without this, ncurses maps \r → \n on input and the two codes
        # collide, making Ctrl+J indistinguishable from Enter.
        curses.nonl()
        # Escape-sequence bindings for terminals that send them:
        # \x1b\r / \x1b\n — Option+Enter (macOS iTerm2 with "+Esc" Option key setting).
        # \x1b[13;2u     — Shift+Enter (kitty/wezterm or iTerm2 with CSI-u mode).
        # \x1b[200~ / \x1b[201~ — bracketed paste start / end (enabled in run()).
        for seqs, code in ((("\x1b\r", "\x1b\n", "\x1b[13;2u"), _KEY_SHIFT_ENTER),
                           (("\x1b[1;5D", "\x1b[5D"), _KEY_CTRL_LEFT),
                           (("\x1b[1;5C", "\x1b[5C"), _KEY_CTRL_RIGHT),
                           (("\x1b[200~",), _KEY_PASTE_START),
                           (("\x1b[201~",), _KEY_PASTE_END)):
            for seq in seqs:
                try:
                    curses.define_key(seq, code)
                except Exception:
                    pass
        self.stdscr.nodelay(True)  # non-blocking reads — keys processed immediately

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

    def _prefix(self) -> str:
        """The input prompt: ? answering momo, ⊘ busy, › ready."""
        return "? " if self._waiting_for_input else "⊘ " if self._busy else "› "

    def _build_windows(self):
        # Called on startup and on every resize / companion toggle.  Old window
        # objects are dropped; curses frees them when they are collected.
        self._companion.drawn = None   # the new companion window starts blank
        L = self._layout
        cols = L["cols"]
        # A terminal too small for the fixed regions makes newwin raise: flag it
        # and let _redraw paint a "too small" placeholder instead.
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
        # leaveok(True): doupdate need not park the hardware cursor in these
        # windows.  Only _input_win keeps leaveok=False, so the cursor always
        # ends up in the input box.
        self._chat_win.leaveok(True)
        self._status_win.leaveok(True)
        if L["companion_h"] > 0:
            self._companion_win = curses.newwin(L["companion_h"], cols, L["companion_y"], 0)
            self._companion_win.leaveok(True)
        else:
            self._companion_win = None

    def _rebuild(self):
        """New terminal size (or companion shown/hidden): new windows, and the
        transcript re-wrapped to the new width."""
        rows, cols = self.stdscr.getmaxyx()
        self._layout = _compute_layout(
            rows, cols, companion_h=_COMPANION_H if self._view["companion"] else 0)
        self._build_windows()
        self._rebuild_chat_buf()
        self.stdscr.clear()
        self.stdscr.noutrefresh()
        self._redraw()

    def _redraw(self):
        # Each draw method calls win.noutrefresh(); the single doupdate() at the
        # end flushes them in one pass.  _draw_input() MUST be last: doupdate()
        # parks the cursor at the last leaveok=False window refreshed.
        self._input_dirty = False
        if self._too_small:
            self._draw_too_small()
            return
        L = self._layout
        chat_edge = _C_FOCUS if self._focus == "chat" else _C_BORDER
        self._chat_buf.render(self._chat_win, L["chat_h"], L["cols"], edge_color=chat_edge)
        self._update_suggest()
        self._draw_suggest()
        if self._companion_win is not None:
            self._companion.draw(self._companion_win, L["cols"])
        self._draw_status()
        self._draw_input()
        curses.doupdate()

    def _draw_too_small(self):
        """Render a placeholder when the terminal can't fit the layout, instead
        of crashing in newwin/addnstr. Recovers automatically on the next resize."""
        self.stdscr.erase()
        rows, cols = self.stdscr.getmaxyx()
        msg = "Terminal too small — please enlarge"
        _put(self.stdscr, max(0, rows // 2), max(0, (cols - len(msg)) // 2), msg, max(1, cols - 1))
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
        # A spinner tick repaints only the text row; the rules are static.
        if not spinner_only:
            win.erase()
            rule = "─" * (cols - 1)
            _put(win, 0, 0, rule, cols - 1, curses.color_pair(top_color))
            _put(win, 2, 0, rule, cols - 1, curses.color_pair(bottom_color))
        _put(win, 1, 0, line, cols - 1, line_attr)
        win.noutrefresh()

    def _compose_status(self, avail: int) -> str:
        """Build the status text, shortening DIR from the front to fit `avail` cols."""
        head = (f"MODE: {self._st_mode} | VIA: {self._st_provider} | MODEL: {self._st_model} | "
                f"HOST: {self._st_host} | CTX: {self._st_ctx}% | DIR: ")
        tail = self._st_extra + (" | ▼ NEW (PgDn)" if self._new_below else "")
        budget = avail - len(head) - len(tail)
        return head + _shorten_path_left(self._st_dir, budget) + tail

    def _build_screen_state(self) -> tuple[list[str], list[int], int, int]:
        """Return (screen_lines, line_starts_raw, cursor_screen_line, cursor_screen_col).

        screen_lines[i]    — text of screen row i
        line_starts_raw[i] — index in (prefix + self._input) where row i begins
        cursor_screen_line/col — cursor's current screen position
        """
        cols = self._layout["cols"]
        w    = max(1, cols - 2)  # rightmost column reserved for scrollbar
        prefix = self._prefix()
        raw_full      = prefix + self._input
        cursor_in_raw = len(prefix) + self._cursor

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
                # A pasted tab stays a tab in the message but takes one cell here,
                # so the caret arithmetic (one char = one column) holds.
                line_buf.append(" " if ch == "\t" else ch); sc_col += 1
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
        return max(0, i)

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
        plen = len(self._prefix())
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
        plen = len(self._prefix())
        screen_lines, _starts, cursor_screen_line, cursor_screen_col = self._build_screen_state()

        # Scroll so the cursor row is always visible.
        first_visible = max(0, cursor_screen_line + 1 - h)
        visible    = screen_lines[first_visible:first_visible + h]
        cursor_row = cursor_screen_line - first_visible

        prefix_attr = curses.color_pair(_C_FOCUS) if focused else curses.color_pair(0)
        cmd_attr = curses.color_pair(_C_CMD) if self._input.startswith("/") else curses.color_pair(0)
        text_w = cols - 2  # rightmost column belongs to the scrollbar
        for row, text in enumerate(visible):
            if first_visible + row == 0:
                # First screen line — render prefix in its own colour.
                _put(win, row, 0, text[:plen], plen, prefix_attr)
                if text[plen:]:
                    _put(win, row, plen, text[plen:], text_w - plen, cmd_attr)
            else:
                _put(win, row, 0, text, text_w, cmd_attr)
        _draw_vbar(win, cols - 1, h, len(screen_lines), first_visible,
                   _C_FOCUS if focused else _C_BORDER)
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
        self._input_dirty = False
        if self._too_small:
            self._draw_too_small()
            return
        was_open = bool(self._sugg)
        self._update_suggest()
        if was_open or self._sugg:  # the popup lives in the chat pane
            self._redraw()
            return
        self._draw_input()
        curses.doupdate()

    # ── @-path and /command suggestions ──────────────────────────────────────

    def _update_suggest(self):
        """Recompute suggestions when the "@query" or "/command" being typed changes."""
        query = None
        if self._focus == "input":
            v = self._input
            m = _AT_RX.search(v[:self._cursor])
            if m:
                query = ("@", m.group(1))
            elif v.startswith("/") and "\n" not in v and not re.search(r"\s\S*\s", v):
                query = ("/", v)  # until a second word is finished, like the web UI
        if query == self._sugg_query:
            return  # unchanged (also keeps an Esc-dismissed list closed)
        self._sugg_query = query
        self._sugg_idx = -1
        self._sugg_waiting = False
        if query is None:
            self._sugg = []
        elif query[0] == "@":
            # Never walk the tree on the key loop: until the background walk has a
            # list, say so, and the idle tick recomputes when it arrives.
            files = workspace_files_nowait(self.harness.workdir)
            if files is None:
                self._sugg_waiting = True
                self._sugg = [{"note": "searching the workspace…"}]
            else:
                self._sugg = [{"path": p} for p in fuzzy_search(files, query[1])]
        else:
            v = query[1]
            word = v.split()[0].lower()
            self._sugg = [c for c in self._commands
                          if c["cmd"].startswith(word) or c["usage"].startswith(v)]
            if len(self._sugg) == 1 and self._sugg[0]["usage"] == v.strip():
                self._sugg = []  # already fully typed

    def _close_suggest(self):
        self._sugg = []
        self._sugg_idx = -1
        self._sugg_waiting = False

    def _pick_suggest(self, i: int):
        item = self._sugg[i]
        if "note" in item:
            return
        if "path" in item:
            # Replace the pending "@query" with `path`, like the web UI.
            path = item["path"]
            before = re.sub(r"@[\w./~+-]*$", lambda _m: f"`{path}`", self._input[:self._cursor])
            after = self._input[self._cursor:]
            glue = "" if after.startswith(" ") else " "
            self._input = before + glue + after
            self._cursor = len(before) + len(glue)
        else:
            # Commands that take an argument get a trailing space to type it.
            usage = item["usage"]
            takes_arg = " " in usage and " | " not in usage
            self._input = item["cmd"] + (" " if takes_arg else "")
            self._cursor = len(self._input)
        self._close_suggest()

    def _draw_suggest(self):
        if not self._sugg or self._chat_win is None:
            return
        win = self._chat_win
        cols = self._layout["cols"]
        content_h = self._layout["chat_h"] - 1  # bottom row is the h-scrollbar
        n = min(len(self._sugg), _SUGGEST_MAX_ROWS, content_h)
        if n <= 0:
            return
        # Scroll the visible slice so the highlighted row stays in view.
        first = min(max(0, self._sugg_idx - n + 1), len(self._sugg) - n)
        kind = "path" if "path" in self._sugg[0] else "note" if "note" in self._sugg[0] else "cmd"
        if kind == "path":
            width = min(cols - 2, max(len(c["path"]) for c in self._sugg) + 4)
        elif kind == "note":
            width = min(cols - 2, len(self._sugg[0]["note"]) + 4)
        else:
            usage_w = max(len(c["usage"]) for c in self._sugg)
            width = min(cols - 2, usage_w + max(len(c["desc"]) for c in self._sugg) + 4)
        for row, i in enumerate(range(first, first + n)):
            item = self._sugg[i]
            if kind == "path":
                label = "@ " + _shorten_path_left(item["path"], width - 3)
            elif kind == "note":
                label = item["note"]
            else:
                label = f"{item['usage'].ljust(usage_w)}  {item['desc']}"
            attr = (curses.A_REVERSE | curses.color_pair(_C_FOCUS) if i == self._sugg_idx
                    else curses.color_pair(_C_CMD))
            _put(win, content_h - n + row, 0, (" " + label).ljust(width), width, attr)
        win.noutrefresh()

    # ── adding lines to chat buffer ───────────────────────────────────────────

    def _add(self, *ev):
        """Record a transcript event (for re-rendering on view changes) and render
        it — above the live preview while a reply streams, so it doesn't cut it."""
        self._chat_events.append(ev)
        if self._stream_start is None:
            self._render_event(ev)
            return
        self._chat_buf.truncate(self._stream_start)
        self._render_event(ev)
        self._stream_start = len(self._chat_buf)
        self._render_stream_preview()

    def _render_event(self, ev: tuple):
        kind = ev[0]
        if kind == "chat":
            self._render_chat(ev[1], ev[2])
        elif kind == "tool_call":
            self._render_tool_call(ev[1], ev[2])
        elif kind == "tool_result":
            self._render_tool_result(ev[1], ev[2])
        elif kind == "think":
            self._render_think(ev[1])
        elif kind == "diff":
            self._render_diff(ev[1])

    _ROLE_LABELS = {
        "user": ("[user]", _C_USER),
        "assistant": ("[assistant]", _C_ASSISTANT),
        "system": ("[system]", _C_SYSTEM),
        "ask": ("[momo asks]", _C_WARN),   # ask_user: momo waits for your answer
    }

    def _render_chat(self, role: str, text: str):
        cols = max(20, self._layout["cols"] - 2)
        label, color = self._ROLE_LABELS.get(role, (f"[{role}]", _C_SYSTEM))
        if self._view["md_render"] and role == "assistant":
            self._chat_buf.append(label, color)
            for (line_text, line_color, line_attrs) in md_render.process(text, cols):
                self._chat_buf.append(line_text, line_color, line_attrs)
            self._chat_buf.append("", 0)
            return
        indent = " " * (len(label) + 1)
        first = True
        for src in text.splitlines() or [""]:
            for wl in textwrap.wrap(src, width=cols - len(label) - 1) or [""]:
                self._chat_buf.append(f"{label} {wl}" if first else f"{indent}{wl}", color)
                first = False
        self._chat_buf.append("", 0)

    def _render_tool_call(self, name: str, args: dict):
        cols = max(20, self._layout["cols"] - 2)
        full = f"▶ {name}({_brief_args(args)})"
        if self._view["tool_output"]:
            for line in textwrap.wrap(full, width=cols) or [full]:
                self._chat_buf.append(line, _C_TOOL_NAME)
        else:
            self._chat_buf.append((full[:50] + "…") if len(full) > 50 else full, _C_TOOL_NAME)

    def _render_tool_result(self, name: str, result: str):
        if not self._view["tool_output"]:
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

    def _render_think(self, text: str):
        if not self._view["think_output"]:
            return
        cols = max(20, self._layout["cols"] - 4)
        self._chat_buf.append("[thinking]", _C_THINK)
        for src in text.splitlines() or [""]:
            for line in textwrap.wrap(src, width=cols - 2) or [src]:
                self._chat_buf.append("  " + line, _C_THINK)
        self._chat_buf.append("", 0)

    _DIFF_KIND_COLOR = {
        "add":  _C_DIFF_ADD,
        "del":  _C_DIFF_DEL,
        "hunk": _C_DIFF_HUNK,
        "ctx":  _C_TOOL_RES,
    }
    _DIFF_MAX_LINES = 40  # cap body lines shown per diff, like tool results

    def _render_diff(self, ev: DiffEvent):
        if not self._view["diff_output"]:
            return

        # Header line(s) — presentation differs by style; the body is identical.
        if ev.op == "move":
            self._chat_buf.append(f"± renamed {ev.path} → {ev.dst}", _C_DIFF_META,
                                  curses.A_BOLD)
            self._chat_buf.append("", 0)
            return

        if self._view["diff_style"] == "git":
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
        """Re-render every event from scratch: display options or the width
        changed.  Keeps the reading position unless following new output."""
        old_scroll = self._chat_buf._scroll
        self._chat_buf = _LineBuffer()
        for ev in self._chat_events:
            self._render_event(ev)
        if self._stream_start is not None:  # keep the live preview below the rebuilt transcript
            self._stream_start = len(self._chat_buf)
            # The parts were wrapped for the old width: wrap their text again.
            self._stream = {kind: self._new_stream_part(kind, p.text) for kind, p in self._stream.items()}
            self._render_stream_preview()
        if self._follow:
            self._chat_buf.scroll_to_bottom()
        else:
            self._chat_buf.scroll_to(old_scroll)

    # ── event processing ──────────────────────────────────────────────────────

    def _drain_events(self):
        """Apply every queued harness event; returns whether anything changed.
        The subscription is a thread-safe queue, and all TUI state is touched
        only on this (the main) thread."""
        changed = False
        stream_dirty = False
        lines_before = len(self._chat_buf)
        # Buffer index just before the last message rendered this cycle, so the
        # start of a new message (not its end) is what comes into view.
        last_chat_start: int | None = None
        try:
            while True:
                ev = self._events.get_nowait()
                if isinstance(ev, CompanionEvent):   # animation state only, no redraw
                    self._companion.idle = ev.idle
                    self._companion.recaps.update(ev.lines)
                    continue
                changed = True
                if isinstance(ev, (ChatEvent, UserEvent, ErrorEvent, AskUserEvent)):
                    last_chat_start = len(self._chat_buf)
                    if isinstance(ev, ChatEvent):
                        self._add("chat", ev.role, ev.text)
                    elif isinstance(ev, UserEvent):
                        self._add("chat", "user", ev.text)
                    elif isinstance(ev, AskUserEvent):
                        self._add("chat", "ask", ev.question)
                    else:
                        self._add("chat", "system", f"ERROR: {ev.text}")
                elif isinstance(ev, ResetEvent):
                    self._chat_events = []
                    self._chat_buf = _LineBuffer()
                    self._end_stream_preview()
                    last_chat_start = None
                    lines_before = 0
                    self._follow, self._new_below = True, False
                elif isinstance(ev, DeltaEvent):
                    if self._stream_start is None:
                        self._stream_start = len(self._chat_buf)
                    part = self._stream.get(ev.kind)
                    if part is None:
                        part = self._stream[ev.kind] = self._new_stream_part(ev.kind)
                    part.add(ev.text)
                    stream_dirty = True
                elif isinstance(ev, StreamEndEvent):
                    self._end_stream_preview()
                    stream_dirty = False
                elif isinstance(ev, ToolCallEvent):
                    self._add("tool_call", ev.name, ev.args)
                elif isinstance(ev, ToolResultEvent):
                    self._add("tool_result", ev.name, ev.result)
                elif isinstance(ev, ThinkEvent):
                    self._add("think", ev.text)
                elif isinstance(ev, DiffEvent):
                    self._add("diff", ev)
                elif isinstance(ev, StatusEvent):
                    self._apply_status(ev)
                elif isinstance(ev, DoneEvent):
                    self._spinner_frame = 0
                # BusyEvent: nothing to store, the redraw shows the new state.
        except queue.Empty:
            pass

        if stream_dirty:
            self._render_stream_preview()
        if len(self._chat_buf) != lines_before or stream_dirty:
            if not self._follow:
                self._new_below = True       # keep the reading position, flag the news
            elif last_chat_start is not None and not stream_dirty:
                self._chat_buf.scroll_to_top_of(last_chat_start, self._layout["chat_h"])
            else:
                self._chat_buf.scroll_to_bottom()
        return changed

    def _apply_status(self, ev: StatusEvent):
        ctx_map = {"normal": _C_STATUS, "yellow": _C_WARN, "red": _C_DANGER}
        self._ctx_color = ctx_map.get(ev.ctx_color, _C_STATUS)
        parts = []
        if not ev.tools_enabled:
            parts.append("TOOLS: off")
        if ev.run_confirm:
            parts.append("RUN: confirm")
        if ev.net_access != "off":
            parts.append(f"NET: {ev.net_access}" + ("" if ev.net_confirm else " (writes: auto)"))
        if ev.index_enabled:
            idx = "IDX"
            if ev.index_progress:
                idx += f" {ev.index_progress}"
            elif ev.index_state == "stopped":
                idx += " stopped"
            if ev.index_degraded:
                idx += " (degraded)"
            parts.append(idx)
        if ev.guides:
            parts.append("GUIDES")
        self._st_extra = "".join(f" | {p}" for p in parts)
        self._st_mode  = f"{ev.mode} [{ev.plan_progress}]" if ev.plan_progress else ev.mode
        self._st_model = ev.model
        self._st_host  = ev.host
        self._st_provider = ev.provider or self._st_provider
        self._st_ctx   = ev.ctx_pct
        if ev.workdir != self._st_dir:
            workspace_files_nowait(self.harness.workdir)   # warm the new tree's @-search list
        self._st_dir   = ev.workdir

    def _new_stream_part(self, kind: str, text: str = "") -> _StreamPart:
        cols = self._layout["cols"]
        if kind == "thinking":
            part = _StreamPart(max(20, cols - 4) - 2, "  ", "  ")
        else:
            label = self._ROLE_LABELS["assistant"][0]
            part = _StreamPart(max(20, cols - 2) - len(label) - 1, label + " ", " " * (len(label) + 1))
        if text:
            part.add(text)
        return part

    def _render_stream_preview(self):
        """Redraw the in-progress reply below the final transcript lines, as plain
        text (the final message renders the Markdown)."""
        self._chat_buf.truncate(self._stream_start)
        think = self._stream.get("thinking")
        if think is not None and self._view["think_output"]:
            self._chat_buf.append("[thinking]", _C_THINK)
            for line in think.lines():
                self._chat_buf.append(line, _C_THINK)
            self._chat_buf.append("", 0)
        content = self._stream.get("content")
        if content is not None:
            for line in content.lines(" ▍"):
                self._chat_buf.append(line, _C_ASSISTANT)
            self._chat_buf.append("", 0)

    def _end_stream_preview(self):
        if self._stream_start is not None:
            self._chat_buf.truncate(self._stream_start)
        self._stream_start = None
        self._stream = {}

    # ── scrolling ─────────────────────────────────────────────────────────────

    def _scrolled(self):
        """After the user scrolled the chat: follow new output again only once
        they are back at the bottom."""
        self._follow = self._chat_buf.at_bottom()
        if self._follow:
            self._new_below = False

    def _follow_bottom(self):
        self._chat_buf.scroll_to_bottom()
        self._follow, self._new_below = True, False

    # ── view options ──────────────────────────────────────────────────────────

    def _set_view(self, changes: dict):
        """Apply view options (a CommandResult's view fields or a Shift+key)."""
        changes = {k: v for k, v in changes.items() if k in self._view and self._view[k] != v}
        if not changes:
            return
        self._view.update(changes)
        if "companion" in changes:
            self._rebuild()          # a new layout, which re-renders and redraws too
        else:
            self._rebuild_chat_buf()
            self._redraw()

    def _toggle_run_confirm(self):
        self.controller.toggle_run_confirm()
        self._drain_events()  # consume the StatusEvent so the bar updates now
        self._redraw()

    # ── history ───────────────────────────────────────────────────────────────

    def _history_prev(self):
        if not self._history:
            return
        if self._history_idx == -1:
            self._history_stash = self._input
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self._input = self._history[self._history_idx]
        self._cursor = 0   # at the top, so the next ↑ keeps walking back (as in the web UI)

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
        self._close_suggest()
        self._history_idx = -1
        self._history_stash = ""
        self._follow_bottom()     # your own message: show it and what follows
        outcome = self.controller.submit(text, source="tui")
        if outcome.exit_app:
            raise SystemExit(0)
        v = outcome.view
        if "edit_index_filter" in v:
            self._drain_events()  # the echoed command first
            self._edit_index_filter()
            return
        self._set_view(v)
        self._drain_events()  # show the echoed input / command output right away
        self._redraw()

    def _edit_index_filter(self):
        """/index-filter edit: suspend curses, open $VISUAL/$EDITOR on a private
        copy of the filter, and apply it when it changed."""
        text, _ = self.harness.index_filter()
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        fd, tmp = tempfile.mkstemp(prefix="momo-index-filter-", suffix=".gitignore")
        new, note = None, ""
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            curses.def_prog_mode()
            curses.endwin()
            try:
                subprocess.run(shlex.split(editor) + [tmp])
                with open(tmp, encoding="utf-8", errors="replace") as f:
                    new = f.read()
            except (OSError, ValueError) as e:
                note = f"ERROR: could not run the editor {editor!r}: {e}"
            finally:
                curses.reset_prog_mode()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if new is not None:
            note = (self.harness.set_index_filter(new) if new != text
                    else f"Code index filter unchanged. (If {editor!r} returned before you "
                         f"saved, it does not wait: set $VISUAL to e.g. 'code -w'.)")
        self._add("chat", "system", note)
        self._rebuild()

    # ── key input ─────────────────────────────────────────────────────────────

    def _read_key(self):
        """One key without waiting: a str for text, an int for a control or
        special key, None when nothing is buffered.  get_wch, unlike getch,
        returns a whole character, so é or → is one str, not two bytes."""
        if self._pending_keys:
            return self._pending_keys.pop(0)
        try:
            k = self.stdscr.get_wch()
        except curses.error:
            return None
        if isinstance(k, str) and (ord(k) < 32 or ord(k) == 127):
            return ord(k)      # control characters dispatch like keys
        return k

    def _read_key_soon(self, wait: float = 0.03):
        """The next key of a sequence that may not have arrived yet."""
        end = time.monotonic() + wait
        while True:
            k = self._read_key()
            if k is not None or time.monotonic() >= end:
                return k
            time.sleep(0.002)

    def _next_key(self):
        """The next key, with escape sequences decoded: returns _KEY_ESC for a
        lone Esc, _KEY_IGNORED for an unbound sequence (swallowed whole — pushing
        it back would type "[1;2A" into the input), and a pasted block as one str."""
        k = self._read_key()
        if k == _KEY_PASTE_START:
            return self._read_paste()
        if k != 27:
            return k
        nxt = self._read_key_soon()
        if nxt is None:
            return _KEY_ESC
        if nxt in (10, 13):
            return _KEY_SHIFT_ENTER            # Option+Enter
        if nxt == "b":
            return _KEY_CTRL_LEFT              # Option+Left / Meta+b
        if nxt == "f":
            return _KEY_CTRL_RIGHT             # Option+Right / Meta+f
        if nxt in ("[", "O"):
            seq = nxt + self._read_sequence(nxt)
            return self._read_paste() if seq == "[200~" else _KEY_IGNORED
        if nxt == 27:
            self._pending_keys.insert(0, 27)   # Esc Esc: two Escs
            return _KEY_ESC
        return nxt                             # Alt+key: the key itself

    def _read_sequence(self, intro: str) -> str:
        """The rest of an escape sequence: CSI ends at a byte in @..~, SS3 after one char."""
        out = ""
        while len(out) < 32:
            k = self._read_key_soon()
            if not isinstance(k, str):
                break
            out += k
            if intro == "O" or "@" <= k <= "~":
                break
        return out

    def _read_paste(self) -> str:
        """Everything up to the bracketed-paste end marker, as literal text:
        newlines and tabs kept, no key dispatch (a tab would switch focus, a
        capital T toggle a view), and never submitted."""
        buf: list[str] = []
        idle_since = time.monotonic()
        while True:
            k = self._read_key()
            if k is None:
                if time.monotonic() - idle_since > _PASTE_IDLE_S:
                    break              # the end marker never came
                time.sleep(0.002)
                continue
            idle_since = time.monotonic()
            if k == _KEY_PASTE_END:
                break
            if k == 27:
                if self._read_key_soon() == "[" and self._read_sequence("[") == "201~":
                    break
                continue
            if isinstance(k, str):
                buf.append(k)
            elif k == 13:
                buf.append("\n")
            elif k == 10:
                if not (buf and buf[-1] == "\n" and self._last_paste_cr):
                    buf.append("\n")
            elif k == 9:
                buf.append("\t")
            self._last_paste_cr = k == 13
        self._last_paste_cr = False
        return "".join(buf)

    _last_paste_cr = False   # the previous pasted key was CR (a CRLF pair is one newline)

    def _insert(self, text: str):
        self._input = self._input[:self._cursor] + text + self._input[self._cursor:]
        self._cursor += len(text)

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        # Bracketed paste: the terminal marks pasted text so it is inserted, not typed.
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        try:
            self._run()
        finally:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()

    def _run(self):
        # Clear stdscr so leftover terminal content doesn't bleed through.
        self.stdscr.clear()
        self.stdscr.noutrefresh()
        # A pre-loaded session was pushed onto the bus by the Controller and
        # arrives here through the backlog replay.
        self.harness.emit_status()
        self._drain_events()
        self._follow_bottom()
        self._redraw()

        while True:
            key = self._next_key()
            if key == curses.KEY_RESIZE:
                self._rebuild()
                continue
            changed = self._drain_events()
            if key is None:
                self._idle(changed)
                continue
            drawn = self._handle_key(key)
            if changed or drawn == "full":
                self._redraw()
            elif drawn == "input":
                # Draw once the keys stop coming: a fast typist or an unbracketed
                # paste would otherwise redraw the whole input per character.
                self._input_dirty = True

    def _idle(self, changed: bool):
        """No key waiting: catch up on drawing, then animate."""
        if self._sugg_waiting and workspace_files_nowait(self.harness.workdir) is not None:
            self._sugg_query = None      # the file list arrived: recompute the @ list
            changed = True
        if changed:
            self._redraw()
        elif self._input_dirty:
            self._redraw_input_only()
        elif not self._too_small:
            self._animate()
        time.sleep(0.02)  # idle — avoids CPU spin without adding key lag

    def _animate(self):
        drew = False
        now = time.time()
        if self._busy and not self._waiting_for_input and now - self._spinner_ts >= _SPINNER_INTERVAL:
            self._spinner_frame += 1
            self._spinner_ts = now
            self._draw_status(spinner_only=True)
            drew = True
        c = self._companion
        if self._companion_win is not None and now - c.ts >= _COMPANION_INTERVAL:
            c.ts = now
            c.advance(self._layout["cols"], (self.harness.mode, self._busy and not self._waiting_for_input))
            c.draw(self._companion_win, self._layout["cols"])
            drew = True
        if drew:
            # The input window must be refreshed last so doupdate parks the cursor there.
            self._input_win.noutrefresh()
            curses.doupdate()

    def _handle_key(self, key) -> str | None:
        """Act on one key.  Returns "full" when the whole screen needs a redraw,
        "input" when only the input box changed, None when nothing is to draw."""
        chat = self._focus == "chat"

        # A pasted block (bracketed paste) or a typed character.
        if isinstance(key, str):
            if len(key) > 1:
                self._insert(key.replace("\r\n", "\n").replace("\r", "\n"))
                return "input"
            if chat and key in _VIEW_KEYS:           # Shift+T/M/D/Q — chat focus only,
                opt = _VIEW_KEYS[key]                 # so typing capitals still works
                self._set_view({opt: not self._view[opt]})
                return None
            if chat and key == "C":
                self.controller.cancel()
                return None
            if chat and key == "P":
                self._toggle_run_confirm()
                return None
            if key.isprintable():
                self._insert(key)
                return "input"
            return None

        # Open suggestion list: ↑/↓ move, Tab picks (first by default), Enter
        # picks only a highlighted row, Esc dismisses.
        if self._sugg and not chat:
            n = len(self._sugg)
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                self._sugg_idx = ((self._sugg_idx + 1) % n if key == curses.KEY_DOWN
                                  else self._sugg_idx - 1 if self._sugg_idx > 0 else n - 1)
                return "full"
            if key == 9 or (key in (13, curses.KEY_ENTER) and self._sugg_idx >= 0):
                self._pick_suggest(max(0, self._sugg_idx))
                return "full"

        if key == _KEY_ESC:
            # Close the suggestions, else interrupt a running turn (as in the web UI).
            if self._sugg:
                self._close_suggest()
            elif self._busy and not self._waiting_for_input:
                self.controller.cancel()
            return "full"
        if key == curses.KEY_BTAB:                   # Shift+Tab cycles the modes
            mode = self.harness.mode
            self.controller.set_mode(MODES[(MODES.index(mode) + 1) % len(MODES)]
                                     if mode in MODES else MODES[0])
            self._drain_events()
            return "full"
        if key == 9:                                 # Tab: focus chat ↔ input
            self._focus = "input" if chat else "chat"
            return "full"

        # Chat scrolling: PgUp/PgDn always; ↑/↓, Shift+↑/↓ and ←/→ with chat focus.
        page = self._layout["chat_h"] - 1
        scroll = {curses.KEY_PPAGE: lambda: self._chat_buf.scroll_up(page),
                  curses.KEY_NPAGE: lambda: self._chat_buf.scroll_down(page)}
        if chat:
            scroll.update({curses.KEY_UP: self._chat_buf.scroll_up,
                           curses.KEY_SR: self._chat_buf.scroll_up,
                           curses.KEY_DOWN: self._chat_buf.scroll_down,
                           curses.KEY_SF: self._chat_buf.scroll_down,
                           curses.KEY_LEFT: self._chat_buf.scroll_left,
                           curses.KEY_RIGHT: lambda: self._chat_buf.scroll_right(
                               display_w=self._layout["cols"] - 2)})
        if key in scroll:
            scroll[key]()
            self._scrolled()
            return "full"

        # Input box: history, caret movement and editing (whatever the focus).
        if key == curses.KEY_SR:                     # Shift+↑/↓: history regardless of caret
            self._history_prev()
            return "full"
        if key == curses.KEY_SF:
            self._history_next()
            return "full"
        if key == curses.KEY_UP:                     # caret up; history at the first line
            if not self._cursor_move_vertical(-1):
                self._history_prev()
            return "full"
        if key == curses.KEY_DOWN:
            if not self._cursor_move_vertical(1):
                self._history_next()
            return "full"
        moves = {
            curses.KEY_LEFT: lambda: max(0, self._cursor - 1),
            curses.KEY_RIGHT: lambda: min(len(self._input), self._cursor + 1),
            curses.KEY_HOME: lambda: 0,
            curses.KEY_END: lambda: len(self._input),
            _KEY_CTRL_LEFT: self._word_start_left,
            _KEY_CTRL_RIGHT: self._word_end_right,
            1: lambda: self._input.rfind("\n", 0, self._cursor) + 1,          # Ctrl+A: line start
            5: lambda: (i if (i := self._input.find("\n", self._cursor)) >= 0  # Ctrl+E: line end
                        else len(self._input)),
        }
        if key in moves:
            self._cursor = moves[key]()
            return "input"

        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self._cursor > 0:
                self._input = self._input[:self._cursor - 1] + self._input[self._cursor:]
                self._cursor -= 1
            return "input"
        if key in (10, _KEY_SHIFT_ENTER):
            # Ctrl+J (LF; nonl() keeps Enter as CR 13) or Shift/Option+Enter: a newline.
            self._insert("\n")
            return "input"
        if key in (13, curses.KEY_ENTER):
            # A terminal without bracketed paste sends a pasted newline as Enter:
            # more input already buffered means a paste, so insert a newline.
            nxt = self._read_key()
            if nxt is None:
                self._submit()   # draws itself
                return None
            self._insert("\n")
            if nxt != 10:        # the LF half of a CRLF pair
                self._pending_keys.insert(0, nxt)
            return "input"
        if key == 11:  # Ctrl+K — kill to end of line (the newline itself when right before it)
            i = self._input.find("\n", self._cursor)
            end = len(self._input) if i < 0 else self._cursor + 1 if i == self._cursor else i
            self._input = self._input[:self._cursor] + self._input[end:]
            return "input"
        if key == 21:  # Ctrl+U — kill from start of line to cursor
            start = self._input.rfind("\n", 0, self._cursor) + 1
            self._input = self._input[:start] + self._input[self._cursor:]
            self._cursor = start
            return "input"
        return None      # _KEY_IGNORED and anything unbound


def run_tui(stdscr, harness: Harness, controller: Controller):
    tui = TUI(stdscr, harness, controller)
    try:
        tui.run()
    finally:
        tui._events.close()
