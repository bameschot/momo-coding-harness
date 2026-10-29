"""Undoing the checkpoint of a plan step or a story puts that step or story
(and the done stories built on it) back to pending, so /plan resume runs it
again; undoing any other checkpoint leaves the plan as it was.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import unittest

from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse
from harness.plan import PLAN_FILENAME
from test_story_plan import (ADD_OK, CALC_RIGHT, CALC_WRONG, FIX_ADD, PLAN_ARGS, Base,
                             ScriptedClient, call, criterion)
from home_tmp import setUpModule, tearDownModule  # noqa: F401

NORMAL = {"title": "Calc", "goal": "Fix add().",
          "steps": [{"title": "Fix add", "details": "calc.py add() returns a + b", "files": ["calc.py"]},
                    {"title": "Say so", "details": "Nothing to change."}]}
CHECK_ADD = {"id": "S2", "title": "Check add()", "description": "add() adds.",
             "technical_details": "nothing to change", "depends_on": ["S1"],
             "acceptance": [criterion("add(2, 3) → 5", ADD_OK)]}


class _Undo(Base):
    def checkpoint(self, step: str) -> int:
        return next(t["id"] for t in self.h.checkpoints.turns() if t["step"] == step)


class NormalPlan(_Undo):
    def run_paused_in_step_2(self):
        """Step 1 fixes calc.py; step 2 is interrupted, so the plan pauses."""
        orig = self.h._run_loop

        def run_loop(*a, **k):
            if self.h.plan and self.h.plan_phase == "executing" and self.h.plan.current_index() == 1:
                return "cancelled"
            return orig(*a, **k)
        self.h._run_loop = run_loop
        self.h.set_mode("plan")
        ScriptedClient.script = [call("create_plan", **NORMAL),
                                 call("write_file", path="calc.py", content=CALC_RIGHT),
                                 call("complete_step", summary="add fixed")]
        self.h.send("fix it")
        self.h._run_loop = orig
        plan = self.h.plan
        self.assertEqual([s.status for s in plan.steps], ["done", "in_progress"])

    def test_undoing_a_step_puts_it_back_to_pending(self):
        self.run_paused_in_step_2()
        report = self.h.undo_files(self.checkpoint("step 1/2"))
        self.assertEqual((self.wd / "calc.py").read_text(), CALC_WRONG)
        plan = self.h.plan
        self.assertEqual([s.status for s in plan.steps], ["pending", "pending"])
        self.assertEqual((plan.steps[0].note, plan.steps[0].finished_at, plan.steps[0].turns),
                         ("", "", 0))
        self.assertEqual(plan.current_index(), 0)
        self.assertIn('Plan progress back to pending: step 1/2 "Fix add"', report)
        self.assertIn("- [ ] 1. **Fix add**", (self.wd / PLAN_FILENAME).read_text())

    def test_the_undo_question_says_so(self):
        self.run_paused_in_step_2()
        res = handle_command(f"/undo {self.checkpoint('step 1/2')}", self.h)
        self.assertIn('Plan progress goes back to pending: step 1/2 "Fix add"', res.confirm_prompt)

    def test_another_plans_checkpoint_leaves_the_plan_alone(self):
        self.run_paused_in_step_2()
        self.h.plan.title = "Something else"
        report = self.h.undo_files(self.checkpoint("step 1/2"))
        self.assertNotIn("Plan progress", report)
        self.assertEqual(self.h.plan.steps[0].status, "done")

    def test_a_plain_turn_leaves_the_plan_alone(self):
        self.run_paused_in_step_2()
        store = self.h.checkpoints
        store.begin_turn(99, "by hand")
        paths = store.record(["notes.txt"], "write_file")
        (self.wd / "notes.txt").write_text("x\n")
        store.after(paths)
        report = self.h.undo_files()
        self.assertFalse((self.wd / "notes.txt").exists())
        self.assertNotIn("Plan progress", report)
        self.assertEqual(self.h.plan.steps[0].status, "done")


class StoriesPlan(_Undo):
    def run_paused_in_phase_2(self):
        """Phase 1: S1 fixes calc.py, S2 (built on it) changes nothing; the
        phase 2 writer answers in text, so the plan pauses."""
        self.h.set_mode("plan")
        self.assertEqual(self.h.set_plan_kind("stories"), "")
        ScriptedClient.script = [
            call("create_plan", **PLAN_ARGS),
            call("write_stories", stories=[FIX_ADD, CHECK_ADD]),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add(a, b) returns a + b."),
            call("complete_story", summary="add checked.")]
        self.h.send("make calc work")
        plan = self.h.plan
        self.assertEqual([s.status for s in plan.steps], ["done", "in_progress"])
        self.assertEqual([s.status for s in plan.story_sets[1].stories], ["done", "done"])

    def test_undoing_a_story_puts_it_and_its_dependents_back(self):
        self.run_paused_in_phase_2()
        report = self.h.undo_files(self.checkpoint("P1 S1"))
        self.assertEqual((self.wd / "calc.py").read_text(), CALC_WRONG)
        plan = self.h.plan
        sset = plan.story_sets[1]
        self.assertEqual([s.status for s in sset.stories], ["pending", "pending"])
        self.assertEqual((sset.get("S1").delivered, sset.get("S1").facts), ("", {}))
        self.assertEqual([s.status for s in plan.steps], ["pending", "pending"])
        self.assertEqual(plan.steps[0].report, "")
        self.assertEqual(plan.current_index(), 0)
        self.assertIn("Plan progress back to pending: P1 S1, S2", report)
        story_file = (self.wd / plan.stories_dir / "phase-1.md").read_text()
        self.assertIn("## [ ] S1 — Make add() add", story_file)

    def test_resume_runs_the_story_again(self):
        self.run_paused_in_phase_2()
        self.h.undo_files(self.checkpoint("P1 S1"))
        ScriptedClient.script = [
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add fixed again."),
            call("complete_story", summary="add checked again.")]
        self.h.send("continue")
        plan = self.h.plan
        self.assertEqual((self.wd / "calc.py").read_text(), CALC_RIGHT)
        self.assertEqual(plan.story_sets[1].get("S1").delivered, "add fixed again.")
        self.assertEqual([s.status for s in plan.steps], ["done", "in_progress"])


if __name__ == "__main__":
    unittest.main()
