"""explore: a sub-agent with a fresh context (harness/explorer.py).

The caller's context keeps only the call and the report; the explorer gets its
own prompt and tool set, and the caller's turn state (history, archive, plan
step, steering, loaded tools) is the same after it as before.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness.harness as hmod
from harness import explorer as explorer_mod
from harness import session as session_mod
from harness import transcript as transcript_mod
from harness.events import ChatEvent, ToolResultEvent
from harness.llm.base import ChatResponse, ToolCall
from harness.plan import Plan, Step
from home_tmp import setUpModule, tearDownModule  # noqa: F401
from test_loop_robustness import Scripted, read, reply


def call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name, args)], done_reason="stop")


def explore(question, **more):
    return call("explore", question=question, **more)


def names(tools):
    return {t["function"]["name"] for t in tools}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self._patches = [
            mock.patch.object(hmod, "make_client", lambda *a, **k: Scripted("http://fake", "fake")),
            mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
            mock.patch.object(session_mod, "SESSION_DIR", self.home / "sessions"),
        ]
        for p in self._patches:
            p.start()
        self.work = self.home / "work"
        self.work.mkdir()
        (self.work / "app.py").write_text("def main():\n    return 42\n")
        (self.work / "notes.txt").write_text("hello\n")
        for i in range(3):
            (self.work / f"f{i}.txt").write_text(f"file {i}\n")
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.work)
        self.h.stream = False
        self.h.session_titles = False
        self.h.set_mode("coding")
        self.client = self.h.client
        self.events = self.h.event_queue.subscribe(replay=False)

    def tearDown(self):
        self.events.close()
        for p in self._patches:
            p.stop()
        self.h.logger.close()
        self.tmp.cleanup()

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.events.get(timeout=0.05))
            except Exception:
                return out

    def explore_result(self):
        return next(m["content"] for m in self.h.messages
                    if m.get("role") == "tool" and m.get("name") == "explore")

    def explorer_requests(self):
        return [r for r in self.client.requests
                if "Role: explorer" in r["messages"][0]["content"]
                or r["messages"][0]["content"].startswith("You are a code **explorer**")]


class Offered(Base):
    def test_offered_in_the_main_roles_not_in_story_roles(self):
        for mode in ("design", "chat", "plan", "coding", "momo"):
            self.h.set_mode(mode)
            self.assertIn("explore", names(self.h._current_tools()), mode)
        self.h.set_mode("coding")
        self.h.story_role = "story-exec"
        self.assertNotIn("explore", names(self.h._current_tools()))

    def test_explorer_tools_read_and_guarded_shell_only(self):
        self.h._explore = {"question": "q", "paths": []}
        got = names(self.h._current_tools())
        self.h._explore = None
        self.assertIn("read_file", got)
        self.assertIn("run_command", got)
        for absent in ("explore", "ask_user", "ask_choice", "write_file", "edit_file",
                       "scratch_write", "delete_file", "complete_step", "create_plan"):
            self.assertNotIn(absent, got)

    def test_turned_off_with_tools_picker(self):
        self.h.disabled_tools["coding"] = {"explore"}
        self.assertNotIn("explore", names(self.h._current_tools()))


class Runs(Base):
    def test_report_comes_back_and_the_reads_stay_out_of_the_callers_context(self):
        self.client.script = [
            explore("Where is main defined?", paths=["app.py"]),
            read("app.py"),
            reply("main is defined at app.py:1 and returns 42."),
            reply("It is in app.py."),
        ]
        self.h.send("where is main?")
        result = self.explore_result()
        self.assertTrue(result.startswith("The explorer's report:"))
        self.assertIn("app.py:1 and returns 42", result)
        self.assertIn("Files it looked at: app.py", result)
        # the caller's history: the question, the call, its result, the reply
        roles = [m["role"] for m in self.h.messages[1:]]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertFalse(any(m.get("name") == "read_file" for m in self.h.messages))
        # the explorer's context is archived just before the explore result
        recs = [r for r in self.h.compacted if r.get("by") == "explore"]
        self.assertTrue(recs)
        self.assertTrue(all(r["pos"] == 3 for r in recs))
        full = transcript_mod.full_transcript(self.h.messages, self.h.compacted)
        kinds = [(m.get("role"), m.get("name")) for m, _ in full]
        i = kinds.index(("tool", "explore"))
        self.assertIn(("tool", "read_file"), kinds[:i])
        self.assertGreater(kinds.index(("tool", "read_file")), kinds.index(("assistant", None)))

    def test_explorer_request_has_its_own_prompt_brief_and_tools(self):
        self.client.script = [explore("How is X done?", paths=["app.py"]),
                              reply("X is done nowhere."), reply("ok")]
        self.h.send("how is X done?")
        sub = self.client.requests[1]
        self.assertIn("explorer", sub["messages"][0]["content"])
        self.assertEqual(len(sub["messages"]), 2)
        self.assertIn("How is X done?", sub["messages"][1]["content"])
        self.assertIn("`app.py`", sub["messages"][1]["content"])
        self.assertNotIn("explore", names(sub["tools"]))
        self.assertNotIn("write_file", names(sub["tools"]))
        # the caller's next request is back on its own prompt and tools
        back = self.client.requests[2]
        self.assertEqual(back["messages"][0]["content"], self.client.requests[0]["messages"][0]["content"])
        self.assertIn("explore", names(back["tools"]))

    def test_caller_state_is_restored(self):
        self.h._append_user("look")
        turn_user, archive = self.h._turn_user, list(self.h.compacted)
        loaded, system = set(self.h.loaded_tools), self.h.messages[0]["content"]
        watch = self.h._syntax_watch
        self.client.script = [explore("q"), reply("report"), reply("done")]
        self.h._run_loop(self.h._current_tools(), 10)
        self.assertIs(self.h._turn_user, turn_user)
        self.assertIs(self.h._syntax_watch, watch)
        self.assertEqual(self.h.loaded_tools, loaded)
        self.assertEqual(self.h.messages[0]["content"], system)
        self.assertIsNone(self.h._explore)
        self.assertEqual([r for r in self.h.compacted if r.get("by") != "explore"], archive)

    def test_the_explorers_reply_is_not_a_chat_bubble(self):
        self.client.script = [explore("q"), reply("THE REPORT"), reply("caller reply")]
        self.h.send("hi")
        events = self.drain()
        bubbles = [e.text for e in events if isinstance(e, ChatEvent) and e.role == "assistant"]
        self.assertEqual(bubbles, ["caller reply"])
        system = [e.text for e in events if isinstance(e, ChatEvent) and e.role == "system"]
        self.assertTrue(any(t.startswith("🔍 Exploring: q") for t in system))
        self.assertTrue(any(t.startswith("🔍 Explorer done — 0 tool calls") for t in system))
        self.assertTrue(any(isinstance(e, ToolResultEvent) and e.name == "explore"
                            and "THE REPORT" in e.result for e in events))

    def test_no_question_is_an_error(self):
        self.client.script = [call("explore", question=" "), reply("ok")]
        self.h.send("hi")
        self.assertTrue(self.explore_result().startswith("ERROR: explore needs a question"))
        self.assertEqual(len(self.client.requests), 2)

    def test_the_round_cap_summary_is_the_report(self):
        self.client.script = [explore("q")] + [read(f"f{i}.txt") for i in range(3)] + [
            reply("Partial: f0-f2 say file N."), reply("ok")]
        with mock.patch.object(explorer_mod, "EXPLORE_CAP", 3):
            self.h.send("hi")
        result = self.explore_result()
        self.assertIn("round cap", result)
        self.assertIn("Partial: f0-f2 say file N.", result)

    def test_no_report_is_an_error_with_the_files(self):
        self.client.script = [explore("q")] + [read(f"f{i}.txt") for i in range(3)] + [
            reply(""), reply("ok")]
        with mock.patch.object(explorer_mod, "EXPLORE_CAP", 3):
            self.h.send("hi")
        result = self.explore_result()
        self.assertTrue(result.startswith("ERROR: the explorer gave no report"))
        self.assertIn("Files it looked at: f0.txt, f1.txt, f2.txt", result)

    def test_a_long_report_is_cut(self):
        long = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        self.client.script = [explore("q"), reply(long), reply("ok")]
        self.h.send("hi")
        self.assertIn("(report cut after", self.explore_result())
        self.assertLess(len(self.explore_result()), explorer_mod.REPORT_CHARS + 300)


class Insulated(Base):
    def test_a_plan_step_is_not_ended_or_ticked_by_the_explorer(self):
        self.h.plan = Plan(title="t", steps=[Step(title="s")])
        self.h.plan_phase = "executing"
        self.assertTrue(self.h._plan_executing())
        self.h._append_user("do the step")
        self.client.script = [explore("q"), reply("report"),
                              call("complete_step", summary="did it")]
        outcome = self.h._run_loop(self.h._current_tools(), 10)
        self.assertEqual(outcome, "done")
        self.assertEqual(len(self.client.requests), 3)
        self.assertEqual(self.h.plan.steps[0].turns, 2)       # the caller's two rounds
        self.assertIn("complete_step", names(self.client.requests[2]["tools"]))
        self.assertNotIn("complete_step", names(self.client.requests[1]["tools"]))
        self.assertNotIn("Current plan state", self.client.requests[1]["messages"][0]["content"])

    def test_in_plan_mode_the_explorer_reply_gets_no_planner_nudge(self):
        self.h.set_mode("plan")
        self.h._plan_draft = {"turns": 0}
        self.client.script = [explore("q"), reply("report"), reply("What should it do?")]
        self.h.send("plan something")
        self.assertEqual(len(self.client.requests), 3)
        self.assertIn("report", self.explore_result())
        self.assertEqual(self.h._plan_draft["turns"], 2)
        self.h._plan_draft = None

    def test_a_message_typed_during_the_explore_reaches_the_caller(self):
        def typed():
            self.h.steer("also check tests")
            return reply("report")
        self.client.script = [explore("q"), typed, reply("ok")]
        self.h.send("hi")
        sub = self.client.requests[1]["messages"]
        self.assertFalse(any("also check tests" in str(m.get("content")) for m in sub))
        last = self.client.requests[2]["messages"]
        self.assertTrue(any("also check tests" in str(m.get("content")) for m in last))

    def test_escape_during_the_explore(self):
        def esc():
            self.h._cancel.set()
            return read("notes.txt")
        self.client.script = [explore("q"), esc, reply("never")]
        self.h.send("hi")
        self.assertTrue(self.explore_result().startswith("ERROR: the explorer did not finish"))
        self.assertEqual(len(self.client.requests), 2)
        self.assertIsNone(self.h._explore)

    def test_the_shell_guard_refuses_project_writes(self):
        self.h._explore = {"question": "q", "paths": []}
        try:
            refused = self.h._read_only_shell_guard("run_command", {"command": "echo x > app.py"})
            allowed = self.h._read_only_shell_guard("run_command", {"command": "git log -3"})
        finally:
            self.h._explore = None
        self.assertTrue(refused.startswith("ERROR"))
        self.assertEqual(allowed, "")

    def test_a_crash_inside_restores_the_callers_history(self):
        self.client.script = [explore("q"), RuntimeError("boom")]
        self.h._append_user("hi")
        before = list(self.h.messages)
        with mock.patch.object(hmod, "_server_error_kind", side_effect=ValueError("bug")):
            with self.assertRaises(ValueError):
                self.h._run_loop(self.h._current_tools(), 10)
        self.assertIsNone(self.h._explore)
        self.assertEqual(self.h.messages[:len(before)], before)
        self.assertEqual(self.h.messages[-1]["tool_calls"][0]["function"]["name"], "explore")

    def test_an_undeclared_write_is_refused(self):
        self.client.script = [explore("q"), call("write_file", path="app.py", content="gone"),
                              reply("report"), reply("ok")]
        self.h.send("hi")
        self.assertEqual((self.work / "app.py").read_text(), "def main():\n    return 42\n")
        sub = self.client.requests[2]["messages"]
        self.assertTrue(sub[-1]["content"].startswith("ERROR: write_file is not available to the explorer"))

    def test_mcp_tools_are_read_only_for_the_explorer(self):
        self.assertFalse(self.h._mcp_read_only_role())        # coding
        self.h._explore = {"question": "q", "paths": []}
        try:
            self.assertTrue(self.h._mcp_read_only_role())
        finally:
            self.h._explore = None


if __name__ == "__main__":
    unittest.main()
