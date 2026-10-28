"""Stories plans (/plan stories) run end to end: the planner's phases, a story
writer per phase, each story in a fresh context and accepted only when its
checks pass, the phase checks and report, re-splitting a failed story, and
the commands and settings.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness import session as session_mod
from harness.commands import handle as handle_command
from harness.events import ChatEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.plan import PLAN_FILENAME
from home_tmp import setUpModule, tearDownModule  # noqa: F401

PY = shlex.quote(sys.executable)
CALC_WRONG = "def add(a, b):\n    return 0\n"
CALC_RIGHT = "def add(a, b):\n    return a + b\n"
CALC_SUB = CALC_RIGHT + "\n\ndef sub(a, b):\n    return a - b\n"
ADD_OK = f"{PY} -c \"from calc import add; assert add(2, 3) == 5\""
SUB_OK = f"{PY} -c \"from calc import sub; assert sub(5, 3) == 2\""
REPORT = "## What exists now\ncalc.add(a, b) adds."


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


def criterion(text, check):
    return {"text": text, "check": check}


PLAN_ARGS = {
    "title": "Calc", "goal": "add() returns 0; also add sub().",
    "steps": [
        {"title": "Fix add", "details": "calc.py add()", "outcome": "add(2, 3) == 5",
         "checks": [ADD_OK], "files": ["calc.py"]},
        {"title": "Add sub", "details": "calc.py sub()", "outcome": "sub(5, 3) == 2",
         "checks": [SUB_OK], "files": ["calc.py"]}]}

FIX_ADD = {"id": "S1", "title": "Make add() add", "description": "add() returns 0.",
           "technical_details": "def add(a, b) -> a + b", "files": ["calc.py"],
           "provides": ["add(a, b) -> a + b in calc.py"],
           "acceptance": [criterion("add(2, 3) → 5", ADD_OK)]}
ADD_SUB = {"id": "S1", "title": "Add sub()", "description": "Subtract.",
           "technical_details": "def sub(a, b) -> a - b", "files": ["calc.py"],
           "depends_on": ["P1.S1"], "acceptance": [criterion("sub(5, 3) → 2", SUB_OK)]}


class ScriptedClient(LLMClient):
    """Replies with `script`, one per tool-loop turn, then "Done.".  A call
    without tools (the phase report) gets REPORT.  Every request is kept."""
    provider_name = "fake"
    script: list = []
    requests: list = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        names = [t["function"]["name"] for t in tools]
        ScriptedClient.requests.append({"messages": [dict(m) for m in messages], "tools": names})
        if not tools:
            return ChatResponse(content=REPORT, done_reason="stop")
        if ScriptedClient.script:
            return ScriptedClient.script.pop(0)
        return ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        (self.wd / "calc.py").write_text(CALC_WRONG)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "fake")
        ScriptedClient.script, ScriptedClient.requests = [], []
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.stream = False
        self.h.session_titles = False
        self.questions = []
        self.answers = []
        self.h._ask_user = self.ask

    def tearDown(self):
        ScriptedClient.script = []
        hmod.make_client = self._orig
        self.h.logger.close()
        self.tmp.cleanup()

    def ask(self, question, options=None):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else "y"

    def run_plan(self, script):
        """Plan mode with a stories plan: create_plan, approval, execution."""
        self.h.set_mode("plan")
        self.assertEqual(self.h.set_plan_kind("stories"), "")
        ScriptedClient.script = [call("create_plan", **PLAN_ARGS)] + script
        self.h.send("make calc work")

    def story_requests(self):
        """The first request of each story: its fresh context."""
        return [r for r in ScriptedClient.requests
                if "complete_story" in r["tools"] and len(r["messages"]) == 2]

    def writer_requests(self):
        return [r for r in ScriptedClient.requests
                if "write_stories" in r["tools"] and len(r["messages"]) == 2]

    def system_lines(self):
        return [ev.text for ev in self.h.event_queue._backlog
                if isinstance(ev, ChatEvent) and ev.role == "system"]


HAPPY = [
    call("write_stories", stories=[FIX_ADD]),               # phase 1 writer
    call("write_file", path="calc.py", content=CALC_RIGHT),  # P1 S1
    call("complete_story", summary="add(a, b) returns a + b."),
    call("write_stories", stories=[ADD_SUB]),               # phase 2 writer
    call("write_file", path="calc.py", content=CALC_SUB),    # P2 S1
    call("complete_story", summary="Added sub(a, b)."),
]
class Run(Base):
    def test_two_phases_end_to_end(self):
        self.run_plan(list(HAPPY))
        self.assertIsNone(self.h.plan)                       # complete
        self.assertEqual((self.wd / "calc.py").read_text(), CALC_SUB)
        self.assertFalse((self.wd / PLAN_FILENAME).exists())
        folders = list((self.wd / ".momo" / "stories").iterdir())
        self.assertEqual(len(folders), 1)
        self.assertTrue(folders[0].name.startswith("calc-"))
        self.assertEqual(sorted(p.name for p in folders[0].iterdir()),
                         ["phase-1.md", "phase-2.md", "plan.md"])   # kept as history
        self.assertIn("## [x] S1 — Make add() add", (folders[0] / "phase-1.md").read_text())
        self.assertTrue(any("Plan complete: Calc" in s for s in self.system_lines()))

    def test_each_story_and_writer_starts_fresh(self):
        self.run_plan(list(HAPPY))
        stories, writers = self.story_requests(), self.writer_requests()
        self.assertEqual(len(stories), 2)
        self.assertEqual(len(writers), 2)
        brief = stories[0]["messages"][1]["content"]
        self.assertIn("# Story S1 of phase 1: Make add() add", brief)
        self.assertIn(ADD_OK, brief)
        self.assertIn("## Implementing one story", stories[0]["messages"][0]["content"])
        self.assertIn("story writer", writers[0]["messages"][0]["content"])
        self.assertTrue(any(r.get("by") == "story" for r in self.h.compacted))

    def test_the_next_writer_sees_what_was_delivered(self):
        self.run_plan(list(HAPPY))
        brief = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("## Report of phase 1: Fix add", brief)
        self.assertIn(REPORT, brief)
        self.assertIn("- P1.S1 Make add() add", brief)
        self.assertIn("delivered: add(a, b) returns a + b.", brief)

    def test_a_dependency_brings_its_delivered_note_and_outline(self):
        self.run_plan(list(HAPPY))
        brief = self.story_requests()[1]["messages"][1]["content"]
        self.assertIn("### P1.S1: Make add() add", brief)
        self.assertIn("- delivered: add(a, b) returns a + b.", brief)
        self.assertIn("Current outline of calc.py:", brief)
        self.assertIn("def add(a, b):", brief)

    def test_facts_are_recorded(self):
        ScriptedClient.script = []
        self.run_plan(list(HAPPY[:3]) + [ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "done")
        self.assertEqual(s1.facts["files"], ["calc.py"])
        self.assertEqual(s1.facts["checks"], [ADD_OK])
        self.assertEqual(self.h.plan.steps[0].report, REPORT)

    def test_a_failing_check_refuses_the_story(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD]),
            call("complete_story", summary="done?"),               # calc.py still wrong
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="fixed"),
            ChatResponse(content="stop", done_reason="stop")])
        results = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                   if m.get("role") == "tool" and "story not complete" in (m.get("content") or "")]
        self.assertTrue(results)
        self.assertIn("✗ add(2, 3) → 5", results[0])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "done")


class Progress(Base):
    """What the web UI's plan timeline shows: plan_view() and the run data."""

    def test_a_run_records_times_turns_and_checks(self):
        self.run_plan(list(HAPPY[:3]) + [ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertTrue(s1.started_at and s1.finished_at)
        self.assertEqual(s1.turns, 2)                          # write_file, complete_story
        self.assertEqual(s1.check_results[0]["passed"], True)
        self.assertEqual(self.h.plan.story_sets[1].gate["passed"], True)
        self.assertTrue(any(s.startswith("▶ P1.S1 — Make add() add") for s in self.system_lines()))

    def test_a_refusal_shows_until_the_fix(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD]),
            call("complete_story", summary="done?"),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="fixed"),
            ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.refusals, 1)
        self.assertEqual(s1.check_results[0]["passed"], True)  # the later pass replaced the ✗

    def test_the_status_line_counts_turns(self):
        seen = []
        orig = self.h._story_tick

        def tick(i):
            orig(i)
            seen.append(self.h._plan_progress())
        self.h._story_tick = tick
        self.run_plan(list(HAPPY[:3]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertIn("phase 1/2 · S1 (0/1) · turn 2/30", seen)
        self.assertIn("phase 1/2 · writing stories", seen)

    def test_the_view(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD, {**ADD_SUB, "id": "S2", "depends_on": ["S1"]}]),
            ChatResponse(content="I give up.", done_reason="stop"),
            ChatResponse(content="stop", done_reason="stop")])
        view = self.h.plan_view()
        self.assertEqual(view["kind"], "stories")
        self.assertEqual([p["title"] for p in view["steps"]], ["Fix add", "Add sub"])
        p1 = view["steps"][0]
        self.assertEqual([s["id"] for s in p1["stories"]], ["S1", "S2"])
        s1, s2 = p1["stories"]
        self.assertEqual(s1["dependents"], ["S2"])
        self.assertEqual(s2["depends_on"], ["S1"])
        self.assertEqual(s1["criteria"][0]["check"], ADD_OK)
        self.assertEqual(s1["technical_details"], "def add(a, b) -> a + b")
        self.assertEqual(s1["provides"], ["add(a, b) -> a + b in calc.py"])
        self.assertEqual(view["cap"], 30)
        self.assertEqual(view["current"], {"step": 1, "turns": 0})   # paused, writer gone
        self.assertEqual(view["steps"][1]["stories"], [])
        self.assertTrue(p1["started_at"])

    def test_current_while_a_story_and_the_writer_run(self):
        seen = []
        orig = self.h._story_tick

        def tick(i):
            orig(i)
            seen.append(self.h.plan_view()["current"])
        self.h._story_tick = tick
        self.run_plan(list(HAPPY[:3]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertIn({"step": 1, "writing": True, "turns": 1}, seen)
        self.assertIn({"step": 1, "story": "S1", "turns": 2}, seen)

    def test_no_view_without_a_plan(self):
        self.assertIsNone(self.h.plan_view())


class Writer(Base):
    def test_an_invalid_set_goes_back_to_the_writer(self):
        bad = {**FIX_ADD, "acceptance": []}
        self.run_plan([call("write_stories", stories=[bad]),
                       call("write_stories", stories=[FIX_ADD]),
                       ChatResponse(content="stop", done_reason="stop")])
        refusals = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                    if m.get("role") == "tool" and "need changes" in (m.get("content") or "")]
        self.assertTrue(refusals)
        self.assertIn("S1 (Make add() add): no acceptance criteria", refusals[0])
        self.assertIn(1, self.h.plan.story_sets)

    def test_feedback_sends_the_writer_back(self):
        self.answers = ["y", "split it smaller"]               # plan yes, stories feedback
        self.run_plan([call("write_stories", stories=[FIX_ADD]),
                       call("write_stories", stories=[FIX_ADD]),
                       ChatResponse(content="stop", done_reason="stop")])
        feedback = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                    if m.get("role") == "tool" and "User requested changes" in (m.get("content") or "")]
        self.assertIn("split it smaller", feedback[0])
        self.assertTrue(self.h.plan.story_sets[1].approved)

    def test_approve_once_does_not_ask(self):
        self.h.stories_approve = "once"
        self.run_plan(list(HAPPY))
        self.assertIsNone(self.h.plan)
        self.assertFalse(any("Run these" in q for q in self.questions))

    def test_hand_edits_before_approval_are_used(self):
        def edit_then_yes(question, options=None):
            if "Run these" in question:
                d = self.wd / self.h.plan.stories_dir
                f = d / "phase-1.md"
                f.write_text(f.read_text().replace("Make add() add", "Make add() really add"))
            return "y"
        self.h._ask_user = edit_then_yes
        self.run_plan(list(HAPPY[:3]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").title, "Make add() really add")


class Failure(Base):
    def test_a_failed_story_is_re_split(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD]),
            ChatResponse(content="I give up.", done_reason="stop"),         # nudged once
            ChatResponse(content="Still no.", done_reason="stop"),          # S1 fails
            call("write_stories", replaces=["S1"], stories=[{**FIX_ADD, "id": "S2"}]),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories], [("S1", "replaced"), ("S2", "done")])
        self.assertEqual(sset.get("S2").attempts, 1)
        resplit = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("Story S1 (Make add() add) failed", resplit)
        self.assertIn("Still no.", resplit)

    def test_a_story_the_loop_guard_stops_is_re_split(self):
        self.h.loop_resets = 0
        self.run_plan([call("write_stories", stories=[FIX_ADD])]
                      + [call("read_file", path="calc.py")] * 3
                      + [call("write_stories", replaces=["S1"], stories=[{**FIX_ADD, "id": "S2"}]),
                         ChatResponse(content="stop", done_reason="stop")])
        resplit = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("the loop guard stopped it", resplit)
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "replaced")

    def test_failing_again_pauses_the_plan(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD]),
            ChatResponse(content="no", done_reason="stop"),
            ChatResponse(content="no", done_reason="stop"),
            call("write_stories", replaces=["S1"], stories=[{**FIX_ADD, "id": "S2"}]),
            ChatResponse(content="still no", done_reason="stop"),
            ChatResponse(content="still no", done_reason="stop")])
        self.assertIsNotNone(self.h.plan)
        self.assertEqual(self.h.plan.story_sets[1].get("S2").status, "failed")
        self.assertTrue(any("failed again" in s for s in self.system_lines()))
        self.assertTrue(any("Plan paused in phase 1/2" in s for s in self.system_lines()))


