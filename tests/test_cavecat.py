"""/cavecat off|on: caveman-style compression of the model's plans and tool
text, no narration between tool calls and a short reply, as a system-prompt
section next to the role (the command-output side is in test_run_output).

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest
from dataclasses import asdict

from harness import session as session_mod
from harness.plan import Plan, Step
from harness.prompts import CAVECAT_LEVELS, _cavecat_rules
from harness.llm.base import ChatResponse, ToolCall
from tests.test_review_fixes import HarnessCase
from tests.test_tool_choices import Base


class Rules(unittest.TestCase):
    def test_two_levels(self):
        self.assertEqual(CAVECAT_LEVELS, ("off", "on"))

    def test_off_momo_and_unknown_have_no_section(self):
        self.assertIsNone(_cavecat_rules("off", "coding"))
        self.assertIsNone(_cavecat_rules("on", "momo"))
        self.assertIsNone(_cavecat_rules("full", "coding"))     # aliases are resolved before

    def test_on_has_every_rule_in_every_role(self):
        for role in ("design", "chat", "plan", "coding", "plan-exec"):
            text = _cavecat_rules("on", role)
            self.assertTrue(text.startswith("## Cavecat\n"), role)
            self.assertIn("keep your reply short", text)
            self.assertIn("**Your reply:** short", text)
            self.assertIn("Between tool calls:** no narration", text)
            self.assertIn("Never compress", text)

    def test_role_names_what_it_writes(self):
        self.assertIn("create_plan", _cavecat_rules("on", "plan"))
        self.assertIn("complete_step", _cavecat_rules("on", "plan-exec"))
        self.assertIn("specs", _cavecat_rules("on", "design"))
        self.assertIn("telegraphic", _cavecat_rules("on", "plan"))

    def test_thinking_is_left_alone(self):
        self.assertNotIn("Thinking", _cavecat_rules("on", "coding"))


class Section(Base):
    def keys(self):
        return [s["key"] for s in self.h.system_prompt_sections()]

    def test_off_by_default(self):
        self.assertEqual(self.h.cavecat, "off")
        self.assertNotIn("cavecat", self.keys())

    def test_section_follows_the_role(self):
        self.h.cavecat = "on"
        keys = self.keys()
        self.assertEqual(keys[keys.index("role") + 1], "cavecat")
        self.assertEqual(keys[-1], "nav")               # nav keeps the last slot

    def test_momo_mode_has_none(self):
        self.h.cavecat = "on"
        self.h.set_mode("momo")
        self.assertNotIn("cavecat", self.keys())

    def test_plan_execution_has_it(self):
        self.h.cavecat = "on"
        self.h.set_mode("plan")
        self.h.plan = Plan(title="t", steps=[Step(title="s")])
        self.h.plan_phase = "executing"
        self.h.set_mode("coding")
        sec = next(s for s in self.h.system_prompt_sections() if s["key"] == "cavecat")
        self.assertIn("complete_step", sec["text"])


class Nudge(HarnessCase):
    """The nudge after many tool-only turns asks for text; with no narration
    between calls asked for, it waits longer."""
    def nudges(self, level):
        self.h.set_mode("coding")
        self.h.cavecat = level
        # A different folder each turn: the same call made again would be a loop.
        for n in range(12):
            (self.h.workdir / f"d{n}").mkdir()
        self.client.replies = [ChatResponse(tool_calls=[ToolCall("list_directory", {"path": f"d{n}"})])
                               for n in range(12)] + [ChatResponse(content="done")]
        self.h.send("look around")
        return [m for m in self.h.messages if m["role"] == "user"
                and "without a text response" in (m.get("content") or "")]

    def test_off_nudges_after_ten(self):
        self.assertEqual(len(self.nudges("off")), 1)

    def test_cavecat_lets_a_quiet_model_work(self):
        self.assertEqual(self.nudges("on"), [])


class Command(Base):
    def test_set_show_save_and_reject(self):
        self.assertIn("off", self.cmd("/cavecat"))
        self.assertIn("on —", self.cmd("/cavecat on"))
        self.assertEqual(self.h.cavecat, "on")
        self.assertEqual(session_mod.load_prefs().get("cavecat"), "on")
        self.assertIn("## Cavecat", self.h.messages[0]["content"])
        self.assertTrue(self.cmd("/cavecat ultra").startswith("ERROR"))
        self.assertEqual(self.h.cavecat, "on")
        self.cmd("/cavecat off")
        self.assertNotIn("Cavecat", self.h.messages[0]["content"])

    def test_earlier_levels_mean_on(self):
        for old in ("text", "full", "lite"):
            self.cmd("/cavecat off")
            self.cmd(f"/cavecat {old}")
            self.assertEqual(self.h.cavecat, "on", old)
            self.assertEqual(session_mod.load_prefs().get("cavecat"), "on")

    def test_status_carries_it_to_the_web(self):
        self.cmd("/cavecat on")
        self.assertEqual(asdict(self.h.status_event())["cavecat"], "on")


if __name__ == "__main__":
    unittest.main()
