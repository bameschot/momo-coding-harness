"""A test that patches only HOME must not write into the real ~/.momo-harness:
session.py resolves its folders per call, not once at import."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient
from harness.paths import momo_home


class _Scripted(LLMClient):
    provider_name = "fake"

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HomeIsolation(unittest.TestCase):
    def test_patching_home_keeps_sessions_and_prefs_out_of_the_real_folder(self):
        real = momo_home()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HOME": tmp}), \
                mock.patch.object(hmod, "make_client",
                                  lambda *a, **k: _Scripted("http://fake", "fake")):
            home = Path(tmp) / ".momo-harness"
            self.assertEqual(session_mod.session_dir(), home / "sessions")
            self.assertEqual(session_mod.prefs_path(), home / "prefs.json")
            h = hmod.Harness(host="http://fake", model="fake", workdir=Path(tmp))
            try:
                h.send("hello")
                h._autosave()
                self.assertTrue(h.session_path().is_relative_to(home))
                self.assertTrue((home / "sessions" / f"{h.session_path().stem}.log").exists())
            finally:
                h.logger.close()
        self.assertFalse((real / "sessions" / f"{h.session_path().stem}.json").exists())

    def test_an_override_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(session_mod, "SESSION_DIR", Path(tmp)):
            self.assertEqual(session_mod.session_path("x"), Path(tmp) / "x.json")


if __name__ == "__main__":
    unittest.main()
