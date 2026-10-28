"""The loop guard: a call repeated with nothing changed in between, or one error
from call after call, stops the batch; the model writes an analysis of the
loop, and the history is cut back to before it with that analysis in its place.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import json
import tempfile
import unittest
from pathlib import Path

import harness.harness as hmod
from harness import commands, session as session_mod, transcript
from harness.loop_guard import LoopGuard, error_key, failure_line
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from tests.test_tool_choices import Base
from home_tmp import setUpModule, tearDownModule  # noqa: F401

GOLDEN = Path(__file__).parent / "golden"
ANALYSIS = "## Why it is not working\nThe file never changes.\n## A different approach\nEdit it."


def _call(name, **args):
    return ChatResponse(content="", tool_calls=[ToolCall(name=name, arguments=args)],
                        done_reason="stop")


class ScriptedClient(LLMClient):
    """Replies with `script`, one per tool-loop turn, then a plain answer.  A call
    with no tools is the loop analysis: it gets `analysis` (or raises)."""
    provider_name = "fake"
    script: list = []
    analysis = ANALYSIS

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.replies = list(type(self).script)
        self.analysis_calls = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        if not tools:
            self.analysis_calls.append({"messages": messages, "think": think})
            if isinstance(self.analysis, Exception):
                raise self.analysis
            return ChatResponse(content=self.analysis, done_reason="stop")
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class Detector(unittest.TestCase):
    def setUp(self):
        self.g = LoopGuard()

    def rec(self, name, result="ok", mutating=False, **args):
        msg = {"role": "assistant"}
        return msg, self.g.record(name, args, result, msg, mutating)

    def test_the_third_identical_call_is_a_loop(self):
        first, hit = self.rec("read_file", path="a.py")
        self.assertIsNone(hit)
        self.assertIsNone(self.rec("read_file", path="a.py")[1])
        _, hit = self.rec("read_file", path="a.py")
        self.assertEqual((hit.kind, hit.name, hit.count), ("call", "read_file", 3))
        self.assertIs(hit.msgs[0], first)

    def test_a_new_write_in_between_is_progress(self):
        for n in range(3):
            self.assertIsNone(self.rec("read_file", path="a.py")[1])
            self.assertIsNone(self.rec("edit_file", mutating=True, path="a.py",
                                       old_string=str(n), new_string=str(n + 1))[1])

    def test_the_same_write_three_times_is_a_loop(self):
        for _ in range(2):
            self.assertIsNone(self.rec("write_file", mutating=True, path="a.py", content="x")[1])
        _, hit = self.rec("write_file", mutating=True, path="a.py", content="x")
        self.assertEqual(hit.kind, "call")

    def test_bump_starts_the_count_again(self):
        self.rec("read_file", path="a.py")
        self.rec("read_file", path="a.py")
        self.g.bump()
        self.assertIsNone(self.rec("read_file", path="a.py")[1])

    def test_one_error_from_different_calls_is_a_loop(self):
        err = "ERROR: old_string '{}' not found in a.py"
        first, _ = self.rec("edit_file", err.format("x"), True, path="a.py", old_string="x")
        self.rec("edit_file", err.format("y"), True, path="a.py", old_string="y")
        _, hit = self.rec("edit_file", err.format("z"), True, path="a.py", old_string="z")
        self.assertEqual((hit.kind, hit.count), ("error", 3))
        self.assertIs(hit.msgs[0], first)
        self.assertIn("old_string", hit.describe())

    def test_a_success_clears_the_errors(self):
        err = "ERROR: old_string '{}' not found in a.py"
        self.rec("edit_file", err.format("x"), True, path="a.py", old_string="x")
        self.rec("edit_file", err.format("y"), True, path="a.py", old_string="y")
        self.rec("edit_file", "OK", True, path="a.py", old_string="ok")
        self.assertIsNone(self.rec("edit_file", err.format("z"), True,
                                   path="a.py", old_string="z")[1])

    def test_user_and_plan_calls_are_not_counted(self):
        for _ in range(4):
            self.assertIsNone(self.rec("ask_user", question="ok?")[1])
            self.assertIsNone(self.rec("complete_step", summary="done")[1])

    def test_a_declined_call_is_not_counted(self):
        for _ in range(4):
            self.assertIsNone(self.rec("run_command", "ERROR: command declined by user",
                                       command="rm x")[1])

    def test_error_key_ignores_what_varies(self):
        self.assertEqual(error_key("ERROR: no match for 'a' in src/x.py at line 3"),
                         error_key("ERROR: no match for 'bcd' in lib/y.py at line 40"))


def _run(code, *lines):
    """A new-mode run_command result: the output, then the saved-log footer."""
    return "\n".join(["[last 3 of 3 lines, from line 1]", *lines,
                      f"[output saved: .momo/runs/r1.log — 3 lines, 90 bytes, exit code {code}]"])


EDITED = "OK — 1 change applied — line 2 now reads (inside function encode, L1-4):\n 2:     x"


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.g = LoopGuard()

    def rec(self, name, result="ok", mutating=False, **args):
        msg = {"role": "assistant"}
        return msg, self.g.record(name, args, result, msg, mutating)

    def cmd(self, command, code, *lines):
        msg = {"role": "assistant"}
        return msg, self.g.record("run_command", {"command": command}, _run(code, *lines),
                                  msg, False, code)


class Commands(GuardCase):
    """A failing run_command counts as an error; a success clears only its kind."""

    def test_the_failure_line_is_the_last_that_names_an_error(self):
        self.assertEqual(failure_line(_run(1, "Traceback (most recent call last):",
                                           "ValueError: bad byte")), "ValueError: bad byte")
        self.assertEqual(failure_line(_run(1, "warning: unused", "3 files")), "")

    def test_three_commands_failing_alike_are_a_loop(self):
        first, _ = self.cmd("python3 -c 'a()'", 1, "ValueError: byte 300")
        self.cmd("python3 -c 'b()'", 1, "ValueError: byte 301")
        _, hit = self.cmd("python3 -c 'c()'", 1, "ValueError: byte 302")
        self.assertEqual((hit.kind, hit.severity, hit.count), ("error", "rewind", 3))
        self.assertIs(hit.msgs[0], first)
        self.assertIn("ValueError: byte 302", hit.describe())

    def test_a_non_zero_exit_that_names_no_error_is_not_counted(self):
        for n in range(4):
            self.assertIsNone(self.cmd(f"grep -n x{n} a.py", 1, "")[1])

    def test_a_different_command_succeeding_does_not_clear(self):
        test = "python3 -m pytest tests/test_ws.py -x"
        self.cmd(test, 1, "FAILED tests/test_ws.py::test_frame")
        self.cmd(test + " -q", 1, "FAILED tests/test_ws.py::test_frame")
        self.assertIsNone(self.cmd("ls -la mud", 0, "ws.py")[1])
        self.assertEqual(self.cmd(test, 1, "FAILED tests/test_ws.py::test_frame")[1].kind, "error")

    def test_a_similar_command_succeeding_clears(self):
        test = "python3 -m pytest tests/test_ws.py -x"
        self.cmd(test, 1, "FAILED tests/test_ws.py::test_frame")
        self.cmd(test, 1, "FAILED tests/test_ws.py::test_frame")
        self.assertIsNone(self.cmd(test + " | tail -5", 0, "1 passed")[1])
        self.assertIsNone(self.cmd(test + " -q", 1, "FAILED tests/test_ws.py::test_frame")[1])


    def test_a_pipe_that_hides_the_exit_code_still_fails(self):
        """The 2026-10-28 rerun, calls 45, 47, 48: the third run piped through
        sed exited 0 and, as a success like the failing check, cleared it."""
        check = "PYTHONPATH=. python3 .momo/scratch/verify_ws.py 2>&1"
        err = "mud.ws.WSHandshakeError: no response to handshake"
        self.cmd(check + " | tail -25", 1, err)
        self.cmd(check + " | tail -25", 1, err)
        _, hit = self.cmd(check + " | sed -n '39,63p'", 0, err)
        self.assertEqual((hit.kind, hit.count), ("error", 3))

    def test_a_pipe_with_clean_output_clears_nothing(self):
        check = "python3 -m pytest tests/test_ws.py -x"
        self.cmd(check, 1, "FAILED tests/test_ws.py::test_frame")
        self.cmd(check + " -q", 1, "FAILED tests/test_ws.py::test_frame")
        self.assertIsNone(self.cmd(check + " | grep -c passed", 0, "3")[1])
        self.assertEqual(self.cmd(check + " -v", 1, "FAILED tests/test_ws.py::test_frame")[1].kind,
                         "error")

    def test_an_exit_0_that_prints_an_error_clears_nothing(self):
        check = "python3 -m pytest tests/test_ws.py -x"
        self.cmd(check, 1, "FAILED tests/test_ws.py::test_frame")
        self.cmd(check + " -q", 1, "FAILED tests/test_ws.py::test_frame")
        self.assertIsNone(self.cmd(check + " --tb=no", 0, "ValueError: expected in this probe")[1])
        self.assertEqual(self.cmd(check + " -v", 1, "FAILED tests/test_ws.py::test_frame")[1].kind,
                         "error")

    def test_or_and_quoted_bars_are_not_pipes(self):
        from harness.loop_guard import pipe_masks_exit
        self.assertFalse(pipe_masks_exit("make test || echo failed"))
        self.assertFalse(pipe_masks_exit("python3 -c \"print(1 | 2)\""))
        self.assertFalse(pipe_masks_exit("set -o pipefail; pytest | tee out.txt"))
        self.assertFalse(pipe_masks_exit("pytest 2>&1 | tail -20"))      # the harness strips it
        self.assertTrue(pipe_masks_exit("pytest 2>&1 | grep FAIL | tail -20"))


class Probes(GuardCase):
    """A scratchpad script rewritten between runs is a new check each time."""
    PROBE = "PYTHONPATH=. python3 .momo/scratch/dbg.py 2>&1 | head -30"
    ERR = "mud.ws.WSHandshakeError: no response to handshake"

    def write(self, n, path="dbg.py"):
        return self.rec("scratch_write", f"Written: .momo/scratch/{path} (3 lines, {n} bytes)",
                        path=path, content=f"probe {n}")

    def test_a_rewritten_probe_passing_clears_nothing(self):
        """The 2026-10-28 rerun, calls 49-75: dbg.py rewritten and rerun, the
        same command each time; a version that exited 0 cleared the error."""
        self.write(0)
        self.cmd(self.PROBE, 1, self.ERR)
        self.write(1)
        self.cmd(self.PROBE, 1, self.ERR)
        self.write(2)
        self.assertIsNone(self.cmd(self.PROBE, 0, "sent request")[1])
        self.write(3)
        _, hit = self.cmd(self.PROBE, 1, self.ERR)
        self.assertEqual((hit.kind, hit.count), ("error", 3))

    def test_the_same_probe_passing_clears(self):
        self.write(0)
        self.cmd(self.PROBE, 1, self.ERR)
        self.cmd(self.PROBE.replace("-30", "-20"), 1, self.ERR)
        self.assertIsNone(self.cmd(self.PROBE.replace("-30", "-40"), 0, "ok")[1])
        self.write(1)
        self.assertIsNone(self.cmd(self.PROBE, 1, self.ERR)[1])

    def test_rerunning_a_rewritten_probe_is_not_a_repeat(self):
        for n in range(4):
            self.write(n)
            self.assertIsNone(self.cmd(self.PROBE, 0, f"step {n}")[1])

    def test_the_same_probe_unchanged_three_times_is(self):
        self.write(0)
        for _ in range(2):
            self.assertIsNone(self.cmd(self.PROBE, 0, "same")[1])
        self.assertEqual(self.cmd(self.PROBE, 0, "same")[1].kind, "call")

    def test_other_probes_are_versioned_apart(self):
        self.write(0, "a.py")
        self.write(0, "b.py")
        cmd = "python3 .momo/scratch/a.py"
        self.cmd(cmd, 1, self.ERR)
        self.cmd(cmd + " -v", 1, self.ERR)
        self.write(1, "b.py")                       # a.py unchanged: still the same check
        self.assertIsNone(self.cmd(cmd + " -q", 0, "ok")[1])
        self.assertIsNone(self.cmd(cmd, 1, self.ERR)[1])

    def test_a_rewritten_probe_passing_does_not_end_churn(self):
        for n in range(2):
            self.rec("edit_file", EDITED, True, path="ws.py", old_string=str(n), new_string=str(n + 1))
            self.write(n)
            self.cmd(self.PROBE, 1, f"AssertionError: case {n}")
        self.write(9)
        self.cmd(self.PROBE, 0, "looks fine")
        self.rec("edit_file", EDITED, True, path="ws.py", old_string="2", new_string="3")
        hit = self.cmd(self.PROBE, 1, "AssertionError: case z")[1]
        self.assertEqual((hit.kind, hit.severity, hit.count), ("churn", "nudge", 3))

    def test_a_project_file_the_command_runs_is_not_versioned(self):
        """Fixing the code a command runs is how its check gets to pass."""
        cmd = "python3 mud/ws.py --selftest"
        for n in range(2):
            self.rec("edit_file", EDITED, True, path="mud/ws.py", old_string=str(n), new_string=str(n + 1))
            self.cmd(cmd, 1, f"AssertionError: case {n}")
        self.rec("edit_file", EDITED, True, path="mud/ws.py", old_string="2", new_string="3")
        self.assertIsNone(self.cmd(cmd, 0, "all ok")[1])
        self.rec("edit_file", EDITED, True, path="mud/ws.py", old_string="3", new_string="4")
        self.assertIsNone(self.cmd(cmd, 1, "AssertionError: case y")[1])   # churn began again


class Churn(GuardCase):
    """Edit one definition, run, fail, again: a nudge at n cycles, a loop at n+1."""
    def edit(self, n, result=EDITED, path="ws.py"):
        return self.rec("edit_file", result, True, path=path, old_string=str(n), new_string=str(n + 1))

    def cycle(self, n, path="ws.py", result=EDITED):
        self.edit(n, result, path)
        return self.cmd(f"python3 check{n}.py", 1, f"AssertionError: case {chr(97 + n)}")[1]

    def test_a_nudge_then_a_loop(self):
        self.assertIsNone(self.cycle(0))
        self.assertIsNone(self.cycle(1))
        hit = self.cycle(2)
        self.assertEqual((hit.kind, hit.severity, hit.count), ("churn", "nudge", 3))
        self.assertIn("editing encode in ws.py", hit.describe())
        hit = self.cycle(3)
        self.assertEqual((hit.kind, hit.severity, hit.count), ("churn", "rewind", 4))
        self.assertEqual(hit.msgs[0]["role"], "assistant")
        self.assertEqual(len(hit.msgs), 8)          # each cycle's edit and failing run

    def test_different_definitions_do_not_add_up(self):
        for n in range(4):
            other = EDITED.replace("encode", f"f{n}")
            self.assertIsNone(self.cycle(n, result=other))

    def test_the_check_passing_ends_the_churn(self):
        self.cycle(0)
        self.cycle(1)
        self.edit(2)
        self.assertIsNone(self.cmd("python3 check1.py", 0, "ok")[1])
        self.assertIsNone(self.cycle(3))
        self.assertIsNone(self.cycle(4))

    def test_a_probe_passing_does_not_end_it(self):
        self.cycle(0)
        self.cycle(1)
        self.edit(2)
        self.cmd("ls -la", 0, "ws.py")
        self.assertIsNone(self.cmd("python3 check2.py", 1, "AssertionError: case z")[1])
        self.assertEqual(self.cycle(3).kind, "churn")   # the edit before the probe got no cycle

    def test_a_piped_failure_is_a_cycle_and_a_piped_pass_does_not_end_it(self):
        self.cycle(0)
        self.edit(1)
        self.cmd("python3 check1.py | sed -n 1,9p", 0, "ok")        # no proof it passed
        self.edit(2)
        self.assertIsNone(self.cmd("python3 check2.py | sed -n 1,9p", 0, "AssertionError: x")[1])
        self.edit(3)
        hit = self.cmd("python3 check3.py | grep -v DEBUG", 0, "AssertionError: y")[1]
        self.assertEqual((hit.kind, hit.severity, hit.count), ("churn", "nudge", 3))

    def test_a_write_without_an_owner_counts_the_file(self):
        hits = [self.cycle(n, result="Written: ws.py (3 lines, 20 bytes)") for n in range(3)]
        self.assertIn("editing ws.py while", hits[-1].describe())


class Replay(unittest.TestCase):
    """The run that made this: Ornith-9B patching a WebSocket frame encoder for a
    whole plan step (60 calls) without the guard noticing."""
    def test_it_is_caught_at_call_21(self):
        calls = json.loads((GOLDEN / "loop_ws_frame.json").read_text())["calls"]
        g, hits = LoopGuard(), []
        for i, c in enumerate(calls, 1):
            hit = g.record(c["name"], c["args"], c["result"], {"i": i}, c["mutating"], c["exit_code"])
            if hit:
                hits.append((i, hit.kind, hit.severity))
                if hit.severity == "rewind":
                    break
        self.assertEqual(hits, [(17, "churn", "nudge"), (21, "error", "rewind")])


class Rewind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self.tmp.name)
        (self.wd / "a.py").write_text("x = 1\n")
        self._orig = hmod.make_client
        hmod.make_client = lambda *a, **k: ScriptedClient("http://fake", "fake")
        ScriptedClient.analysis = ANALYSIS

    def tearDown(self):
        hmod.make_client = self._orig
        self.h.logger.close()
        self.tmp.cleanup()

    def run_script(self, script):
        ScriptedClient.script = script
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False
        self.h.send("go")
        return self.h.messages[self.h.messages.index(self.h._turn_user):]

    def events(self):
        sub = self.h.event_queue.subscribe(replay=True)
        out = []
        while True:
            try:
                out.append(sub.get_nowait())
            except Exception:
                return out

    def test_the_loop_is_replaced_by_the_analysis(self):
        turn = self.run_script([_call("read_file", path="a.py")] * 3)
        self.assertEqual([m["role"] for m in turn], ["user", "assistant", "user", "assistant"])
        self.assertEqual(turn[1]["content"], ANALYSIS)
        self.assertIn("Do not repeat read_file", turn[2]["content"])
        self.assertTrue(turn[2]["content"].startswith("[Note from the harness, not the user]"))
        self.assertIn("changed no files", turn[2]["content"])
        self.assertEqual(turn[3]["content"], "Done.")
        call = self.h.client.analysis_calls[0]
        self.assertFalse(call["think"])
        self.assertEqual(sum(m["role"] == "tool" for m in call["messages"]), 3)  # it saw the loop
        self.assertIn("STOP", call["messages"][-1]["content"])
        # The loop is kept for /export full, marked as a loop reset.
        self.assertEqual(sum(r["message"].get("role") == "tool" for r in self.h.compacted), 3)
        notes = {n for _, n in transcript.full_transcript(self.h.messages, self.h.compacted) if n}
        self.assertTrue(notes and all("loop reset #1" in n for n in notes), notes)

    def test_the_turn_goes_on_before_the_loop(self):
        turn = self.run_script([_call("list_files", path=".")]
                               + [_call("read_file", path="a.py")] * 3)
        self.assertEqual([m["role"] for m in turn],
                         ["user", "assistant", "tool", "assistant", "user", "assistant"])
        self.assertEqual(turn[1]["tool_calls"][0]["function"]["name"], "list_files")

    def test_the_nudge_names_the_files_the_loop_changed(self):
        turn = self.run_script([_call("write_file", path="b.py", content="y = 1\n")] * 3)
        self.assertIn("changed b.py — still changed on disk", turn[2]["content"])
        self.assertEqual((self.wd / "b.py").read_text(), "y = 1\n")

    def test_a_failed_analysis_still_resets(self):
        ScriptedClient.analysis = ConnectionError("server gone")
        turn = self.run_script([_call("read_file", path="a.py")] * 3)
        self.assertEqual([m["role"] for m in turn], ["user", "assistant", "user", "assistant"])
        self.assertIn("stuck in a loop: read_file", turn[1]["content"])

    def test_it_stops_after_the_resets(self):
        turn = self.run_script([_call("read_file", path="a.py")] * 9 + [ChatResponse(content="never")])
        self.assertEqual(len(self.h.client.analysis_calls), 2)
        self.assertNotEqual(turn[-1].get("content"), "never")
        errors = [ev for ev in self.events() if type(ev).__name__ == "ErrorEvent"]
        self.assertTrue(any("kept repeating read_file" in str(vars(ev)) for ev in errors), errors)

    def test_edit_and_rerun_is_not_a_loop(self):
        self.run_script([
            _call("read_file", path="a.py"),
            _call("edit_file", path="a.py", old_string="x = 1", new_string="x = 2"),
            _call("read_file", path="a.py"),
            _call("edit_file", path="a.py", old_string="x = 2", new_string="x = 3"),
            _call("read_file", path="a.py"),
        ])
        self.assertEqual(self.h.client.analysis_calls, [])
        self.assertEqual((self.wd / "a.py").read_text(), "x = 3\n")

    def test_churn_nudges_then_rewinds(self):
        (self.wd / "a.py").write_text("def f():\n    return 0\n")
        script = []
        for n in range(4):
            script += [_call("edit_file", path="a.py", old_string=f"return {n}",
                             new_string=f"return {n + 1}"),
                       _call("run_command", command=f"python3 -c \"print('ValueError: case {'abcd'[n]}');"
                                                    f" raise SystemExit({n + 1})\"")]
        turn = self.run_script(script)
        # The nudge came after the 3rd cycle; the 4th's rewind cut it with the loop.
        cut = [r["message"] for r in self.h.compacted]
        nudges = [m for m in cut
                  if m.get("role") == "user" and "Stop patching" in (m.get("content") or "")]
        self.assertEqual(len(nudges), 1)
        self.assertIn("changed f in a.py 3 times", nudges[0]["content"])
        self.assertIn('last: "ValueError: case c"', nudges[0]["content"])
        self.assertEqual(sum(m.get("role") == "tool" for m in cut), 8)
        self.assertEqual(len(self.h.client.analysis_calls), 1)       # the 4th cycle rewound
        self.assertIn("each change I made to that code",
                      self.h.client.analysis_calls[0]["messages"][-1]["content"])
        self.assertEqual(turn[1]["content"], ANALYSIS)               # cut back to the 1st edit
        self.assertIn("changed a.py", turn[2]["content"])

    def test_off_lets_the_model_repeat(self):
        ScriptedClient.script = [_call("read_file", path="a.py")] * 4
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False
        self.h.loop_guard = 0
        self.h.send("go")
        self.assertEqual(self.h.client.analysis_calls, [])
        self.assertEqual(sum(m["role"] == "tool" for m in self.h.messages), 4)

    def test_a_lower_threshold_and_no_resets_stop_at_once(self):
        ScriptedClient.script = [_call("read_file", path="a.py")] * 2 + [ChatResponse(content="never")]
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.wd)
        self.h.set_mode("coding")
        self.h.stream = False
        self.h.loop_guard, self.h.loop_resets = 2, 0
        self.h.send("go")
        self.assertEqual(self.h.client.analysis_calls, [])
        errors = [str(vars(ev)) for ev in self.events() if type(ev).__name__ == "ErrorEvent"]
        self.assertTrue(any("2 times. /loop-guard" in e for e in errors), errors)


class Command(Base):
    """/loop-guard: shown, set, saved, refused out of range, sent in the status."""
    def test_show_set_and_save(self):
        self.assertIn("the same call 3 times", self.cmd("/loop-guard"))
        self.assertIn("the same call 5 times", self.cmd("/loop-guard 5"))
        self.assertEqual(self.h.loop_guard, 5)
        self.assertEqual(session_mod.load_prefs().get("loop_guard"), 5)
        self.assertIn("up to 4 resets", self.cmd("/loop-guard resets 4"))
        self.assertEqual(session_mod.load_prefs().get("loop_resets"), 4)
        self.assertIn("first loop stops the turn", self.cmd("/loop-guard resets 0"))
        self.assertEqual(self.h.status_event().loop_resets, 0)

    def test_off_and_on(self):
        self.cmd("/loop-guard 6")
        self.assertIn("off", self.cmd("/loop-guard off"))
        self.assertEqual(self.h.status_event().loop_guard, 0)
        self.cmd("/loop-guard on")
        self.assertEqual(self.h.loop_guard, 3)

    def test_out_of_range_is_refused(self):
        for bad in ("1", "21", "x", "resets 11", "resets -1", "resets", "3 4"):
            self.assertTrue(self.cmd(f"/loop-guard {bad}").startswith("ERROR"), bad)
        self.assertEqual((self.h.loop_guard, self.h.loop_resets), (3, 2))

    def test_listed_in_help(self):
        self.assertIn("/loop-guard", {c["cmd"] for c in commands.help_commands()})


if __name__ == "__main__":
    unittest.main()
