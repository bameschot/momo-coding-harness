"""plan_view(): the web UI's plan timeline for normal and TDD plans (stories
plans are covered in test_story_plan.py) — the steps, how each run went, and
the node being worked on.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest

from harness.llm.base import ChatResponse
from harness.plan import Plan, Step
from test_story_plan import CALC_RIGHT, Base, ScriptedClient, call
from home_tmp import setUpModule, tearDownModule  # noqa: F401

NORMAL = {"title": "Calc", "goal": "Fix add().",
          "steps": [{"title": "Fix add", "details": "calc.py add() returns a + b", "files": ["calc.py"]},
                    {"title": "Say so", "details": "Nothing to change."}]}


class Normal(Base):
    def run_normal(self, script):
        self.h.set_mode("plan")
        ScriptedClient.script = [call("create_plan", **NORMAL)] + script
        self.h.send("fix it")

    def test_steps_record_their_run(self):
        seen = []
        orig = self.h._step_tick

        def tick():
            orig()
            seen.append((self.h.plan_view()["current"], self.h._plan_progress()))
        self.h._step_tick = tick
        self.run_normal([
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_step", summary="add fixed"),
            call("complete_step", summary="nothing to do")])
        self.assertIsNone(self.h.plan)                         # complete
        self.assertIn(({"step": 1, "turns": 1}, "exec 1/2 · turn 1"), seen)
        self.assertIn(({"step": 2, "turns": 1}, "exec 2/2 · turn 1"), seen)

    def test_the_view_mid_plan(self):
        self.run_normal([call("write_file", path="calc.py", content=CALC_RIGHT),
                         call("complete_step", summary="add fixed"),
                         ChatResponse(content="stop", done_reason="stop")])
        # The text reply ended step 2 as well; look at a paused copy instead.
        plan = Plan.from_dict({"title": "Calc", "steps": [
            {"title": "Fix add", "status": "done", "started_at": "2026-10-28T10:00:00",
             "finished_at": "2026-10-28T10:02:00", "turns": 2, "note": "add fixed"},
            {"title": "Say so", "status": "in_progress", "started_at": "2026-10-28T10:02:00",
             "turns": 3}]})
        self.h.plan, self.h.plan_phase = plan, "executing"
        view = self.h.plan_view()
        self.assertEqual(view["kind"], "normal")
        self.assertEqual([(s["title"], s["status"]) for s in view["steps"]],
                         [("Fix add", "done"), ("Say so", "in_progress")])
        self.assertEqual(view["steps"][0]["turns"], 2)
        self.assertEqual(view["steps"][0]["note"], "add fixed")
        self.assertEqual(view["current"], {"step": 2, "turns": 3})
        self.assertEqual(view["steps"][1]["stories"], [])

    def test_run_data_stays_out_of_the_plan_file(self):
        plan = Plan(title="T", steps=[Step(title="A", started_at="2026-10-28T10:00:00", turns=4)])
        self.assertEqual(Plan.from_dict(plan.to_dict()), plan)
        self.assertNotIn("10:00", plan.to_markdown())


class Tdd(Base):
    def test_a_tdd_step_carries_its_expectations_and_stage(self):
        self.h.plan = Plan(title="T", kind="tdd", test_command="pytest", steps=[
            Step(title="Write tests", stage="tests"),
            Step(title="Fix add", tests=["add(2, 3) → 5"], tag="tdd-step-1"),
            Step(title="Acceptance", stage="acceptance", tag="tdd-acceptance")])
        view = self.h.plan_view()
        self.assertEqual(view["kind"], "tdd")
        self.assertEqual([s["stage"] for s in view["steps"]], ["tests", "impl", "acceptance"])
        self.assertEqual(view["steps"][1]["tests"], ["add(2, 3) → 5"])
        self.assertEqual(view["steps"][1]["tag"], "tdd-step-1")
        self.assertIsNone(view["current"])                     # not executing


if __name__ == "__main__":
    unittest.main()
