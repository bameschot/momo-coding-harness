"""Project guides (/guides): AGENTS.md / CLAUDE.md / ... from the workdir in the
system prompt, re-read on new session, /clear, compaction and workdir change.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
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
        return ChatResponse(content="SUMMARY", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: FakeClient("http://fake", "fake")
        self._prefs = mock.patch.object(session_mod, "_PREFS_PATH",
                                        Path(self.home.name) / "prefs.json")
        self._prefs.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")

    def tearDown(self):
        hmod.make_client = self._orig
        self._prefs.stop()
        self.h.logger.close()
        self.tmp.cleanup()
        self.home.cleanup()

    def system(self) -> str:
        return self.h.messages[0]["content"]

    def cmd(self, text) -> str:
        return handle_command(text, self.h).output


class Guides(Base):
    def test_off_by_default(self):
        (self.wd / "AGENTS.md").write_text("AGENT RULE ONE")
        self.h.reload_guides()
        self.assertNotIn("AGENT RULE ONE", self.system())
        self.assertEqual(self.h.guides_summary(), "")

    def test_on_loads_in_order_case_insensitive_and_dedups(self):
        (self.wd / "claude.md").write_text("CLAUDE RULE")
        (self.wd / "AGENTS.md").write_text("AGENT RULE")
        (self.wd / "GEMINI.md").write_text("AGENT RULE")   # identical → skipped
        out = self.cmd("/guides on")
        self.assertIn("AGENTS.md, claude.md", out)
        text = self.system()
        self.assertLess(text.index("AGENT RULE"), text.index("CLAUDE RULE"))
        self.assertEqual(text.count("AGENT RULE"), 1)
        self.assertNotIn("GEMINI.md", text)

    def test_reloaded_on_clear_new_session_and_compact(self):
        guide = self.wd / "AGENTS.md"
        guide.write_text("VERSION 1")
        self.cmd("/guides on")
        self.assertIn("VERSION 1", self.system())

        guide.write_text("VERSION 2")
        self.assertIn("VERSION 2", self.cmd("/clear") + self.system())
        self.assertIn("VERSION 2", self.system())

        guide.write_text("VERSION 3")
        self.assertIn("AGENTS.md", self.h.new_session())
        self.assertIn("VERSION 3", self.system())

        guide.write_text("VERSION 4")
        for n in range(12):
            self.h.messages += [{"role": "user", "content": f"q{n} " + "q" * 4000},
                                {"role": "assistant", "content": "a" * 4000}]
        notice = self.h.compact()
        self.assertIn("Project guides loaded", notice)
        self.assertIn("VERSION 4", self.system())
        self.assertNotIn("VERSION 3", self.system())

    def test_unchanged_guides_not_announced_on_compact(self):
        (self.wd / "AGENTS.md").write_text("STABLE")
        self.cmd("/guides on")
        for n in range(12):
            self.h.messages += [{"role": "user", "content": f"q{n} " + "q" * 4000},
                                {"role": "assistant", "content": "a" * 4000}]
        self.assertNotIn("Project guides", self.h.compact())
        self.assertIn("STABLE", self.system())

    def test_workdir_change_reloads(self):
        (self.wd / "AGENTS.md").write_text("FIRST DIR")
        self.cmd("/guides on")
        other = self.wd / "sub"
        other.mkdir()
        (other / "CLAUDE.md").write_text("SECOND DIR")
        self.cmd(f"/workdir {other}")
        self.assertIn("SECOND DIR", self.system())
        self.assertNotIn("FIRST DIR", self.system())

    def test_size_cap(self):
        (self.wd / "AGENTS.md").write_text("x" * (hmod._GUIDE_MAX_CHARS + 5000))
        (self.wd / "CLAUDE.md").write_text("OVER BUDGET")
        self.cmd("/guides on")
        text = self.system()
        self.assertIn("[… truncated: AGENTS.md", text)
        self.assertIn("[… omitted: CLAUDE.md", text)
        self.assertNotIn("OVER BUDGET", text)

    def test_context_breakdown_attributes_guides(self):
        def cats():
            return {c["key"]: c["tokens"] for c in self.h.context_breakdown()["categories"]}
        before = cats()
        self.assertEqual(before["guides"], 0)
        (self.wd / "AGENTS.md").write_text("g" * 4000)
        self.cmd("/guides on")
        after = cats()
        self.assertGreaterEqual(after["guides"], 1000)
        self.assertLess(abs(after["system"] - before["system"]), 5)

    def test_off_removes_and_persists(self):
        (self.wd / "AGENTS.md").write_text("GONE SOON")
        self.cmd("/guides on")
        prefs = json.loads(session_mod._PREFS_PATH.read_text())
        self.assertTrue(prefs["guides"])
        self.assertEqual(self.cmd("/guides off"), "Project guides: off")
        self.assertNotIn("GONE SOON", self.system())
        self.assertFalse(json.loads(session_mod._PREFS_PATH.read_text())["guides"])
        self.assertFalse(self.h.status_event().guides)

    def test_none_found(self):
        self.assertIn("No project guide files found", self.cmd("/guides on"))

    def test_bad_arg(self):
        self.assertTrue(self.cmd("/guides maybe").startswith("ERROR"))


if __name__ == "__main__":
    unittest.main()
