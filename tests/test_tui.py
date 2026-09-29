"""The TUI's logic without a terminal: key decoding (non-ASCII, escape sequences,
bracketed paste), following new output, the incremental stream preview, and
tool-call argument cutting.

A TUI object is built with __new__ and only the state these paths touch, and a
fake screen feeds keys the way curses' get_wch returns them (str for text,
int for special keys, curses.error when nothing is buffered).
"""
import curses
import queue
import textwrap
import unittest
from unittest import mock

from harness import tui as T
from harness.events import ChatEvent, DeltaEvent, StreamEndEvent


class FakeScreen:
    def __init__(self, keys=()):
        self.keys = list(keys)

    def get_wch(self):
        if not self.keys:
            raise curses.error("no input")
        return self.keys.pop(0)


class FakeEvents:
    def __init__(self):
        self.q = queue.Queue()

    def get_nowait(self):
        return self.q.get_nowait()


def make_tui(keys=(), cols=80, chat_h=10):
    t = T.TUI.__new__(T.TUI)
    t.stdscr = FakeScreen(keys)
    t._pending_keys = []
    t._layout = {"cols": cols, "chat_h": chat_h}
    t._chat_buf = T._LineBuffer()
    t._chat_events = []
    t._view = dict(T._VIEW_DEFAULTS)
    t._stream_start = None
    t._stream = {}
    t._follow, t._new_below = True, False
    t._events = FakeEvents()
    t._companion = T._Companion()
    t._spinner_frame = 0
    t._picker = None
    return t


class Keys(unittest.TestCase):
    def test_non_ascii_text_is_one_key(self):
        t = make_tui(["é", "→", "中"])
        self.assertEqual([t._next_key() for _ in range(3)], ["é", "→", "中"])

    def test_control_characters_become_codes(self):
        t = make_tui(["\t", "\r", "\x7f", "\x01"])
        self.assertEqual([t._next_key() for _ in range(4)], [9, 13, 127, 1])

    def test_lone_esc(self):
        self.assertEqual(make_tui(["\x1b"])._next_key(), T._KEY_ESC)

    def test_unbound_sequence_is_swallowed_whole(self):
        t = make_tui(list("\x1b[1;2A") + ["x"])
        self.assertEqual(t._next_key(), T._KEY_IGNORED)
        self.assertEqual(t._next_key(), "x")      # nothing of "[1;2A" leaks into the input

    def test_ss3_sequence_is_swallowed(self):
        t = make_tui(list("\x1bOP") + ["y"])
        self.assertEqual(t._next_key(), T._KEY_IGNORED)
        self.assertEqual(t._next_key(), "y")

    def test_option_keys(self):
        t = make_tui(["\x1b", "\r", "\x1b", "b", "\x1b", "f"])
        self.assertEqual([t._next_key() for _ in range(3)],
                         [T._KEY_SHIFT_ENTER, T._KEY_CTRL_LEFT, T._KEY_CTRL_RIGHT])

    def test_bracketed_paste_via_define_key_codes(self):
        t = make_tui([T._KEY_PASTE_START, "a", "\t", "é", "\r", "\n", "T", "\r", T._KEY_PASTE_END])
        self.assertEqual(t._next_key(), "a\té\nT\n")

    def test_bracketed_paste_via_raw_sequence(self):
        t = make_tui(list("\x1b[200~") + ["x", "\r", "y"] + list("\x1b[201~") + ["z"])
        self.assertEqual(t._next_key(), "x\ny")
        self.assertEqual(t._next_key(), "z")

    def test_paste_is_inserted_not_dispatched(self):
        t = make_tui()
        t._input, t._cursor, t._focus = "", 0, "chat"   # chat focus: "T" alone would toggle
        self.assertEqual(t._handle_key("line1\n\tTab T\n"), "input")
        self.assertEqual(t._input, "line1\n\tTab T\n")
        self.assertTrue(t._view["think_output"])


class Follow(unittest.TestCase):
    def fill(self, t, n=30):
        for i in range(n):
            t._events.q.put(ChatEvent("system", f"message {i}"))
        t._drain_events()

    def test_follows_new_output_at_the_bottom(self):
        t = make_tui()
        self.fill(t)
        self.assertTrue(t._chat_buf.at_bottom())

    def test_scrolled_up_view_stays_and_flags_news(self):
        t = make_tui()
        self.fill(t)
        t._chat_buf.scroll_up(20)
        t._scrolled()
        where = t._chat_buf._scroll
        self.assertFalse(t._follow)
        t._events.q.put(ChatEvent("system", "news"))
        t._events.q.put(DeltaEvent("content", "streaming…"))
        t._drain_events()
        self.assertEqual(t._chat_buf._scroll, where)
        self.assertTrue(t._new_below)
        t._chat_buf.scroll_to_bottom()
        t._scrolled()
        self.assertTrue(t._follow)
        self.assertFalse(t._new_below)

    def test_rebuild_keeps_the_reading_position(self):
        t = make_tui()
        self.fill(t)
        t._chat_buf.scroll_up(20)
        t._scrolled()
        where = t._chat_buf._scroll
        t._layout["cols"] = 60        # a resize re-wraps; short lines keep their count
        t._rebuild_chat_buf()
        self.assertEqual(t._chat_buf._scroll, where)


