"""The startup check against the LLM server (Harness.check_backend): the saved
model and context size are replaced by what the server actually serves.

session.SESSION_DIR is computed at import time, so it is patched to a temp dir.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import commands
from harness import harness as hmod
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient


class FakeClient(LLMClient):
    provider_name = "fake"
    models = ["qwen3.5:9b", "llama3:latest"]
    loaded: list = []
    ctx: int | None = 8192
    can_switch_model = True

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="ok")

    def context_length(self):
        return self.ctx

    def list_models(self):
        return list(self.models)

    def loaded_models(self):
        return list(self.loaded)

    def abort(self):
        pass


class FixedClient(FakeClient):
    """llama.cpp-like: one model per server."""
    can_switch_model = False
    models = ["Qwen3.5-9B"]


class CheckBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "sessions").mkdir()
        self._patches = [
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(hmod, "make_client",
                              lambda provider, host, model, **k: self.cls(host, model)),
        ]
        for p in self._patches:
            p.start()
        self.harnesses = []

    def tearDown(self):
        for h in self.harnesses:
            h.logger.close()
        for p in reversed(self._patches):
            p.stop()
        self.tmp.cleanup()

    def make(self, model="qwen3.5:9b", cls=FakeClient, **client_attrs):
        self.cls = type("Client", (cls,), client_attrs)
        h = hmod.Harness(host="http://fake", model=model, workdir=self.home)
        self.harnesses.append(h)
        return h

    def session(self, **data):
        p = self.home / "sessions" / "2026-01-01T00-00-00.json"
        p.write_text(json.dumps({"messages": [], "model": "qwen3.5:9b", "host": "http://fake",
                                 "context_limit": 4096, **data}))
        return p

    def test_up_to_date_reports_model_and_context(self):
        h = self.make()
        msg = h.check_backend()
        self.assertIn("Model: qwen3.5:9b", msg)
        self.assertIn("context window 8,192 tokens, compaction at 4,096 (50%)", msg)
        self.assertNotIn("Updated", msg)

    def test_restored_limit_follows_a_changed_window(self):
        h = self.make(ctx=32768)
        h.load_session(self.session(context_limit=4096))        # saved with an 8k window
        msg = h.check_backend()
        self.assertEqual(h.context_limit, 16384)
        self.assertEqual(h.model_max_ctx, 32768)
        self.assertIn("context limit 4,096 → 16,384 tokens", msg)

    def test_user_set_limit_is_kept(self):
        h = self.make(ctx=32768)
        h.load_session(self.session(context_limit=10000, context_fixed=True))
        msg = h.check_backend()
        self.assertEqual(h.context_limit, 10000)
        self.assertIn("compaction at 10,000 (set by you)", msg)
        self.assertNotIn("Updated", msg)

    def test_user_set_limit_over_the_window_is_lowered(self):
        h = self.make(ctx=8192)
        h.load_session(self.session(context_limit=20000, context_fixed=True))
        msg = h.check_backend()
        self.assertEqual(h.context_limit, 4096)
        self.assertFalse(h.context_fixed)
        self.assertIn("over the model's 8,192-token window", msg)

    def test_missing_model_switches_to_the_loaded_one(self):
        h = self.make(model="gone:7b", loaded=["llama3:latest"])
        msg = h.check_backend()
        self.assertEqual(h.client.model, "llama3:latest")
        self.assertIn("gone:7b → llama3:latest", msg)
        self.assertEqual(json.loads((self.home / "prefs.json").read_text())["model"], "llama3:latest")

    def test_missing_model_with_nothing_loaded_warns(self):
        h = self.make(model="gone:7b", loaded=[])
        msg = h.check_backend()
        self.assertEqual(h.client.model, "gone:7b")
        self.assertIn("WARNING: gone:7b is not on the server", msg)

    def test_untagged_name_matches_latest(self):
        h = self.make(model="llama3")
        self.assertNotIn("Updated", h.check_backend())

    def test_unreachable_server_changes_nothing(self):
        h = self.make(models=[])
        limit = h.context_limit
        msg = h.check_backend()
        self.assertIn("not reachable", msg)
        self.assertEqual(h.context_limit, limit)

    def test_fixed_backend_adopts_the_served_model(self):
        h = self.make(model="Old-Model", cls=FixedClient)      # relaunched with another model
        msg = h.check_backend()
        self.assertEqual(h.client.model, "Qwen3.5-9B")
        self.assertIn("Old-Model → Qwen3.5-9B (the server's loaded model)", msg)

    def test_explicit_switch_is_not_reported_as_stale(self):
        h = self.make()
        h.load_session(self.session(model="gone:7b"))
        h.switch_backend("fake", "http://fake", "llama3:latest")   # --model on the command line
        self.assertNotIn("Updated", h.check_backend())

    def test_context_command_marks_limit_fixed(self):
        h = self.make()
        commands.handle("/context 5000", h)
        self.assertTrue(h.context_fixed)
        commands.handle("/context 25%", h)
        self.assertFalse(h.context_fixed)
        self.assertEqual(h.context_limit, 2048)


if __name__ == "__main__":
    unittest.main()
