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
    t._changes = None
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


class StatusLine(unittest.TestCase):
    def compose(self, offline):
        t = make_tui()
        t._st_mode, t._st_provider, t._st_model = "coding", "llama.cpp", "Qwen3.5-9B"
        t._st_host, t._st_ctx, t._st_dir, t._st_extra = "http://localhost:8080", 3, "/w", ""
        t._st_offline = offline
        return t._compose_status(200)

    def test_unreachable_server_marks_the_model_offline(self):
        self.assertIn("MODEL: Qwen3.5-9B (offline) | HOST", self.compose(True))
        self.assertIn("MODEL: Qwen3.5-9B | HOST", self.compose(False))


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



class _Win:
    def __init__(self):
        self.lines = {}

    def addnstr(self, y, x, text, n, attr=0):
        self.lines[y] = text[:n]

    def noutrefresh(self):
        pass


class ChangesOverlay(unittest.TestCase):
    """/changes: the checkpoint stack over the chat pane, a file's diff, and the
    actions that go through /undo and /keep (answered y/N in the input box)."""

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path

        from harness.checkpoints import CheckpointStore
        from harness.tools import dispatch
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"HOME": str(home)})
        self.env.start()
        wd = home / "project"
        wd.mkdir()
        (wd / "a.py").write_text("x = 1\n")
        store = CheckpointStore(wd, "s")
        store.begin_turn(1, "first change")
        dispatch("write_file", {"path": "a.py", "content": "x = 2\n"}, wd, checkpoints=store)
        store.begin_turn(2, "second change")
        dispatch("write_file", {"path": "b.py", "content": "new\n"}, wd, checkpoints=store)

        class H:
            checkpoints = store

        class C:
            def __init__(self):
                self.sent = []

            def submit(self, text, source="tui"):
                self.sent.append(text)

        self.t = make_tui()
        self.t.harness, self.t.controller = H(), C()
        self.t._focus = "input"
        self.t._sugg, self.t._sugg_idx, self.t._sugg_waiting = [], -1, False
        self.t._drain_events = lambda: None
        self.t._open_changes()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def texts(self):
        return [r["text"] for r in self.t._changes["rows"]]

    def test_rows_are_turns_newest_first_with_their_files(self):
        rows = self.texts()
        self.assertIn("#2  turn 2 · second change", rows[0])
        self.assertIn("A b.py", rows[1])
        self.assertIn("#1  turn 1 · first change", rows[2])
        self.assertIn("M a.py  +1 -1", rows[3])

    def test_enter_shows_the_diff_and_esc_goes_back(self):
        t = self.t
        for _ in range(3):
            t._handle_key(curses.KEY_DOWN)
        t._handle_key(13)
        lines = [x[0] for x in t._changes["diff"]["lines"]]
        self.assertIn("a.py", lines[0])
        self.assertTrue(any(line.endswith("-x = 1") for line in lines))
        self.assertTrue(any(line.endswith("+x = 2") for line in lines))
        t._chat_win, t._layout = _Win(), {"cols": 100, "chat_h": 20}
        with mock.patch.object(curses, "color_pair", lambda n: 0):
            t._draw_changes()
        self.assertIn("+x = 2", "\n".join(t._chat_win.lines.values()))
        t._handle_key(T._KEY_ESC)
        self.assertIsNone(t._changes["diff"])
        self.assertIsNotNone(t._changes)

    def test_actions_go_through_the_commands(self):
        t = self.t
        t._handle_key(curses.KEY_DOWN)
        t._handle_key(curses.KEY_DOWN)               # turn #1
        t._handle_key("u")
        self.assertEqual(t.controller.sent, ["/undo 1"])
        self.assertIsNone(t._changes)
        t._open_changes()
        t._handle_key(curses.KEY_DOWN)               # b.py
        t._handle_key("r")
        t._open_changes()
        t._handle_key("k")
        self.assertEqual(t.controller.sent, ["/undo 1", "/undo file b.py", "/keep"])

    def test_tab_shows_the_net_change_per_file(self):
        self.t._handle_key(9)
        self.assertEqual(self.t._changes["view"], "keep")
        self.assertEqual([r.strip() for r in self.texts()], ["M a.py  +1 -1", "A b.py  +1 -0"])

    def test_empty_stack_and_close(self):
        self.t.harness.checkpoints.keep()
        self.t._open_changes()
        self.t._chat_win, self.t._layout = _Win(), {"cols": 100, "chat_h": 20}
        with mock.patch.object(curses, "color_pair", lambda n: 0):
            self.t._draw_changes()
        self.assertIn("Nothing to undo", "\n".join(self.t._chat_win.lines.values()))
        self.t._handle_key("q")
        self.assertIsNone(self.t._changes)


if __name__ == "__main__":
    unittest.main()


TODOS = [{"text": "Rename parse()", "status": "done"},
         {"text": "Add a test", "status": "in_progress"},
         {"text": "Update the README", "status": "pending"}]


