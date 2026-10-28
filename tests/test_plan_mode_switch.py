"""An approved plan runs in coding mode: the coder's role, tools, tool choices
and skills, and once it is done the next message is a coding turn, not a new plan.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""

import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.plan import Plan, Step
from home_tmp import setUpModule, tearDownModule  # noqa: F401


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


PLAN_ARGS = {"title": "Say hi", "goal": "g",
             "steps": [{"title": "Write hi.txt", "details": "d", "files": ["hi.txt"]}]}


class ScriptedClient(LLMClient):
    provider_name = "fake"
    script: list = []
    calls: list = []      # (tool names, system prompt) per chat() call

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        ScriptedClient.calls.append(({t["function"]["name"] for t in tools},
                                     messages[0]["content"]))
        if ScriptedClient.script:
            return ScriptedClient.script.pop(0)
        return ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class PlanRunsInCodingMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "fake")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.stream = False
        self.answer = "y"
        self.h._ask_user = lambda q: self.answer
        ScriptedClient.script, ScriptedClient.calls = [], []

    def tearDown(self):
        ScriptedClient.script, ScriptedClient.calls = [], []
        hmod.make_client = self._orig
        self.h.logger.close()
        self.tmp.cleanup()

    def test_approval_switches_to_coding_and_stays_there(self):
        self.h.set_mode("plan")
        self.h.load_skill("python")
        ScriptedClient.script = [
            call("create_plan", **PLAN_ARGS),
            call("write_file", path="hi.txt", content="hi\n"),
            call("complete_step", summary="wrote hi.txt"),
        ]
        self.h.send("add a hi file")
        investigate, step = ScriptedClient.calls[0], ScriptedClient.calls[1]
        self.assertIn("create_plan", investigate[0])
        self.assertNotIn("write_file", investigate[0])
        # The step runs with the coding tools plus the plan's own, never create_plan.
        self.assertTrue({"write_file", "edit_file", "complete_step", "revise_plan"} <= step[0])
        self.assertNotIn("create_plan", step[0])
        self.assertIn("Executing an approved plan", step[1])
        self.assertIn("# Skill: Python", step[1])
        self.assertEqual((self.wd / "hi.txt").read_text(), "hi\n")
        # Done: still coding, no plan, and the next turn is a coding turn.
        self.assertEqual(self.h.mode, "coding")
        self.assertIsNone(self.h.plan)
        self.assertEqual(self.h.status_event().mode, "coding")
        self.h.send("thanks")
        tools, prompt = ScriptedClient.calls[-1]
        self.assertNotIn("create_plan", tools)
        self.assertNotIn("complete_step", tools)
        self.assertNotIn("Executing an approved plan", prompt)

    def test_keep_for_later_stays_in_plan_mode(self):
        self.h.set_mode("plan")
        self.answer = "n"
        ScriptedClient.script = [call("create_plan", **PLAN_ARGS)]
        self.h.send("add a hi file")
        self.assertEqual(self.h.mode, "plan")
        self.assertEqual(self.h.plan_phase, "awaiting_approval")

    def test_plan_run_and_resume_switch_to_coding(self):
        self.h.set_mode("design")
        self.h.plan = Plan(title="t", steps=[Step(title="s")])
        result = handle_command("/plan run", self.h)
        self.assertTrue(result.run_plan)
        self.assertEqual((self.h.mode, self.h.plan_phase), ("coding", "executing"))
        self.assertTrue(self.h._plan_executing())
        self.h.set_mode("design")                    # a paused plan is dormant elsewhere
        self.assertFalse(self.h._plan_executing())
        self.assertTrue(handle_command("/plan resume", self.h).run_plan)
        self.assertEqual(self.h.mode, "coding")

    def test_paused_plan_status_shows_in_coding_mode(self):
        self.h.set_mode("coding")
        self.h.plan = Plan(title="t", steps=[Step(title="a"), Step(title="b")])
        self.h.plan_phase = "executing"
        self.assertTrue(self.h.status_event().plan_progress.startswith("exec"))

    def test_old_session_executing_in_plan_mode_loads_as_coding(self):
        self.h.set_mode("coding")
        self.h.plan = Plan(title="t", steps=[Step(title="s")])
        self.h.plan_phase = "executing"
        self.h.messages.append({"role": "user", "content": "hi"})
        self.h.mode = "plan"                          # how such sessions were saved
        self.h._autosave()
        self.assertEqual(session_mod.load(self.h.session_path())["mode"], "plan")
        self.h.load_session(self.h.session_path())
        self.assertEqual(self.h.mode, "coding")
        self.assertTrue(self.h._plan_executing())


if __name__ == "__main__":
    unittest.main()