# A write_stories call Gemma wrote as Python source in a text reply, with the
# stray quote that made it unreadable (2026-10-28 tiny-mud run, story S7's check).
BROKEN_TEXT_CALL = r"""I will replace the failed story.

```python
write_stories(stories=[
    {
        "id": "S2",
        "title": "Make add() add",
        "description": "add() returns 0.",
        "technical_details": "def add(a, b) -> a + b",
        "acceptance": [
            {
                "text": "add(2, 3) → 5",
                "check": 'python3 -c "from calc import add; assert add(2, 3) == \'5\''"
            }
        ]
    }
], replaces=["S1"])
```
"""


class Recovery(Base):
    """What keeps a stories plan going when a model answers in text."""

    def test_the_text_call_error_says_where(self):
        from harness.toolcall_text import _extract_text_tool_calls, text_call_error
        from harness.tools import ToolContext, registry
        tools = registry.offered(ToolContext(role="story-writer"))
        err = text_call_error(BROKEN_TEXT_CALL, tools)
        self.assertIn("write_stories(…) written as text could not be read", err)
        self.assertIn("unterminated string literal", err)
        self.assertIn("\"check\":", err)                          # the line it broke on
        fixed = BROKEN_TEXT_CALL.replace("\\'5\\''\"", "\\'5\\'\"'")
        self.assertEqual(text_call_error(fixed, tools), "")
        self.assertEqual(_extract_text_tool_calls(fixed, tools)[0]["name"], "write_stories")
        self.assertEqual(text_call_error("Let's perform the edit.", tools), "")

    def test_a_story_that_answers_in_text_is_nudged_once(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD]),
            ChatResponse(content="Let's perform the edit.", done_reason="stop"),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "done")
        notes = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                 if m.get("role") == "user" and "Your reply had no tool call" in (m.get("content") or "")]
        self.assertTrue(notes)
        self.assertIn("A story ends only with complete_story", notes[0])

    def test_a_writer_that_writes_its_call_as_text_is_told_why(self):
        self.run_plan([
            ChatResponse(content=BROKEN_TEXT_CALL.replace(', replaces=["S1"]', ""), done_reason="stop"),
            call("write_stories", stories=[FIX_ADD]),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertTrue(self.h.plan.story_sets[1].approved)
        notes = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                 if m.get("role") == "user" and "Your reply had no tool call" in (m.get("content") or "")]
        self.assertIn("could not be read (unterminated string literal", notes[0])
        self.assertIn("Call the write_stories tool itself", notes[0])

    def test_resuming_retries_a_re_split_that_was_never_written(self):
        self.run_plan([
            call("write_stories", stories=[FIX_ADD]),
            ChatResponse(content="no", done_reason="stop"),
            ChatResponse(content="no", done_reason="stop"),            # S1 fails
            ChatResponse(content=BROKEN_TEXT_CALL, done_reason="stop"),
            ChatResponse(content=BROKEN_TEXT_CALL, done_reason="stop")])   # re-split: no stories
        self.assertTrue(any("stopped without stories for phase 1: it replied with text" in s
                            for s in self.system_lines()))
        ScriptedClient.script = [
            call("write_stories", replaces=["S1"], stories=[{**FIX_ADD, "id": "S2"}]),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")]
        self.h.send("continue")
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories], [("S1", "replaced"), ("S2", "done")])
        self.assertFalse(any("cannot go on" in s for s in self.system_lines()))
        briefs = [r["messages"][1]["content"] for r in self.writer_requests()
                  if "Story S1 (Make add() add) failed" in r["messages"][1]["content"]]
        self.assertEqual(len(briefs), 2)                          # after the failure, and on resume
        self.assertIn("send ONLY the replacement stories", briefs[-1])
        self.assertEqual(self.h.plan.steps[0].status, "done")    # and the plan went on

    def test_a_re_split_that_resends_finished_stories_is_accepted(self):
        two = [FIX_ADD, {**ADD_SUB, "id": "S2", "depends_on": ["S1"], "files": ["calc.py"],
                         "acceptance": [criterion("sub(5, 3) → 2", SUB_OK)]}]
        self.run_plan([
            call("write_stories", stories=two),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="no", done_reason="stop"),
            ChatResponse(content="no", done_reason="stop"),            # S2 fails
            call("write_stories", replaces=["S2"],
                 stories=[FIX_ADD, {**two[1], "id": "S3"}]),          # S1 sent again
            call("write_file", path="calc.py", content=CALC_SUB),
            call("complete_story", summary="sub works"),
            ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories],
                         [("S1", "done"), ("S2", "replaced"), ("S3", "done")])
        self.assertEqual(sset.get("S1").delivered, "add works")    # kept as delivered
        # The writer's conversation ends with the accepted call; it is in the archive.
        accepted = [r["message"]["content"] for r in self.h.compacted
                    if r["message"].get("role") == "tool"
                    and "kept as delivered" in (r["message"].get("content") or "")]
        self.assertIn("S1 are done and were kept as delivered", accepted[0])


