"""/system-prompt: the composed system prompt shown part by part (TUI pager,
web drawer).  The parts must join to exactly the prompt the model gets.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import re
import unittest
from pathlib import Path

import harness.harness as hmod
from harness import commands
from harness.plan import Plan, Step
from tests.test_tool_choices import Base
from home_tmp import setUpModule, tearDownModule  # noqa: F401


class _StubIndex:
    """Enough of a ProjectIndex for the prompt: its presence switches the index
    tools and the index banner/rules on."""
    state, total, done = "idle", 0, 0

    def __init__(self, root):
        self.root = Path(root).resolve()

    def live_count(self):
        return 0

    def mem_used(self):
        return 0

    def degraded(self):
        return False


class Sections(Base):
    def joined(self):
        return "".join(s["sep"] + s["text"] for s in self.h.system_prompt_sections())

    def test_parts_join_to_the_prompt_in_every_state(self):
        (self.home / "AGENTS.md").write_text("Be kind to the tests.\n")
        for index in (None, _StubIndex(self.home)):
            self.h.index = index
            for mode in ("design", "chat", "plan", "coding", "momo"):
                self.h.set_mode(mode)
                for skills in ([], ["python"]):
                    self.h.active_skills = skills
                    for guides in (False, True):
                        self.h.guides = guides
                        self.h.reload_guides()
                        self.h.rebuild_system_prompt()
                        self.assertEqual(self.joined(), self.h.messages[0]["content"],
                                         (mode, skills, guides, index))
                        self.assertEqual(self.h.system_prompt_view()["text"],
                                         self.h.messages[0]["content"])
        self.h.index = None

    def test_order_and_labels(self):
        self.h.active_skills = ["python"]
        self.h.guides = True
        (self.home / "AGENTS.md").write_text("x\n")
        self.h.reload_guides()
        self.h.index = _StubIndex(self.home)
        keys = [s["key"] for s in self.h.system_prompt_sections()]
        self.h.index = None
        self.assertEqual(keys, ["index", "role", "guides", "skill:python", "tools", "env", "nav"])
        self.h.set_net_access("on")
        keys = [s["key"] for s in self.h.system_prompt_sections()]
        self.h.set_net_access("off")
        self.assertEqual(keys[-3:], ["net", "env", "nav"])

    def test_another_directory_changes_the_prompt_only_after_the_tool_reference(self):
        # what a server's prompt cache can share between projects
        other = self.home / "other-project"
        other.mkdir()
        h2 = hmod.Harness(host="http://fake", model="fake", workdir=other)
        h2.set_mode(self.h.mode)
        try:
            a, b = self.h._build_system_prompt(), h2._build_system_prompt()
        finally:
            h2.logger.close()
        first = next(i for i in range(min(len(a), len(b))) if a[i] != b[i])
        self.assertGreater(first, a.index(self.h._tool_ref) + len(self.h._tool_ref))
        self.assertGreaterEqual(first, a.index("## Environment"))

    def net_section(self):
        return next((s["text"] for s in self.h.system_prompt_sections() if s["key"] == "net"),
                    None)

    def test_net_section(self):
        self.h.set_mode("coding")
        self.assertIsNone(self.net_section())
        self.h.set_net_access("on")
        try:
            text = self.net_section()
            self.assertIn("Check, don't recall", text)
            if "wikipedia" in self.h.search_sources.sources:
                self.assertIn('source="wikipedia"', text)
            self.h.set_tool_enabled("web_search", False)
            text = self.net_section()
            self.assertNotIn("web_search", text)
            self.assertNotIn("wikipedia", text)
            self.h.set_tool_enabled("fetch_url", False)
            self.assertIsNone(self.net_section())
        finally:
            self.h.set_tool_enabled("web_search", True)
            self.h.set_tool_enabled("fetch_url", True)
            self.h.set_net_access("off")

    def test_environment(self):
        self.h.set_mode("coding")
        env = next(s["text"] for s in self.h.system_prompt_sections() if s["key"] == "env")
        self.assertIn(str(self.h.workdir), env)
        self.assertIn("Today:", env)
        self.assertIn("Git:", env)
        self.assertIn("Internet: off", env)
        self.h.set_mode("chat")          # no run_command: no shell or git facts
        env = next(s["text"] for s in self.h.system_prompt_sections() if s["key"] == "env")
        self.assertNotIn("Git:", env)

    def test_prompt_names_only_offered_tools(self):
        """No role text or generated section may teach a tool the mode does not
        offer — the hand-copied role tables went stale exactly this way."""
        from harness.tools import registry
        every = {t.name for t in registry.all_tools()}
        for net in ("off", "on"):
            self.h.set_net_access(net)
            for index in (None, _StubIndex(self.home)):
                self.h.index = index
                for mode in ("design", "chat", "plan", "coding", "momo"):
                    self.h.set_mode(mode)
                    offered = {t["function"]["name"] for t in self.h._current_tools()}
                    text = "".join(s["text"] for s in self.h.system_prompt_sections()
                                   if s["key"] != "tools")
                    stale = sorted(n for n in every - offered if re.search(rf"\b{n}\b", text))
                    self.assertEqual(stale, [], (mode, bool(index), net))
        self.h.index = None
        self.h.set_net_access("off")

    def test_index_prompt_has_no_translation_patch(self):
        self.h.index = _StubIndex(self.home)
        for mode in ("design", "chat", "plan", "coding", "momo"):
            self.h.set_mode(mode)
            prompt = self.h.messages[0]["content"]
            self.assertNotIn("read them as", prompt)
            self.assertNotIn("find_references", prompt.split("## Tool reference")[0], mode)
        self.h.index = None

    def test_compact_tool_reference(self):
        self.h.set_mode("coding")
        self.assertEqual(self.h.tool_ref, "minimal")        # the default
        minimal = next(s for s in self.h.system_prompt_view()["sections"] if s["key"] == "tools")
        self.assertIn("<tool_call>", minimal["text"])       # how to write a call by hand
        self.assertNotIn("read_file", minimal["text"])      # but no line per tool
        self.cmd("/tool-ref full")
        full = self.h.system_prompt_view()
        self.cmd("/tool-ref compact")
        compact = self.h.system_prompt_view()
        self.assertEqual(self.h.tool_ref, "compact")
        ref = next(s for s in compact["sections"] if s["key"] == "tools")
        self.assertLess(ref["tokens"] * 2, next(s["tokens"] for s in full["sections"]
                                               if s["key"] == "tools"))
        for t in self.h._current_tools():       # every tool still has a copyable example
            self.assertIn(f'<tool_call>{{"name": "{t["function"]["name"]}"', ref["text"])
        self.assertTrue(self.cmd("/tool-ref nope").startswith("ERROR"))
        self.assertEqual(self.h.tool_ref, "compact")
        self.assertIn("full", self.cmd("/tool-ref full"))
        self.assertEqual(self.h.status_event().tool_ref, "full")    # the web UI's picker
        self.assertEqual(self.h.tool_ref, "full")

    def test_plan_parts(self):
        self.h.set_mode("plan")
        self.h.plan = Plan(title="t", goal="g", steps=[Step(title="s", details="d")])
        role = self.h.system_prompt_sections()[0]
        self.assertIn("Current draft plan", role["text"])
        self.h.plan_phase = "executing"
        role = self.h.system_prompt_sections()[0]   # paused, seen from plan mode
        self.assertIn("Plan in progress", role["text"])
        self.assertNotIn("Current draft plan", role["text"])
        self.h.set_mode("coding")                   # an approved plan runs in coding mode
        role = self.h.system_prompt_sections()[0]
        self.assertEqual(role["label"], "Role: coding (executing plan)")
        self.assertIn("Current plan state", role["text"])

    def test_tool_count_follows_choices(self):
        def tools_label():
            return next(s["label"] for s in self.h.system_prompt_view()["sections"]
                        if s["key"] == "tools")
        before = tools_label()
        self.cmd("/tools run_command off")
        self.assertNotEqual(before, tools_label())
        self.assertNotIn('"run_command"', self.h.system_prompt_view()["schemas"]["text"])


class Command(Base):
    def test_list(self):
        out = self.cmd("/system-prompt list")
        self.assertIn("System prompt for coding mode", out)
        self.assertIn("Role: coding", out)
        self.assertIn("Tool schemas", out)
        self.assertIsNone(commands.handle("/system-prompt list", self.h).show_system_prompt)

    def test_show_flag_and_alias(self):
        self.assertTrue(commands.handle("/system-prompt", self.h).show_system_prompt)
        self.assertTrue(commands.handle("/prompt", self.h).show_system_prompt)
        self.assertIn("Usage", self.cmd("/system-prompt nope"))


if __name__ == "__main__":
    unittest.main()
