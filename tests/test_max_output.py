"""/max-output-tokens: a cap on one model reply, sent with every request, so a
model caught in a repetition loop is cut off (and the loop's cut-off retry
takes over) instead of holding the turn until the context is full.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient


class FakeClient(LLMClient):
    """Records the cap each request was made with; the first reply is cut off
    with nothing usable, the retry answers."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requests = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.requests.append({"cap": self.max_output_tokens, "think": think})
        if len(self.requests) == 1:
            return ChatResponse(thinking="loop " * 50, done_reason="length")
        return ChatResponse(content="done", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class MaxOutputTokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.prefs = self.home / "prefs.json"
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: FakeClient("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.prefs),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.stream = False

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def cmd(self, text):
        return handle_command(text, self.h).output

    def test_default_cap_is_sent_and_a_cut_off_reply_is_retried(self):
        self.h.send("hi")
        reqs = self.h.client.requests
        self.assertEqual([r["cap"] for r in reqs], [25000, 25000])
        self.assertFalse(reqs[1]["think"])          # the retry runs without thinking
        self.assertEqual(self.h.messages[-1]["content"], "done")

    def test_command_sets_saves_and_turns_off(self):
        self.assertIn("25,000 tokens", self.cmd("/max-output-tokens"))
        self.assertIn("30,000 tokens", self.cmd("/max-output-tokens 30000"))
        self.assertEqual(session_mod.load_prefs().get("max_output_tokens"), 30000)
        self.assertIn("off", self.cmd("/max-output-tokens off"))
        self.assertEqual(self.h.max_output_tokens, 0)
        self.h.send("hi")
        self.assertIsNone(self.h.client.requests[0]["cap"])
        self.assertTrue(self.cmd("/max-output-tokens 10").startswith("ERROR"))
        self.assertTrue(self.cmd("/max-output-tokens lots").startswith("ERROR"))
        self.assertEqual(self.h.max_output_tokens, 0)

    def test_status_carries_the_cap(self):
        self.cmd("/max-output-tokens 12000")
        self.assertEqual(self.h.status_event().max_output_tokens, 12000)


if __name__ == "__main__":
    unittest.main()
