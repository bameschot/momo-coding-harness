"""/run-mode new: run_command saves its output to a log and returns a view.

The view helpers are checked on plain text; run_command and command_output
through dispatch; the existing run_command suites are run again in new mode.

Run with HOME pointed at a scratch dir — the harness writes prefs and sessions.
"""
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_run_command_web as web_base
import test_tool_robustness as base

import harness.harness as hmod
from harness import commands, run_store
from harness import session as session_mod
from harness.llm.base import ChatResponse, LLMClient, ToolCall
from harness.tools import dispatch

_FOOTER_RE = re.compile(r"\n?\[output saved: .*", re.DOTALL)


def body(out: str) -> str:
    """A new-mode result without its footer (the log path and the hint)."""
    return _FOOTER_RE.sub("", out)


def saved_path(out: str) -> str:
    m = re.search(r"\[output saved: (\S+\.log) — ", out)
    assert m, out
    return m.group(1)


class Views(unittest.TestCase):
    def test_short_text_is_returned_whole(self):
        self.assertEqual(run_store.window("a\nb", 100), "a\nb")

    def test_long_text_keeps_a_quarter_head_and_three_quarter_tail(self):
        text = "\n".join(f"line {i:04d}" for i in range(1, 2001))   # 10 chars + \n each
        out = run_store.window(text, 1000)
        head, note, tail = re.split(r"\n(\[… .* not shown.*\])\n", out)
        self.assertTrue(head.startswith("line 0001"))
        self.assertTrue(tail.endswith("line 2000"))
        self.assertLessEqual(len(head), 250)
        self.assertLessEqual(len(tail), 750)
        self.assertGreater(len(tail), 2 * len(head))
        # Cut on line boundaries, and the note counts what is missing.
        shown = head.split("\n") + tail.split("\n")
        self.assertTrue(all(re.fullmatch(r"line \d{4}", ln) for ln in shown))
        self.assertIn(f"{2000 - len(shown):,} lines", note)

    def test_one_huge_line_is_cut_on_both_ends(self):
        out = run_store.window("x" * 50_000, 1000)
        self.assertLess(len(out), 1200)
        self.assertIn("not shown", out)

    def test_tail(self):
        out = run_store.view("\n".join(map(str, range(1, 101))), 5000, tail_n=3)
        self.assertEqual(out, "[last 3 of 100 lines, from line 98]\n98\n99\n100")

    def test_grep_with_context_and_line_numbers(self):
        text = "a\nb\nERROR one\nc\nd\ne\nerror two\nf"
        out = run_store.view(text, 5000, pattern="error", context=1)
        self.assertEqual(out, '[grep "error": 2 matching lines of 8]\n'
                              "2- b\n3: ERROR one\n4- c\n--\n6- e\n7: error two\n8- f")

    def test_grep_and_tail_give_the_last_matches(self):
        text = "\n".join(f"hit {i}" for i in range(10))
        out = run_store.view(text, 5000, pattern="hit", tail_n=2)
        self.assertIn("last 2 of 10 matching", out)
        self.assertTrue(out.endswith("9: hit 8\n10: hit 9"), out)

    def test_invalid_regex_is_matched_literally(self):
        out = run_store.view("f(x\ny", 5000, pattern="f(x")
        self.assertIn("matched as plain text", out)
        self.assertIn("1: f(x", out)

    def test_bre_alternation(self):
        out = run_store.view("error\nok\nfail", 5000, pattern="error\\|fail")
        self.assertIn("2 matching", out)

    def test_many_matches_are_windowed(self):
        text = "\n".join(f"error {i}" for i in range(5000))
        out = run_store.view(text, 1000, pattern="error")
        self.assertLess(len(out), 1200)
        self.assertIn("5,000 matching", out)
        self.assertTrue(out.endswith("error 4999"))

    def test_no_match(self):
        self.assertIn("no matching lines in 2", run_store.view("a\nb", 5000, pattern="zzz"))

    def test_line_range_and_negative_start(self):
        text = "\n".join(map(str, range(1, 11)))
        self.assertEqual(run_store.view(text, 5000, start_line=2, end_line=3),
                         "[lines 2-3 of 10]\n2: 2\n3: 3")
        self.assertIn("[lines 9-10 of 10]", run_store.view(text, 5000, start_line=-2))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.wd = self.root / "wd"
        self.wd.mkdir()
        self.store = run_store.RunStore(self.root / "runs" / "session")

    def tearDown(self):
        self.tmp.cleanup()

    def cmd(self, command, limit=5000, **args):
        return dispatch("run_command", {"command": command, **args}, self.wd,
                        run_mode="new", run_store=self.store, run_output_limit=limit)

    def output(self, path, limit=5000, **args):
        return dispatch("command_output", {"path": path, **args}, self.wd,
                        run_mode="new", run_store=self.store, run_output_limit=limit)


