"""TDD plans: tests are written first (skipped, tagged), and every stage ends
at a gate the harness checks itself — the tag markers, then the test command.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness import session as session_mod
from harness import plan_runner, tdd
from harness.commands import handle as handle_command
from harness.events import ChatEvent, DiffEvent, ToolCallEvent, ToolResultEvent
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.plan import LEGACY_PLAN_FILENAME, PLAN_FILENAME, Plan, Step

PY = shlex.quote(sys.executable)
TEST_CMD = f"{PY} -m unittest discover -q -s tests -t ."

CALC_WRONG = "def add(a, b):\n    return 0\n"
CALC_RIGHT = "def add(a, b):\n    return a + b\n"
BASELINE = ("import unittest\n\n\nclass Baseline(unittest.TestCase):\n"
            "    def test_true(self):\n        self.assertTrue(True)\n")


def calc_tests(step_skip: bool, acceptance_skip: bool) -> str:
    def skip(on, tag):
        return f'    @unittest.skip("{tag}")\n' if on else ""
    return ("import unittest\n\nfrom calc import add\n\n\nclass Add(unittest.TestCase):\n"
            + skip(step_skip, "tdd-step-1")
            + "    def test_adds(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
            + skip(acceptance_skip, "tdd-acceptance")
            + "    def test_negative(self):\n        self.assertEqual(add(-1, 1), 0)\n")


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


def write(path, content):
    return call("write_file", path=path, content=content)


CREATE_ARGS = {
    "title": "Make add() add", "goal": "add() returns 0; unittest, skip with @unittest.skip.",
    "test_command": TEST_CMD,
    "acceptance": ["add(-1, 1) → 0"],
    "steps": [{"title": "Fix add()", "details": "In calc.py add(), return a + b.",
               "files": ["calc.py"], "tests": ["add(2, 3) → 5"]}],
}


class ScriptedClient(LLMClient):
    provider_name = "fake"
    script: list = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        self.seen_tools = [t["function"]["name"] for t in tools]
        if ScriptedClient.script:
            return ScriptedClient.script.pop(0)
        return ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        (self.wd / "tests").mkdir()
        (self.wd / "tests" / "__init__.py").write_text("")
        (self.wd / "tests" / "test_baseline.py").write_text(BASELINE)
        (self.wd / "calc.py").write_text(CALC_WRONG)
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "fake")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.stream = False
        self.h._ask_user = lambda q: "y"
        self.sub = self.h.event_queue.subscribe(replay=False)

    def tearDown(self):
        ScriptedClient.script = []
        hmod.make_client = self._orig
        self.sub.close()
        self.h.logger.close()
        self.tmp.cleanup()

    def events(self):
        out = []
        while True:
            try:
                out.append(self.sub.get_nowait())
            except Exception:
                return out

    def tdd_plan(self) -> Plan:
        plan = Plan(title="t", steps=self.h._parse_steps(CREATE_ARGS["steps"]))
        self.h.plan_kind = "tdd"
        self.assertEqual(self.h._make_tdd_plan(plan, CREATE_ARGS), "")
        return plan


class PlanModel(unittest.TestCase):
    def test_normal_plan_markdown_unchanged(self):
        plan = Plan(title="T", goal="G", steps=[Step(title="A", details="d", files=["x.py"])])
        self.assertEqual(plan.to_markdown(),
                         "# Plan: T\n\n## Goal\n\nG\n\n## Steps\n\n- [ ] 1. **A**\n  d\n"
                         "  Files: x.py\n\n<!-- Legend: [ ] pending · [~] in progress · [x] done "
                         "· [-] skipped. Edit steps freely before approving; this file is deleted "
                         "when the plan completes. -->\n")
        self.assertNotIn("kind", plan.to_dict())
        self.assertNotIn("tag", plan.to_dict()["steps"][0])

    def test_tdd_round_trips(self):
        plan = Plan(title="T", goal="G", kind="tdd", test_command="make test",
                    step_test_command="make test FILES='{files}'", steps=[
            Step(title="Write tests", stage="tests", details="all skipped"),
            Step(title="A", details="do a", tests=["x → 1", "y → 2"], tag="tdd-step-1",
                 status="done", note="ok"),
            Step(title="Acc", stage="acceptance", tag="tdd-acceptance", tests=["z → 3"])])
        for back in (Plan.from_markdown(plan.to_markdown()), Plan.from_dict(plan.to_dict())):
            self.assertEqual(back, plan)

    def test_hand_edit_of_expectations_is_read(self):
        md = Plan(title="T", kind="tdd", test_command="pytest", steps=[
            Step(title="A", tests=["x → 1"], tag="tdd-step-1")]).to_markdown()
        md = md.replace("  Expect: x → 1", "  Expect: x → 1\n  Expect: w → 0")
        self.assertEqual(Plan.from_markdown(md).steps[0].tests, ["x → 1", "w → 0"])

    def test_revise_keeps_tags_and_stages(self):
        plan = Plan(title="T", kind="tdd", test_command="t", steps=[
            Step(title="Write tests", stage="tests", status="done"),
            Step(title="A", tag="tdd-step-1", status="done"),
            Step(title="B", tag="tdd-step-2"),
            Step(title="C", tag="tdd-step-3"),
            Step(title="Acc", stage="acceptance", tag="tdd-acceptance")])
        plan.replace_remaining([Step(title="New"), Step(title="c"), Step(title="Acc")])
        self.assertEqual([(s.title, s.tag) for s in plan.steps],
                         [("Write tests", ""), ("A", "tdd-step-1"), ("New", "tdd-step-4"),
                          ("c", "tdd-step-3"), ("Acc", "tdd-acceptance")])


class Gate(_Base):
    def test_find_tags_skips_plan_file_and_momo_dir(self):
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(True, True))
        (self.wd / ".momo").mkdir()
        (self.wd / PLAN_FILENAME).write_text("Tag: tdd-step-9\n")
        (self.wd / LEGACY_PLAN_FILENAME).write_text("Tag: tdd-step-7\n")
        (self.wd / ".momo" / "r1.log").write_text("skipped 'tdd-step-8'\n")
        found = tdd.find_tags(self.wd)
        self.assertEqual(sorted(found), ["tdd-acceptance", "tdd-step-1"])
        self.assertEqual(found["tdd-step-1"], ["tests/test_calc.py:7"])

    def test_tests_stage_refuses_hollow_tests(self):
        plan = self.tdd_plan()
        (self.wd / "tests" / "test_calc.py").write_text(
            'import unittest\n\n\nclass Add(unittest.TestCase):\n    def test_adds(self):\n'
            '        self.skipTest("tdd-step-1")\n\n    def test_negative(self):\n'
            '        self.skipTest("tdd-acceptance")\n')
        ok, report = tdd.check_gate(plan, 0, self.wd)
        self.assertFalse(ok)
        self.assertIn("no body after their skip marker: tests/test_calc.py:6, tests/test_calc.py:9",
                      report)

    def test_tests_stage_needs_every_tag(self):
        plan = self.tdd_plan()
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(True, False))
        ok, report = tdd.check_gate(plan, 0, self.wd)
        self.assertFalse(ok)
        self.assertIn("tdd-acceptance", report)
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(True, True))
        ok, report = tdd.check_gate(plan, 0, self.wd)
        self.assertTrue(ok, report)

    def test_impl_stage_refuses_leftover_marker_and_red_suite(self):
        plan = self.tdd_plan()
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(True, True))
        ok, report = tdd.check_gate(plan, 1, self.wd)
        self.assertFalse(ok)
        self.assertIn("tdd-step-1: tests/test_calc.py:7", report)
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(False, True))
        ok, report = tdd.check_gate(plan, 1, self.wd)
        self.assertFalse(ok)
        self.assertIn("exit code: 1", report)
        (self.wd / "calc.py").write_text(CALC_RIGHT)
        self.assertTrue(tdd.check_gate(plan, 1, self.wd)[0])

    def test_acceptance_stage_needs_no_tags_left(self):
        plan = self.tdd_plan()
        (self.wd / "calc.py").write_text(CALC_RIGHT)
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(False, True))
        ok, report = tdd.check_gate(plan, 2, self.wd)
        self.assertFalse(ok)
        self.assertIn("tdd-acceptance", report)
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(False, False))
        self.assertTrue(tdd.check_gate(plan, 2, self.wd)[0])


class Markers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, text):
        (self.wd / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.wd / rel).write_text(text)

    def test_switch_on_removes_plain_markers_only(self):
        self.put("t/test_a.py", 'class A:\n    @unittest.skip("tdd-step-1")\n    def test_a(self):\n'
                                '        assert f()\n\n    @pytest.mark.skip(reason="tdd-step-1")\n'
                                '    def test_b(self):\n        self.skipTest("tdd-step-1")\n'
                                '        assert g()\n\n    @unittest.skip("tdd-step-2")\n'
                                '    def test_c(self):\n        assert h()\n')
        self.put("t/A.java", '  @Disabled("tdd-step-1")\n  @Test\n  void f() {\n    check();\n  }\n')
        self.put("t/lib.rs", '#[test]\n#[ignore = "tdd-step-1"]\nfn f() {\n    check();\n}\n')
        self.put("t/a.test.js", 'it.skip("adds (tdd-step-1)", () => {\n  expect(add(1, 2)).toBe(3);\n});\n')
        before = (self.wd / "t/lib.rs").read_text()
        removed, left, changed = tdd.switch_on(self.wd, "tdd-step-1")
        self.assertEqual(removed, 5)
        self.assertEqual(left, ["t/a.test.js:1"])
        self.assertEqual(sorted(changed), ["t/A.java", "t/lib.rs", "t/test_a.py"])
        self.assertEqual(changed["t/lib.rs"], before)
        self.assertEqual(sorted(tdd.find_tags(self.wd)), ["tdd-step-1", "tdd-step-2"])
        self.assertEqual(tdd.find_tags(self.wd)["tdd-step-1"], ["t/a.test.js:1"])
        py = (self.wd / "t/test_a.py").read_text()
        self.assertIn("    def test_a(self):\n        assert f()", py)
        self.assertIn('@unittest.skip("tdd-step-2")', py)
        self.assertEqual((self.wd / "t/lib.rs").read_text(), "#[test]\nfn f() {\n    check();\n}\n")

    def test_hollow_tests_are_found(self):
        self.put("test_h.py", 'class A:\n    def test_a(self):\n        self.skipTest("tdd-step-1")\n\n'
                              '    @unittest.skip("tdd-step-1")\n    def test_b(self):\n        pass\n\n'
                              '    @unittest.skip("tdd-step-2")\n    def test_c(self):\n'
                              '        self.assertEqual(f(), 1)\n\n    def test_d(self):\n'
                              '        self.skipTest("tdd-step-2")\n        self.assertTrue(g())\n')
        self.put("x_test.go", 'func TestX(t *testing.T) {\n\tt.Skip("tdd-step-3")\n}\n')
        self.put("B.java", '  @Disabled("tdd-step-3")\n  void f() {\n  }\n')
        self.put("a.test.js", 'it.skip("adds (tdd-step-4)", () => {\n});\n\n'
                              'it.skip("subs (tdd-step-4)", () => {\n  expect(sub(3, 1)).toBe(2);\n});\n')
        self.assertEqual(sorted(tdd.hollow_sites(self.wd, tdd.find_tags(self.wd))),
                         ["B.java:1", "a.test.js:1", "test_h.py:3", "test_h.py:5", "x_test.go:2"])

    def test_owners_survive_the_skip_being_removed(self):
        self.put("test_o.py", 'class A:\n    @unittest.skip("tdd-step-1")\n    def test_a(self):\n'
                              '        assert f()\n\n    def test_b(self):\n'
                              '        self.skipTest("tdd-step-2")\n        assert g()\n')
        self.put("o.test.js", 'it.skip("adds (tdd-step-3)", () => {\n  expect(add(1, 2)).toBe(3);\n});\n')
        owners = tdd.test_owners(self.wd, tdd.find_tags(self.wd))
        self.assertEqual(owners["test_o.py"], {"def test_a self": "tdd-step-1",
                                               "def test_b self": "tdd-step-2"})
        self.assertEqual(owners["o.test.js"], {"it adds": "tdd-step-3"})
        self.assertEqual(tdd._key('it("adds", () => {'), "it adds")

    def _audit(self, old, new, finished=()):
        self.put("test_x.py", old)
        owners = tdd.test_owners(self.wd, tdd.find_tags(self.wd))
        tdd.switch_on(self.wd, "tdd-step-1")
        tdd.switch_on(self.wd, "tdd-step-2")
        snap = tdd.snapshot(self.wd, owners)
        self.put("test_x.py", new)
        return tdd.audit(snap, self.wd, owners, set(finished))

    OLD = ('class A:\n    @unittest.skip("tdd-step-1")\n    def test_a(self):\n'
           '        x = make()\n        self.assertEqual(x.total(), 3)\n\n'
           '    @unittest.skip("tdd-step-2")\n    def test_b(self):\n'
           '        self.assertEqual(low(3), ["pear", "apple"])\n')
    ACTIVE = OLD.replace('    @unittest.skip("tdd-step-1")\n', '').replace('    @unittest.skip("tdd-step-2")\n', '')

    def test_audit_refuses_a_deleted_check(self):
        refusal, _ = self._audit(self.OLD, self.ACTIVE.replace(
            "        self.assertEqual(low(3), [\"pear\", \"apple\"])\n", "        pass\n"))
        self.assertEqual(refusal, "")       # a replaced line is a change, not a deletion
        refusal, _ = self._audit(self.OLD, self.ACTIVE.replace(
            "        self.assertEqual(x.total(), 3)\n", ""))
        self.assertIn("These checks were deleted", refusal)
        self.assertIn("test_x.py:4  self.assertEqual(x.total(), 3)", refusal)

    def test_audit_notes_a_corrected_expectation(self):
        refusal, notes = self._audit(self.OLD, self.ACTIVE.replace('["pear", "apple"]', '["apple", "pear"]'),
                                     finished=["tdd-step-1"])
        self.assertEqual(refusal, "")
        self.assertEqual(len(notes), 1)
        self.assertIn('["apple", "pear"]', notes[0])

    def test_audit_refuses_changes_to_a_finished_steps_test(self):
        refusal, _ = self._audit(self.OLD, self.ACTIVE.replace("x.total(), 3", "x.total(), 4"),
                                 finished=["tdd-step-1"])
        self.assertIn("belong to finished steps", refusal)
        self.assertIn("[tdd-step-1]", refusal)


class Commands(unittest.TestCase):
    def test_step_command_fills_the_stage_files(self):
        plan = Plan(title="t", kind="tdd", test_command="pytest -q",
                    step_test_command="pytest -q {files} | {dirs} | {stems}",
                    test_owners={"tests/test_a b.py": {"def test_x": "tdd-step-1"},
                                 "t2.py": {"x": "tdd-step-1"}, "o.py": {"y": "tdd-step-2"}})
        self.assertEqual(tdd.step_command(plan, "tdd-step-1"),
                         "pytest -q t2.py 'tests/test_a b.py' | . ./tests | t2 'test_a b'")
        self.assertEqual(tdd.stage_files(plan, "tdd-step-2"), ["o.py"])
        self.assertEqual(tdd.step_command(plan, "tdd-step-9"), "")      # no files: full suite
        plan.step_test_command = ""
        self.assertEqual(tdd.step_command(plan, "tdd-step-1"), "")

    def test_same_command(self):
        planned = "python3 -m unittest discover -s tests -t ."
        for ran, same in [(planned, True), (planned + " -v", True), ("  " + planned, True),
                          (planned + " -v 2>&1 | tail -25", True),      # run_command strips it
                          (planned + " 2>&1 | grep -E 'OK|FAIL'", True),    # this too
                          (planned + " | grep -v OK", False), (planned + "; echo $?", False),
                          (planned + " > out.txt", False), ("python3 -m unittest tests.test_x", False),
                          (planned + " && rm -rf x", False), ("$(echo " + planned + ")", False)]:
            self.assertEqual(tdd.same_command(ran, planned), same, ran)

    def test_run_command_reports_its_exit_code(self):
        from harness import run_store
        from harness.tools import dispatch
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for mode in ("classic", "new"):
                store = run_store.RunStore(d / "runs") if mode == "new" else None
                for args, code in (({"command": "true"}, 0), ({"command": "exit 3"}, 3),
                                   ({"command": "sleep 3", "timeout": 1}, None)):
                    got = {}
                    dispatch("run_command", args, d, run_mode=mode, run_store=store, run_exit=got)
                    self.assertEqual(got.get("code"), code, (mode, args))


class Harness(_Base):
    def test_plan_tdd_command_switches_schema_and_persists(self):
        out = handle_command("/plan tdd", self.h).output
        self.assertIn("test-driven", out)
        self.assertEqual(self.h.mode, "plan")
        create = next(t for t in self.h._current_tools() if t["function"]["name"] == "create_plan")
        self.assertIn("test_command", create["function"]["parameters"]["required"])
        self.assertIn("This plan is test-driven", self.h.messages[0]["content"])
        self.assertEqual(self.h.status_event().plan_kind, "tdd")
        handle_command("/plan normal", self.h)
        create = next(t for t in self.h._current_tools() if t["function"]["name"] == "create_plan")
        self.assertNotIn("test_command", create["function"]["parameters"]["properties"])

    def test_tdd_prompts_name_only_offered_tools(self):
        import re
        from harness.tools import ALL_TOOLS, PLAN_TOOLS
        every = {t["function"]["name"] for t in ALL_TOOLS + PLAN_TOOLS}
        handle_command("/plan tdd", self.h)
        for phase in ("investigating", "executing"):
            if phase == "executing":
                self.h.plan, self.h.plan_phase = self.tdd_plan(), "executing"
                self.h.set_mode("coding")       # an approved plan runs in coding mode
                self.h.rebuild_system_prompt()
            offered = {t["function"]["name"] for t in self.h._current_tools()}
            text = "".join(s["text"] for s in self.h.system_prompt_sections()
                           if s["key"] != "tools")
            self.assertIn("test-driven", text)
            stale = sorted(n for n in every - offered if re.search(rf"\b{n}\b", text))
            self.assertEqual(stale, [], phase)

    def test_plan_kind_saved_with_session(self):
        handle_command("/plan tdd", self.h)
        self.h.messages.append({"role": "user", "content": "hi"})
        self.h._autosave()
        data = session_mod.load(self.h.session_path())
        self.assertEqual(data["plan_kind"], "tdd")
        self.h.plan_kind = "normal"
        self.h.load_session(self.h.session_path())
        self.assertEqual(self.h.plan_kind, "tdd")

    def test_closing_run_the_tests_step_is_dropped(self):
        # The three closing steps the 9B wrote in live runs: the acceptance
        # stage already runs the suite, and these have nothing to implement.
        verify = [
            {"title": "Keep existing add tests green", "tests": ["add tests still pass"], "details": ""},
            {"title": "Acceptance", "tests": ["everything works"], "details": "End to end."},
            {"title": "Run the test suite to verify", "tests": ["the full test suite passes"],
             "details": f"Run `{TEST_CMD}` and confirm all tests pass; fix anything that fails."},
            {"title": "Verify full suite", "tests": ["exits 0 with all tests OK"],
             "details": f"Run the project's test command to confirm everything passes: {TEST_CMD}"},
            {"title": "Run the full test suite", "tests": ["exits 0 with all tests OK"],
             "details": "Confirm all Inventory tests pass with a non-zero exit on failure."},
        ]
        kept = [{"title": "Check input validation", "details": "In calc.py add() reject None.",
                 "files": ["calc.py"], "tests": ["add(None, 1) → TypeError"]},
                {"title": "Implement add() overflow and run tests", "details": "In calc.py.",
                 "tests": ["add(2**64, 1) → 2**64 + 1"]},
                {"title": "Add the CLI entry point", "details": "Write cli.py main().",
                 "tests": ["main(['2', '3']) prints 5"]}]
        for last, dropped in [(v, True) for v in verify] + [(k, False) for k in kept]:
            plan = Plan(title="t", steps=self.h._parse_steps(CREATE_ARGS["steps"] + [last]))
            self.h.plan_kind = "tdd"
            self.assertEqual(self.h._make_tdd_plan(plan, CREATE_ARGS), "")
            titles = [st.title for st in plan.steps]
            self.assertEqual(last["title"] not in titles, dropped, titles)

    def test_tdd_planner_has_no_closing_verification_rule(self):
        handle_command("/plan tdd", self.h)
        prompt = self.h.messages[0]["content"]
        self.assertNotIn("Verified at the end", prompt)
        self.assertIn("6. **Verified by the harness**", prompt)
        self.assertIn("7. **Goal carries the findings**", prompt)
        handle_command("/plan normal", self.h)
        self.assertIn("Verified at the end", self.h.messages[0]["content"])

    def test_deleting_an_assert_during_a_step_is_refused(self):
        handle_command("/plan tdd", self.h)
        active = calc_tests(False, True)
        ScriptedClient.script = [
            call("create_plan", **CREATE_ARGS),
            write("tests/test_calc.py", calc_tests(True, True)),
            call("complete_step", summary="tests written"),
            # step 1: delete the failing check instead of implementing
            write("tests/test_calc.py", active.replace("        self.assertEqual(add(2, 3), 5)\n", "        pass\n")
                  .replace("    def test_adds(self):\n        pass\n", "")),
            call("complete_step", summary="green"),
        ]
        self.h.send("make add work")
        results = [e.result for e in self.events()
                   if isinstance(e, ToolResultEvent) and e.name == "complete_step"]
        self.assertIn("These checks were deleted", results[1])
        self.assertIn("assertEqual(add(2, 3), 5)", results[1])

    def _failing_step(self, answers):
        """A step whose gate the model keeps failing; answers for _ask_user."""
        self.h.set_mode("coding")
        self.h.plan = self.tdd_plan()
        self.h.plan.steps[0].status = "done"
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(True, True))
        self.h.plan_phase = "executing"
        asked = []
        self.h._ask_user = lambda q: asked.append(q) or answers.pop(0)
        return asked

    def test_three_refusals_ask_the_user_and_pause(self):
        asked = self._failing_step(["pause"])
        ScriptedClient.script = [call("complete_step", summary="x")] * 3 + [
            call("complete_step", summary="never reached")]
        self.h._execute_plan()
        self.assertEqual(len(asked), 1)
        self.assertIn("still does not pass its gate after 3 attempts", asked[0])
        self.assertEqual(len(ScriptedClient.script), 1)        # stopped after the 3rd
        self.assertEqual(self.h.plan.steps[1].status, "in_progress")

    def test_guidance_after_refusals_reaches_the_model(self):
        asked = self._failing_step(["return a + b in calc.py"])
        ScriptedClient.script = [call("complete_step", summary="x")] * 3 + [
            write("calc.py", CALC_RIGHT), call("complete_step", summary="fixed")]
        self.h._execute_plan()
        self.assertEqual(len(asked), 1)
        tool_results = [m["content"] for m in self.h.messages if m.get("role") == "tool"]
        self.assertTrue(any("User guidance after 3 failed attempts: return a + b in calc.py" in r
                            for r in tool_results))
        self.assertIsNone(self.h.plan)     # the step passed and the rest of the plan ran

    def test_no_op_steps_are_dropped_mid_plan(self):
        steps = [CREATE_ARGS["steps"][0],
                 {"title": "Keep existing add tests green", "details": "", "tests": ["still pass"]},
                 {"title": "Acceptance", "details": "", "tests": ["works"]},
                 {"title": "Verify full suite", "details": "", "tests": ["exits 0"],
                  "files": ["tests/test_calc.py"]}]
        plan = Plan(title="t", steps=self.h._parse_steps(steps))
        self.h.plan_kind = "tdd"
        self.assertEqual(self.h._make_tdd_plan(plan, CREATE_ARGS), "")
        self.assertEqual([st.title for st in plan.steps if st.stage == "impl"], ["Fix add()"])
        notes = [e.text for e in self.events() if isinstance(e, ChatEvent)]
        self.assertTrue(any(n.startswith("Dropped steps the TDD plan does itself") and
                            '"Acceptance"' in n for n in notes), notes)

    def test_a_plan_of_only_no_op_steps_is_refused(self):
        plan = Plan(title="t", steps=self.h._parse_steps(
            [{"title": "Run the full test suite", "details": "", "tests": ["exits 0"]}]))
        self.h.plan_kind = "tdd"
        self.assertIn("only runs or checks the tests", self.h._make_tdd_plan(plan, CREATE_ARGS))

    def test_create_plan_requires_test_command_and_tests(self):
        handle_command("/plan tdd", self.h)
        err = self.h._handle_create_plan({**CREATE_ARGS, "test_command": ""})
        self.assertTrue(err.startswith("ERROR"), err)
        err = self.h._handle_create_plan({**CREATE_ARGS, "steps": [{"title": "x", "details": "y"}]})
        self.assertIn("needs 'tests'", err)
        self.assertIsNone(self.h.plan)

    def test_end_to_end_gate_refuses_then_completes(self):
        handle_command("/plan tdd", self.h)
        ScriptedClient.script = [
            call("create_plan", **CREATE_ARGS),
            # stage 1: write every test, skipped
            write("tests/test_calc.py", calc_tests(True, True)),
            call("complete_step", summary="tests written"),
            # step 1: the harness switched its tests on; claiming done → refused
            call("complete_step", summary="done?"),
            write("calc.py", CALC_RIGHT),
            call("complete_step", summary="add fixed"),
            # acceptance: switched on by the harness too, already green
            call("complete_step", summary="all green"),
        ]
        self.h.send("make add work")
        self.assertEqual(ScriptedClient.script, [])
        events = self.events()
        gates = [e for e in events if isinstance(e, ToolCallEvent) and e.name == "tdd_gate"]
        self.assertEqual([g.args for g in gates], [{"command": TEST_CMD}] * 4)
        switched = [e.args["tag"] for e in events
                    if isinstance(e, ToolCallEvent) and e.name == "tdd_switch_on"]
        self.assertEqual(switched, ["tdd-step-1", "tdd-acceptance"])
        diffs = [e.path for e in events if isinstance(e, DiffEvent)]
        self.assertEqual(diffs.count("tests/test_calc.py"), 3)   # the write + two switch-ons
        results = [e.result for e in events
                   if isinstance(e, ToolResultEvent) and e.name == "complete_step"]
        self.assertEqual(len(results), 4, results)
        self.assertEqual(results[0], "Step marked complete. Tests pass.")
        self.assertTrue(results[1].startswith("ERROR: step not complete"), results[1])
        self.assertIn("FAIL: test_adds", results[1])
        self.assertEqual(results[2:], ["Step marked complete. Tests pass."] * 2)
        step_msgs = [m["content"] for m in self.h.messages if m.get("role") == "user"
                     and m["content"].startswith("Plan step ")]
        self.assertIn("They fail, as they should before the implementation", step_msgs[1])
        self.assertIn("FAIL: test_adds", step_msgs[1])
        self.assertIn("They already pass. Check that they cover", step_msgs[2])
        self.assertNotIn("tdd-", (self.wd / "tests" / "test_calc.py").read_text())
        self.assertIsNone(self.h.plan)
        self.assertFalse((self.wd / PLAN_FILENAME).exists())

    def _through_tests_stage(self, extra_script, **create):
        handle_command("/plan tdd", self.h)
        ScriptedClient.script = [
            call("create_plan", **{**CREATE_ARGS, **create}),
            write("tests/test_calc.py", calc_tests(True, True)),
            call("complete_step", summary="tests written"),
        ] + extra_script
        self.h.send("make add work")
        return self.events()

    def test_step_command_runs_the_red_check(self):
        events = self._through_tests_stage([write("calc.py", CALC_RIGHT),
                                            call("complete_step", summary="done")],
                                           step_test_command=f"{PY} -m unittest {{files}}")
        switch = [e.result for e in events if isinstance(e, ToolResultEvent) and e.name == "tdd_switch_on"]
        self.assertIn(f"ran `{PY} -m unittest tests/test_calc.py`: tests fail", switch[0])
        step_msg = next(m["content"] for m in self.h.messages if m.get("role") == "user"
                        and m["content"].startswith("Plan step 2/"))
        self.assertIn(f"Run this stage's tests with `{PY} -m unittest tests/test_calc.py`", step_msg)
        self.assertIsNone(self.h.plan)

    def test_a_failing_step_command_is_dropped(self):
        events = self._through_tests_stage([write("calc.py", CALC_RIGHT),
                                            call("complete_step", summary="done")],
                                           step_test_command=f"{PY} -m unittest tests/nope_{{stems}}.py")
        notes = [e.text for e in events if isinstance(e, ChatEvent) and e.role == "system"]
        self.assertTrue(any(n.startswith("The step test command failed") for n in notes), notes)
        switch = [e.result for e in events if isinstance(e, ToolResultEvent) and e.name == "tdd_switch_on"]
        self.assertIn(f"ran `{TEST_CMD}`", switch[0])     # the full suite instead

    def test_gate_reuses_the_models_own_green_run(self):
        run = call("run_command", command=TEST_CMD + " -v")
        events = self._through_tests_stage([
            write("calc.py", CALC_RIGHT), run, call("complete_step", summary="green"),   # reused
            run, write("tests/test_extra.py", "\n"), call("complete_step", summary="ok"),  # wrote since
        ])
        gates = [e.result for e in events if isinstance(e, ToolResultEvent) and e.name == "tdd_gate"]
        self.assertTrue(gates[1].startswith(f"Tests pass: your run of `{TEST_CMD}` just before"), gates[1])
        self.assertTrue(gates[2].startswith(f"Tests pass: {TEST_CMD}\n"), gates[2])   # ran fresh
        self.assertIsNone(self.h.plan)

    def test_a_tail_piped_run_is_reused_a_grep_piped_one_not(self):
        events = self._through_tests_stage([
            write("calc.py", CALC_RIGHT), call("run_command", command=TEST_CMD + " 2>&1 | tail -3"),
            call("complete_step", summary="green"),
            call("run_command", command=TEST_CMD + " 2>&1 | grep -c ok"),
            call("complete_step", summary="green")])
        gates = [e.result for e in events if isinstance(e, ToolResultEvent) and e.name == "tdd_gate"]
        self.assertTrue(gates[1].startswith("Tests pass: your run of"), gates[1])
        self.assertTrue(gates[2].startswith(f"Tests pass: {TEST_CMD}\n"), gates[2])

    def test_step_whose_tests_already_pass_is_told_so(self):
        self.h.set_mode("coding")
        self.h.plan = self.tdd_plan()
        self.h.plan.steps[0].status = "done"
        (self.wd / "calc.py").write_text(CALC_RIGHT)
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(True, True))
        self.h.plan_phase = "executing"
        ScriptedClient.script = [call("complete_step", summary="x")]
        self.h._execute_plan()
        msg = next(m["content"] for m in self.h.messages if m.get("role") == "user"
                   and m["content"].startswith("Plan step 2/"))
        self.assertIn("already pass before any code change", msg)
        self.assertIn("call complete_step and say so", msg)

    def test_text_reply_cannot_skip_the_gate(self):
        self.h.set_mode("coding")
        self.h.plan = self.tdd_plan()
        self.h.plan.steps[0].status = "done"
        (self.wd / "tests" / "test_calc.py").write_text(calc_tests(False, True))
        self.h.plan_phase = "executing"
        self.h._execute_plan()      # the client only ever answers "Done."
        step = self.h.plan.steps[1]
        self.assertEqual(step.status, "in_progress")
        texts = [e.text for e in self.events() if isinstance(e, ChatEvent)]
        self.assertTrue(any("ended without passing its tests" in t for t in texts), texts)
        gate_msgs = [m for m in self.h.messages if m.get("role") == "user"
                     and m["content"].startswith("The step is not complete")]
        self.assertEqual(len(gate_msgs), plan_runner._TDD_TEXT_RETRIES)


if __name__ == "__main__":
    unittest.main()