class Streaming(unittest.TestCase):
    def test_incremental_wrap_matches_wrapping_the_whole(self):
        text = ("word " * 40 + "\n") * 5 + "tail of the reply " * 6
        part = T._StreamPart(30, "> ", "  ")
        for i in range(0, len(text), 7):
            part.add(text[i:i + 7])
        expected, first = [], True
        for src in text.split("\n"):
            for wl in textwrap.wrap(src, width=30) or [""]:
                expected.append(("> " if first else "  ") + wl)
                first = False
        self.assertEqual(part.lines(), expected)

    def test_message_during_a_stream_goes_above_the_preview(self):
        t = make_tui()
        t._events.q.put(DeltaEvent("content", "partial reply"))
        t._drain_events()
        t._events.q.put(ChatEvent("system", "Context: 12%"))
        t._events.q.put(DeltaEvent("content", " continues"))
        t._drain_events()
        texts = [line for line, _, _ in t._chat_buf._lines]
        self.assertIn("[system] Context: 12%", texts)
        preview = [line for line in texts if line.startswith("[assistant]")]
        self.assertEqual(preview, ["[assistant] partial reply continues ▍"])
        self.assertLess(texts.index("[system] Context: 12%"), texts.index(preview[0]))
        t._events.q.put(StreamEndEvent())
        t._drain_events()
        self.assertFalse(any(line.startswith("[assistant]") for line, _, _ in t._chat_buf._lines))


class ToolArgs(unittest.TestCase):
    def test_long_values_are_cut(self):
        out = T._brief_args({"path": "a.py", "content": "x" * 5000})
        self.assertLess(len(out), 100)
        self.assertIn('path="a.py"', out)
        self.assertIn("…", out)


class ToolPicker(unittest.TestCase):
    """/tools opens a checklist; Space toggles through the /tools command."""

    class FakeHarness:
        mode = "coding"

        def __init__(self):
            self.off = set()

        def tool_choices(self):
            return [{"name": n, "group": "files", "enabled": n not in self.off,
                     "locked": n == "locked_tool", "note": "", "desc": f"What {n} does."}
                    for n in ("read_file", "grep_files", "locked_tool")]

    class FakeController:
        def __init__(self, harness):
            self.h, self.sent = harness, []

        def submit(self, text, source="tui"):
            self.sent.append(text)
            if len(text.split()) == 3:
                name, on = text.split()[1:]
                (self.h.off.discard if on == "on" else self.h.off.add)(name)

    def setUp(self):
        self.t = make_tui()
        self.t.harness = self.FakeHarness()
        self.t.controller = self.FakeController(self.t.harness)
        self.t._focus = "input"
        self.t._sugg, self.t._sugg_idx, self.t._sugg_waiting = [], -1, False
        self.t._open_picker()

    def test_move_and_toggle(self):
        t = self.t
        self.assertEqual(t._handle_key(curses.KEY_DOWN), "full")
        t._handle_key(" ")
        self.assertEqual(t.controller.sent, ["/tools grep_files off"])
        self.assertFalse(t._picker["items"][1]["enabled"])      # reloaded
        t._handle_key(13)
        self.assertEqual(t.controller.sent[-1], "/tools grep_files on")

    def test_locked_rows_do_not_toggle_and_up_wraps(self):
        t = self.t
        t._handle_key(curses.KEY_UP)                             # wraps to the last row
        self.assertEqual(t._picker["idx"], 2)
        t._handle_key(" ")
        self.assertEqual(t.controller.sent, [])

    def test_question_mark_prints_details_and_closes(self):
        self.t._handle_key(curses.KEY_DOWN)
        self.t._handle_key("?")
        self.assertEqual(self.t.controller.sent, ["/tools grep_files"])
        self.assertIsNone(self.t._picker)

    def test_highlighted_description_is_drawn(self):
        class Win:
            def __init__(self):
                self.lines = {}

            def addnstr(self, y, x, text, n, attr=0):
                self.lines[y] = text[:n]

            def noutrefresh(self):
                pass

        t = self.t
        t._chat_win = Win()
        t._layout = {"cols": 100, "chat_h": 20}
        t._handle_key(curses.KEY_DOWN)
        with mock.patch.object(curses, "color_pair", lambda n: 0):   # no initscr here
            t._draw_picker()
        self.assertIn("What grep_files does.", "\n".join(t._chat_win.lines.values()))

    def test_esc_closes(self):
        self.t._handle_key(T._KEY_ESC)
        self.assertIsNone(self.t._picker)


if __name__ == "__main__":
    unittest.main()
