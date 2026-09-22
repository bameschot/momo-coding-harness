"""Context accounting: the per-category breakdown behind /context and the web
popover, and the status bar moving while a reply streams in.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import queue
import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness.commands import _format_context
from harness.events import DeltaEvent, StreamEndEvent
from harness.llm.base import ChatResponse, LLMClient


class FakeClient(LLMClient):
    provider_name = "fake"
    reply = "word " * 2000          # ~2,500 tokens, streamed in small pieces
    prompt_tokens: int | None = 1234

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if on_delta:
            for i in range(0, len(self.reply), 40):
                on_delta("content", self.reply[i:i + 40])
        return ChatResponse(content=self.reply, prompt_tokens=self.prompt_tokens,
                            eval_tokens=len(self.reply) // 4 if self.prompt_tokens else None,
                            done_reason="stop")

    def context_length(self):
        return 8192

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: FakeClient("http://fake", "fake")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=Path(self.tmp.name))
        self.h.set_mode("chat")
        self.sub = self.h.event_queue.subscribe(replay=False)

    def tearDown(self):
        hmod.make_client = self._orig
        self.sub.close()
        self.h.logger.close()
        self.tmp.cleanup()

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.sub.get_nowait())
            except queue.Empty:
                return out


class Breakdown(Base):
    def cats(self):
        return {c["key"]: c for c in self.h.context_breakdown()["categories"]}

    def test_categories(self):
        self.h.messages += [
            {"role": "user", "content": "u" * 400},
            {"role": "thinking", "content": "t" * 800},
            {"role": "assistant", "content": "a" * 200,
             "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x.py"}}}]},
            {"role": "tool", "name": "read_file", "content": "r" * 1200},
        ]
        c = self.cats()
        self.assertEqual(c["user"]["tokens"], 100)
        self.assertEqual(c["assistant"]["tokens"], 50)
        self.assertEqual(c["tool_results"]["tokens"], 300)
        self.assertEqual(c["thinking"]["tokens"], 200)
        self.assertFalse(c["thinking"]["sent"])
        self.assertGreater(c["tool_calls"]["tokens"], 0)
        self.assertGreater(c["tools"]["tokens"], 0)
        self.assertGreater(c["system"]["tokens"], 0)
        # The estimate the bar uses is the sum of what is sent — thinking excluded.
        sent = sum(v["tokens"] for v in c.values() if v["sent"])
        self.assertEqual(self.h._estimate(schemas=True), sent)
        # Compaction measures without the fixed tool schemas.
        self.assertLess(self.h._estimate(), sent)

    def test_tool_reference_attributed_to_tools(self):
        with_tools = self.cats()
        self.h.tools_enabled = False
        without = self.cats()
        # Schemas stop being sent, but the reference text stays in the prompt.
        self.assertLess(without["tools"]["tokens"], with_tools["tools"]["tokens"])
        self.assertEqual(without["system"]["tokens"], with_tools["system"]["tokens"])

    def test_format_context(self):
        text = _format_context(self.h)
        self.assertIn("System prompt", text)
        self.assertIn("Tool results", text)
        self.assertIn("not sent to the model", text)
        self.assertNotIn("Generating", text)


class Streaming(Base):
    def setUp(self):
        super().setUp()
        self.h.context_limit = 20_000  # the ~2.5k-token reply spans many whole percents

    def run_turn(self):
        self.h.send("hello")
        return self.drain()

    def test_status_moves_during_stream(self):
        evs = self.run_turn()
        first = next(i for i, e in enumerate(evs) if isinstance(e, DeltaEvent))
        end = next(i for i, e in enumerate(evs) if isinstance(e, StreamEndEvent))
        live = [e.ctx_pct for e in evs[first:end] if isinstance(e, hmod.StatusEvent)]
        self.assertGreater(len(live), 3, "status should update while streaming")
        self.assertEqual(live, sorted(live))
        self.assertEqual(self.h._stream_chars, 0)
        self.assertEqual(self.h._measured_tokens, 1234 + len(FakeClient.reply) // 4)

    def test_schemas_alone_do_not_trigger_compaction(self):
        # A limit below system prompt + schemas but above the history measure:
        # compacting could not shrink the schemas, so it must not run.
        self.h.context_limit = self.h._estimate() + 50
        self.assertGreater(self.h._estimate(schemas=True), self.h.context_limit)
        evs = self.run_turn()
        self.assertFalse(any(getattr(e, "text", "").startswith("Context compacted") for e in evs))

    def test_no_server_counts_keeps_streamed_tokens(self):
        FakeClient.prompt_tokens = None
        try:
            self.run_turn()
        finally:
            FakeClient.prompt_tokens = 1234
        self.assertIsNone(self.h._measured_tokens)
        self.assertGreaterEqual(self.h._token_estimate, len(FakeClient.reply) // 4)


if __name__ == "__main__":
    unittest.main()
