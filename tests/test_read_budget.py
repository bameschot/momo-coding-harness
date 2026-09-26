"""read_file against the context budget, read_file from the end, and a turn
whose answer came only as reasoning.

Found by evals/run_command_bench.py: read_file(log, start_line=-50) read the
whole 175 KB log (a negative start was clamped to line 1); a whole-log read has
no limit at all; and a reply given only in reasoning after a write_file ended
the turn with "File written." and no answer.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.events import ChatEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.tools import dispatch


class ReadFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "log.txt").write_text("".join(f"line {i}\n" for i in range(1, 101)))

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, **kw):
        budget = kw.pop("read_budget", None)
        return dispatch("read_file", {"path": "log.txt", **kw}, self.root, read_budget=budget)

    def test_negative_start_reads_from_the_end(self):
        out = self.read(start_line=-2)
        self.assertEqual(out, "  99: line 99\n 100: line 100\n")

    def test_negative_start_beyond_the_top_reads_it_all(self):
        self.assertTrue(self.read(start_line=-500).startswith("   1: line 1\n"))

    def test_negative_start_with_an_earlier_end_is_an_error(self):
        self.assertTrue(self.read(start_line=-2, end_line=10).startswith("ERROR: end_line 10"))

    def test_read_over_budget_is_refused_with_what_fits(self):
        out = self.read(read_budget=300)
        self.assertTrue(out.startswith("ERROR: nothing was read: lines 1-100 of log.txt"), out)
        self.assertIn("About 23 lines fit", out)              # ~13 chars a line
        self.assertIn('read_file(path="log.txt", start_line=..., end_line=...)', out)
        self.assertIn('grep_file(pattern="<text or regex you are looking for>", path="log.txt")', out)

    def test_read_within_budget(self):
        out = self.read(start_line=1, end_line=10, read_budget=300)
        self.assertTrue(out.startswith("   1: line 1\n"), out)

    def test_no_budget_means_no_limit(self):
        self.assertIn(" 100: line 100", self.read())


class Scripted(LLMClient):
    provider_name = "fake"
    replies: list[ChatResponse] = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HarnessBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name).resolve()
        self._patches = [mock.patch.object(hmod, "make_client",
                                           lambda *a, **k: Scripted("http://fake", "fake")),
                         mock.patch.object(session_mod, "_PREFS_PATH",
                                           Path(self.home.name) / "prefs.json"),
                         mock.patch.dict(os.environ, {"HOME": self.home.name})]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()
        self.home.cleanup()

    def run_turn(self, replies, text="go") -> list[ChatEvent]:
        self.h.client.replies = list(replies)
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send(text)
        events = []
        while True:
            try:
                ev = sub.get(timeout=0.1)
            except Exception:
                break
            events.append(ev)
        sub.close()
        return [e for e in events if isinstance(e, ChatEvent)]


class ReadBudget(HarnessBase):
    def test_budget_is_the_free_context_less_ten_percent(self):
        self.h.context_limit = 20000
        used = self.h._estimate(schemas=True)
        self.assertEqual(self.h._read_budget(), (20000 - used - 2000) * 4)

    def test_budget_has_a_floor(self):
        self.h.context_limit = 1000               # far below the system prompt
        self.assertEqual(self.h._read_budget(), hmod._READ_FLOOR_TOKENS * 4)

    def test_harness_refuses_a_read_that_does_not_fit(self):
        (self.wd / "big.txt").write_text(("y" * 200 + "\n") * 3000)       # ~600 KB
        out = self.h._dispatch("read_file", {"path": "big.txt"})
        self.assertTrue(out.startswith("ERROR: nothing was read"), out[:200])
        self.assertTrue(self.h._dispatch("read_file", {"path": "big.txt", "start_line": -5})
                        .startswith("2996: "))


class AnswerOnlyInReasoning(HarnessBase):
    def test_after_write_file(self):
        chats = self.run_turn([
            ChatResponse(tool_calls=[ToolCall(name="write_file",
                                              arguments={"path": "out.txt", "content": "x\n"})],
                         done_reason="stop"),
            ChatResponse(thinking="Three tests fail: a, b and c.", done_reason="stop"),
        ])
        answers = [e.text for e in chats if e.role == "assistant"]
        self.assertEqual(answers, ["Three tests fail: a, b and c."])
        self.assertEqual(self.h.messages[-1], {"role": "assistant",
                                               "content": "Three tests fail: a, b and c."})

    def test_after_two_reasoning_only_replies(self):
        chats = self.run_turn([
            ChatResponse(thinking="Thinking about it.", done_reason="stop"),
            ChatResponse(thinking="The answer is 42.", done_reason="stop"),
        ])
        self.assertIn("The answer is 42.", [e.text for e in chats if e.role == "assistant"])
        self.assertEqual(self.h.messages[-1]["content"], "The answer is 42.")
        # The harness's own retry prompt does not stay in the history.
        self.assertFalse(any(m.get("role") == "user" and "reasoning but no response"
                             in str(m.get("content")) for m in self.h.messages))

    def test_nothing_at_all_still_says_no_response(self):
        chats = self.run_turn([ChatResponse(done_reason="stop"), ChatResponse(done_reason="stop")])
        self.assertIn("No response. Please rephrase or add more detail and try again.",
                      [e.text for e in chats if e.role == "system"])


if __name__ == "__main__":
    unittest.main()