class RunCommandNew(Base):
    def test_small_output_is_whole_and_names_the_full_log_path(self):
        out = self.cmd("echo hi; echo oops >&2; exit 3")
        self.assertEqual(body(out), "hi\noops")          # stdout and stderr, in order
        path = saved_path(out)
        self.assertTrue(Path(path).is_absolute())
        self.assertEqual(Path(path), self.store.root / "r1.log")
        self.assertIn("2 lines", out)
        self.assertIn("exit code 3", out)
        self.assertNotIn("command_output(", out)          # nothing hidden, no hint

    def test_the_printed_path_works_in_command_output(self):
        out = self.cmd("seq 1 50")
        again = self.output(saved_path(out), tail=2)
        self.assertEqual(body(again), "[last 2 of 50 lines, from line 49]\n49\n50")
        self.assertIn(saved_path(out), again)            # the footer repeats the path

    def test_large_output_is_windowed_with_a_hint(self):
        out = self.cmd("seq 1 20000; echo 'FAILED test_x - AssertionError'", limit=2000)
        self.assertLess(len(body(out)), 2100)
        self.assertTrue(body(out).startswith("1\n2\n"))
        self.assertIn("FAILED test_x", body(out))       # the ending survives
        self.assertIn("not shown", out)
        self.assertIn("1 line mentions error/fail/warning", out)
        self.assertIn(f'command_output(path="{saved_path(out)}"', out)

    def test_tail_and_grep_args(self):
        self.assertEqual(body(self.cmd("seq 1 1000", tail=2)),
                         "[last 2 of 1,000 lines, from line 999]\n999\n1000")
        self.assertIn("500: 500", self.cmd("seq 1 1000", grep="^500$"))

    def test_command_output_accepts_id_and_file_name(self):
        self.cmd("echo one")
        for ref in ("r1", "r1.log", str(self.store.root / "r1.log")):
            with self.subTest(ref=ref):
                self.assertEqual(body(self.output(ref)), "one")

    def test_command_output_only_ever_opens_its_own_logs(self):
        self.cmd("echo one")
        secret = self.root / "r1.log"
        secret.write_text("secret\n")
        (self.store.root / "r9.log").symlink_to(secret)
        for ref in ("/etc/passwd", "r9", "", "r2", "../x.txt"):
            with self.subTest(ref=ref):
                out = self.output(ref)
                self.assertTrue(out.startswith("ERROR: no saved command output"), out)
                self.assertNotIn("secret\n", out)
        # A path elsewhere that ends in rN.log names this store's rN, never that file;
        # so does a mistyped session directory.
        for ref in (str(secret), "../r1.log", str(self.store.root.parent / "other" / "r1.log")):
            with self.subTest(ref=ref):
                self.assertEqual(body(self.output(ref)), "one")
        self.assertIn(str(self.store.root / "r1.log"), self.output("nope"))

    def test_old_logs_are_pruned(self):
        for i in range(run_store.KEEP_LOGS + 3):
            self.cmd(f"echo {i}")
        logs = self.store.logs()
        self.assertEqual(len(logs), run_store.KEEP_LOGS)
        self.assertEqual(logs[-1].name, f"r{run_store.KEEP_LOGS + 3}.log")
        self.assertTrue(self.output("r1").startswith("ERROR"))

    def test_timeout_leaves_a_queryable_log(self):
        out = self.cmd("echo before; sleep 30", timeout=1)
        self.assertTrue(out.startswith("ERROR: command timed out"), out)
        self.assertIn("before", out)
        self.assertEqual(body(self.output(saved_path(out))), "before")

    def test_interrupt_leaves_a_queryable_log(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        out = dispatch("run_command", {"command": "echo started; sleep 30"}, self.wd,
                       cancel=cancel, run_mode="new", run_store=self.store)
        self.assertIn("interrupted", out)
        self.assertEqual(body(self.output("r1")), "started")

    def test_classic_mode_ignores_the_store(self):
        out = dispatch("run_command", {"command": "echo hi"}, self.wd,
                       run_mode="classic", run_store=self.store)
        self.assertEqual(out, "hi")
        self.assertEqual(self.store.logs(), [])

    def test_clear_removes_the_logs(self):
        self.cmd("echo x")
        self.store.clear()
        self.assertFalse(self.store.root.exists())
        self.assertIn("r1.log", saved_path(self.cmd("echo y")))


# The classic suites, run again in new mode with the footer stripped: the same
# commands must behave the same way.
class ClassicSuiteInNewMode(base.RunCommand):
    def setUp(self):
        super().setUp()
        self.store = run_store.RunStore(self.root / ".runs")

    def run_tool(self, name, **args):
        return body(dispatch(name, args, self.root, run_mode="new", run_store=self.store))

    def test_cancel_stops_a_running_command(self):
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        t0 = time.monotonic()
        out = dispatch("run_command", {"command": "sleep 30"}, self.root, cancel=cancel,
                       run_mode="new", run_store=self.store)
        self.assertIn("interrupted", out)
        self.assertLess(time.monotonic() - t0, 10)


class WebSuiteInNewMode(web_base.Dispatch):
    def run_cmd(self, command, net_access):
        store = run_store.RunStore(Path(self.tmp.name) / ".runs")
        return body(dispatch("run_command", {"command": command}, self.workdir, net_access,
                             run_mode="new", run_store=store))


class _Scripted(LLMClient):
    provider_name = "fake"
    replies: list[ChatResponse] = []

    def chat(self, messages, tools, think=None, num_ctx=None, on_delta=None):
        return self.replies.pop(0) if self.replies else ChatResponse(content="Done.",
                                                                     done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class HarnessWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                        mock.patch.object(session_mod, "_PREFS_PATH", self.home / "prefs.json"),
                        mock.patch.object(hmod, "make_client",
                                          lambda *a, **k: _Scripted("http://fake", "fake"))]
        for p in self.patches:
            p.start()
        self.h = hmod.Harness(host="http://fake", model="fake", workdir=self.home)
        self.h.set_mode("coding")

    def tearDown(self):
        self.h.logger.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def names(self):
        return [t["function"]["name"] for t in self.h._current_tools()]

    def test_new_is_the_default_and_offers_command_output(self):
        self.assertEqual(self.h.run_mode, "new")
        names = self.names()
        self.assertEqual(names[names.index("run_command") + 1], "command_output")
        self.assertIn("tail=N", self.h.messages[0]["content"])

    def test_run_mode_command_swaps_the_schema(self):
        out = commands.handle("/run-mode classic", self.h).output
        self.assertIn("classic", out)
        self.assertNotIn("command_output", self.names())
        self.assertNotIn("command_output", self.h.messages[0]["content"])
        commands.handle("/run-mode on", self.h)
        self.assertEqual(self.h.run_mode, "new")
        self.assertIn("command_output", self.names())
        self.assertTrue(commands.handle("/run-mode sideways", self.h).output.startswith("ERROR"))

    def test_plan_modes_follow_the_run_mode(self):
        self.h.mode = "plan"
        self.assertIn("command_output", self.names())
        self.h.mode = "chat"
        self.assertNotIn("run_command", self.names())
        self.assertNotIn("command_output", self.names())

    def test_output_limit_command(self):
        self.assertIn("ERROR", commands.handle("/run-output-limit 10", self.h).output)
        commands.handle("/run-output-limit 800", self.h)
        self.assertEqual(self.h.run_output_limit, 800)
        out = self.h._dispatch("run_command", {"command": "seq 1 5000"})
        self.assertLess(len(body(out)), 900)

    def test_logs_live_in_the_session_dir_and_go_with_it(self):
        out = self.h._dispatch("run_command", {"command": "echo hi"})
        path = Path(saved_path(out))
        self.assertEqual(path.parent, self.home / ".momo-harness" / "runs" / self.h._ts)
        commands.handle("/clear", self.h)
        self.assertFalse(path.exists())
        out = self.h._dispatch("run_command", {"command": "echo again"})
        self.h.new_session()
        self.assertFalse(Path(saved_path(out)).exists())

    def tool_result_of(self, command):
        _Scripted.replies = [ChatResponse(tool_calls=[ToolCall(name="run_command",
                                                               arguments={"command": command})])]
        self.h.send("run it")
        return [m["content"] for m in self.h.messages if m.get("role") == "tool"][-1]

    def test_tool_result_cap_leaves_windowed_output_alone(self):
        # /tool-result would cut the head+tail view from the front and lose the
        # footer with the log path; new-mode results window themselves.
        self.h.max_tool_result = 500
        out = self.tool_result_of("seq 1 5000")
        self.assertNotIn("truncated after", out)
        self.assertIn("5000", out)
        self.assertIn("[output saved: ", out)
        commands.handle("/run-mode classic", self.h)
        self.assertIn("truncated after", self.tool_result_of("seq 1 5000"))

if __name__ == "__main__":
    unittest.main()