class TodoStrip(unittest.TestCase):
    """The todo strip between the chat and the companion: its place in the
    layout, its height, its lines, and the relayout a status change causes."""

    def test_the_regions_stack_without_overlap(self):
        L = T._compute_layout(40, 80, companion_h=4, todos_h=3)
        self.assertEqual(L["todos_y"], L["chat_y"] + L["chat_h"])
        self.assertEqual(L["companion_y"], L["todos_y"] + L["todos_h"])
        self.assertEqual(L["status_y"], L["companion_y"] + L["companion_h"])
        self.assertEqual(L["input_y"] + L["input_h"], 40)
        self.assertEqual(L["chat_h"], T._compute_layout(40, 80, companion_h=4)["chat_h"] - 3)

    def test_height(self):
        self.assertEqual(T._todos_strip_h([], 40, 4), 0)
        self.assertEqual(T._todos_strip_h([{"text": "a", "status": "done"}], 40, 4), 0)
        self.assertEqual(T._todos_strip_h(TODOS, 40, 4), 2)
        many = [{"text": str(i), "status": "pending"} for i in range(9)]
        self.assertEqual(T._todos_strip_h(many, 40, 4), T._TODOS_MAX_H)
        self.assertEqual(T._todos_strip_h(TODOS, 20, 4), 0)     # the chat would get under 8 rows

    def test_lines(self):
        lines = T._todos_lines(TODOS, 40, 4)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith(" [>] Add a test"))
        self.assertTrue(lines[0].endswith(" 1/3 "))
        self.assertEqual(len(lines[0]), 40)
        self.assertEqual(lines[1], " [ ] Update the README")
        self.assertNotIn("Rename", "".join(lines))

    def test_long_text_is_cut_and_overflow_becomes_a_more_line(self):
        long = [{"text": "x" * 100, "status": "in_progress"}]
        self.assertTrue(T._todos_lines(long, 30, 4)[0].rstrip().endswith("0/1"))
        self.assertIn("…", T._todos_lines(long, 30, 4)[0])
        many = [{"text": f"item {i}", "status": "pending"} for i in range(6)]
        lines = T._todos_lines(many, 40, 4)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[-1], " … +3 more  (/todos)")

    @staticmethod
    def status():
        from harness.events import StatusEvent
        return StatusEvent(mode="coding", model="m", context_limit=1, workdir="", ctx_pct=0,
                           ctx_color="normal", todos=TODOS, todos_progress="1/3")

    def test_a_status_with_a_new_list_relayouts_once(self):
        t = make_tui(cols=80, chat_h=20)
        t.stdscr.getmaxyx = lambda: (40, 80)
        t._layout["todos_h"] = 0
        t._todos = []
        t._st_provider, t._st_dir = "", ""
        t.harness = mock.Mock()
        with mock.patch.object(T.TUI, "_rebuild") as rebuild, \
                mock.patch.object(T.TUI, "_reload_picker"), mock.patch.object(T.TUI, "_reload_changes"), \
                mock.patch.object(T, "workspace_files_nowait"):
            t._apply_status(self.status())
            rebuild.assert_called_once()
            self.assertIn("TODO: 1/3", t._st_extra)
            t._layout["todos_h"] = 2          # what the rebuild laid out
            t._apply_status(self.status())
            rebuild.assert_called_once()      # same height: no second relayout
            t._view["todos"] = False          # Shift+L
            t._apply_status(self.status())
            self.assertEqual(rebuild.call_count, 2)


class Completion(unittest.TestCase):
    """Picking a suggestion: a folder keeps the list open on its contents, a
    file is inserted and the list closes (harness/complete.py makes the items)."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from harness import complete as complete_mod
        from harness.commands import help_commands
        self.tmp = tempfile.TemporaryDirectory()
        wd = Path(self.tmp.name).resolve()
        (wd / "src" / "sub").mkdir(parents=True)
        (wd / "src" / "a.py").write_text("x")
        self.t = t = make_tui()
        t.harness = mock.Mock(workdir=wd)
        t._complete_src = complete_mod.Sources(t.harness, help_commands())
        t._focus = "input"
        t._sugg, t._sugg_idx, t._sugg_waiting, t._sugg_query, t._sugg_start = [], -1, False, None, 0

    def tearDown(self):
        self.tmp.cleanup()

    def type(self, text):
        self.t._input, self.t._cursor = text, len(text)
        self.t._update_suggest()

    def test_a_folder_keeps_browsing_and_a_file_closes(self):
        self.type("see @src/")
        self.assertEqual([c["value"] for c in self.t._sugg], ["@src/sub/", "`src/a.py` "])
        self.t._pick_suggest(0)
        self.assertEqual(self.t._input, "see @src/sub/")
        self.assertEqual(self.t._cursor, len("see @src/sub/"))
        self.assertEqual(self.t._sugg, [])          # src/sub is empty: nothing more to list
        # The cursor before " now": the file goes in there, with one space after it.
        self.t._input, self.t._cursor = "see @src/ now", len("see @src/")
        self.t._update_suggest()
        self.t._pick_suggest(1)
        self.assertEqual(self.t._input, "see `src/a.py` now")
        self.assertEqual(self.t._sugg, [])

    def test_a_command_goes_on_word_by_word(self):
        self.type("/plan sto")
        self.t._pick_suggest(0)
        self.assertEqual(self.t._input, "/plan stories ")
        self.assertIn("approve ", [c["value"] for c in self.t._sugg])     # still open, one level down

    def test_esc_keeps_the_list_closed_until_the_text_changes(self):
        self.type("/plan ")
        self.assertTrue(self.t._sugg)
        self.t._close_suggest()
        self.t._update_suggest()
        self.assertEqual(self.t._sugg, [])
        self.type("/plan s")
        self.assertTrue(self.t._sugg)
