"""Per-role tool choices (/tools <name> on|off, the web Tools menu, the TUI
picker): filtered per mode, coupled to /net and /index, saved in the session.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import commands
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient, ToolCall


class _Scripted(LLMClient):
    provider_name = "fake"
    replies: list[ChatResponse] = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.",
                                                                     done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake")),
                        # No indexer thread: only the toggle's bookkeeping is under test.
                        mock.patch.object(hmod.Harness, "_start_index", lambda self: None)]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def names(self):
        return [t["function"]["name"] for t in self.h._current_tools()]

    def choice(self, name):
        return next(c for c in self.h.tool_choices() if c["name"] == name)

    def cmd(self, text):
        return commands.handle(text, self.h).output


class PerRole(Base):
    def test_off_in_one_mode_only(self):
        self.cmd("/tools run_command off")
        self.assertNotIn("run_command", self.names())
        self.assertNotIn("**run_command**", self.h._tool_ref)
        self.assertFalse(self.choice("run_command")["enabled"])
        self.h.set_mode("momo")
        self.assertIn("run_command", self.names())
        self.h.set_mode("coding")
        self.assertNotIn("run_command", self.names())
        self.cmd("/tools run_command on")
        self.assertIn("run_command", self.names())
        self.assertEqual(self.h.disabled_tools, {})

    def test_several_names_at_once(self):
        self.cmd("/tools edit_file write_file off")
        self.assertNotIn("edit_file", self.names())
        self.assertNotIn("write_file", self.names())

    def test_unknown_and_locked_names_are_refused(self):
        self.assertIn("ERROR", self.cmd("/tools nope off"))
        self.assertIn("Usage", self.cmd("/tools write_file maybe"))
        self.h.set_mode("plan")
        self.assertIn("ERROR", self.cmd("/tools create_plan off"))
        self.assertIn("create_plan", self.names())

    def test_plan_choice_holds_during_execution(self):
        self.h.set_mode("plan")
        self.cmd("/tools write_file off")        # only in the execute set
        self.h.plan = mock.Mock()
        self.h.plan_phase = "executing"
        self.assertNotIn("write_file", self.names())
        self.assertIn("edit_file", self.names())

    def test_master_switch_and_listing_still_work(self):
        self.assertIn("off", self.cmd("/tools off"))
        self.assertFalse(self.h.tools_enabled)
        self.cmd("/tools grep_files off")
        listing = self.cmd("/tools list")
        self.assertIn("✗ grep_files", listing)
        self.assertIn("✓ read_file", listing)
        self.assertTrue(commands.handle("/tools", self.h).tool_picker)

    def test_disabled_tool_called_anyway_is_refused(self):
        (self.home / "a.txt").write_text("hello\n")
        self.cmd("/tools read_file off")
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(name="read_file",
                                                               arguments={"path": "a.txt"})])]
        self.h.send("read it")
        result = [m["content"] for m in self.h.messages if m.get("role") == "tool"][-1]
        self.assertIn("turned off for this role", result)
        self.assertNotIn("hello", result)

    def test_status_carries_this_modes_choices(self):
        self.cmd("/tools grep_files off")
        self.assertEqual(self.h.status_event().tools_off, ["grep_files"])
        self.h.set_mode("chat")
        self.assertEqual(self.h.status_event().tools_off, [])


class Descriptions(Base):
    def test_every_choice_has_a_description(self):
        for mode in ("design", "chat", "plan", "coding", "momo"):
            self.h.set_mode(mode)
            for c in self.h.tool_choices():
                self.assertTrue(c["desc"], c["name"])

    def test_web_search_lists_the_loaded_sources(self):
        name = next(iter(self.h.search_sources.sources))
        self.assertIn(name, self.choice("web_search")["desc"])

    def test_tools_name_prints_details(self):
        out = self.cmd("/tools read_file")
        self.assertIn(self.choice("read_file")["desc"][:40], out)
        self.assertIn("* path:", out)
        self.assertIn("on in coding mode", out)
        self.assertIn("ERROR: no tool 'nope'", self.cmd("/tools nope"))
        self.assertIn("Tool calls: off", self.cmd("/tools off"))


class NetCoupling(Base):
    def test_ticking_a_net_tool_turns_net_on_with_only_that_tool(self):
        self.assertFalse(self.choice("fetch_url")["enabled"])
        self.cmd("/tools fetch_url on")
        self.assertEqual(self.h.net_access, "on")
        self.assertIn("fetch_url", self.names())
        self.assertNotIn("web_search", self.names())
        self.h.set_mode("momo")                   # other roles: the whole group
        self.assertIn("web_search", self.names())

    def test_net_on_clears_choices_and_net_off_keeps_them(self):
        self.cmd("/tools fetch_url on")
        self.cmd("/net off")
        self.assertFalse(self.choice("fetch_url")["enabled"])
        self.assertIn("web_search", self.h.disabled_tools["coding"])   # kept while off
        self.cmd("/net on")
        self.assertIn("web_search", self.names())
        self.assertEqual(self.h.disabled_tools, {})

    def test_unticking_the_last_net_tool_keeps_net_on(self):
        self.cmd("/net on")
        self.cmd("/tools fetch_url web_search add_search_source off")
        self.assertEqual(self.h.net_access, "on")
        self.assertNotIn("fetch_url", self.names())

    def test_net_local_stays_local(self):
        self.cmd("/net local")
        self.cmd("/tools fetch_url off")
        self.cmd("/tools fetch_url on")
        self.assertEqual(self.h.net_access, "local")


class IndexCoupling(Base):
    def setUp(self):
        super().setUp()
        if not hmod.INDEX_TOOLS:
            self.skipTest("tree-sitter not installed")

    def test_ticking_an_index_tool_turns_the_index_on_with_only_that_tool(self):
        self.cmd("/tools index_text on")
        self.assertTrue(self.h.index_enabled)
        self.assertEqual(self.h.disabled_tools["coding"],
                         hmod.INDEX_TOOL_NAMES - {"index_text"})

    def test_index_on_clears_choices_off_keeps_them(self):
        self.cmd("/tools index_text on")
        self.cmd("/index off")
        self.assertIn("index_map", self.h.disabled_tools["coding"])
        self.cmd("/index on")
        self.assertEqual(self.h.disabled_tools, {})

    def test_startup_index_keeps_restored_choices(self):
        self.h.disabled_tools = {"coding": {"index_map"}}
        self.h.set_index(True, clear_choices=False)
        self.assertEqual(self.h.disabled_tools, {"coding": {"index_map"}})


class Persistence(Base):
    def test_round_trip_and_new_session_resets(self):
        self.h.messages.append({"role": "user", "content": "hi"})
        self.cmd("/tools run_command off")        # autosaves: there is history
        path = self.h.session_path()
        self.h.new_session()
        self.assertEqual(self.h.disabled_tools, {})
        self.assertIn("run_command", self.names())
        self.h.load_session(path)
        self.assertEqual(self.h.disabled_tools, {"coding": {"run_command"}})
        self.assertNotIn("run_command", self.names())

    def test_old_sessions_load_with_everything_on(self):
        self.h.messages.append({"role": "user", "content": "hi"})
        self.h._autosave()
        path = self.h.session_path()
        data = session_mod.load(path)
        del data["disabled_tools"]
        path.write_text(__import__("json").dumps(data))
        self.h.disabled_tools = {"coding": {"x"}}
        self.h.load_session(path)
        self.assertEqual(self.h.disabled_tools, {})


if __name__ == "__main__":
    unittest.main()
