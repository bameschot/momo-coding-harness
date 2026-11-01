"""/think-budget and /progress-note: the reasoning watch stops a streamed reply
that thinks too long or repeats itself and asks the model to act, and a note
follows many tool calls that changed nothing, even when each came with text.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness import think_watch
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from home_tmp import setUpModule, tearDownModule  # noqa: F401


def _varied(n_chars: int) -> str:
    """Long reasoning that never repeats a line."""
    out, i = [], 0
    while sum(map(len, out)) < n_chars:
        out.append(f"Step {i}: the value of item {i} depends on item {i + 1}, so check it next.\n")
        i += 1
    return "".join(out)


class Watch(unittest.TestCase):
    def test_budget_trips_past_the_budget(self):
        w = think_watch.ThinkWatch(budget=1000, repeat=False)
        w.feed("x" * 3999)
        with self.assertRaises(think_watch.Overrun) as cm:
            w.feed("x" * 10)
        self.assertEqual(cm.exception.kind, "budget")
        self.assertEqual(len(cm.exception.reasoning), 4009)

    def test_repetition_trips_on_a_line_said_three_times(self):
        w = think_watch.ThinkWatch(budget=0)
        line = "Let me re-read the function to see where the frame length is computed.\n"
        w.feed(line + "Something else entirely different happens in this paragraph now.\n")
        w.feed(line)
        with self.assertRaises(think_watch.Overrun) as cm:
            # streamed in small pieces, as a server sends it
            for i in range(0, len(line), 7):
                w.feed(line[i:i + 7])
        self.assertEqual(cm.exception.kind, "repeat")
        self.assertIn("frame length", cm.exception.line)

    def test_long_varied_reasoning_and_short_lines_do_not_trip(self):
        w = think_watch.ThinkWatch(budget=0)
        for i in range(0, 40_000, 50):
            w.feed(_varied(40_000)[i:i + 50])
        for _ in range(20):
            w.feed("Wait.\nHmm, no.\n")

    def test_a_repeat_outside_the_window_does_not_count(self):
        w = think_watch.ThinkWatch(budget=0)
        line = "I should check whether the cache is cleared before the second request.\n"
        for _ in range(think_watch.REPEATS - 1):
            w.feed(line)
            w.feed(_varied(think_watch.WINDOW + 100))
        w.feed(line)

    def test_off(self):
        self.assertFalse(think_watch.ThinkWatch(budget=0, repeat=False).active)


class StreamingClient(LLMClient):
    """Replies from a script: each entry is (thinking, content, tool calls).  The
    thinking streams through on_delta in 100-char pieces, as a server sends it."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.script, self.requests, self.aborts = [], [], 0

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append({"think": think, "messages": list(messages)})
        thinking, content, calls = (self.script.pop(0) if self.script else ("", "done", []))
        if on_delta is not None:
            for i in range(0, len(thinking), 100):
                on_delta("thinking", thinking[i:i + 100])
            if content:
                on_delta("content", content)
        return ChatResponse(content=content, thinking=thinking, tool_calls=calls,
                            done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        self.aborts += 1


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(hmod, "make_client",
                              lambda *a, **k: StreamingClient("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.work = self.home / "work"
        self.work.mkdir()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.work)
        self.h.set_mode("coding")
        self.client = self.h.client

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def cmd(self, text):
        return handle_command(text, self.h).output

    def notes(self, text):
        return [m for m in self.h.messages
                if m.get("role") == "user" and text in (m.get("content") or "")]


class Overrun(Base):
    def test_a_long_reply_is_stopped_and_retried_without_thinking(self):
        self.h.think_budget = 1000
        self.client.script = [("1. read the file\n" + _varied(8000), "", [])]
        self.h.send("fix it")
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(self.client.aborts, 1)
        self.assertFalse(self.client.requests[1]["think"])
        note = self.notes("Your reasoning ran past 1,000 tokens")
        self.assertEqual(len(note), 1)
        self.assertIn("Where your reasoning had got to", note[0]["content"])
        stored = [m for m in self.h.messages if m.get("role") == "thinking"]
        self.assertTrue(stored[0]["unsent"])
        # roles alternate in what is sent: user, (empty) assistant, the note
        sent = [m["role"] for m in self.client.requests[1]["messages"]]
        self.assertEqual(sent[-3:], ["user", "assistant", "user"])
        self.assertEqual(self.h.messages[-1]["content"], "done")

    def test_repeating_reasoning_is_stopped_with_the_line_quoted(self):
        line = "I need to verify the header length field before the mask key is read.\n"
        self.client.script = [(line * 5, "", [])]
        self.h.send("fix it")
        note = self.notes("Your reasoning kept repeating itself")
        self.assertEqual(len(note), 1)
        self.assertIn("> I need to verify the header length field", note[0]["content"])

    def test_at_most_two_stops_per_turn(self):
        self.h.think_budget = 1000
        self.client.script = [(_varied(8000), "", [])] * 2 + [(_varied(8000), "done", [])]
        self.h.send("fix it")
        self.assertEqual(self.client.aborts, think_watch.STOPS_PER_TURN)
        self.assertEqual(len(self.notes("[Note from the harness")), 2)
        # the third long reply was let through and ended the turn
        self.assertEqual(len(self.client.requests), 3)

    def test_off_and_without_streaming_nothing_is_stopped(self):
        for setup in (lambda: self.cmd("/think-budget off") and self.cmd("/think-budget repeat off"),
                      lambda: setattr(self.h, "stream", False)):
            self.h.think_budget, self.h.think_repeat, self.h.stream = 1000, True, True
            setup()
            self.client.requests.clear()
            self.client.script = [(_varied(8000), "done", [])]
            self.h.send("hi")
            self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(self.client.aborts, 0)


class ProgressNote(Base):
    def reads(self, n):
        for i in range(n):
            (self.work / f"f{i}.txt").write_text(f"file {i}\n")
        return [("", f"Reading f{i}.txt.", [ToolCall("read_file", {"path": f"f{i}.txt"})])
                for i in range(n)]

    def test_narrated_reads_get_a_note_at_the_threshold_and_twice_that(self):
        self.client.script = self.reads(35)
        self.h.send("look around")
        notes = self.notes("tool calls in a row without a change")
        self.assertEqual([n["content"].split("]")[1].split(" tool")[0].strip() for n in notes],
                         ["15", "30"])

    def test_a_write_resets_the_count(self):
        script = self.reads(20)
        script[10] = ("", "Writing.", [ToolCall("write_file", {"path": "new.txt", "content": "x\n"})])
        self.client.script = script
        self.h.send("look around")
        self.assertEqual(len(self.notes("tool calls in a row without a change")), 0)

    def test_off(self):
        self.cmd("/progress-note off")
        self.client.script = self.reads(20)
        self.h.send("look around")
        self.assertEqual(self.notes("tool calls in a row"), [])


class Commands(Base):
    def test_think_budget(self):
        self.assertIn("8,000 tokens", self.cmd("/think-budget"))
        self.assertIn("6,000 tokens", self.cmd("/think-budget 6000"))
        self.assertEqual(session_mod.load_prefs().get("think_budget"), 6000)
        self.assertIn("no reasoning budget", self.cmd("/think-budget off"))
        self.assertEqual(self.h.think_budget, 0)
        self.assertIn("6,000", self.cmd("/think-budget 6,000"))
        self.assertTrue(self.cmd("/think-budget 50").startswith("ERROR"))
        self.assertTrue(self.cmd("/think-budget lots").startswith("ERROR"))
        self.assertEqual(self.h.think_budget, 6000)

    def test_think_budget_repeat(self):
        self.assertIn("is not stopped", self.cmd("/think-budget repeat off"))
        self.assertFalse(self.h.think_repeat)
        self.assertIs(session_mod.load_prefs().get("think_repeat"), False)
        self.assertIn("repeats itself is stopped", self.cmd("/think-budget repeat on"))
        self.assertTrue(self.cmd("/think-budget repeat maybe").startswith("ERROR"))

    def test_progress_note(self):
        self.assertIn("on", self.cmd("/progress-note"))
        self.assertIn("off", self.cmd("/progress-note off"))
        self.assertIs(session_mod.load_prefs().get("progress_note"), False)
        self.assertTrue(self.cmd("/progress-note sometimes").startswith("ERROR"))

    def test_status_carries_the_settings(self):
        self.cmd("/think-budget 12000")
        self.cmd("/think-budget repeat off")
        self.cmd("/progress-note off")
        ev = self.h.status_event()
        self.assertEqual((ev.think_budget, ev.think_repeat, ev.progress_note), (12000, False, False))


if __name__ == "__main__":
    unittest.main()
