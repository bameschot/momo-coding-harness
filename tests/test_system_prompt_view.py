"""/system-prompt: the composed system prompt shown part by part (TUI pager,
web drawer).  The parts must join to exactly the prompt the model gets.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest
from pathlib import Path

import harness.harness as hmod
from harness import commands
from harness.plan import Plan, Step
from tests.test_tool_choices import Base


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
        self.assertEqual(keys[0], "index")
        self.assertEqual(keys[1], "role")
        self.assertLess(keys.index("tools"), keys.index("guides"))
        self.assertLess(keys.index("guides"), keys.index("skill:python"))
        self.assertEqual(keys[-1], "index-rules")

    def test_plan_parts(self):
        self.h.set_mode("plan")
        self.h.plan = Plan(title="t", goal="g", steps=[Step(title="s", details="d")])
        role = self.h.system_prompt_sections()[0]
        self.assertIn("Current draft plan", role["text"])
        self.h.plan_phase = "executing"
        role = self.h.system_prompt_sections()[0]
        self.assertEqual(role["label"], "Role: plan (executing)")
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
