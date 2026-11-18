"""The DoneEvent at the end of a model turn says what the turn cost (web UI footer).

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import unittest
from unittest import mock

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
                         prompt_tokens=1200, eval_tokens=30, cached_tokens=0, prompt_ms=950.0),
            ChatResponse(content="two calls done", prompt_tokens=1500, eval_tokens=12,
                         cached_tokens=1150, prompt_ms=250.0),
        ]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("look around")
        (done,) = self.done_events(sub)
        self.assertEqual((done.prompt_tokens, done.eval_tokens, done.tool_calls), (1500, 42, 2))
        self.assertEqual(done.prompt_total, 2700)       # both requests' prompts: what it re-read
        self.assertEqual((done.prompt_cached, done.prompt_secs), (1150, 1.2))
        self.assertGreaterEqual(done.secs, 0)

    def test_each_turn_starts_from_zero(self):
        self.client.replies = [ChatResponse(content="a", prompt_tokens=10, eval_tokens=5),
                               ChatResponse(content="b", prompt_tokens=20, eval_tokens=7)]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("one")
        self.h.send("two")
        first, second = self.done_events(sub)
        self.assertEqual((first.eval_tokens, second.eval_tokens), (5, 7))
        self.assertEqual((first.prompt_total, second.prompt_total), (10, 20))
        self.assertEqual((first.tool_calls, second.tool_calls), (0, 0))

    def split_turn(self):
        self.client.replies = [
            ChatResponse(content="Looking.", thinking="t" * 40,
                         tool_calls=[ToolCall("list_directory", {})], eval_tokens=50),
            ChatResponse(content="r" * 80, eval_tokens=30)]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("look around")
        (done,) = self.done_events(sub)
        return done

    def test_output_is_split_by_what_the_model_wrote(self):
        done = self.split_turn()                  # no tokenizer: estimated
        self.assertFalse(done.split_exact)
        self.assertEqual((done.think_tokens, done.preamble_tokens, done.reply_tokens), (10, 2, 20))
        self.assertGreater(done.args_tokens, 0)

    def test_the_servers_tokenizer_makes_it_exact(self):
        self.client.count_tokens = lambda texts: [len(t) for t in texts]
        done = self.split_turn()
        self.assertTrue(done.split_exact)
        self.assertEqual((done.think_tokens, done.preamble_tokens, done.reply_tokens), (40, 8, 80))
        self.assertEqual(done.args_tokens, len('{"name": "list_directory", "arguments": {}}'))

    def test_a_text_tool_call_counts_as_args(self):
        self.client.replies = [
            ChatResponse(content='<tool_call>{"name": "list_directory", "arguments": {}}</tool_call>'),
            ChatResponse(content="done")]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("look around")
        (done,) = self.done_events(sub)
        self.assertEqual(done.preamble_tokens, 0)
        self.assertGreater(done.args_tokens, 0)

    def test_a_turn_that_writes_names_its_checkpoint(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.h.set_mode("coding")
            self.client.replies = [
                ChatResponse(tool_calls=[ToolCall("write_file", {"path": "a.txt", "content": "one\n"}),
                                         ToolCall("write_file", {"path": "b.txt", "content": "two\n"})]),
                ChatResponse(content="wrote two files")]
            sub = self.h.event_queue.subscribe(replay=False)
            self.h.send("write two files")
            (done,) = self.done_events(sub)
            newest = self.h.checkpoints.turns()[0]
            self.assertEqual((done.checkpoint, done.files_changed), (newest["id"], 2))

    def test_a_turn_that_writes_nothing_names_no_checkpoint(self):
        self.client.replies = [ChatResponse(tool_calls=[ToolCall("list_directory", {})]),
                               ChatResponse(content="just looked")]
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.send("look")
        (done,) = self.done_events(sub)
        self.assertEqual((done.checkpoint, done.files_changed), (0, 0))

    def test_compaction_done_carries_no_stats(self):
        sub = self.h.event_queue.subscribe(replay=False)
        self.h.compact_threaded(summarise=False)
        (done,) = self.done_events(sub)
        self.assertEqual(done, DoneEvent())


if __name__ == "__main__":
    unittest.main()
