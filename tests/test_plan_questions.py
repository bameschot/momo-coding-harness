"""/plan questions: the planner asks about the decisions that shape a plan before
it writes one (roles/planner.md, step 4).  A first create_plan made before any
question to the user is refused once; sent again, it goes through.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import plan_runner
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from home_tmp import setUpModule, tearDownModule  # noqa: F401

PLAN = {"title": "Fix paging", "goal": "The last page is dropped.",
        "steps": [{"title": "Fix the bound", "details": "In pager.py paginate(), use <=.",
                   "files": ["pager.py"]}]}
CHOICE = {"question": "Where should the fix go?",
          "options": ["In paginate() (recommended): one place", "In each caller: duplicated"]}


def plan_call():
    return ("Submitting the plan.", [ToolCall("create_plan", dict(PLAN))])


def choice_call():
    return ("Asking where the fix goes.", [ToolCall("ask_choice", dict(CHOICE))])


class Scripted(LLMClient):
    """Replies from a script of (content, tool calls); "done" once it runs out."""
    provider_name = "fake"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.script, self.results = [], []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if messages and messages[-1].get("role") == "tool":
            self.results.append(messages[-1]["content"])
        content, calls = self.script.pop(0) if self.script else ("Done.", [])
        return ChatResponse(content=content, tool_calls=calls, done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class PlanQuestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: Scripted("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.work = self.home / "work"
        self.work.mkdir()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.work)
        self.h.stream = False
        self.h.set_mode("plan")
        self.client = self.h.client
        self.approve = "n"            # keep the plan for later: nothing executes
        self.asked = []
        self.h._ask_user = self.ask

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def ask(self, question, options=None):
        self.asked.append(question)
        return "1" if options else self.approve

    def cmd(self, text):
        return handle_command(text, self.h).output

    def refusals(self):
        return [r for r in self.client.results if r == plan_runner._ASK_FIRST]

    def test_a_first_plan_without_a_question_is_refused_once(self):
        self.client.script = [plan_call(), plan_call()]
        self.h.send("fix paging")
        self.assertEqual(len(self.refusals()), 1)
        self.assertIsNotNone(self.h.plan)          # the second call went through
        self.assertEqual(self.h.plan.title, "Fix paging")

    def test_a_plan_after_a_question_goes_through(self):
        self.client.script = [choice_call(), plan_call()]
        self.h.send("fix paging")
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.asked[0], CHOICE["question"])
        self.assertIsNotNone(self.h.plan)

    # Run 3: the planner asked its questions in its reply, the user answered in
    # the next message, and the plan was refused for asking nothing; its
    # request was the answer, not the request.
    def test_questions_asked_in_text_count_and_the_request_stays_the_first_message(self):
        self.client.script = [("Where should the fix go: paginate() or the callers?", [])]
        self.h.send("fix paging")
        self.client.script = [plan_call()]
        self.h.send("In paginate(), please.")
        self.assertEqual(self.refusals(), [])
        self.assertEqual(self.h.plan.request, "fix paging")

    def test_a_follow_up_without_a_question_does_not_count(self):
        self.client.script = [("I looked at pager.py.", [])]
        self.h.send("fix paging")
        self.client.script = [plan_call(), plan_call()]
        self.h.send("go on")
        self.assertEqual(len(self.refusals()), 1)
        self.assertEqual(self.h.plan.request, "fix paging")

    def test_planning_does_not_write_project_files_through_the_shell(self):
        self.client.script = [("Prototyping.", [ToolCall("run_command",
                                                         {"command": "cat > game.py <<EOF\nx = 1\nEOF"})]),
                              choice_call(), plan_call()]
        self.h.send("fix paging")
        self.assertFalse((self.work / "game.py").exists())
        self.assertTrue(any("ERROR: not run: it writes game.py" in r for r in self.client.results))

    def test_off_asks_nothing_first(self):
        self.cmd("/plan questions off")
        self.client.script = [plan_call()]
        self.h.send("fix paging")
        self.assertEqual(self.refusals(), [])
        self.assertIsNotNone(self.h.plan)

    def test_a_revised_draft_is_not_refused(self):
        self.client.script = [choice_call(), plan_call()]
        self.h.send("fix paging")
        self.client.results.clear()
        self.client.script = [plan_call()]
        self.h.send("make the step smaller")
        self.assertEqual(self.refusals(), [])

    def test_approval_resets_the_count_for_the_next_plan(self):
        self.client.script = [choice_call(), plan_call()]
        self.h.send("fix paging")
        self.assertEqual(self.h._plan_asked, 1)
        self.assertEqual(self.h.approve_plan(), "")
        self.assertEqual((self.h._plan_asked, self.h._plan_gate_refused), (0, False))

    def test_cancel_resets_it_too(self):
        self.client.script = [plan_call(), plan_call()]
        self.h.send("fix paging")
        self.cmd("/plan cancel")
        self.assertEqual((self.h._plan_asked, self.h._plan_gate_refused), (0, False))

    def test_command_saves_and_shows_in_status(self):
        self.assertIn("on", self.cmd("/plan questions"))
        self.assertIn("off", self.cmd("/plan questions off"))
        self.assertFalse(self.h.plan_questions)
        self.assertIs(session_mod.load_prefs().get("plan_questions"), False)
        self.assertFalse(self.h.status_event().plan_questions)
        self.assertTrue(self.cmd("/plan questions maybe").startswith("ERROR"))
        self.assertIn("on", self.cmd("/plan questions on"))
        self.assertTrue(self.h.status_event().plan_questions)

    def test_the_role_asks_before_planning(self):
        prompt = self.h.system_prompt_view()
        text = prompt if isinstance(prompt, str) else str(prompt)
        self.assertIn("Investigate the options", text)
        self.assertIn("Ask before you plan", text)


if __name__ == "__main__":
    unittest.main()
