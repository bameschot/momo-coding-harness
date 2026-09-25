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


if __name__ == "__main__":
    unittest.main()
