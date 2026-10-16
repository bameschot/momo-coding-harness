"""The DoneEvent at the end of a model turn says what the turn cost (web UI footer).

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest

from harness.events import DoneEvent
from harness.llm.base import ChatResponse, ToolCall
from test_review_fixes import HarnessCase


class TurnStats(HarnessCase):
    def done_events(self, sub):
        out = []
        while True:
            try:
                ev = sub.get_nowait()
            except Exception:
                return out
            if isinstance(ev, DoneEvent):
                out.append(ev)

    def test_a_turn_reports_tokens_and_tool_calls(self):
        self.client.replies = [
            ChatResponse(tool_calls=[ToolCall("list_directory", {}), ToolCall("file_info", {"path": "."})],
                         prompt_tokens=1200, eval_tokens=30),
            ChatResponse(content="two calls done", prompt_tokens=1500, eval_tokens=12),
        ]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("look around")
        (done,) = self.done_events(sub)
        self.assertEqual((done.prompt_tokens, done.eval_tokens, done.tool_calls), (1500, 42, 2))
        self.assertGreaterEqual(done.secs, 0)

    def test_each_turn_starts_from_zero(self):
        self.client.replies = [ChatResponse(content="a", prompt_tokens=10, eval_tokens=5),
                               ChatResponse(content="b", prompt_tokens=20, eval_tokens=7)]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("one")
        self.h.send("two")
        first, second = self.done_events(sub)
        self.assertEqual((first.eval_tokens, second.eval_tokens), (5, 7))
        self.assertEqual((first.tool_calls, second.tool_calls), (0, 0))

    def test_compaction_done_carries_no_stats(self):
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.compact_threaded(summarise=False)
        (done,) = self.done_events(sub)
        self.assertEqual(done, DoneEvent())


if __name__ == "__main__":
    unittest.main()