class Commands(Base):
    def cmd(self, text):
        return handle_command(text, self.h).output

    def test_kind_and_settings(self):
        self.assertIn("stories: the plan's steps are phases", self.cmd("/plan stories"))
        self.assertEqual(self.h.plan_kind, "stories")
        self.assertIn("run without asking", self.cmd("/plan stories approve once"))
        self.assertEqual(session_mod.load_prefs()["stories_approve"], "once")
        self.assertIn("at most 12 turns", self.cmd("/plan stories cap 12"))
        self.assertEqual(session_mod.load_prefs()["stories_cap"], 12)
        self.assertTrue(self.cmd("/plan stories cap 2").startswith("ERROR"))
        self.assertTrue(self.cmd("/plan stories approve sometimes").startswith("ERROR"))

    def test_create_plan_needs_outcomes_and_checks(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        args = {**PLAN_ARGS, "steps": [{"title": "x", "details": "y"}]}
        self.assertIn("needs an 'outcome'", self.h._handle_create_plan(args))

    def test_show_stories(self):
        self.run_plan(list(HAPPY[:3]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertIn("# Stories: phase 1 — Fix add", self.cmd("/plan show stories 1"))
        self.assertIn("no stories yet", self.cmd("/plan show stories 2"))


if __name__ == "__main__":
    unittest.main()