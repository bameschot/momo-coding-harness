"""Stories plans (/plan stories) run end to end: the planner's phases, a story
writer per phase, each story in a fresh context and accepted only when an
independent verifier pass finds its expected behaviour, the phase gate and
report, re-splitting a failed story, and the commands and settings.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import re
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
from harness.plan import Plan, Step, request_commands
from harness.plan_runner import _request_command_note
from harness.story_runner import STORY_NUDGES
from home_tmp import setUpModule, tearDownModule  # noqa: F401

PY = shlex.quote(sys.executable)
CALC_WRONG = "def add(a, b):\n    return 0\n"
CALC_RIGHT = "def add(a, b):\n    return a + b\n"
CALC_SUB = CALC_RIGHT + "\n\ndef sub(a, b):\n    return a - b\n"
REPORT = "## What exists now\ncalc.add(a, b) adds."


def give_up(last="no"):
    """Text replies that fail a story: one per note it gets, then the last."""
    return ([ChatResponse(content="no", done_reason="stop")] * STORY_NUDGES
            + [ChatResponse(content=last, done_reason="stop")])


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


def write_set(*stories, revision=None):
    """A story-writing pass's calls: the outline, one write_story per story
    (one per turn), then finish_stories."""
    outline = [{"id": st["id"], "title": st["title"], "depends_on": st.get("depends_on", [])}
               for st in stories]
    return ([call("plan_stories", outline=outline)]
            + [call("write_story", story=st) for st in stories]
            + [call("finish_stories", **({"phase_revision": revision} if revision else {}))])


def criterion(text, given, value, covers=None):
    """An expected behaviour with one example: `given` is a call of calc's
    public functions, `value` the result it must give."""
    return {"behaviour": text, "examples": [{"given": given, "expect": f"`{value}`"}],
            **({"covers": covers} if covers else {})}


ADD = criterion("add() adds", "add(2, 3)", 5, ["R1"])
SUB = criterion("sub() subtracts", "sub(5, 3)", 2, ["R2"])

PLAN_ARGS = {
    "title": "Calc", "goal": "add() returns 0; also add sub().",
    "interfaces": ["Library: `import calc`"],
    "steps": [
        {"title": "Fix add", "details": "calc.py add()", "outcome": "add(2, 3) == 5",
         "acceptance": [criterion("adding works", "add(1, 1)", 2)], "files": ["calc.py"]},
        {"title": "Add sub", "details": "calc.py sub()", "outcome": "sub(5, 3) == 2",
         "acceptance": [criterion("subtracting works", "sub(3, 1)", 2)], "files": ["calc.py"]}],
    "requirements": [{"id": "R1", "text": "add(2, 3) returns 5", "phase": 1},
                     {"id": "R2", "text": "sub(5, 3) returns 2", "phase": 2}]}

FIX_ADD = {"id": "S1", "title": "Make add() add", "description": "add() returns 0.",
           "technical_details": "def add(a, b) -> a + b", "files": ["calc.py"],
           "provides": ["add(a, b) -> a + b in calc.py"], "acceptance": [ADD]}
ADD_SUB = {"id": "S1", "title": "Add sub()", "description": "Subtract.",
           "technical_details": "def sub(a, b) -> a - b", "files": ["calc.py"],
           "depends_on": ["P1.S1"], "acceptance": [SUB]}

# The verifier's test the fake model writes for an example: it calls calc's
# public function the example gives and compares the result with the value it
# expects, so a wrong add() really fails verification.
TEST_PY = """import sys
sys.path.insert(0, ".")
from calc import *
sys.exit(0 if repr(eval({given!r})) == {value!r} else 1)
"""
_RX_EXPECTATION = re.compile(r"### Expectation (\d+): .*\n- Example: (.+?) → `([^`]+)`\n"
                             r"(?:- Example: .*\n)*- Save its test as `([^`]+)`")


def auto_verify(messages):
    """The fake verifier: first turn, write and save a test per expectation;
    then the verdicts, read from what saving them reported."""
    saved = [m for m in messages if m.get("role") == "tool" and m.get("name") == "save_acceptance_test"]
    found = _RX_EXPECTATION.findall(messages[1]["content"])
    if not saved:
        calls = []
        for num, given, value, name in found:
            path = name.replace("<short-name>.<ext>", "auto.py")
            calls += [ToolCall("write_file", {"path": path,
                                              "content": TEST_PY.format(given=given, value=value)}),
                      ToolCall("save_acceptance_test", {"expectation": int(num), "path": path,
                                                        "command": f"{PY} {path}"})]
        return ChatResponse(content="", tool_calls=calls, done_reason="stop")
    results = [{"expectation": int(num),
                "verdict": "pass" if "it passes" in m["content"] else "fail",
                "evidence": "" if "it passes" in m["content"] else "the call gave another value"}
               for (num, *_), m in zip(found, saved[-len(found):])]
    return ChatResponse(content="", tool_calls=[ToolCall("finish_verification", {"results": results})],
                        done_reason="stop")


class ScriptedClient(LLMClient):
    """Replies with `script`, one per tool-loop turn, then "Done.".  A call
    without tools (the phase report) gets REPORT; the verifier role gets
    `verifier` replies if any are queued, else auto_verify.  Every request is
    kept."""
    provider_name = "fake"
    script: list = []
    verifier: list = []
    requests: list = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        names = [t["function"]["name"] for t in tools]
        ScriptedClient.requests.append({"messages": [dict(m) for m in messages], "tools": names})
        if not tools:
            return ChatResponse(content=REPORT, done_reason="stop")
        if "finish_verification" in names:
            if ScriptedClient.verifier:
                return ScriptedClient.verifier.pop(0)
            return auto_verify(messages)
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
        ScriptedClient.script, ScriptedClient.requests, ScriptedClient.verifier = [], [], []
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.plan_questions = False   # scripts plan without asking; see test_plan_questions
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

    def run_plan(self, script, plan=None, request="make calc work"):
        """Plan mode with a stories plan: create_plan, approval, execution."""
        self.h.set_mode("plan")
        self.assertEqual(self.h.set_plan_kind("stories"), "")
        ScriptedClient.script = [call("create_plan", **(plan or PLAN_ARGS))] + script
        self.h.send(request)

    def story_requests(self):
        """The first request of each story: its fresh context."""
        return [r for r in ScriptedClient.requests
                if "complete_story" in r["tools"] and len(r["messages"]) == 2]

    def verifier_requests(self):
        return [r for r in ScriptedClient.requests
                if "finish_verification" in r["tools"] and len(r["messages"]) == 2]

    def writer_requests(self):
        return [r for r in ScriptedClient.requests
                if "write_story" in r["tools"] and len(r["messages"]) == 2]

    def system_lines(self):
        return [ev.text for ev in self.h.event_queue._backlog
                if isinstance(ev, ChatEvent) and ev.role == "system"]


HAPPY = [
    *write_set(FIX_ADD),                                    # phase 1 writer
    call("write_file", path="calc.py", content=CALC_RIGHT),  # P1 S1
    call("complete_story", summary="add(a, b) returns a + b."),
    *write_set(ADD_SUB),                                    # phase 2 writer
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

    def test_the_request_reaches_the_writer_and_the_report_is_shown(self):
        self.run_plan(list(HAPPY))
        brief = self.writer_requests()[0]["messages"][1]["content"]
        self.assertIn("## The user's request\nmake calc work", brief)
        self.assertIn("Cover every requirement of the request", brief)
        self.assertTrue(any(t.startswith("Phase 1 report:\n" + REPORT) for t in self.system_lines()))
        folder = next((self.wd / ".momo" / "stories").iterdir())
        self.assertIn("## Report\n\n" + REPORT, (folder / "phase-1.md").read_text())
        self.assertIn("## Request\n\nmake calc work", (folder / "plan.md").read_text())

    def test_each_story_and_writer_starts_fresh(self):
        self.run_plan(list(HAPPY))
        stories, writers = self.story_requests(), self.writer_requests()
        self.assertEqual(len(stories), 2)
        self.assertEqual(len(writers), 2)
        brief = stories[0]["messages"][1]["content"]
        self.assertIn("# Story S1 of phase 1: Make add() add", brief)
        self.assertIn("- add() adds\n  Example: add(2, 3) → `5`", brief)
        self.assertIn("- Library: `import calc`", brief)
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
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "done")
        self.assertEqual(s1.facts["files"], ["calc.py"])
        self.assertEqual(s1.facts["checks"], [f"{PY} tests/acceptance/p1_s1_c1_auto.py"])
        report = self.h.plan.steps[0].report
        self.assertTrue(report.startswith(REPORT))
        self.assertIn(f"## Requirements\n- ✓ R1 add(2, 3) returns 5 — P1.S1: "
                      f"`{PY} tests/acceptance/p1_s1_c1_auto.py`", report)

    def test_an_unmet_expectation_goes_back_to_the_implementer(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("complete_story", summary="done?"),               # calc.py still wrong
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="fixed"),
            ChatResponse(content="stop", done_reason="stop")])
        reports = {m["content"] for r in ScriptedClient.requests for m in r["messages"]
                   if m.get("role") == "user" and "[Verifier report" in (m.get("content") or "")}
        self.assertEqual(len(reports), 1)
        report = reports.pop()
        self.assertIn("✗ add() adds\n  example: add(2, 3) → `5`", report)
        self.assertIn(f"run its test: `{PY} tests/acceptance/p1_s1_c1_auto.py`", report)
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual((s1.status, s1.refusals), ("done", 1))
        self.assertTrue(s1.acceptance[0].proven)
        # the implementer went on in its own context, after its complete_story
        impl = [r for r in ScriptedClient.requests if "complete_story" in r["tools"]]
        self.assertTrue(any("[Verifier report" in (m.get("content") or "") for m in impl[-1]["messages"]))
        self.assertEqual(len(self.verifier_requests()), 3)   # S1 twice, then phase 1's gate

    def test_the_verifier_cannot_touch_product_code_or_cheat(self):
        ScriptedClient.verifier = [
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("write_file", path="tests/acceptance/p1_s1_c1_x.py", content="import sys\nsys.exit(0)\n"),
            call("save_acceptance_test", expectation=1, path="tests/acceptance/p1_s1_c1_x.py",
                 command=f"{PY} tests/acceptance/p1_s1_c1_x.py"),
            call("finish_verification", results=[{"expectation": 1, "verdict": "pass"}]),
        ]
        self.run_plan([*write_set(FIX_ADD), call("complete_story", summary="done?"),
                       ChatResponse(content="stop", done_reason="stop")])
        results = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                   if m.get("role") == "tool"]
        self.assertTrue(any("the verifier writes only in tests/acceptance/" in x for x in results))
        self.assertEqual((self.wd / "calc.py").read_text(), CALC_WRONG)
        self.assertTrue(any("does not check the example's expected value `5`" in x for x in results))
        self.assertTrue(any("'pass' needs a saved test that passes — none is saved" in x
                            for x in results))


class Progress(Base):
    """What the web UI's plan timeline shows: plan_view() and the run data."""

    def test_a_run_records_times_turns_and_checks(self):
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertTrue(s1.started_at and s1.finished_at)
        self.assertEqual(s1.turns, 2)                          # write_file, complete_story
        self.assertEqual(s1.acceptance[0].verdict["passed"], True)
        self.assertEqual(self.h.plan.story_sets[1].gate["passed"], True)
        self.assertTrue(any(s.startswith("▶ P1.S1 — Make add() add") for s in self.system_lines()))

    def test_a_refusal_shows_until_the_fix(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("complete_story", summary="done?"),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="fixed"),
            ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.refusals, 1)
        self.assertEqual(s1.acceptance[0].verdict["passed"], True)  # the later pass replaced the ✗

    def test_the_status_line_counts_turns(self):
        seen = []
        orig = self.h._story_tick

        def tick(i):
            orig(i)
            seen.append(self.h._plan_progress())
        self.h._story_tick = tick
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertIn("phase 1/2 · S1 (0/1) · turn 2/40", seen)
        self.assertIn("phase 1/2 · writing stories", seen)

    def test_the_view(self):
        self.run_plan([
            *write_set(FIX_ADD, {**ADD_SUB, "id": "S2", "depends_on": ["S1"]}),
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
        self.assertEqual(s1["criteria"][0]["text"], "add() adds\n  e.g. add(2, 3) → `5`")
        self.assertEqual(s1["criteria"][0]["check"], f"{PY} tests/acceptance/p1_s1_c1_auto.py")
        self.assertEqual(s1["criteria"][0]["result"]["passed"], False)   # calc.py is still wrong
        self.assertEqual(s1["technical_details"], "def add(a, b) -> a + b")
        self.assertEqual(s1["provides"], ["add(a, b) -> a + b in calc.py"])
        self.assertEqual(view["cap"], 40)
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
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertIn({"step": 1, "writing": True, "turns": 1}, seen)
        self.assertIn({"step": 1, "story": "S1", "turns": 2}, seen)

    def test_no_view_without_a_plan(self):
        self.assertIsNone(self.h.plan_view())


class Writer(Base):
    def test_an_invalid_set_goes_back_to_the_writer(self):
        bad = {**FIX_ADD, "acceptance": []}
        self.run_plan([call("plan_stories", outline=[{"id": "S1", "title": "Make add() add"}]),
                       call("write_story", story=bad),
                       call("write_story", story=FIX_ADD),
                       call("finish_stories"),
                       ChatResponse(content="stop", done_reason="stop")])
        refusals = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                    if m.get("role") == "tool" and "needs changes" in (m.get("content") or "")]
        self.assertTrue(refusals)
        self.assertIn("S1 (Make add() add): no acceptance criteria", refusals[0])
        self.assertIn(1, self.h.plan.story_sets)

    def test_feedback_sends_the_writer_back(self):
        self.answers = ["y", "split it smaller"]               # plan yes, stories feedback
        self.run_plan([*write_set(FIX_ADD),
                       call("finish_stories"),                     # the drafts are kept
                       ChatResponse(content="stop", done_reason="stop")])
        feedback = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                    if m.get("role") == "tool" and "User requested changes" in (m.get("content") or "")]
        self.assertIn("split it smaller", feedback[0])
        self.assertTrue(self.h.plan.story_sets[1].approved)

    def test_approve_once_does_not_ask(self):
        self.h.stories_approve = "auto"
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
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").title, "Make add() really add")


