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


class _Base(unittest.TestCase):
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


class CheckBackend(_Base):
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


class ServerRelaunch(_Base):
    """A llama.cpp server relaunched mid-session with another model or -c size:
    refresh_backend (run by send() and the controller's idle poll) adopts both."""

    def drain(self, sub) -> list:
        events = []
        while True:
            try:
                events.append(sub.get_nowait())
            except Exception:
                return events

    def relaunch(self, **attrs):
        for k, v in attrs.items():
            setattr(self.cls, k, v)

    def test_model_and_window_change_are_adopted(self):
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, ctx=8192)
        sub = h.event_queue.subscribe(replay=False)
        self.relaunch(models=["Gemma-4"], ctx=32768)
        self.assertTrue(h.refresh_backend())
        self.assertEqual((h.client.model, h.model_max_ctx, h.context_limit),
                         ("Gemma-4", 32768, 16384))
        notes = [e.text for e in self.drain(sub) if isinstance(e, hmod.ChatEvent)]
        self.assertEqual(notes, ["Server changed: model Qwen3.5-9B → Gemma-4; context window "
                                 "8,192 → 32,768 tokens (compaction at 16,384 tokens)."])
        self.assertEqual(h.status_event().context_limit, 16384)
        self.assertEqual(json.loads((self.home / "prefs.json").read_text())["model"], "Gemma-4")
        self.assertFalse(h.refresh_backend())               # nothing new the second time

    def test_unreachable_server_keeps_the_last_known_values(self):
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, ctx=8192)
        self.relaunch(models=[], ctx=None)                   # down / still loading
        self.assertTrue(h.refresh_backend())                # reachability flipped
        self.assertFalse(h.refresh_backend())               # ... only once
        self.assertEqual((h.client.model, h.model_max_ctx, h.context_limit),
                         ("Qwen3.5-9B", 8192, 4096))
        self.assertIs(h.status_event().backend_reachable, False)

    def test_server_back_up_with_another_model(self):
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, ctx=8192)
        self.relaunch(models=[], ctx=None)
        h.refresh_backend()
        self.relaunch(models=["Ornith-1.5-9B"], ctx=238848)
        self.assertTrue(h.refresh_backend())
        self.assertIs(h.backend_reachable, True)
        self.assertEqual((h.client.model, h.model_max_ctx), ("Ornith-1.5-9B", 238848))

    def test_startup_check_records_reachability(self):
        h = self.make(models=[])
        h.check_backend()
        self.assertIs(h.status_event().backend_reachable, False)
        self.relaunch(models=["qwen3.5:9b"])
        h.check_backend()
        self.assertIs(h.backend_reachable, True)

    def test_host_command_reports_an_unreachable_server(self):
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, models=[])
        out = commands.handle("/host http://localhost:9931", h).output
        self.assertIn("(not reachable)", out)
        self.relaunch(models=["Ornith-1.5-9B"])
        out = commands.handle("/host http://localhost:9931", h).output
        self.assertNotIn("not reachable", out)
        self.assertEqual(h.client.model, "Ornith-1.5-9B")

    def test_user_set_limit_over_a_smaller_window_falls_back(self):
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, ctx=32768)
        commands.handle("/context 20000", h)
        self.relaunch(ctx=8192)
        self.assertTrue(h.refresh_backend())
        self.assertFalse(h.context_fixed)
        self.assertEqual(h.context_limit, 4096)

    def test_send_picks_up_a_new_window(self):
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, ctx=8192)
        self.relaunch(ctx=65536)
        h.send("hi")
        self.assertEqual((h.model_max_ctx, h.context_limit), (65536, 32768))

    def test_idle_poll_refreshes_only_when_idle(self):
        from harness import controller as cmod
        h = self.make(model="Qwen3.5-9B", cls=FixedClient, ctx=8192)
        h.idle_recap = False
        h.session_titles = False
        c = cmod.Controller(h)
        sub = h.event_queue.subscribe(replay=False)
        self.relaunch(ctx=16384)
        c._last_backend_check -= cmod._BACKEND_POLL + 1
        c._busy = True
        c._idle_tick()
        self.assertEqual(h.model_max_ctx, 8192)
        c._busy = False
        c._idle_tick()
        self.assertEqual(h.model_max_ctx, 16384)
        statuses = [e for e in self.drain(sub) if isinstance(e, hmod.StatusEvent)]
        self.assertEqual(statuses[-1].context_limit, 8192)
        self.relaunch(ctx=4096)
        c._idle_tick()                                        # within _BACKEND_POLL: no check
        self.assertEqual(h.model_max_ctx, 16384)


if __name__ == "__main__":
    unittest.main()
