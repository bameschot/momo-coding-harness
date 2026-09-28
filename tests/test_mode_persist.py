"""The selected mode is remembered at once: a /code, /chat, ... switch is written
to the session file (when it has history) and to prefs (for a fresh start),
without waiting for the next turn's autosave.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient


class FakeClient(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="ok", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class ModePersist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: FakeClient("http://fake", "fake")
        self._patches = [
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)

    def tearDown(self):
        hmod.make_client = self._orig
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def saved_mode(self):
        return json.loads(self.h.session_path().read_text())["mode"]

    def test_switch_saves_session_with_history(self):
        self.h.messages.append({"role": "user", "content": "hi"})
        handle_command("/code", self.h)
        self.assertEqual(self.saved_mode(), "coding")
        self.assertEqual(session_mod.load_prefs()["mode"], "coding")

        other = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        other.load_session(self.h.session_path())
        self.assertEqual(other.mode, "coding")
        other.logger.close()

    def test_switch_in_empty_session_only_writes_prefs(self):
        handle_command("/chat", self.h)
        self.assertFalse(self.h.session_path().exists())
        self.assertEqual(session_mod.load_prefs()["mode"], "chat")

    def test_same_mode_does_not_save(self):
        self.h.messages.append({"role": "user", "content": "hi"})
        self.h.set_mode(self.h.mode)    # a workdir refresh, not a switch
        self.assertFalse(self.h.session_path().exists())
        self.assertNotIn("mode", session_mod.load_prefs())


if __name__ == "__main__":
    unittest.main()