class Failure(Base):
    def test_a_failed_story_is_re_split(self):
        self.run_plan([
            *write_set(FIX_ADD),
            *give_up("Still no."),                                          # S1 fails
            *write_set({**FIX_ADD, "id": "S2"}),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories], [("S1", "replaced"), ("S2", "done")])
        self.assertEqual(sset.get("S2").attempts, 1)
        resplit = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("Story S1 (Make add() add) failed", resplit)
        self.assertIn("Still no.", resplit)

    # The story-mud run: S1 wrote the code, ran its check (it passed), then summed
    # up in text instead of calling complete_story, and was failed and re-split.
    def test_a_story_that_ends_in_text_with_passing_checks_is_accepted(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            *[ChatResponse(content="add() now adds.", done_reason="stop")] * (STORY_NUDGES + 1),
            ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "done")
        self.assertEqual(s1.attempts, 0)
        self.assertEqual(s1.delivered, "add() now adds.")
        self.assertIn("calc.py", s1.facts["files"])
        self.assertTrue(any("S1 ended without complete_story; its expectations are verified — "
                            "accepted" in t for t in self.system_lines()))
        self.assertEqual(len(self.writer_requests()), 2)     # phase 2's writer, no re-split

    def test_a_story_that_ends_in_text_with_an_unmet_expectation_fails(self):
        self.run_plan([
            *write_set(FIX_ADD),
            *[ChatResponse(content="add() now adds.", done_reason="stop")] * (STORY_NUDGES + 1),
            *write_set({**FIX_ADD, "id": "S2"}),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "replaced")
        resplit = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("It ended without complete_story, and the verifier found:", resplit)
        self.assertIn("✗ add() adds", resplit)

    def test_a_story_the_loop_guard_stops_is_re_split(self):
        self.h.loop_resets = 0
        self.run_plan(write_set(FIX_ADD)
                      + [call("read_file", path="calc.py")] * 3
                      + write_set({**FIX_ADD, "id": "S2"})
                      + [ChatResponse(content="stop", done_reason="stop")])
        resplit = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("the loop guard stopped it", resplit)
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "replaced")

    def test_failing_again_pauses_the_plan(self):
        self.run_plan([
            *write_set(FIX_ADD),
            *give_up(),
            *write_set({**FIX_ADD, "id": "S2"}),
            *give_up("still no")])
        self.assertIsNotNone(self.h.plan)
        self.assertEqual(self.h.plan.story_sets[1].get("S2").status, "failed")
        self.assertTrue(any("failed again" in s for s in self.system_lines()))
        self.assertTrue(any("Plan paused in phase 1/2" in s for s in self.system_lines()))


DOC_ADD = {"id": "S2", "title": "Document add()", "description": "Add a docstring.",
           "files": ["calc.py"], "acceptance": [criterion("adding zero keeps the number", "add(7, 0)", 7)]}


class Retire(Base):
    """A re-split or a reworked set retires stories with plan_stories' replaces,
    and duplicates of pending stories are refused (the story-mud run ended up
    with the same three stories three times)."""

    def tool_results(self, needle):
        return [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                if m.get("role") == "tool" and needle in (m.get("content") or "")]

    def ids(self):
        return [(s.id, s.status) for s in self.h.plan.story_sets[1].stories]

    def test_a_duplicate_of_a_pending_story_is_refused_and_replaces_retires_it(self):
        fix = {**FIX_ADD, "id": "S4", "title": "Make add() add, smaller"}
        self.run_plan([
            *write_set(FIX_ADD, DOC_ADD),
            *give_up(),                                             # S1 fails: re-split
            *write_set({**DOC_ADD, "id": "S3"}, fix),               # S3 copies pending S2
            call("plan_stories", outline=[{"id": "S3", "title": "Document add()"},
                                          {"id": "S4", "title": fix["title"]}],
                 replaces=["S2"]),
            call("write_story", story={**DOC_ADD, "id": "S3"}),
            call("write_story", story=fix),
            call("finish_stories"),
            call("complete_story", summary="documented"),           # S3
            call("write_file", path="calc.py", content=CALC_RIGHT),  # S4
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")])
        refusal = self.tool_results("S3 duplicates S2")[0]
        self.assertIn("replaces=[S2]", refusal)
        self.assertTrue(self.tool_results("Retired S2 (marked replaced)"))
        self.assertEqual(self.ids(), [("S1", "replaced"), ("S2", "replaced"),
                                      ("S3", "done"), ("S4", "done")])

    def test_feedback_on_a_re_split_does_not_keep_its_drafts(self):
        self.answers = ["y", "y", "call it 'Fix add()'", "y"]
        fix = {**FIX_ADD, "id": "S2"}
        self.run_plan([
            *write_set(FIX_ADD),
            *give_up(),                                             # S1 fails: re-split
            *write_set(fix),                                        # sent back with feedback
            call("plan_stories", outline=[{"id": "S2", "title": "Fix add()"}]),
            call("write_story", story={**fix, "title": "Fix add()"}),
            call("finish_stories"),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertEqual(self.ids(), [("S1", "replaced"), ("S2", "done")])
        self.assertEqual(self.h.plan.story_sets[1].get("S2").title, "Fix add()")
        self.assertIn("User feedback to address: call it 'Fix add()'",
                      self.tool_results("Outline stored (1 story)")[-1])

    def test_replaces_takes_only_pending_stories_of_an_adding_pass(self):
        self.run_plan([
            call("plan_stories", outline=[{"id": "S1", "title": "x"}], replaces=["S1"]),
            *write_set(FIX_ADD),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertTrue(self.tool_results("nothing to retire: this pass writes the phase's whole set"))
        # an unknown or finished story cannot be retired by a later pass
        self.h._writing = {"phase": 1, "amend": {"replaces": []}, "replaces": [],
                           "base": self.h.plan.story_sets[1].stories if self.h.plan else []}
        if self.h.plan:
            self.assertIn("only pending or failed", self.h._take_replaces(self.h._writing, ["S1"]))
            self.assertIn("not a story of this phase", self.h._take_replaces(self.h._writing, ["S9"]))
        self.h._writing = None


class Resume(Base):
    """An adding pass whose draft the user sent back, then stopped: /plan
    resume goes on with that pass, and the phase's finished stories stay."""

    def test_a_re_split_sent_back_and_stopped_resumes_with_the_done_stories(self):
        self.answers = ["y", "y", "make it smaller", "y"]
        wrong = {**DOC_ADD, "acceptance": [criterion("two and two", "add(2, 2)", 5)]}   # cannot pass
        doc = {**DOC_ADD, "id": "S3"}
        self.run_plan([
            *write_set(FIX_ADD, wrong),
            call("write_file", path="calc.py", content=CALC_RIGHT),  # S1
            call("complete_story", summary="add works"),
            *give_up(),                                              # S2 fails: re-split
            *write_set(doc),                                         # sent back with feedback
            *give_up()])                                             # the writer stops
        self.assertIsNotNone(self.h.plan.story_sets[1].amend)
        ScriptedClient.script = [*write_set(doc), call("complete_story", summary="documented"),
                                 ChatResponse(content="stop", done_reason="stop")]
        self.h.send("go on")
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories],
                         [("S1", "done"), ("S2", "replaced"), ("S3", "done")])
        self.assertEqual(sset.get("S1").delivered, "add works")
        brief = self.writer_requests()[-2]["messages"][1]["content"]
        self.assertIn("Story S2 (Document add()) failed", brief)
        self.assertIn("## [x] S1 — Make add() add", brief)
        self.assertNotIn("[ ] S3", brief)                        # the unapproved draft is not shown

    def test_a_fix_story_may_not_depend_on_the_story_it_unblocks(self):
        fix = {**Defects.FIX, "depends_on": ["S1"]}
        self.run_plan([
            *write_set(FIX_ADD),
            call("report_defect", file="calc.py", problem="add() returns 0"),
            *write_set(fix),                                          # refused: a cycle
            call("plan_stories", outline=[{"id": "S2", "title": fix["title"]}]),
            call("write_story", story=Defects.FIX),
            call("finish_stories"),
            call("write_file", path="calc.py", content=CALC_RIGHT),   # S2
            call("complete_story", summary="add() fixed"),
            call("complete_story", summary="add() works now"),        # S1 again
            ChatResponse(content="stop", done_reason="stop")])
        results = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                   if m.get("role") == "tool" and m.get("name") == "finish_stories"]
        self.assertIn("dependency cycle", results[0])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories], [("S1", "done"), ("S2", "done")])
        self.assertEqual(sset.get("S1").depends_on, ["S2"])

    def test_the_writer_drops_test_runs_and_gets_value_advice(self):
        suite = {"behaviour": "the unit tests pass", "covers": ["R1"],
                 "examples": [{"given": "python3 -m unittest", "expect": "exit code `0`"}]}
        vague = {"behaviour": "add() is documented", "examples": [{"given": "help(add)",
                                                                  "expect": "a description"}]}
        story = {**FIX_ADD, "acceptance": [ADD, suite, vague]}
        self.run_plan([*write_set(story), ChatResponse(content="stop", done_reason="stop")],
                      request="make calc work; `python3 -m unittest` must pass")
        stored = next(m["content"] for r in ScriptedClient.requests for m in r["messages"]
                      if m.get("role") == "tool" and m.get("name") == "write_story")
        self.assertIn("Stored S1", stored)
        self.assertIn("Dropped \"the unit tests pass\": it only runs the tests", stored)
        self.assertIn("no exact value in \"a description\"", stored)
        self.assertEqual([c.text for c in self.h.plan.story_sets[1].get("S1").acceptance],
                         ["add() adds", "add() is documented"])

    def test_the_gate_verifier_has_no_not_yet(self):
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        briefs = [r["messages"][1]["content"] for r in self.verifier_requests()]
        story_brief = next(b for b in briefs if b.startswith("# Verify P1.S1"))
        gate_brief = next(b for b in briefs if b.startswith("# Verify phase 1"))
        self.assertIn("give it `not_yet` at once", story_brief)
        self.assertIn("This is the end of the phase", gate_brief)
        self.assertNotIn("give it `not_yet`", gate_brief)

    def test_an_apostrophe_is_not_a_quote(self):
        from harness.stories import Criterion, criterion_problems
        from harness.story_runner import expected_values
        c = Criterion(text="arrivals are announced", examples=[{
            "given": "Ann sends `go north`",
            "expect": "Ann's reply names `Hall` and Bob's next poll has `Ann arrives`"}])
        self.assertEqual(expected_values(c), ["Hall", "Ann arrives"])
        self.assertEqual(expected_values(Criterion(text="x", examples=[
            {"given": "a", "expect": "it says 'done'"}])), ["done"])
        from harness.stories import criterion_notes
        vague = Criterion(text="x", examples=[{"given": "a", "expect": "Ann's and Bob's lists"}])
        self.assertEqual(criterion_problems(vague), [])
        self.assertTrue(any("no exact value" in e for e in criterion_notes(vague)))


class Revision(Base):
    """A story writer's change to the later phases: checked like a written
    phase, asked after the stories, and the requirements go with it."""

    PLAN3 = {**PLAN_ARGS,
             "steps": PLAN_ARGS["steps"] + [
                 {"title": "Add mul", "details": "calc.py mul()", "outcome": "mul(2, 3) == 6",
                  "acceptance": [criterion("multiplying works", "mul(2, 3)", 6)], "files": ["calc.py"]}],
             "requirements": PLAN_ARGS["requirements"] + [{"id": "R3", "text": "mul(2, 3) returns 6",
                                                            "phase": 3}]}
    MERGED = {"title": "Add sub and mul", "details": "calc.py sub() and mul()",
              "outcome": "sub and mul work", "files": ["calc.py"],
              "acceptance": [criterion("subtracting works", "sub(3, 1)", 2),
                             criterion("multiplying works", "mul(2, 3)", 6)]}

    def test_a_revision_is_checked_and_its_requirements_move(self):
        bad = {"reason": "sub and mul belong together", "phases": [{"title": "Add sub and mul"}]}
        good = {"reason": "sub and mul belong together", "phases": [self.MERGED]}
        self.run_plan([*write_set(FIX_ADD, revision=bad),
                       call("finish_stories", phase_revision=good),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")], plan=self.PLAN3)
        refusals = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                    if m.get("role") == "tool" and m.get("name") == "finish_stories"]
        self.assertIn("phase_revision needs changes:\n- phase 2: missing details, outcome, acceptance",
                      refusals[0])
        # the stories were asked first, then the revision
        self.assertTrue(self.questions[1].startswith("Run these 1 story for phase 1?"))
        self.assertIn("proposes changing the phases after phase 1", self.questions[2])
        plan = self.h.plan
        self.assertEqual([s.title for s in plan.steps], ["Fix add", "Add sub and mul"])
        self.assertEqual({r["id"]: r["phase"] for r in plan.requirements}, {"R1": 1, "R2": 2, "R3": 2})
        self.assertTrue(any("R3 now belongs to phase 2" in t for t in self.system_lines()))

    def test_a_declined_revision_keeps_the_phases(self):
        good = {"reason": "merge", "phases": [self.MERGED]}
        self.answers = ["y", "y", "n"]
        self.run_plan([*write_set(FIX_ADD, revision=good),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")], plan=self.PLAN3)
        self.assertEqual(len(self.h.plan.steps), 3)
        self.assertEqual(self.h.plan.requirements[2]["phase"], 3)


RUN2_REQUEST = ("Build a small MUD in Python 3, stdlib only. … Commands: look, go/n/s/e/w, say, who, "
                "take, drop, inventory, help, quit. … Unit tests with unittest for the command "
                "parser and game logic; python -m unittest must pass.")


class Requirements(Base):
    """Run 2's phase report said n/s/e/w worked; nothing had checked them.  Each
    requirement of the request now needs a check that proves it."""

    def tool_results(self, needle):
        return [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                if m.get("role") == "tool" and needle in (m.get("content") or "")]

    def test_create_plan_needs_requirements(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._turn_user = {"role": "user", "content": "make calc work"}
        args = {k: v for k, v in PLAN_ARGS.items() if k != "requirements"}
        self.assertIn("needs 'requirements'", self.h._handle_create_plan(args))
        bad = {**PLAN_ARGS, "requirements": [{"id": "R1", "text": "x", "phase": 3}]}
        self.assertIn("R1: phase must be 1–2", self.h._handle_create_plan(bad))
        bundled = {**PLAN_ARGS, "requirements": [
            {"id": "R1", "text": "Commands: look, go/n/s/e/w, say, who, take, drop", "phase": 1}]}
        self.assertIn("R1 lists several things", self.h._handle_create_plan(bundled))

    def test_an_uncovered_requirement_refuses_the_stories(self):
        uncovered = {**FIX_ADD, "acceptance": [criterion("add() adds", "add(2, 3)", 5)]}
        self.run_plan([*write_set(uncovered),
                       call("write_story", story=FIX_ADD), call("finish_stories"),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        [refusal] = set(self.tool_results("is not covered"))
        self.assertIn("R1 (add(2, 3) returns 5) is not covered", refusal)
        brief = self.writer_requests()[0]["messages"][1]["content"]
        self.assertIn("## Requirements this phase must cover\n- R1: add(2, 3) returns 5", brief)
        self.assertNotIn("R2", brief.split("## Phases")[0])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "done")

    def test_a_criterion_cannot_cover_everything(self):
        self.h.plan = Plan(title="t", request="r", kind="stories", steps=[Step(title="a")],
                           requirements=[{"id": f"R{i}", "text": f"cmd {i}", "phase": 1}
                                         for i in range(1, 6)])
        from harness.stories import Criterion, Story, StorySet
        blanket = Story(id="S1", title="t", acceptance=[
            Criterion("parse 'go north'", [f"R{i}" for i in range(1, 6)],
                      examples=[{"given": "`go north`", "expect": "`ok`"}])])
        errors = self.h._coverage_problems(1, StorySet(phase=1, stories=[blanket]))
        self.assertTrue(any("covers 5 requirements" in e for e in errors))

    def test_the_last_gate_fails_on_a_requirement_without_a_passing_check(self):
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        passed, report = self.h._phase_checks(self.h.plan.steps[0], sset, last=True)
        self.assertFalse(passed)
        self.assertIn("not proven by a passing acceptance test:\n- R2: sub(5, 3) returns 2", report)
        self.assertEqual(self.h._phase_checks(self.h.plan.steps[0], sset),
                         (True, "1 expectation(s) verified\n2 acceptance test(s) still pass"))

    def test_a_test_suite_requirement_is_proven_by_the_test_command(self):
        # run 7: "Unit tests for parser and logic pass" — the last gate's command proves it
        self.h.plan = Plan(title="t", request="make calc work; python3 -m unittest must pass.",
                           kind="stories", steps=[Step(title="a"), Step(title="b")],
                           requirements=[{"id": "R1", "text": "add(2, 3) returns 5", "phase": 1},
                                         {"id": "R2", "text": "Unit tests for calc pass", "phase": 1}])
        self.assertEqual(self.h._suite_proofs(), {"R2": "python3 -m unittest"})
        self.assertEqual([r["id"] for r in self.h._due_requirements(1)], ["R1"])
        self.assertEqual([r["id"] for r in self.h._requirement_gaps(3)], ["R1"])
        self.assertIn("- · R2 Unit tests for calc pass — `python3 -m unittest`, run at the last "
                      "phase's gate", self.h._requirement_table(1))
        brief = self.h._writer_brief(0)
        self.assertIn("R2 (Unit tests for calc pass) is proven by `python3 -m unittest`", brief)
        self.assertNotIn("- R2:", brief)

    def test_a_test_suite_requirement_without_a_named_command(self):
        # "with unit tests using unittest" names no command: the default one proves it
        request = "A stdlib-only calc library, with unit tests using unittest."
        self.h.plan = Plan(title="t", request=request, kind="stories", interfaces=["Library: `import calc`"],
                           steps=[Step(title="a")],
                           requirements=[{"id": "R1", "text": "Unit tests for calc pass", "phase": 1}])
        self.assertEqual(self.h._suite_proofs(), {"R1": "python3 -m unittest discover -s tests"})
        self.assertEqual(self.h._due_requirements(1), [])
        from harness.plan import default_test_command
        self.assertEqual(default_test_command("a node tool", ["CLI: `node cli.js`"]), "npm test")
        self.assertEqual(default_test_command("a Go service", ["Service: `go run .`"]), "")

    def test_the_last_gate_runs_the_requests_command(self):
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.h.plan.request = "make calc work; `python3 -m unittest` must pass."
        passed, report = self.h._phase_checks(self.h.plan.steps[0], self.h.plan.story_sets[1],
                                              last=True)
        self.assertFalse(passed)
        self.assertIn("`python3 -m unittest` failed", report)
        self.assertIn("no tests ran", report)


class Defects(Base):
    """Run 2's P2.S9: its test found a bug in an earlier story's engine.py; it
    took fixing it to be out of its scope and used up its 30 turns."""

    FIX = {**FIX_ADD, "id": "S2", "title": "Fix add() in calc.py",
           "acceptance": [criterion("one and one make two", "add(1, 1)", 2, ["R1"])]}

    def test_a_reported_defect_gets_a_fix_story_and_the_story_runs_again(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("report_defect", file="calc.py", problem="add() returns 0",
                 evidence="AssertionError"),
            *write_set(self.FIX),                                   # the fix pass
            call("write_file", path="calc.py", content=CALC_RIGHT),  # S2
            call("complete_story", summary="add() fixed"),
            call("complete_story", summary="add() works now"),       # S1 again
            ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        s1 = sset.get("S1")
        self.assertEqual((s1.status, s1.attempts), ("done", 0))
        self.assertIn("S2", s1.depends_on)
        self.assertEqual(s1.defect, "calc.py: add() returns 0\nEvidence:\nAssertionError")
        fix_brief = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("Story S1 (Make add() add) is blocked by a defect", fix_brief)
        self.assertIn("write ONE story, S2", fix_brief)
        self.assertTrue(any("S1 is blocked by a defect" in t for t in self.system_lines()))

    def test_a_second_defect_fails_the_story(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("report_defect", problem="add() returns 0"),
            *write_set(self.FIX),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="fixed"),
            call("report_defect", problem="still broken"),            # S1 again
            ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "failed")
        self.assertIn("second defect", s1.failure)

    def test_a_story_is_told_when_its_turns_run_low(self):
        self.h.stories_cap = 6
        self.run_plan([
            *write_set(FIX_ADD),
            *[call("read_file", path=f"x{i}.py") for i in range(3)],
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="done"),
            ChatResponse(content="stop", done_reason="stop")])
        notes = {m["content"] for r in ScriptedClient.requests for m in r["messages"]
                 if m.get("role") == "tool" and "turns left for this story" in (m.get("content") or "")}
        self.assertEqual(len(notes), 1)
        self.assertIn("report_defect", notes.pop())


class OwnTests(Base):
    """The test files a story writes are run when it submits (run 2's P2.S1
    put its tests where nothing ran them), and a stray root test_server.py is
    refused."""

    FAILING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('no')\n"
    PASSING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"

    def results(self):
        return [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                if m.get("role") == "tool" and m.get("name") == "complete_story"]

    def test_a_new_test_file_is_run(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("write_file", path="tests/test_calc_more.py", content=self.FAILING),
            call("complete_story", summary="done"),
            call("write_file", path="tests/test_calc_more.py", content=self.PASSING),
            call("complete_story", summary="done"),
            ChatResponse(content="stop", done_reason="stop")])
        refusal = next(r for r in self.results() if "story not complete" in r)
        self.assertIn("the story's own tests in tests/test_calc_more.py pass", refusal)
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "done")
        self.assertIn("python3 -m unittest tests/test_calc_more.py", s1.facts["checks"])

    def test_a_stray_test_file_in_the_root_is_refused(self):
        self.run_plan([
            *write_set(FIX_ADD),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("write_file", path="test_server.py", content=self.PASSING),
            call("complete_story", summary="done"),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertIn("test_server.py: a test file outside the story's files", self.results()[0])



class Run3(Base):
    """What run 3 ran into: a server left running after its story, an
    expectation no code could meet, and a failed story's edits."""

    def tool_results(self, name):
        seen, out = set(), []
        for r in ScriptedClient.requests:
            for m in r["messages"]:
                if m.get("role") == "tool" and m.get("name") == name and id(m) not in seen:
                    if m["content"] not in out:
                        out.append(m["content"])
        return out

    def test_a_storys_background_command_is_stopped_when_it_ends(self):
        self.run_plan([*write_set(FIX_ADD),
                       call("run_command", command="sleep 30", background=True),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        self.assertTrue(any("Stopped the background command P1.S1 left running" in t
                            for t in self.system_lines()), self.system_lines())
        self.assertEqual(self.h.run_store.running(), [])

    def test_a_wrong_expectation_gets_a_corrected_story(self):
        wrong = {**FIX_ADD, "acceptance": [criterion("add() adds", "add(2, 3)", 6, ["R1"])]}
        doc = {**DOC_ADD, "depends_on": ["S1"]}
        fixed = {**FIX_ADD, "id": "S3", "title": "Make add() add (corrected)"}
        self.run_plan([
            *write_set(wrong, doc),
            call("report_defect", problem="2 + 3 is 5, not 6", criterion="add() adds"),
            *write_set(fixed),                                       # the correction pass
            call("write_file", path="calc.py", content=CALC_RIGHT),   # S3
            call("complete_story", summary="add works"),
            call("complete_story", summary="documented"),            # S2
            ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories],
                         [("S1", "replaced"), ("S2", "done"), ("S3", "done")])
        self.assertEqual(sset.get("S2").depends_on, ["S3"])
        self.assertEqual(sset.get("S3").attempts, 0)
        brief = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("reports that one of its acceptance criteria is wrong", brief)
        self.assertIn("The acceptance criterion \"add() adds\" is wrong: 2 + 3 is 5, not 6", brief)

    def test_the_resplit_brief_names_what_the_failed_story_changed(self):
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_WRONG + "# wip\n"),
                       *give_up(),
                       *write_set({**FIX_ADD, "id": "S2"}),
                       ChatResponse(content="stop", done_reason="stop")])
        brief = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("Its changes are still in the code: calc.py.", brief)
        self.assertIn("undo what is wrong", brief)


class Verifier(Base):
    """The independent verifier pass: verdicts held against saved tests, an
    unclear expectation corrected by the writer, the gate's regression run."""

    def test_an_unclear_expectation_is_deferred_to_the_gate(self):
        # it says nothing against the work: the story is not rewritten
        ScriptedClient.verifier = [call("finish_verification", results=[
            {"expectation": 1, "verdict": "unclear",
             "evidence": "the story asks for add() but the example calls plus()"}])]
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status, s.attempts) for s in sset.stories], [("S1", "done", 0)])
        self.assertIn("1 of them deferred by stories", sset.gate["output"])
        self.assertTrue(sset.get("S1").acceptance[0].proven)
        self.assertEqual(len(self.writer_requests()), 2)     # phase 2's writer: no rewrite

    def test_an_unclear_expectation_at_the_gate_is_restated_and_retired(self):
        unclear = {"verdict": "unclear", "evidence": "the example calls plus()"}
        ScriptedClient.verifier = [
            call("finish_verification", results=[{"expectation": 1, **unclear}]),    # S1
            call("finish_verification", results=[{"expectation": k, **unclear}       # the gate
                                                 for k in (1, 2)])]
        restated = {**FIX_ADD, "id": "S2", "title": "Restate add()",
                    "acceptance": [criterion("two and three make five", "add(2, 3)", 5, ["R1"])]}
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       call("plan_stories", outline=[{"id": "S2", "title": restated["title"]}]),
                       call("write_story", story=restated),
                       call("finish_stories", retire=[
                           {"expectation": "phase 1.1", "why": "restated in S2"},
                           {"expectation": "P1.S1.1", "why": "restated in S2"}]),
                       call("complete_story", summary="nothing to change"),
                       ChatResponse(content="stop", done_reason="stop")])
        fix = self.writer_requests()[1]["messages"][1]["content"]
        self.assertIn("\"adding works\" (phase 1.1) cannot be verified as written", fix)
        self.assertIn("Restate each as an expectation of a new story", fix)
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories], [("S1", "done"), ("S2", "done")])
        self.assertTrue(sset.get("S1").acceptance[0].retired)
        self.assertTrue(sset.gate["passed"])

    # Run 4: the verifier of the engine story ran out of turns (the server it
    # had to test through came in a later story), and the harness had a good
    # story rewritten twice as "unclear".
    def test_a_verifier_that_gets_nowhere_defers_to_the_gate(self):
        idle = [ChatResponse(content="Looking around.", done_reason="stop")] * (STORY_NUDGES + 1)
        ScriptedClient.verifier = idle * 2            # two passes, no test saved
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "done")
        self.assertTrue(any("1 deferred to the phase gate" in t for t in self.system_lines()))
        self.assertTrue(any("(second pass)" in t for t in self.system_lines()))
        self.assertTrue(s1.acceptance[0].proven)             # the gate verified it
        self.assertIn("2 expectation(s) verified, 1 of them deferred by stories",
                      self.h.plan.story_sets[1].gate["output"])
        self.assertEqual(len(self.writer_requests()), 2)     # phase 2's writer: no rewrite

    def test_not_yet_defers_and_the_gate_insists(self):
        not_yet = call("finish_verification", results=[
            {"expectation": 1, "verdict": "not_yet", "evidence": "the command line comes in S2"}])
        gate_not_yet = call("finish_verification", results=[
            {"expectation": k, "verdict": "not_yet", "evidence": "the command line comes in S2"}
            for k in (1, 2)])                                  # the phase's own, then S1's
        ScriptedClient.verifier = [not_yet, gate_not_yet]
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual(s1.status, "done")
        self.assertTrue(any("P1.S1: expectations 1 deferred to the phase gate" in t
                            for t in self.system_lines()))
        gate = self.h.plan.story_sets[1].gate
        self.assertFalse(gate["passed"])
        self.assertIn("found no way to exercise it through the product's interfaces: the command "
                      "line comes in S2", gate["output"])

    def test_the_gate_reruns_earlier_acceptance_tests(self):
        broken = CALC_SUB.replace("return a + b", "return a * b")      # phase 2 breaks add()
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       *write_set(ADD_SUB),
                       call("write_file", path="calc.py", content=broken),
                       call("complete_story", summary="sub added"),
                       ChatResponse(content="stop", done_reason="stop")])
        gate = self.h.plan.story_sets[2].gate
        self.assertFalse(gate["passed"])
        fix = self.writer_requests()[-1]["messages"][1]["content"]
        # every broken one, with the id finish_stories' retire takes
        self.assertIn("An acceptance test that passed before fails now (P1.S1.1: add() adds)", fix)
        self.assertIn("An acceptance test that passed before fails now (phase 1.1: adding works)", fix)
        self.assertIn("tests/acceptance/p1_s1_c1_auto.py", fix)
        self.assertIn("retire it with finish_stories' `retire`", fix)

    def test_the_phase_expectations_are_verified_at_the_gate(self):
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        phase = self.h.plan.steps[0].acceptance[0]
        self.assertTrue(phase.proven)
        self.assertEqual(phase.test["path"], "tests/acceptance/phase_1_c1_auto.py")
        self.assertTrue((self.wd / "tests/acceptance/phase_1_c1_auto.py").exists())
        self.assertTrue(any("🔎 phase 1: expectations all met." in t for t in self.system_lines()))

    def test_create_plan_wants_interfaces_and_expectations(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        no_ui = {k: v for k, v in PLAN_ARGS.items() if k != "interfaces"}
        self.assertIn("name how the product is used in 'interfaces'", self.h._handle_create_plan(no_ui))
        checks_only = {**PLAN_ARGS, "steps": [{**PLAN_ARGS["steps"][0], "acceptance": [],
                                               "checks": ["true"]}, PLAN_ARGS["steps"][1]]}
        self.assertIn("phase 1: has checks, details, files, outcome, title — missing acceptance",
                      self.h._handle_create_plan(checks_only))
        # run 5: a phase that arrived as {"acceptance": …} alone
        lost = {**PLAN_ARGS, "steps": PLAN_ARGS["steps"] + [{"acceptance": PLAN_ARGS["steps"][0]["acceptance"]}]}
        self.assertIn("phase 3: has acceptance — missing title, details, outcome",
                      self.h._handle_create_plan(lost))

    def test_a_story_with_check_commands_is_refused(self):
        self.run_plan([*write_set({**FIX_ADD, "acceptance": [{"text": "adds", "check": "true",
                                                              "covers": ["R1"]}]}),
                       ChatResponse(content="stop", done_reason="stop")])
        results = {m["content"] for r in ScriptedClient.requests for m in r["messages"]
                   if m.get("role") == "tool" and m.get("name") == "write_story"}
        self.assertTrue(any("acceptance criteria are expected behaviour with examples, not "
                            "commands" in x for x in results))


BROKEN_ADD = CALC_SUB.replace("return a + b", "return a * b")      # phase 2 changes add()
REGRESSION = [*write_set(FIX_ADD),
              call("write_file", path="calc.py", content=CALC_RIGHT),
              call("complete_story", summary="add works"),
              *write_set(ADD_SUB),
              call("write_file", path="calc.py", content=BROKEN_ADD),
              call("complete_story", summary="sub added")]
KEEP_SUB = {"id": "S2", "title": "Keep sub()", "description": "sub() stays.",
            "technical_details": "nothing to change", "files": ["calc.py"],
            "depends_on": ["S1"], "acceptance": [criterion("sub still subtracts", "sub(9, 4)", 5, ["R2"])]}


class Run6Fixes(Base):
    """What run 6 ran into: a message at a pause that reached no one, a test of
    replaced behaviour that could not be retired, and a gate verifier pass
    with too many expectations whose running out sent the writer off."""

    def tool_results(self, name):
        return [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                if m.get("role") == "tool" and m.get("name") == name]

    def test_a_message_at_a_pause_reaches_the_next_writer(self):
        self.run_plan(list(REGRESSION))                  # the fix pass writes nothing: paused
        self.assertTrue(any(t.startswith("Plan paused in phase 2/2") and "send a message" in t
                            for t in self.system_lines()))
        before = len(self.writer_requests())
        self.h.send("P1.S1.1 tested a placeholder: retire it")
        briefs = [r["messages"][1]["content"] for r in self.writer_requests()[before:]]
        self.assertIn("## The user's message (sent when resuming the plan)\n"
                      "P1.S1.1 tested a placeholder: retire it", briefs[0])
        self.assertEqual(self.h._resume_note, "")
        self.assertFalse(any("## The user's message" in b for b in briefs[1:]))

    def test_a_replaced_behaviour_is_retired(self):
        retire = [{"expectation": "P1.S1.1", "why": "phase 2 made add() multiply on purpose"},
                  {"expectation": "phase 1.1", "why": "the same"}]
        self.run_plan(list(REGRESSION) + [
            call("plan_stories", outline=[{"id": "S2", "title": KEEP_SUB["title"], "depends_on": ["S1"]}]),
            call("write_story", story=KEEP_SUB),
            call("finish_stories", retire=[{"expectation": "P9.S1.1", "why": "x"}]),
            call("finish_stories", retire=retire),
            call("complete_story", summary="nothing to change"),
            ChatResponse(content="stop", done_reason="stop")])
        refused = self.tool_results("finish_stories")[0]
        self.assertIn("P9.S1.1 is not an expectation that can be retired", refused)
        self.assertIn("P1.S1.1", refused)                            # the ids it can retire
        p1 = self.h.plan.story_sets[1].get("S1").acceptance[0]
        self.assertEqual(p1.retired["by"], "phase 2")
        self.assertTrue(self.h.plan.steps[0].acceptance[0].retired)
        self.assertTrue(any("retires these earlier acceptance tests" in t for t in self.system_lines()))
        self.assertNotIn("P1.S1.1", [cid for cid, _c in self.h._saved_tests(2)])
        folder = next((self.wd / ".momo" / "stories").iterdir())
        self.assertIn("Retired (phase 2): phase 2 made add() multiply on purpose",
                      (folder / "phase-1.md").read_text())
        # R1 was proven only by the retired tests: the last gate wants new proof
        gate = self.h.plan.story_sets[2].gate
        self.assertFalse(gate["passed"])
        self.assertIn("R1: add(2, 3) returns 5", gate["output"])
        self.assertNotIn("passed before fails now", gate["output"])

    def test_a_retired_line_survives_the_story_file(self):
        from harness.stories import Criterion, criterion_line
        c = Criterion(text="x")
        self.assertTrue(criterion_line(c, "Retired (phase 2): the engine replaced it"))
        self.assertEqual(c.retired, {"by": "phase 2", "why": "the engine replaced it"})
        self.assertEqual(Criterion.from_dict(c.to_dict()).retired, c.retired)

    def test_scratch_files_are_questioned_once(self):
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("write_file", path="verify_api.py", content="print(1)\n"),
                       call("complete_story", summary="add works"),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        first = self.tool_results("complete_story")[0]
        self.assertIn("look like scratch work and are not among the story's files: verify_api.py", first)
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "done")   # kept on the second call

    def test_the_gate_verifies_in_batches(self):
        many = [criterion(f"adds {k}", f"add({k}, 1)", k + 1) for k in range(5)]
        plan = {**PLAN_ARGS, "steps": [{**PLAN_ARGS["steps"][0], "acceptance": many},
                                       PLAN_ARGS["steps"][1]]}
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")], plan=plan)
        self.assertTrue(any("🔎 Verifying phase 1 — 5 expectations in 2 batches" in t
                            for t in self.system_lines()))
        gate = [r for r in self.verifier_requests()
                if r["messages"][1]["content"].startswith("# Verify phase 1")]
        self.assertEqual([len(_RX_EXPECTATION.findall(r["messages"][1]["content"])) for r in gate],
                         [4, 1])
        self.assertTrue(self.h.plan.story_sets[1].gate["passed"])

    def test_a_gate_verifier_that_runs_out_verifies_again_and_writes_no_stories(self):
        idle = [ChatResponse(content="Looking around.", done_reason="stop")] * (STORY_NUDGES + 1)
        # the story's two passes (deferred); at the gate the batch's one and one
        # alone for each, twice: no story can help a verifier that ran out
        ScriptedClient.verifier = idle * 8
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")])
        lines = self.system_lines()
        self.assertTrue(any("(alone" in t for t in lines))
        self.assertEqual(sum("2 not verified (the verifier ran out of turns)" in t for t in lines), 2)
        self.assertTrue(any("the verifier ran out of turns twice" in t for t in lines))
        self.assertEqual(len(self.writer_requests()), 1)     # no fix pass
        self.assertEqual(self.h._verify_bonus, 0)

    def test_a_regression_of_replaced_behaviour_needs_only_retire(self):
        retire = [{"expectation": "P1.S1.1", "why": "phase 2 made add() multiply on purpose"},
                  {"expectation": "phase 1.1", "why": "the same"}]
        self.run_plan(list(REGRESSION) + [call("finish_stories", retire=retire),
                                          ChatResponse(content="stop", done_reason="stop")])
        self.assertTrue(any(l.startswith("Retired P1.S1.1") for l in self.system_lines()))
        self.assertEqual([s.id for s in self.h.plan.story_sets[2].stories], ["S1"])
        self.assertTrue(self.h.plan.story_sets[1].get("S1").acceptance[0].retired)


class Run4Fixes(Base):
    """What run 4 ran into: verifier tests of internals, finished work failed at
    the turn cap, test suites that find no tests, engine-first plans and lists
    folded into one requirement, and verifier passes with nothing to test."""

    def verifying(self, interfaces):
        """A finished phase 1, and the harness set up as if verifying P1.S1."""
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.h.plan.interfaces = interfaces
        c = self.h.plan.story_sets[1].get("S1").acceptance[0]
        self.h.story_role = "story-verifier"
        self.h._verifying = {"criteria": [c], "todo": [0], "saved": {}, "results": None}
        test = self.wd / "tests/acceptance/p1_s1_c1_x.py"
        test.write_text("import sys\nsys.path.insert(0, '.')\nfrom calc import add\n"
                        "sys.exit(0 if add(2, 3) == 5 else 1)\n")
        return {"expectation": 1, "path": "tests/acceptance/p1_s1_c1_x.py",
                "command": f"{PY} tests/acceptance/p1_s1_c1_x.py"}

    def test_a_verifier_test_may_not_import_the_project(self):
        args = self.verifying(["CLI: `python3 calc.py add <a> <b>`"])
        self.assertIn("the test imports calc", self.h._handle_save_acceptance_test(args))
        self.h.plan.interfaces = ["Library: `import calc`"]
        self.assertIn("it passes", self.h._handle_save_acceptance_test(args))

    def test_unsaved_verifier_files_are_removed(self):
        ScriptedClient.verifier = [call("write_file", path="tests/acceptance/p1_s1_c1_draft.py",
                                        content="print('draft')\n")]
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertFalse((self.wd / "tests/acceptance/p1_s1_c1_draft.py").exists())
        self.assertTrue((self.wd / "tests/acceptance/p1_s1_c1_auto.py").exists())

    def test_finished_work_at_the_turn_cap_is_verified(self):
        # run 4b's P2.S9: code and test done, out of turns before complete_story
        self.h.stories_cap = 3
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       *[call("read_file", path="calc.py")] * 4,
                       ChatResponse(content="stop", done_reason="stop")])
        s1 = self.h.plan.story_sets[1].get("S1")
        self.assertEqual((s1.status, s1.attempts), ("done", 0))
        self.assertTrue(any("S1 reached the turn limit; its expectations are verified — accepted" in t
                            for t in self.system_lines()))

    def test_the_users_test_command_must_find_the_tests(self):
        test = ("import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        self.run_plan([*write_set(FIX_ADD),
                       call("write_file", path="calc.py", content=CALC_RIGHT),
                       call("write_file", path="tests/test_calc.py", content=test),
                       call("complete_story", summary="add works"),
                       call("write_file", path="tests/__init__.py", content=""),
                       call("complete_story", summary="add works"),
                       ChatResponse(content="stop", done_reason="stop")],
                      request="make calc work; `python3 -m unittest` must pass")
        brief = self.story_requests()[0]["messages"][1]["content"]
        self.assertIn("## How the project's tests run\nThe last phase's gate runs `python3 -m unittest`", brief)
        refusals = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                    if m.get("role") == "tool" and "finds no tests" in (m.get("content") or "")]
        self.assertTrue(refusals)
        self.assertIn("tests/ has an __init__.py", refusals[0])
        self.assertEqual(self.h.plan.story_sets[1].get("S1").status, "done")

    def test_an_import_error_of_a_project_module_gets_a_hint(self):
        self.assertIn("calc is the project's own module",
                      self.h._import_hint("ModuleNotFoundError: No module named 'calc'"))
        self.assertEqual(self.h._import_hint("ModuleNotFoundError: No module named 'requests'"), "")

    def test_phase_one_is_a_slice_and_request_lists_are_split(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        engine_first = {**PLAN_ARGS, "interfaces": ["CLI: `python3 calc.py add <a> <b>`"]}
        self.assertIn("phase 1: make it a thin end-to-end slice", self.h._handle_create_plan(engine_first))
        self.h._plan_first_request = "make calc work. Functions: add, sub, mul."
        err = self.h._handle_create_plan(dict(PLAN_ARGS))
        self.assertIn("the request lists add, sub, mul; give each its own requirement — none names mul",
                      err)

    def test_no_verifier_pass_while_the_interface_does_not_exist(self):
        cli = criterion("adding works", "`python3 calc_cli.py add 1 1`", 2)
        plan = {**PLAN_ARGS, "interfaces": ["CLI: `python3 calc_cli.py add <a> <b>`"],
                "steps": [{**PLAN_ARGS["steps"][0], "acceptance": [cli]}, PLAN_ARGS["steps"][1]]}
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")], plan=plan)
        self.assertTrue(any("P1.S1: 1 expectation deferred to the phase gate — calc_cli.py does not "
                            "exist yet" in t for t in self.system_lines()))
        story_passes = [r for r in self.verifier_requests() if "Verify P1.S1" in r["messages"][1]["content"]]
        self.assertEqual(story_passes, [])

    def test_the_verifier_cap_scales_with_the_expectations(self):
        from harness.story_runner import DEFAULT_STORIES_CAP, verify_cap
        self.assertEqual([verify_cap(n) for n in (1, 2, 3, 8)], [13, 18, 23, 40])
        self.assertEqual(DEFAULT_STORIES_CAP, 40)


FRAME = {k: v for k, v in PLAN_ARGS.items() if k != "steps"}
OUTLINE = [{"title": st["title"], "outcome": st["outcome"]} for st in PLAN_ARGS["steps"]]
PHASE1, PHASE2 = ({k: v for k, v in st.items() if k != "title"} for st in PLAN_ARGS["steps"])
VAGUE = {**PHASE2, "acceptance": [{"behaviour": "sub works",
                                   "examples": [{"given": "sub two numbers", "expect": "the difference"}]}]}
CODED = {**PHASE2, "acceptance": [{"behaviour": "sub works",                 # refused: code
                                   "examples": [{"given": "assert sub(3, 1) == 2", "expect": "`2`"}]}]}
SLICE = {**PHASE1, "acceptance": [{"behaviour": "adding works",              # not through an interface
                                   "examples": [{"given": "the numbers 1 and 1", "expect": "`2`"}]}]}


class PhaseByPhase(Base):
    """Run 5's planner could not resend one large plan after refusals: 17
    create_plan calls, none accepted.  A stories plan is now an outline, one
    phase per turn, and finish_plan; and a refused call restarts the planner
    from the harness's summary, in a cleared context."""

    def plan_in_steps(self, script, answers=()):
        self.h.set_mode("plan")
        self.assertEqual(self.h.set_plan_kind("stories"), "")
        self.answers = list(answers)
        ScriptedClient.script = script
        self.h.send("make calc work")

    def test_outline_then_phases_then_finish(self):
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **PHASE1),
                            call("write_phase", phase=2, **PHASE2),
                            call("finish_plan"),
                            *HAPPY])
        self.assertIsNone(self.h.plan)                         # approved and run to the end
        self.assertTrue(any("Plan complete: Calc" in t for t in self.system_lines()))
        results = list(dict.fromkeys(m["content"] for r in ScriptedClient.requests for m in r["messages"]
                                     if m.get("role") == "tool" and m.get("name") in ("create_plan", "write_phase")))
        self.assertIn("Outline stored (2 phases). Next: write phase 1 — Fix add", results[0])
        self.assertIn("Stored phase 1 (Fix add). Next: write phase 2 — Add sub", results[1])

    def test_a_text_reply_mid_plan_gets_a_note(self):
        # run 7: the outline was stored, then "I'm ready to start with Phase 1." ended the turn
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            ChatResponse(content="I'm ready to start with Phase 1.", done_reason="stop"),
                            call("write_phase", phase=1, **PHASE1),
                            call("write_phase", phase=2, **PHASE2),
                            call("finish_plan"),
                            *HAPPY])
        self.assertIsNone(self.h.plan)                         # approved and run to the end
        self.assertTrue(any("asked the planner to go on" in t for t in self.system_lines()))
        notes = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                 if m.get("role") == "user" and "The plan is not finished" in (m.get("content") or "")]
        self.assertIn("Next: write phase 1 — Fix add", notes[0])

    def test_a_question_mid_plan_ends_the_turn(self):
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            ChatResponse(content="Should sub() accept floats?", done_reason="stop")])
        self.assertFalse(any("asked the planner to go on" in t for t in self.system_lines()))
        self.assertIsNotNone(self.h._plan_draft)

    def test_a_re_sent_outline_keeps_the_written_phases(self):
        # run 6c: after each restart the planner re-sent its outline with reworded
        # titles and lost the phases it had written
        reworded = [{**o, "title": f"Phase {i}: {o['title']}"} for i, o in enumerate(OUTLINE, 1)]
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **PHASE1),
                            call("create_plan", **FRAME, steps=reworded),
                            ChatResponse(content="stop", done_reason="stop")])
        self.assertEqual(sorted(self.h._plan_draft["phases"]), [1])
        self.assertIn("Next: write phase 2", self.h._plan_next())

    def test_one_phase_per_turn(self):
        two = ChatResponse(content="", done_reason="stop", tool_calls=[
            ToolCall("write_phase", {"phase": 1, **PHASE1}), ToolCall("write_phase", {"phase": 2, **PHASE2})])
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE), two,
                            ChatResponse(content="stop", done_reason="stop")])
        results = [m["content"] for m in ScriptedClient.requests[-1]["messages"] if m.get("role") == "tool"]
        self.assertTrue(any(r.startswith("ERROR: one phase per turn — phase 2 was not stored") for r in results))
        self.assertEqual(sorted(self.h._plan_draft["phases"]), [1])

    def test_a_first_refusal_is_answered_in_place(self):
        # run 7 cleared the context for every refusal, one missing requirement included
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **PHASE1),
                            call("write_phase", phase=2, **CODED),           # refused
                            call("write_phase", phase=2, **PHASE2),
                            call("finish_plan"),
                            ChatResponse(content="stop", done_reason="stop")])
        self.assertFalse(any("context cleared" in t for t in self.system_lines()))
        self.assertTrue(any(m.get("role") == "tool" and m["content"].startswith(
            "ERROR: the plan needs these changes") for r in ScriptedClient.requests
            for m in r["messages"]))                                      # left in the context
        self.assertIsNotNone(self.h.plan)

    def test_a_second_refusal_in_a_row_restarts_from_a_summary(self):
        choice = call("ask_choice", question="Where should sub() live?",
                      options=["In calc.py (recommended)", "In a new module"])
        self.plan_in_steps([choice,
                            call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **PHASE1),
                            call("write_phase", phase=2, **CODED),           # refused in place
                            call("write_phase", phase=2, **CODED),           # refused: restart
                            call("write_phase", phase=2, **PHASE2),
                            call("finish_plan"),
                            ChatResponse(content="stop", done_reason="stop")], answers=["1"])
        after = next(r for r in ScriptedClient.requests
                     if len(r["messages"]) == 2 and "# Planning, attempt 2" in (r["messages"][1].get("content") or ""))
        summary = after["messages"][1]["content"]
        self.assertEqual([m["role"] for m in after["messages"]], ["system", "user"])
        for part in ("## The user's request\nmake calc work",
                     "## Decided with the user\n- Where should sub() live? → option 1",
                     "- ✓ 1. Fix add — kept", "- ✗ 2. Add sub — refused, write it again",
                     "## Why it was refused\n- phase 2: \"sub works\" contains code (assert…)",
                     "Next: write phase 2 — Add sub — with write_phase",
                     "do NOT call create_plan again"):
            self.assertIn(part, summary)
        self.assertTrue(any("Plan refused — context cleared; the planner restarts from a summary "
                            "(attempt 2)" in t for t in self.system_lines()))
        self.assertTrue(any(r.get("by") == "plan-retry" for r in self.h.compacted))   # /export full
        self.assertIsNotNone(self.h.plan)                                           # then approved
        self.assertEqual(self.h.plan.steps[0].title, "Fix add")

    def test_the_ask_first_gate_does_not_restart(self):
        self.h.plan_questions = True
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            ChatResponse(content="stop", done_reason="stop")])
        self.assertFalse(any("context cleared" in t for t in self.system_lines()))

    def test_planning_pauses_after_too_many_refusals(self):
        from harness.plan_runner import PLAN_MAX_RESTARTS
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE)]
                           + [call("write_phase", phase=2, **{**PHASE2, "acceptance": []})]
                           * (PLAN_MAX_RESTARTS + 2))
        self.assertTrue(any(f"Planning paused after {PLAN_MAX_RESTARTS} refused attempts" in t
                            for t in self.system_lines()))
        self.assertIn("## Why it was refused", (self.wd / ".momo/plan-draft.md").read_text())
        self.assertIsNone(self.h.plan)

    def test_feedback_at_approval_keeps_the_written_phases(self):
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **PHASE1),
                            call("write_phase", phase=2, **PHASE2),
                            call("finish_plan"),
                            call("write_phase", phase=2, **{**PHASE2, "details": "calc.py sub(), with tests"}),
                            call("finish_plan"),
                            ChatResponse(content="stop", done_reason="stop")],
                           answers=["make phase 2 test sub() too", "n"])
        results = list(dict.fromkeys(m["content"] for r in ScriptedClient.requests for m in r["messages"]
                                     if m.get("role") == "tool" and m.get("name") == "finish_plan"))
        self.assertIn("Rewrite the phases it concerns with write_phase", results[0])
        self.assertEqual(self.h.plan.steps[1].details, "calc.py sub(), with tests")
        self.assertEqual(self.h.plan.steps[0].title, "Fix add")

    def test_a_heuristic_check_gives_way_after_two_refusals(self):
        # run 6c: four of six refusals asked for a backticked value the verifier could test anyway
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work"
        self.assertIn("Outline stored", self.h._handle_create_plan({**FRAME, "steps": OUTLINE}))
        for _ in range(2):
            self.assertIn("make it a thin end-to-end slice",
                          self.h._handle_write_phase({"phase": 1, **SLICE}))
        self.assertIn("Stored phase 1", self.h._handle_write_phase({"phase": 1, **SLICE}))
        self.assertTrue(any("accepted although the check still says" in t for t in self.system_lines()))

    def test_a_heuristic_counts_only_refusals_in_a_row(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work"
        self.h._handle_create_plan({**FRAME, "steps": OUTLINE})
        slice_rule = "make it a thin end-to-end slice"
        refusal = [f"phase 1: {slice_rule} the user can try"]
        self.assertEqual(self.h._yield_heuristics(refusal, {"phase 1"}), refusal)
        self.assertEqual(self.h._yield_heuristics(refusal, {"phase 1"}), refusal)
        self.assertEqual(self.h._yield_heuristics([], {"phase 1"}), [])            # fixed: starts over
        self.assertEqual(self.h._yield_heuristics(refusal, {"phase 2"}), refusal)  # another scope
        self.assertEqual(self.h._yield_heuristics(refusal, {"phase 1"}), refusal)  # not the third time

    def test_reordered_phases_keep_their_own_text(self):
        swapped = list(reversed(OUTLINE))
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **PHASE1),
                            call("write_phase", phase=2, **PHASE2),
                            call("create_plan", **FRAME, steps=swapped),   # all kept: submitted
                            ChatResponse(content="stop", done_reason="stop")], answers=["n"])
        steps = self.h.plan.steps
        self.assertEqual([(s.title, s.details) for s in steps],
                         [("Add sub", PHASE2["details"]), ("Fix add", PHASE1["details"])])

    def test_a_refused_requirement_says_to_send_create_plan_again(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work"
        self.h._handle_create_plan({**FRAME, "steps": OUTLINE})
        self.h._handle_write_phase({"phase": 1, **PHASE1})
        self.h._handle_create_plan({**FRAME, "requirements": [], "steps": OUTLINE})
        self.assertNotIn("do NOT call create_plan", self.h._plan_summary("a stories plan needs 'requirements'"))
        self.h._handle_create_plan({**FRAME, "steps": OUTLINE})
        self.assertIn("do NOT call create_plan", self.h._plan_summary("phase 2: missing acceptance"))

    def test_unit_tests_using_unittest_need_no_expectation(self):
        # run 7's deadlock without a command in the request: the default command proves it
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work, with unit tests using unittest."
        reqs = FRAME["requirements"] + [{"id": "R9", "text": "Unit tests for calc pass", "phase": 2}]
        out = self.h._handle_create_plan({**FRAME, "requirements": reqs, "steps": OUTLINE})
        self.assertIn("R9 is proven by `python3 -m unittest discover -s tests`", out)
        suite = {"behaviour": "all unit tests pass", "covers": ["R9"],
                 "examples": [{"given": "python3 -m unittest", "expect": "exit code `0`"}]}
        out = self.h._handle_write_phase({"phase": 2, **{**PHASE2, "acceptance":
                                                         PHASE2["acceptance"] + [suite]}})
        self.assertIn("Stored phase 2", out)
        self.assertIn("Dropped phase 2's expectation", out)

    def test_a_file_server_request_gets_no_requirement_of_the_harness(self):
        # the confinement requirement is the planner's business, as the prompt says
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work; server.py serves one HTML page via http.server"
        out = self.h._handle_create_plan({**FRAME, "steps": OUTLINE})
        self.assertIn("Outline stored", out)
        self.assertEqual(self.h._plan_draft["frame"]["requirements"], FRAME["requirements"])

    def test_a_test_suite_requirement_needs_no_expectation(self):
        # run 7: "Unit tests for parser and logic pass" was accepted, then each
        # expectation for it refused, at phase 1 and again at phase 3
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work; python -m unittest must pass"
        reqs = FRAME["requirements"] + [{"id": "R9", "text": "Unit tests for calc pass", "phase": 2}]
        out = self.h._handle_create_plan({**FRAME, "requirements": reqs, "steps": OUTLINE})
        self.assertIn("R9 is proven by `python -m unittest`, which the harness runs at the last "
                      "phase's gate: give it no expected behaviour", out)
        suite = {"behaviour": "all unit tests pass", "covers": ["R9"],
                 "examples": [{"given": "python -m unittest", "expect": "exit code `0`"}]}
        out = self.h._handle_write_phase({"phase": 2, **{**PHASE2, "acceptance":
                                                         PHASE2["acceptance"] + [suite]}})
        self.assertIn("Stored phase 2", out)
        self.assertIn("Dropped phase 2's expectation \"all unit tests pass\"", out)
        self.assertEqual([c["text"] for c in self.h._plan_draft["phases"][2]["acceptance"]],
                         ["subtracting works"])
        # alone it is still refused: the phase needs a behaviour the user sees
        self.h._plan_draft["phase_turn"] = None                      # a new turn
        out = self.h._handle_write_phase({"phase": 1, **{**PHASE1, "acceptance": [suite]}})
        self.assertIn("running the tests is not a behaviour", out)

    def test_a_refused_first_phase_says_the_outline_is_kept(self):
        # run 7: phase 1 refused, nothing kept yet, and the planner re-sent its outline
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **{**PHASE1, "acceptance": []}),
                            call("write_phase", phase=1, **{**PHASE1, "acceptance": []}),
                            ChatResponse(content="stop", done_reason="stop")])
        summary = next(m["content"] for r in ScriptedClient.requests for m in r["messages"]
                       if "# Planning, attempt 2" in (m.get("content") or ""))
        self.assertIn("do NOT call create_plan again", summary)

    def test_a_vague_value_is_advice_not_a_refusal(self):
        # run 6c: four of six refusals asked for a backticked value the verifier could test anyway
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work"
        self.h._handle_create_plan({**FRAME, "steps": OUTLINE})
        out = self.h._handle_write_phase({"phase": 2, **VAGUE})
        self.assertIn("Stored phase 2", out)
        self.assertIn("no exact value in \"the difference\"", out)

    def start_outline(self, request="make calc work"):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = request
        self.assertIn("Outline stored", self.h._handle_create_plan({**FRAME, "steps": OUTLINE}))

    def test_a_write_phase_without_its_number_takes_the_due_phase(self):
        # run 8b: after two cut-off replies the planner sent write_phase six times with no `phase`
        self.start_outline()
        out = self.h._handle_write_phase({**PHASE2, "title": "Add sub"})       # named by its title
        self.assertIn("Took this as phase 2 (Add sub). Stored phase 2", out)
        self.h._plan_draft["phase_turn"] = None                               # a new turn
        out = self.h._handle_write_phase(dict(PHASE1))                         # the first not written
        self.assertIn("Took this as phase 1 (Fix add). Stored phase 1", out)
        self.h._plan_draft["phase_turn"] = None
        self.assertIn("'phase' is the phase's number",
                      self.h._handle_write_phase({**PHASE1, "title": "Something else"}))

    def test_a_numberless_fragment_never_restarts_the_planner(self):
        self.start_outline()
        fragment = {"acceptance": PHASE2["acceptance"]}                       # no number, no details
        for _ in range(2):
            out = self.h._handle_write_phase(fragment)
            self.assertIn("missing details", out)
            self.assertIn("(Took this as phase 1 (Fix add).)", out)
            self.h._plan_draft["phase_turn"] = None
        self.assertEqual(self.h._plan_restart, "")

    def test_cut_off_advice_for_a_phase_is_to_split_the_outline(self):
        from harness.settle import retry_advice
        self.assertIn("split it in the outline", retry_advice("write_phase", ["write_phase"]))
        self.assertIn("plan_stories", retry_advice("write_story", ["write_story"]))
        self.assertIn("write each phase with write_phase",
                      retry_advice("create_plan", ["create_plan", "write_phase"]))
        self.assertIn("several calls", retry_advice("create_plan", ["create_plan"]))  # a one-call plan

    def test_a_phase_with_template_tokens_is_not_stored(self):
        # run 8 stored phase 3 titled `Multiplayer & Social Features"}<tool_call|>…`
        leaked = {**PHASE1, "title": 'Fix add"}<tool_call|>)<tool_call|><|channel>thought'}
        self.plan_in_steps([call("create_plan", **FRAME, steps=OUTLINE),
                            call("write_phase", phase=1, **leaked),
                            ChatResponse(content="stop", done_reason="stop")])
        results = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                   if m.get("role") == "tool" and m.get("name") == "write_phase"]
        self.assertIn("'title' argument ran into chat-template tokens", results[0])
        self.assertEqual(self.h._plan_draft["phases"], {})

    def test_a_frame_only_create_plan_keeps_the_outline(self):
        # run 8: a frame-only create_plan after a refusal wiped the outline and phase 1
        self.start_outline()
        self.h._handle_write_phase({"phase": 1, **PHASE1})
        out = self.h._handle_create_plan({k: v for k, v in FRAME.items()})
        self.assertIn("the outline of 2 phases is kept", out)
        self.assertEqual(sorted(self.h._plan_draft["phases"]), [1])
        # a refused one changes nothing either
        out = self.h._handle_create_plan({**FRAME, "requirements": [], "steps": OUTLINE[:1]})
        self.assertIn("Your earlier outline (2 phases) and the 1 phase(s) written are kept", out)
        self.assertEqual((len(self.h._plan_draft["outline"]), sorted(self.h._plan_draft["phases"])),
                         (2, [1]))
        self.h._plan_draft["phase_turn"] = None
        self.h._handle_write_phase({"phase": 2, **PHASE2})
        self.answers = ["n"]                                                  # keep it for later
        self.assertIn("keep the plan for later", self.h._handle_finish_plan({}))

    def test_finish_plan_without_an_outline(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        self.h._plan_first_request = "make calc work"
        self.h._handle_create_plan({k: v for k, v in FRAME.items()})        # refused: no phases
        self.assertIn("there is no outline yet: call create_plan", self.h._handle_finish_plan({}))

    def test_every_problem_in_one_refusal(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        self.h._plan_asked = 1
        bad = {**FRAME, "interfaces": [], "steps": [{**PLAN_ARGS["steps"][0], "acceptance": []},
                                                    {**PLAN_ARGS["steps"][1], **CODED}]}
        err = self.h._handle_create_plan(bad)
        for part in ("name how the product is used in 'interfaces'", "phase 1: has ",
                     "missing acceptance", "phase 2: \"sub works\" contains code",
                     "A valid phase looks like this:"):
            self.assertIn(part, err)

class PhaseChecks(unittest.TestCase):
    def test_the_requests_own_command(self):
        self.assertEqual(request_commands(RUN2_REQUEST), ["python -m unittest"])
        self.assertEqual(request_commands("tests with pytest; `go test ./...` passes"), ["go test ./..."])
        from harness.stories import Criterion
        plan = Plan(title="MUD", request=RUN2_REQUEST, steps=[Step(title="a")])
        self.assertIn("`python -m unittest` must pass", _request_command_note(plan))
        plan.steps[0].acceptance.append(Criterion("the tests pass", examples=[
            {"given": "python -m unittest", "expect": "exit code `0`"}]))
        self.assertEqual(_request_command_note(plan), "")

    def test_requirements_round_trip(self):
        plan = Plan(title="MUD", request="r", steps=[Step(title="a"), Step(title="b")],
                    requirements=[{"id": "R1", "text": "look shows the room", "phase": 1},
                                  {"id": "R2", "text": "n moves north", "phase": 2}])
        md = plan.to_markdown()
        self.assertIn("## Requirements\n\n- R1 (phase 1): look shows the room\n", md)
        back = Plan.from_markdown(md)
        self.assertEqual(back.requirements, plan.requirements)
        self.assertEqual([s.title for s in back.steps], ["a", "b"])
        self.assertEqual(Plan.from_dict(plan.to_dict()).requirements, plan.requirements)


class OneStoryAtATime(Base):
    """The writer outlines first, then writes one story per turn, and may
    rewrite an earlier story or the outline before it finishes."""

    TWO = [FIX_ADD, {**ADD_SUB, "id": "S2", "depends_on": ["S1"]}]
    OUTLINE = [{"id": "S1", "title": "Make add() add"},
               {"id": "S2", "title": "Add sub()", "depends_on": ["S1"]}]
    STOP = [ChatResponse(content="I give up.", done_reason="stop"),
            ChatResponse(content="stop", done_reason="stop")]

    def tool_results(self, needle):
        return [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                if m.get("role") == "tool" and needle in (m.get("content") or "")]

    def test_a_second_story_in_the_same_turn_is_refused(self):
        both = ChatResponse(content="", done_reason="stop", tool_calls=[
            ToolCall(name="write_story", arguments={"story": self.TWO[0]}),
            ToolCall(name="write_story", arguments={"story": self.TWO[1]})])
        self.run_plan([call("plan_stories", outline=self.OUTLINE), both,
                       call("write_story", story=self.TWO[1]), call("finish_stories")]
                      + self.STOP)
        self.assertIn("one story per turn — S2 was not stored", self.tool_results("one story per turn")[0])
        self.assertEqual([s.id for s in self.h.plan.story_sets[1].stories], ["S1", "S2"])

    def test_a_story_before_the_outline_is_refused(self):
        self.run_plan([call("write_story", story=FIX_ADD), *write_set(FIX_ADD)] + self.STOP)
        self.assertTrue(self.tool_results("outline the phase first"))
        self.assertIn(1, self.h.plan.story_sets)

    def test_finishing_with_a_story_missing_names_it(self):
        self.run_plan([call("plan_stories", outline=self.OUTLINE),
                       call("write_story", story=FIX_ADD), call("finish_stories"),
                       call("write_story", story=self.TWO[1]), call("finish_stories")] + self.STOP)
        refusal = self.tool_results("not every story is written")[0]
        self.assertIn("S2 still needs write_story", refusal)
        self.assertIn("Next: write S2 — Add sub() (depends on S1)", refusal)

    def test_each_result_names_the_next_story(self):
        self.run_plan(write_set(*self.TWO) + self.STOP)
        self.assertIn("Next: write S1 — Make add() add", self.tool_results("Outline stored (2 stories)")[0])
        self.assertIn("Next: write S2 — Add sub()", self.tool_results("Stored S1 (1/2 written)")[0])
        self.assertIn("call finish_stories", self.tool_results("Stored S2 (2/2 written)")[0])
        lines = self.system_lines()
        self.assertIn("✎ P1.S1 drafted — Make add() add (1/2)", lines)
        self.assertIn("✎ P1.S2 drafted — Add sub() (2/2)", lines)

    def test_an_earlier_story_can_be_rewritten(self):
        self.run_plan([call("plan_stories", outline=self.OUTLINE),
                       call("write_story", story=self.TWO[0]),
                       call("write_story", story=self.TWO[1]),
                       call("write_story", story={**FIX_ADD, "title": "Make add() add, for sub()"}),
                       call("finish_stories")] + self.STOP)
        self.assertTrue(self.tool_results("Replaced the draft of S1 (2/2 written)"))
        self.assertEqual(self.h.plan.story_sets[1].get("S1").title, "Make add() add, for sub()")
        self.assertIn("✎ P1.S1 rewritten — Make add() add, for sub() (2/2)", self.system_lines())

    # The story-mud run: the outline said "S2 depends on S1", the stories came
    # without dependencies or titles, and ran untitled and unordered.
    def test_a_story_without_title_or_dependencies_keeps_the_outlines(self):
        bare = {k: v for k, v in self.TWO[1].items() if k not in ("title", "depends_on")}
        self.run_plan([call("plan_stories", outline=self.OUTLINE),
                       call("write_story", story=self.TWO[0]),
                       call("write_story", story=bare),
                       call("finish_stories")] + self.STOP)
        s2 = self.h.plan.story_sets[1].get("S2")
        self.assertEqual((s2.title, s2.depends_on), ("Add sub()", ["S1"]))
        self.assertIn("Kept the outline's title and dependencies (S1) for S2",
                      self.tool_results("Stored S2")[0])

    def test_a_new_outline_drops_the_drafts_left_out(self):
        self.run_plan([call("plan_stories", outline=self.OUTLINE),
                       call("write_story", story=self.TWO[0]),
                       call("write_story", story=self.TWO[1]),
                       call("plan_stories", outline=self.OUTLINE[:1]),
                       call("finish_stories")] + self.STOP)
        self.assertTrue(self.tool_results("Dropped your unsaved drafts of S2"))
        self.assertEqual([s.id for s in self.h.plan.story_sets[1].stories], ["S1"])

    def test_a_story_outside_the_outline_is_added_to_it(self):
        self.run_plan([call("plan_stories", outline=self.OUTLINE[:1]),
                       call("write_story", story=self.TWO[0]),
                       call("write_story", story=self.TWO[1]),
                       call("finish_stories")] + self.STOP)
        self.assertTrue(self.tool_results("Stored S2 (2/2 written)"))
        self.assertEqual([s.id for s in self.h.plan.story_sets[1].stories], ["S1", "S2"])

    def test_a_bad_outline_is_refused(self):
        cyclic = [{"id": "S1", "title": "a", "depends_on": ["S2"]},
                  {"id": "S2", "title": "b", "depends_on": ["S1"]}]
        self.run_plan([call("plan_stories", outline=cyclic), *write_set(FIX_ADD)] + self.STOP)
        refusal = self.tool_results("the outline needs changes")[0]
        self.assertIn("dependency cycle", refusal)


# A write_stories call Gemma wrote as Python source in a text reply, with the
# stray quote that made it unreadable (2026-10-28 tiny-mud run, story S7's
# check), as the write_story call it would be now.
BROKEN_TEXT_CALL = r"""I will write the story.

```python
write_story(story={
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
})
```
"""


# A write_stories call Gemma wrote as JSON in a text reply after leaked ChatML
# turn markers, with the stray `1,` that made it unreadable (2026-10-28
# tiny-mud run), as the write_story call it would be now.
BROKEN_JSON_CALL = """I'll now call `write_story` with this story.<|im_end|>
<|im_start|>assistant
```json
{
    "name": "write_story",
    "arguments": {
        "story": {
            1,
            "id": "S3",
            "title": "Implement WebSocket Client for Integration Tests"
        }
    }
}
```
"""


class Recovery(Base):
    """What keeps a stories plan going when a model answers in text."""

    def test_the_text_call_error_reads_a_json_call(self):
        from harness.toolcall_text import _extract_text_tool_calls, text_call_error
        from harness.tools import ToolContext, registry
        tools = registry.offered(ToolContext(role="story-writer"))
        self.assertEqual(_extract_text_tool_calls(BROKEN_JSON_CALL, tools), [])
        err = text_call_error(BROKEN_JSON_CALL, tools)
        self.assertIn("write_story call written as JSON could not be read", err)
        self.assertIn("Expecting property name", err)
        self.assertIn("line 5 of the call: 1,", err)
        fixed = BROKEN_JSON_CALL.replace("            1,\n", "")
        self.assertEqual(text_call_error(fixed, tools), "")
        self.assertEqual(_extract_text_tool_calls(fixed, tools)[0]["name"], "write_story")

    def test_the_text_call_error_says_where(self):
        from harness.toolcall_text import _extract_text_tool_calls, text_call_error
        from harness.tools import ToolContext, registry
        tools = registry.offered(ToolContext(role="story-writer"))
        err = text_call_error(BROKEN_TEXT_CALL, tools)
        self.assertIn("write_story(…) written as text could not be read", err)
        self.assertIn("unterminated string literal", err)
        self.assertIn("\"check\":", err)                          # the line it broke on
        fixed = BROKEN_TEXT_CALL.replace("\\'5\\''\"", "\\'5\\'\"'")
        self.assertEqual(text_call_error(fixed, tools), "")
        self.assertEqual(_extract_text_tool_calls(fixed, tools)[0]["name"], "write_story")
        self.assertEqual(text_call_error("Let's perform the edit.", tools), "")

    def test_a_story_that_answers_in_text_is_nudged_once(self):
        self.run_plan([
            *write_set(FIX_ADD),
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
            ChatResponse(content=BROKEN_TEXT_CALL, done_reason="stop"),
            *write_set(FIX_ADD),
            ChatResponse(content="stop", done_reason="stop")])
        self.assertTrue(self.h.plan.story_sets[1].approved)
        notes = [m["content"] for r in ScriptedClient.requests for m in r["messages"]
                 if m.get("role") == "user" and "Your reply had no tool call" in (m.get("content") or "")]
        self.assertIn("could not be read (unterminated string literal", notes[0])
        self.assertIn("Call the tool itself now", notes[0])
        self.assertIn("outline the stories with plan_stories", notes[0])

    def test_resuming_retries_a_re_split_that_was_never_written(self):
        self.run_plan([
            *write_set(FIX_ADD),
            *give_up(),                                                 # S1 fails
            *[ChatResponse(content=BROKEN_TEXT_CALL, done_reason="stop")]
            * (STORY_NUDGES + 1)])                                      # re-split: no stories
        self.assertTrue(any("stopped without stories for phase 1: it replied with text" in s
                            for s in self.system_lines()))
        ScriptedClient.script = [
            *write_set({**FIX_ADD, "id": "S2"}),
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
        self.assertIn("write ONLY the replacement stories", briefs[-1])
        self.assertEqual(self.h.plan.steps[0].status, "done")    # and the plan went on

    def test_a_re_split_that_resends_finished_stories_is_accepted(self):
        two = [FIX_ADD, {**ADD_SUB, "id": "S2", "depends_on": ["S1"], "files": ["calc.py"],
                         "acceptance": [criterion("sub() subtracts", "sub(5, 3)", 2)]}]
        self.run_plan([
            *write_set(*two),
            call("write_file", path="calc.py", content=CALC_RIGHT),
            call("complete_story", summary="add works"),
            *give_up(),                                                 # S2 fails
            *write_set(FIX_ADD, {**two[1], "id": "S3"}),            # S1 sent again
            call("write_file", path="calc.py", content=CALC_SUB),
            call("complete_story", summary="sub works"),
            ChatResponse(content="stop", done_reason="stop")])
        sset = self.h.plan.story_sets[1]
        self.assertEqual([(s.id, s.status) for s in sset.stories],
                         [("S1", "done"), ("S2", "replaced"), ("S3", "done")])
        self.assertEqual(sset.get("S1").delivered, "add works")    # kept as delivered
        # The writer's outline dropped S1; that conversation is in the archive.
        accepted = [r["message"]["content"] for r in self.h.compacted
                    if r["message"].get("role") == "tool"
                    and "kept as delivered" in (r["message"].get("content") or "")]
        self.assertIn("S1 is done and kept as delivered", accepted[0])


class Commands(Base):
    def cmd(self, text):
        return handle_command(text, self.h).output

    def test_kind_and_settings(self):
        self.assertIn("stories: the plan's steps are phases", self.cmd("/plan stories"))
        self.assertEqual(self.h.plan_kind, "stories")
        self.assertIn("run without asking", self.cmd("/plan stories approve once"))
        self.assertEqual(session_mod.load_prefs()["stories_approve"], "auto")   # once is auto
        self.assertIn("at most 12 turns", self.cmd("/plan stories cap 12"))
        self.assertEqual(session_mod.load_prefs()["stories_cap"], 12)
        self.assertTrue(self.cmd("/plan stories cap 2").startswith("ERROR"))
        self.assertTrue(self.cmd("/plan stories approve sometimes").startswith("ERROR"))

    def test_create_plan_needs_outcomes_and_checks(self):
        self.h.set_mode("plan")
        self.h.set_plan_kind("stories")
        args = {**PLAN_ARGS, "steps": [{"title": "x", "details": "y"}]}
        self.assertIn("phase 1: has details, title — missing outcome, acceptance",
                      self.h._handle_create_plan(args))

    def test_show_stories(self):
        self.run_plan(list(HAPPY[:5]) + [ChatResponse(content="stop", done_reason="stop")])
        self.assertIn("# Stories: phase 1 — Fix add", self.cmd("/plan show stories 1"))
        self.assertIn("no stories yet", self.cmd("/plan show stories 2"))


if __name__ == "__main__":
    unittest.main()